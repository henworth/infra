#!/usr/bin/env python3
"""Create or drop per-env logical databases on the shared Aurora cluster.

Replaces the CFN custom-resource bootstrap path when running against Ministack
(see baseline_stack.py for context). Connects to Postgres directly using master
credentials read from Secrets Manager, just like the Lambda would.

Usage:
    bootstrap-dbs.py create --env-name <envName> [--baseline-stack BaselineStack]
    bootstrap-dbs.py drop   --env-name <envName> [--baseline-stack BaselineStack]

The script reads:
    - `DbHost`, `DbPort`, `DbSecretArn` outputs from the baseline CFN stack
    - The master username/password from Secrets Manager

Identifier sanitization: only `[a-z0-9_]` are allowed, matching the Lambda
handler. f-string interpolation is the standard escape hatch since psycopg /
pg8000 don't bind identifiers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import boto3
import pg8000.dbapi


def _sanitize(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum() or c == "_")


def _aws_session() -> boto3.Session:
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if endpoint:
        return boto3.Session(
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return boto3.Session()


def _stack_outputs(stack_name: str) -> dict[str, str]:
    session = _aws_session()
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    cfn = session.client("cloudformation", endpoint_url=endpoint)
    resp = cfn.describe_stacks(StackName=stack_name)
    outputs = resp["Stacks"][0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}


def _credentials(secret_arn: str) -> tuple[str, str]:
    session = _aws_session()
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    sm = session.client("secretsmanager", endpoint_url=endpoint)
    secret = sm.get_secret_value(SecretId=secret_arn)
    data = json.loads(secret["SecretString"])
    return data["username"], data["password"]


def _connect(host: str, port: int, username: str, password: str, database: str = "postgres"):
    use_ssl = os.environ.get("DB_SSL", "auto").lower()
    ssl_context: bool | None
    if use_ssl == "auto":
        # On Ministack the real Postgres container has no TLS configured;
        # everywhere else (real AWS RDS) TLS is required.
        ssl_context = None if os.environ.get("AWS_ENDPOINT_URL") else True
    else:
        ssl_context = use_ssl in {"1", "true", "yes"}

    kwargs: dict = dict(host=host, port=port, database=database, user=username, password=password)
    if ssl_context:
        kwargs["ssl_context"] = True
    return pg8000.dbapi.connect(**kwargs)


def create_databases(host: str, port: int, secret_arn: str, env_name: str) -> None:
    env_slug = _sanitize(env_name.replace("-", "_"))
    targets = [f"pantry_db_{env_slug}", f"shopping_db_{env_slug}"]
    username, password = _credentials(secret_arn)
    conn = _connect(host, port, username, password)
    conn.autocommit = True
    try:
        cursor = conn.cursor()
        for db in targets:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,))
            if cursor.fetchone() is None:
                print(f"creating database {db}")
                cursor.execute(f'CREATE DATABASE "{db}"')
            else:
                print(f"database {db} already exists")
    finally:
        conn.close()


def drop_databases(host: str, port: int, secret_arn: str, env_name: str) -> None:
    env_slug = _sanitize(env_name.replace("-", "_"))
    targets = [f"pantry_db_{env_slug}", f"shopping_db_{env_slug}"]
    username, password = _credentials(secret_arn)
    conn = _connect(host, port, username, password)
    conn.autocommit = True
    try:
        cursor = conn.cursor()
        for db in targets:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (db,),
            )
            cursor.execute(f'DROP DATABASE IF EXISTS "{db}"')
            print(f"dropped database {db}")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("create", "drop"):
        p = sub.add_parser(name)
        p.add_argument("--env-name", required=True)
        p.add_argument("--baseline-stack", default="BaselineStack")
        p.add_argument(
            "--db-host",
            default=None,
            help="Override Aurora host (otherwise read from baseline CFN output).",
        )
        p.add_argument("--db-port", type=int, default=None)
        p.add_argument("--db-secret-arn", default=None)
    args = parser.parse_args()

    outputs = {}
    if not (args.db_host and args.db_port and args.db_secret_arn):
        outputs = _stack_outputs(args.baseline_stack)
    host = args.db_host or outputs["DbHost"]
    port = args.db_port or int(outputs["DbPort"])
    secret_arn = args.db_secret_arn or outputs["DbSecretArn"]

    if args.action == "create":
        create_databases(host, port, secret_arn, args.env_name)
    elif args.action == "drop":
        drop_databases(host, port, secret_arn, args.env_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
