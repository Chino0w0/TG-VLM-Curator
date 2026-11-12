"""Opt-in PostgreSQL integration support; never accepts a business database as admin DB."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import SkipTest
from uuid import uuid4

import asyncpg
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]


def postgres_admin_url() -> str:
    value = os.environ.get("TGCURATOR_TEST_POSTGRES_ADMIN_URL")
    if value:
        parsed = make_url(value)
        if parsed.database != "postgres" or not parsed.drivername.startswith("postgresql"):
            raise RuntimeError("test admin URL must address the isolated postgres maintenance DB")
        return value
    config_path = os.environ.get("TGCURATOR_TEST_SERVICES_FILE")
    if not config_path:
        raise SkipTest("set TGCURATOR_TEST_POSTGRES_ADMIN_URL for real PostgreSQL tests")
    settings = json.loads(Path(config_path).read_text(encoding="utf-8"))
    from sqlalchemy import URL

    return URL.create(
        "postgresql+asyncpg",
        username=settings["user"],
        password=settings["password"],
        host=settings["host"],
        port=settings["postgres_port"],
        database="postgres",
    ).render_as_string(hide_password=False)


async def _database_command(admin_url: str, name: str, *, create: bool) -> None:
    if not re.fullmatch(r"tgcurator_test_[a-z0-9_]+", name):
        raise RuntimeError("refusing non-test database name")
    parsed = make_url(admin_url)
    if parsed.database != "postgres":
        raise RuntimeError("refusing a non-maintenance test connection")
    conn = await asyncpg.connect(
        host=parsed.host,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        database="postgres",
        timeout=15,
    )
    try:
        sql = (
            f'CREATE DATABASE "{name}"'
            if create
            else f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'
        )
        await conn.execute(sql)
    finally:
        await conn.close()


def migrate_database(database_url: str, revision: str = "head") -> None:
    env = os.environ.copy()
    env.update(TGCURATOR_DATABASE_URL=database_url, TGCURATOR_ENVIRONMENT="test")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        # Do not let URLs/passwords escape through an exception or captured command line.
        parsed = make_url(database_url)
        output = result.stdout.replace(database_url, "[TEST_DATABASE_URL]")
        if parsed.password:
            output = output.replace(parsed.password, "[TEST_PASSWORD]")
        raise AssertionError("PostgreSQL migration failed:\n" + output)


@contextmanager
def temporary_postgres_database(module: str, *, revision: str | None = "head"):
    """Create/migrate/drop one UUID-suffixed database on the explicit isolated server."""
    if not re.fullmatch(r"[a-z0-9_]+", module):
        raise ValueError("test module prefix must contain lowercase letters/digits/underscores")
    admin_url = postgres_admin_url()
    name = f"tgcurator_test_{module}_{uuid4().hex[:12]}"
    parsed = make_url(admin_url).set(drivername="postgresql+asyncpg", database=name)
    database_url = parsed.render_as_string(hide_password=False)
    asyncio.run(_database_command(admin_url, name, create=True))
    try:
        if revision is not None:
            migrate_database(database_url, revision)
        yield database_url
    finally:
        asyncio.run(_database_command(admin_url, name, create=False))
