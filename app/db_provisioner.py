"""Provision a new tenant database.

Steps:
  1. Connect to the PostgreSQL server's 'postgres' default DB.
  2. CREATE DATABASE if it doesn't already exist.
  3. Run `alembic upgrade head` against the new DB URL so the schema is applied.

The DB URL must be a valid psycopg URL:
    postgresql+psycopg://user:password@host:port/dbname
"""
import os
import subprocess
import sys
from urllib.parse import urlparse

from sqlalchemy import create_engine, text


def provision_tenant_db(db_url: str) -> None:
    """Create the database and run migrations.  Idempotent — safe to call twice."""
    parsed = urlparse(db_url)
    db_name = parsed.path.lstrip("/")

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

    # Run alembic upgrade head with the new DB URL injected via env
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Alembic migration failed for '{db_name}':\n{result.stderr}"
        )
