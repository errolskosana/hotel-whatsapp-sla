"""Per-tenant DB engine cache and FastAPI session dependency.

In single-tenant mode (no CONTROL_PLANE_DB_URL) this module is transparent:
get_tenant_session falls through to the regular SessionLocal.

In multi-tenant mode the JWT must carry a 'tenant_id' claim (the UUID of the
TenantHotel row in the control plane).  The first request for a tenant
initialises an SQLAlchemy engine pointing at that tenant's database; subsequent
requests reuse the cached engine.
"""
import threading
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import Request

from app.config import settings
from app.db import SessionLocal

_lock = threading.Lock()
_tenant_cache: dict[str, sessionmaker] = {}   # tenant_id (str) → sessionmaker


def _init_tenant(tenant_id: str) -> sessionmaker:
    """Look up the tenant's DB URL from the control plane, create engine + factory."""
    from app.control_plane import get_cp_session_direct, TenantHotel
    from app.crypto import decrypt_str

    cp = get_cp_session_direct()
    if cp is None:
        raise RuntimeError("Control plane not configured; cannot resolve tenant DB.")
    try:
        tenant = cp.get(TenantHotel, uuid.UUID(tenant_id))
        if not tenant or not tenant.is_active:
            raise ValueError(f"Tenant {tenant_id} not found or is inactive.")
        db_url = decrypt_str(tenant.db_url_enc)
        engine = create_engine(db_url, pool_pre_ping=True, pool_size=5, max_overflow=10)
        return sessionmaker(bind=engine, autoflush=False, autocommit=False)
    finally:
        cp.close()


def _get_factory(tenant_id: str) -> sessionmaker:
    with _lock:
        if tenant_id not in _tenant_cache:
            _tenant_cache[tenant_id] = _init_tenant(tenant_id)
        return _tenant_cache[tenant_id]


def evict_tenant_cache(tenant_id: str) -> None:
    """Remove a tenant's engine from the cache (call after db_url change)."""
    with _lock:
        _tenant_cache.pop(tenant_id, None)


def get_tenant_session(request: Request):
    """FastAPI dependency — yields the correct DB session for the current user.

    Resolution order:
      1. If CONTROL_PLANE_DB_URL is set AND the JWT contains 'tenant_id',
         use the per-tenant engine cache.
      2. Otherwise fall back to the single-tenant SessionLocal (original behaviour).
    """
    token = request.cookies.get("access_token")
    if token and settings.control_plane_db_url:
        from app.auth import decode_token
        payload = decode_token(token)
        if payload:
            tenant_id = payload.get("tenant_id")
            if tenant_id:
                factory = _get_factory(tenant_id)
                db = factory()
                try:
                    yield db
                    return
                finally:
                    db.close()
    # Single-tenant fallback
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_session_for_tenant(tenant_id: str):
    """Open and return a session for a tenant (caller must close). Used by Celery tasks."""
    factory = _get_factory(tenant_id)
    return factory()
