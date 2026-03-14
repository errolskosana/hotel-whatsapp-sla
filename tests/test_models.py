"""Tests for DB model creation and constraints."""
import uuid
import pytest
from sqlalchemy.exc import IntegrityError
from app.models import Hotel, StaffUser, Room, KnowledgeChunk, Conversation, Message
from app.auth import hash_password
from app.crypto import encrypt_str


def test_create_hotel(db):
    h = Hotel(
        name="Test Hotel",
        whatsapp_phone_number_id="99999",
        whatsapp_business_phone_e164="+10000000000",
        whatsapp_access_token_enc=encrypt_str("tok"),
        manager_wa_e164="+10000000001",
        sla_seconds=30,
    )
    db.add(h)
    db.commit()
    assert h.id is not None
    assert h.sla_seconds == 30


def test_hotel_sla_default(db):
    h = Hotel(
        name="Default SLA Hotel",
        whatsapp_phone_number_id="88888",
        whatsapp_business_phone_e164="+10000000002",
        whatsapp_access_token_enc=encrypt_str("tok"),
        manager_wa_e164="+10000000003",
    )
    db.add(h)
    db.commit()
    assert h.sla_seconds == 20


def test_staff_user_password_hash(db, hotel):
    u = StaffUser(
        hotel_id=hotel.id,
        email="test-unique@test.com",
        password_hash=hash_password("password"),
        role="agent",
        is_active=True,
    )
    db.add(u)
    db.commit()
    assert u.password_hash != "password"


def test_knowledge_chunk_creation(db, hotel):
    chunk = KnowledgeChunk(
        hotel_id=hotel.id,
        content="Pool is open 8am to 8pm.",
    )
    db.add(chunk)
    db.commit()
    assert chunk.id is not None
    assert chunk.is_active is True


def test_conversation_opted_out_default(db, hotel):
    conv = Conversation(
        hotel_id=hotel.id,
        wa_id="15551234567",
    )
    db.add(conv)
    db.commit()
    assert conv.opted_out is False
