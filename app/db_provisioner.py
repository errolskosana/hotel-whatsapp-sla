"""Provision and migrate tenant databases.

Provisioning a new tenant:
  1. Connect to the PostgreSQL server's 'postgres' default DB.
  2. CREATE DATABASE if it doesn't already exist.
  3. Run `alembic upgrade head` against the new DB URL so the schema is applied.

Migrating existing tenants is the same step 3, and must be run for every tenant
on every deploy that ships a migration — see app/scripts/migrate_tenants.py.

The DB URL must be a valid psycopg URL:
    postgresql+psycopg://user:password@host:port/dbname

Note: a tenant DB URL contains a password. Never put one in a log line, an
exception message, or anything else that leaves this module.
"""
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

# alembic.ini's script_location is relative, so migrations must be invoked from
# the project root regardless of where the caller happens to be running.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def db_name_of(db_url: str) -> str:
    """The database name, safe to log (unlike the URL, which holds a password)."""
    return urlparse(db_url).path.lstrip("/")


def run_migrations(db_url: str) -> None:
    """Run `alembic upgrade head` against db_url. Idempotent."""
    # alembic/env.py reads settings.database_url, which is populated from the
    # environment, so this is how the target DB is selected.
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Alembic migration failed for '{db_name_of(db_url)}':\n{result.stderr}"
        )


def head_revision() -> str | None:
    """The newest revision in alembic/versions — what every DB should be at."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def current_revision(db_url: str) -> str | None:
    """The revision a database is actually at, or None if never migrated.

    Connection errors propagate — an unreachable tenant is a different problem
    from an un-migrated one, and the caller needs to tell them apart.
    """
    engine = create_engine(db_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            if not inspect(conn).has_table("alembic_version"):
                return None
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
            return row[0] if row else None
    finally:
        engine.dispose()


def provision_tenant_db(db_url: str) -> None:
    """Create the database and run migrations.  Idempotent — safe to call twice."""
    db_name = db_name_of(db_url)

    # Build URL pointing at the default 'postgres' DB for the CREATE DATABASE command
    admin_url = db_url.replace(f"/{db_name}", "/postgres")

    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_name},
            ).fetchone()
            if not exists:
                # psycopg2/psycopg3 don't allow parameterised DDL; use safe f-string
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        engine.dispose()

    run_migrations(db_url)
