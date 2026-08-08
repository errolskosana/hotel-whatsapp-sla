"""Tests for keeping tenant databases at the current schema revision."""
import pytest

import app.tenant_migrations as tm
from app.db_provisioner import db_name_of


HEAD = "0003_wa_comms"


@pytest.fixture
def targets(monkeypatch):
    """Two registered tenants; the DB URLs are opaque handles to the fakes."""
    monkeypatch.setattr(tm.settings, "control_plane_db_url", "postgresql://cp")
    monkeypatch.setattr(tm, "head_revision", lambda: HEAD)
    monkeypatch.setattr(tm, "_tenant_targets", lambda: [
        ("id-1", "hotel-one", "postgresql://u:p@h/hotel_one"),
        ("id-2", "hotel-two", "postgresql://u:p@h/hotel_two"),
    ])
    return None


def test_status_flags_a_tenant_behind_head(targets, monkeypatch):
    monkeypatch.setattr(tm, "current_revision", lambda url: (
        HEAD if "one" in url else "0002_features"
    ))

    statuses = {s.slug: s for s in tm.tenant_migration_status()}
    assert statuses["hotel-one"].is_current
    assert not statuses["hotel-two"].is_current
    assert "0002_features" in statuses["hotel-two"].summary


def test_status_reports_never_migrated_tenant(targets, monkeypatch):
    monkeypatch.setattr(tm, "current_revision", lambda url: None)

    statuses = tm.tenant_migration_status()
    assert all(not s.is_current for s in statuses)
    assert statuses[0].summary == "never migrated"


def test_unreachable_tenant_is_reported_not_raised(targets, monkeypatch):
    def _boom(url):
        raise OSError("connection refused")

    monkeypatch.setattr(tm, "current_revision", _boom)

    statuses = tm.tenant_migration_status()
    assert all(s.error == "connection refused" for s in statuses)
    assert all(not s.is_current for s in statuses)


def test_migrate_only_touches_tenants_behind_head(targets, monkeypatch):
    migrated = []
    revisions = {"hotel_one": HEAD, "hotel_two": "0002_features"}

    def _current(url):
        return revisions[db_name_of(url)]

    def _run(url):
        migrated.append(db_name_of(url))
        revisions[db_name_of(url)] = HEAD

    monkeypatch.setattr(tm, "current_revision", _current)
    monkeypatch.setattr(tm, "run_migrations", _run)

    results = tm.migrate_tenants()
    assert migrated == ["hotel_two"]          # the up-to-date tenant is left alone
    assert all(r.is_current for r in results)


def test_one_failing_tenant_does_not_block_the_others(targets, monkeypatch):
    migrated = []

    def _run(url):
        if "two" in url:
            raise RuntimeError("Alembic migration failed for 'hotel_two'")
        migrated.append(db_name_of(url))

    monkeypatch.setattr(tm, "current_revision", lambda url: "0002_features")
    monkeypatch.setattr(tm, "run_migrations", _run)

    results = {r.slug: r for r in tm.migrate_tenants()}
    assert migrated == ["hotel_one"]
    assert results["hotel-two"].error is not None
    assert not results["hotel-two"].is_current


def test_single_tenant_mode_is_a_noop(monkeypatch):
    monkeypatch.setattr(tm.settings, "control_plane_db_url", None)
    assert tm.migrate_tenants() == []
    assert tm.tenant_migration_status() == []


def test_drift_warning_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("control plane down")

    monkeypatch.setattr(tm, "tenant_migration_status", _boom)
    assert tm.warn_on_schema_drift() == []  # startup must not be blocked


def test_drift_warning_returns_stale_tenants(targets, monkeypatch):
    monkeypatch.setattr(tm, "current_revision", lambda url: (
        HEAD if "one" in url else None
    ))
    stale = tm.warn_on_schema_drift()
    assert [s.slug for s in stale] == ["hotel-two"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_check_exits_nonzero_when_a_tenant_is_behind(targets, monkeypatch, capsys):
    from app.scripts import migrate_tenants as cli

    monkeypatch.setattr(cli.settings, "control_plane_db_url", "postgresql://cp")
    monkeypatch.setattr(cli, "head_revision", lambda: HEAD)
    monkeypatch.setattr(tm, "current_revision", lambda url: (
        HEAD if "one" in url else "0002_features"
    ))

    assert cli.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert "hotel-two" in out
    assert "1 of 2" in out


def test_cli_check_exits_zero_when_all_current(targets, monkeypatch):
    from app.scripts import migrate_tenants as cli

    monkeypatch.setattr(cli.settings, "control_plane_db_url", "postgresql://cp")
    monkeypatch.setattr(cli, "head_revision", lambda: HEAD)
    monkeypatch.setattr(tm, "current_revision", lambda url: HEAD)

    assert cli.main(["--check"]) == 0


def test_cli_check_does_not_migrate(targets, monkeypatch):
    from app.scripts import migrate_tenants as cli

    def _must_not_run(url):
        raise AssertionError("--check must not change anything")

    monkeypatch.setattr(cli.settings, "control_plane_db_url", "postgresql://cp")
    monkeypatch.setattr(cli, "head_revision", lambda: HEAD)
    monkeypatch.setattr(tm, "current_revision", lambda url: "0002_features")
    monkeypatch.setattr(tm, "run_migrations", _must_not_run)

    assert cli.main(["--check"]) == 1


def test_cli_noop_in_single_tenant_mode(monkeypatch):
    from app.scripts import migrate_tenants as cli

    monkeypatch.setattr(cli.settings, "control_plane_db_url", None)
    assert cli.main([]) == 0


def test_db_name_is_extractable_without_exposing_credentials():
    assert db_name_of("postgresql+psycopg://user:secret@host:5432/hotel_two") == "hotel_two"
