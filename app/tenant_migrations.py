"""Keep every tenant database at the current schema revision.

Tenant DBs are migrated once, at provisioning time. Without something like this
module, a deploy that ships a migration upgrades only the main and control-plane
databases: every hotel provisioned before that deploy keeps the old schema, and
the ORM — which selects every mapped column — starts failing on the first query.
That is an outage for those hotels, not a degradation.

So: `migrate_tenants()` on every deploy, `tenant_migration_status()` to check.
"""
from dataclasses import dataclass

from sqlalchemy import select

from app.config import settings
from app.db_provisioner import current_revision, head_revision, run_migrations
from app.logger import get_logger

log = get_logger(__name__)


@dataclass
class TenantSchema:
    """Where one tenant's database sits relative to the current head."""
    tenant_id: str
    slug: str
    current: str | None
    head: str | None
    error: str | None = None

    @property
    def is_current(self) -> bool:
        return self.error is None and self.current is not None and self.current == self.head

    @property
    def summary(self) -> str:
        if self.error:
            return f"error: {self.error}"
        if self.current is None:
            return "never migrated"
        if self.current == self.head:
            return f"up to date ({self.current})"
        return f"behind: at {self.current}, head is {self.head}"


def _tenant_targets() -> list[tuple[str, str, str]]:
    """(tenant_id, slug, db_url) for every tenant, active or not.

    Inactive tenants are included deliberately: skipping them leaves a database
    that breaks the moment somebody reactivates it.
    """
    from app.control_plane import get_cp_session_direct, TenantHotel
    from app.crypto import decrypt_str

    cp = get_cp_session_direct()
    if cp is None:
        return []
    try:
        tenants = cp.execute(select(TenantHotel)).scalars().all()
        targets = []
        for t in tenants:
            try:
                targets.append((str(t.id), t.slug, decrypt_str(t.db_url_enc)))
            except Exception as exc:
                # Never let a decrypt failure surface the ciphertext or URL.
                log.error("tenant_db_url_decrypt_failed", slug=t.slug, error=str(exc))
        return targets
    finally:
        cp.close()


def tenant_migration_status() -> list[TenantSchema]:
    """Report each tenant's schema revision. Read-only."""
    if not settings.control_plane_db_url:
        return []
    head = head_revision()
    results: list[TenantSchema] = []
    for tenant_id, slug, db_url in _tenant_targets():
        try:
            results.append(TenantSchema(tenant_id, slug, current_revision(db_url), head))
        except Exception as exc:
            results.append(TenantSchema(tenant_id, slug, None, head, error=str(exc)))
    return results


def migrate_tenants() -> list[TenantSchema]:
    """Run `alembic upgrade head` against every tenant DB that needs it.

    One tenant failing does not stop the others — the caller gets the full
    picture and decides what to do about it.
    """
    if not settings.control_plane_db_url:
        return []
    head = head_revision()
    results: list[TenantSchema] = []

    for tenant_id, slug, db_url in _tenant_targets():
        try:
            before = current_revision(db_url)
        except Exception as exc:
            log.error("tenant_migration_unreachable", slug=slug, error=str(exc))
            results.append(TenantSchema(tenant_id, slug, None, head, error=str(exc)))
            continue

        if before == head:
            results.append(TenantSchema(tenant_id, slug, before, head))
            continue

        try:
            run_migrations(db_url)
            after = current_revision(db_url)
            log.info("tenant_migrated", slug=slug, from_revision=before, to_revision=after)
            results.append(TenantSchema(tenant_id, slug, after, head))
        except Exception as exc:
            log.error("tenant_migration_failed", slug=slug, error=str(exc))
            results.append(TenantSchema(tenant_id, slug, before, head, error=str(exc)))

    return results


def warn_on_schema_drift() -> list[TenantSchema]:
    """Log loudly about tenants that are behind. Never raises.

    Called at startup so a forgotten migration step shows up in the logs before
    it shows up as failing webhooks.
    """
    try:
        statuses = tenant_migration_status()
    except Exception as exc:
        log.warning("tenant_schema_check_failed", error=str(exc))
        return []

    stale = [s for s in statuses if not s.is_current]
    for s in stale:
        log.error(
            "tenant_schema_drift",
            slug=s.slug,
            status=s.summary,
            remedy="run: python -m app.scripts.migrate_tenants",
        )
    return stale
