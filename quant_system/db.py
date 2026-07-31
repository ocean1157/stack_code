from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import pandas as pd

from .config import ROOT, SETTINGS


def _connection_args() -> tuple[list[str], dict[str, str]]:
    env = os.environ.copy()
    env["PGPASSWORD"] = SETTINGS.postgres_password
    env["PGCLIENTENCODING"] = "UTF8"
    env["PGOPTIONS"] = "-c client_min_messages=warning"
    return ["psql", "-X", "-q", "-h", SETTINGS.postgres_host, "-p", str(SETTINGS.postgres_port), "-U", SETTINGS.postgres_user, "-d", SETTINGS.postgres_database, "-v", "ON_ERROR_STOP=1"], env


def run_sql_file(path: Path) -> None:
    args, env = _connection_args()
    subprocess.run(args + ["-f", str(path)], check=True, env=env)


def _run_sql(sql: str) -> None:
    args, env = _connection_args()
    subprocess.run(args + ["-c", sql], check=True, env=env)


def copy_frame(frame: pd.DataFrame, table: str, columns: list[str], conflict: list[str] | None = None) -> None:
    if frame.empty:
        return
    args, env = _connection_args()
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="", encoding="utf-8") as handle:
        path = Path(handle.name)
        frame[columns].to_csv(handle, index=False, header=False, na_rep="")
    staging = f"staging_{table}_{uuid.uuid4().hex[:10]}"
    try:
        if conflict:
            _run_sql(f"CREATE UNLOGGED TABLE {staging} (LIKE {table} INCLUDING DEFAULTS)")
            destination = staging
        else:
            destination = table
        quoted = str(path).replace("\\", "/").replace("'", "''")
        cols = ",".join(columns)
        sql = f"\\copy {destination} ({cols}) FROM '{quoted}' WITH (FORMAT csv, NULL '')"
        subprocess.run(args + ["-c", sql], check=True, env=env)
        if conflict:
            keys = ",".join(conflict)
            updates = ",".join(f"{c}=EXCLUDED.{c}" for c in columns if c not in conflict)
            _run_sql(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {staging} ON CONFLICT ({keys}) DO UPDATE SET {updates}")
    finally:
        if conflict:
            try:
                _run_sql(f"DROP TABLE IF EXISTS {staging}")
            except subprocess.CalledProcessError:
                pass
        path.unlink(missing_ok=True)


def initialize() -> None:
    run_sql_file(ROOT / "sql" / "schema.sql")
