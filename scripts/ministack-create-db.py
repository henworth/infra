#!/usr/bin/env python3
"""Pre-create a Ministack Postgres DB instance and DBSubnetGroup via the RDS API.

Ministack's CloudFormation engine only provisions `AWS::RDS::DBCluster`,
not `AWS::RDS::DBSubnetGroup` or `AWS::RDS::DBInstance`. So in `ministack_mode`
the CDK BaselineStack skips RDS entirely and this script picks up the slack:

  1. Read the baseline stack outputs (VPC, subnet IDs, DB SG, secret ARN).
  2. CreateDBSubnetGroup in those subnets.
  3. CreateDBInstance (engine=postgres) with master creds from the secret.
  4. Wait for it to become available.
  5. If we reused an existing instance, probe Postgres with the *current*
     Secrets Manager password. If auth fails (``28P01``), delete and recreate
     — Ministack's ``ModifyDBInstance`` only updates control-plane state and
     does not run ``ALTER USER`` on the sidecar Postgres container, so a
     full recreate is the only way to align with a rotated secret.
  6. Print the endpoint host:port so the caller can pass it to the env stack
     deploy via `-c dbHost=... -c dbPort=...`.

Idempotent: skips creation if the subnet group / instance already exist.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import boto3
import pg8000.dbapi
import pg8000.exceptions


DB_INSTANCE_ID = "pantry-postgres"
DB_SUBNET_GROUP_NAME = "pantry-db-subnet-group"


def _wait_for_postgres(host: str, port: int, user: str, password: str) -> None:
    """Block until Postgres at host:port accepts a real auth handshake.

    Ministack reports `DBInstanceStatus=available` as soon as the RDS API has
    spun up its container record, but the actual Postgres process inside the
    container often needs a few more seconds before it accepts connections.
    Pre-warming here avoids spurious failures in downstream steps like
    bootstrap-dbs.py.
    """
    import pg8000.dbapi

    deadline = time.monotonic() + 60
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = pg8000.dbapi.connect(host=host, port=port, user=user, password=password, database="postgres")
            conn.close()
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1)
    raise TimeoutError(f"Postgres at {host}:{port} didn't accept connections within 60s (last error: {last_err!r})")


def _host_port_for_container(container_name: str, container_port: int) -> int | None:
    """Return the Docker host port mapped to ``container_port`` on the named
    container, or ``None`` if not running / not mapped.

    Ministack publishes RDS Postgres containers as `ministack-rds-<dbid>` with
    `5432/tcp` mapped to a host port starting at `RDS_BASE_PORT` (default
    15432). The CFN endpoint reports the container's internal IP+5432, which
    isn't reachable from the host, so we have to discover the host port via
    Docker.
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "port",
                container_name,
                f"{container_port}/tcp",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return int(line.rsplit(":", 1)[-1])
        except ValueError:
            continue
    return None


def _endpoint() -> str:
    return os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")


def _session() -> boto3.Session:
    return boto3.Session(
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )


def _stack_outputs(stack_name: str) -> dict[str, str]:
    cfn = _session().client("cloudformation", endpoint_url=_endpoint())
    outs = cfn.describe_stacks(StackName=stack_name)["Stacks"][0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outs}


def _master_creds(secret_arn: str) -> tuple[str, str]:
    sm = _session().client("secretsmanager", endpoint_url=_endpoint())
    raw = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
    data = json.loads(raw)
    return data["username"], data["password"]


