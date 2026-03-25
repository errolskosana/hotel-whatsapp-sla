"""Control plane: TenantHotel and SuperAdmin models + session factory.

The control plane lives in a separate database (hotel_control) and holds:
  - TenantHotel: routing + branding metadata for every hotel tenant
  - SuperAdmin:  super-administrator accounts

When CONTROL_PLANE_DB_URL is not set the entire module is a no-op and
the app runs in single-tenant mode (original behaviour).
"""
import uuid
from datetime import datetime

from sqlalchemy import create_engine, String, DateTime, Boolean, Text
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from app.config import settings


class ControlBase(DeclarativeBase):
    pass


class TenantHotel(ControlBase):
    __tablename__ = "tenant_hotels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    db_url_enc: Mapped[str] = mapped_column(Text())          # Fernet-encrypted DB URL
    whatsapp_phone_number_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean(), default=True)

    # White-label branding
    brand_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    brand_color_primary: Mapped[str | None] = mapped_column(String(32), nullable=True)   # hex e.g. #1d4ed8
    brand_color_sidebar: Mapped[str | None] = mapped_column(String(32), nullable=True)   # hex e.g. #0f2340
    brand_tagline: Mapped[str | None] = mapped_column(String(500), nullable=True)
    brand_logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow)


class SuperAdmin(ControlBase):
    __tablename__ = "super_admins"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean(), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Engine + session factory (lazy-initialised)
# ---------------------------------------------------------------------------

_cp_engine = None
_ControlSessionLocal = None


def init_control_plane() -> bool:
    """Initialise the control plane engine. Returns True if configured."""
    global _cp_engine, _ControlSessionLocal
    if _cp_engine is not None:
        return True
    if not settings.control_plane_db_url:
        return False
    _cp_engine = create_engine(settings.control_plane_db_url, pool_pre_ping=True)
    _ControlSessionLocal = sessionmaker(bind=_cp_engine, autoflush=False, autocommit=False)
    ControlBase.metadata.create_all(_cp_engine)
    return True


def cp_session():
    """FastAPI dependency — yields a control plane Session (or None if unconfigured)."""
    if not init_control_plane():
        yield None
        return
    db = _ControlSessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_cp_session_direct():
    """Non-dependency helper for startup/Celery — caller must close the session."""
    if not init_control_plane():
        return None
    return _ControlSessionLocal()
