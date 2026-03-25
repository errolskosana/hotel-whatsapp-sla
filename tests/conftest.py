"""Shared test fixtures.

Strategy: patch app.db engine at import time to use SQLite in-memory,
so both the startup hook and DI use the same test DB.
"""
import os
import uuid
from cryptography.fernet import Fernet

# Generate a valid Fernet key and set all required env vars BEFORE any app import
_FERNET_KEY = Fernet.generate_key().decode()
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["META_APP_SECRET"] = "test-secret"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-token"
os.environ["ENCRYPTION_MASTER_KEY"] = _FERNET_KEY
os.environ["VAPID_SUBJECT"] = "mailto:test@test.com"
os.environ["VAPID_PUBLIC_KEY"] = "test-pub-key"
os.environ["VAPID_PRIVATE_KEY"] = "test-priv-key"
os.environ["JWT_SECRET"] = "test-jwt-secret-at-least-32-chars!!"
# Disable multi-tenant control plane in tests (single-tenant SQLite mode)
os.environ.pop("CONTROL_PLANE_DB_URL", None)
os.environ["CONTROL_PLANE_DB_URL"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Patch app.db before importing app.main so the startup hook uses the test engine
import app.db as _app_db

# StaticPool shares ONE connection so all sessions see the same in-memory DB
_TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TEST_SESSIONMAKER = sessionmaker(bind=_TEST_ENGINE, autoflush=False, autocommit=False)
_app_db.engine = _TEST_ENGINE
_app_db.SessionLocal = _TEST_SESSIONMAKER

from app.db import Base
from app.models import Hotel, StaffUser, Room, KnowledgeChunk, Conversation, Message  # noqa: registers models
from app.auth import hash_password, create_access_token
from app.crypto import encrypt_str
from app.main import app
from app.db import db_session
from app.tenant_db import get_tenant_session

# Create schema once
Base.metadata.create_all(_TEST_ENGINE)


# ---------------------------------------------------------------------------
# Per-test table reset for isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_db():
    """Drop and recreate all tables before each test to ensure isolation."""
    Base.metadata.drop_all(_TEST_ENGINE)
    Base.metadata.create_all(_TEST_ENGINE)
    yield


@pytest.fixture
def db(reset_db):
    session = _TEST_SESSIONMAKER()
    yield session
    session.rollback()
    session.close()


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hotel(db):
    h = Hotel(
        name="Test Hotel",
        whatsapp_phone_number_id=str(uuid.uuid4()).replace("-", "")[:15],
        whatsapp_business_phone_e164="+15550001111",
        whatsapp_access_token_enc=encrypt_str("fake-token"),
        manager_wa_e164="+15559999999",
        sla_seconds=20,
    )
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


@pytest.fixture
def manager_user(db, hotel):
    u = StaffUser(
        hotel_id=hotel.id,
        email="manager@test.com",
        password_hash=hash_password("password123"),
        role="manager",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def agent_user(db, hotel):
    u = StaffUser(
        hotel_id=hotel.id,
        email="agent@test.com",
        password_hash=hash_password("password123"),
        role="agent",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def manager_token(manager_user, hotel):
    return create_access_token(str(manager_user.id), str(hotel.id), "manager")


@pytest.fixture
def agent_token(agent_user, hotel):
    return create_access_token(str(agent_user.id), str(hotel.id), "agent")


@pytest.fixture
def client(db):
    def _override_db():
        yield db

    app.dependency_overrides[db_session] = _override_db
    app.dependency_overrides[get_tenant_session] = _override_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()
