"""CloudFormation custom resource handler that creates/drops per-env Postgres databases.

Invoked by the CDK `cr.Provider` framework. The `ResourceProperties` payload
contains `EnvName` and `DatabaseNames`. Master credentials are pulled from
Secrets Manager via `DB_SECRET_ARN`.

Identifier sanitization: only `[a-z0-9_]` are allowed, so untrusted env names
cannot inject SQL via the f-string interpolation used below (psycopg/pg8000
don't support binding identifiers, so f-strings are the standard escape hatch).
"""

from __future__ import annotations

import json
import logging
import os

import boto3
import pg8000.dbapi

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_secrets = boto3.client("secretsmanager")


def _sanitize(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum() or c == "_")


def _get_master_credentials() -> tuple[str, str]:
    secret = _secrets.get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])
    data = json.loads(secret["SecretString"])
    return data["username"], data["password"]


def _connect(database: str = "postgres"):
    username, password = _get_master_credentials()
    return pg8000.dbapi.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        database=database,
        user=username,
        password=password,
        ssl_context=True,
    )


def on_event(event: dict, _context) -> dict:
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    env_name_raw = props["EnvName"]
    env_name = _sanitize(env_name_raw.replace("-", "_"))
    db_names = [_sanitize(n) for n in props["DatabaseNames"]]
    if not all(db_names):
        raise ValueError(f"invalid db names after sanitization: {db_names}")

    logger.info("request_type=%s env=%s dbs=%s", request_type, env_name, db_names)

    if request_type in ("Create", "Update"):
        _create_databases(db_names)
        return {
            "PhysicalResourceId": f"db-bootstrap-{env_name}",
            "Data": {"DatabaseNames": ",".join(db_names)},
        }
    if request_type == "Delete":
        _drop_databases(db_names)
        return {"PhysicalResourceId": event["PhysicalResourceId"]}
    raise ValueError(f"unknown RequestType: {request_type}")


def _create_databases(db_names: list[str]) -> None:
    conn = _connect()
    conn.autocommit = True
    try:
        cursor = conn.cursor()
        for db in db_names:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,))
            if cursor.fetchone() is None:
                logger.info("creating database %s", db)
                cursor.execute(f'CREATE DATABASE "{db}"')
            else:
                logger.info("database %s already exists", db)
    finally:
        conn.close()


def _drop_databases(db_names: list[str]) -> None:
    conn = _connect()
    conn.autocommit = True
    try:
        cursor = conn.cursor()
        for db in db_names:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (db,),
            )
            cursor.execute(f'DROP DATABASE IF EXISTS "{db}"')
            logger.info("dropped database %s", db)
    finally:
        conn.close()
