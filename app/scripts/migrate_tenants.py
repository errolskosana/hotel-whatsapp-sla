"""Apply pending migrations to every tenant database.

Run this on every deploy that ships a migration, right after migrating the main
database. `alembic upgrade head` only touches the DB in DATABASE_URL; tenants
live in their own databases and are otherwise migrated only at provisioning.

    python -m app.scripts.migrate_tenants            # upgrade every tenant
    python -m app.scripts.migrate_tenants --check    # report only; exit 1 if any is behind

Exit codes: 0 all tenants at head, 1 one or more behind or unreachable.
In single-tenant mode (no CONTROL_PLANE_DB_URL) this is a no-op.
"""
import argparse
import sys

from app.config import settings
from app.db_provisioner import head_revision
from app.tenant_migrations import migrate_tenants, tenant_migration_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift without changing anything (for deploy gates / CI)",
    )
    args = parser.parse_args(argv)

    if not settings.control_plane_db_url:
        # Output stays ASCII: this runs in deploy consoles with unknown codepages.
        print("Single-tenant mode (CONTROL_PLANE_DB_URL unset) - nothing to do.")
        return 0

    print(f"Schema head: {head_revision()}\n")

    results = tenant_migration_status() if args.check else migrate_tenants()
    if not results:
        print("No tenants registered.")
        return 0

    width = max(len(r.slug) for r in results)
    for r in results:
        marker = "ok  " if r.is_current else "FAIL"
        print(f"[{marker}] {r.slug.ljust(width)}  {r.summary}")

    stale = [r for r in results if not r.is_current]
    print()
    if stale:
        verb = "are behind" if args.check else "failed to migrate"
        print(f"{len(stale)} of {len(results)} tenant database(s) {verb}.")
        return 1

    print(f"All {len(results)} tenant database(s) at head.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