def _delete_db_instance(rds) -> None:
    rds.delete_db_instance(DBInstanceIdentifier=DB_INSTANCE_ID, SkipFinalSnapshot=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            rds.describe_db_instances(DBInstanceIdentifier=DB_INSTANCE_ID)
            time.sleep(1)
        except rds.exceptions.DBInstanceNotFoundFault:
            return
    raise TimeoutError(f"db instance {DB_INSTANCE_ID} still exists after delete request")


def _create_db_instance(rds, username: str, password: str, db_sg_id: str) -> dict:
    rds.create_db_instance(
        DBInstanceIdentifier=DB_INSTANCE_ID,
        DBInstanceClass="db.t3.micro",
        Engine="postgres",
        EngineVersion="15",
        MasterUsername=username,
        MasterUserPassword=password,
        AllocatedStorage=20,
        DBSubnetGroupName=DB_SUBNET_GROUP_NAME,
        VpcSecurityGroupIds=[db_sg_id],
        PubliclyAccessible=False,
        DBName="postgres",
    )
    print(f"created db instance {DB_INSTANCE_ID}, waiting for it to become available")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        resp = rds.describe_db_instances(DBInstanceIdentifier=DB_INSTANCE_ID)
        inst = resp["DBInstances"][0]
        if inst["DBInstanceStatus"] == "available" and inst.get("Endpoint", {}).get("Address"):
            return inst
        time.sleep(2)
    raise TimeoutError(f"db instance {DB_INSTANCE_ID} did not reach `available` within 180s")


def _auth_probe_ok(host: str, port: int, user: str, password: str) -> bool | None:
    """Quick auth check. ``True``=success, ``False``=password mismatch (28P01),
    ``None``=transient (network, container warming up, etc.)."""
    try:
        conn = pg8000.dbapi.connect(host=host, port=port, user=user, password=password, database="postgres")
        conn.close()
        return True
    except pg8000.exceptions.DatabaseError as e:
        args = e.args[0] if e.args else {}
        if isinstance(args, dict) and args.get("C") == "28P01":
            return False
        return None
    except Exception:  # noqa: BLE001
        return None


def _ensure_subnet_group(rds, subnet_ids: list[str]) -> None:
    try:
        rds.describe_db_subnet_groups(DBSubnetGroupName=DB_SUBNET_GROUP_NAME)
        print(f"subnet group {DB_SUBNET_GROUP_NAME} already exists")
    except rds.exceptions.DBSubnetGroupNotFoundFault:
        rds.create_db_subnet_group(
            DBSubnetGroupName=DB_SUBNET_GROUP_NAME,
            DBSubnetGroupDescription="Pantry preview Postgres",
            SubnetIds=subnet_ids,
        )
        print(f"created subnet group {DB_SUBNET_GROUP_NAME} with subnets {subnet_ids}")


def _container_is_running(container_name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return result.stdout.strip().lower() == "true"


def _ensure_db_instance(
    rds,
    username: str,
    password: str,
    db_sg_id: str,
) -> tuple[dict, bool]:
    """Return ``(instance, was_reused)``.

    Drops + recreates if the API record exists but the sidecar Postgres
    container isn't running (happens after Ministack restarts).
    """
    try:
        inst = rds.describe_db_instances(DBInstanceIdentifier=DB_INSTANCE_ID)["DBInstances"][0]
    except rds.exceptions.DBInstanceNotFoundFault:
        return _create_db_instance(rds, username, password, db_sg_id), False

    sidecar = f"ministack-rds-{DB_INSTANCE_ID}"
    if _container_is_running(sidecar):
        print(f"db instance {DB_INSTANCE_ID} already exists")
        return inst, True

    print(
        f"db instance {DB_INSTANCE_ID} record exists but sidecar container "
        f"{sidecar} is not running; deleting and recreating"
    )
    _delete_db_instance(rds)
    return _create_db_instance(rds, username, password, db_sg_id), False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-stack", default="BaselineStack")
    args = parser.parse_args()

    outs = _stack_outputs(args.baseline_stack)
    subnet_ids = [s.strip() for s in outs["DbSubnetIds"].split(",") if s.strip()]
    db_sg_id = outs["DbSgId"]
    secret_arn = outs["DbSecretArn"]

    username, password = _master_creds(secret_arn)

    rds = _session().client("rds", endpoint_url=_endpoint())
    _ensure_subnet_group(rds, subnet_ids)
    inst, reused = _ensure_db_instance(rds, username, password, db_sg_id)

    endpoint = inst["Endpoint"]
    host_port_probe = _host_port_for_container(f"ministack-rds-{DB_INSTANCE_ID}", int(endpoint["Port"]))

    # Reused instance + rotated secret = stale password. Ministack's
    # `ModifyDBInstance` only updates control-plane state and doesn't actually
    # run `ALTER USER` on the sidecar Postgres container, so the only reliable
    # fix is to delete and recreate the instance.
    if reused and host_port_probe is not None:
        probe = _auth_probe_ok("localhost", host_port_probe, username, password)
        if probe is False:
            print(
                f"auth probe failed on reused db instance {DB_INSTANCE_ID} "
                "(BaselineStack's DbSecret was rotated); recreating"
            )
            _delete_db_instance(rds)
            inst = _create_db_instance(rds, username, password, db_sg_id)
            endpoint = inst["Endpoint"]
            host_port_probe = _host_port_for_container(f"ministack-rds-{DB_INSTANCE_ID}", int(endpoint["Port"]))

    if host_port_probe is not None:
        _wait_for_postgres("localhost", host_port_probe, username, password)
    # `Endpoint.Address` is the container's internal IP and isn't reachable
    # from the host. Discover the host port via Docker, then emit two
    # endpoints:
    #   - DB_HOST=localhost / DB_PORT=<host-port>  for host-side scripts
    #     (bootstrap-dbs.py runs from the user's shell).
    #   - DB_HOST_CONTAINER=host.docker.internal   for the Ministack-managed
    #     ECS task containers, which share the host's Docker daemon and reach
    #     the host via Docker's standard internal DNS name.
    host_port = _host_port_for_container(f"ministack-rds-{DB_INSTANCE_ID}", int(endpoint["Port"]))
    if host_port is None:
        print(
            f"WARNING: couldn't discover host port for ministack-rds-{DB_INSTANCE_ID},"
            f" falling back to internal endpoint {endpoint['Address']}:{endpoint['Port']}",
            file=sys.stderr,
        )
        host_port = int(endpoint["Port"])
        host_addr = endpoint["Address"]
        container_host = endpoint["Address"]
    else:
        host_addr = "localhost"
        container_host = "host.docker.internal"
    print(f"DB_HOST={host_addr}")
    print(f"DB_PORT={host_port}")
    print(f"DB_HOST_CONTAINER={container_host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
