"""Tests for authentication helpers."""
import pytest
from app.auth import hash_password, verify_password, create_access_token, decode_token


def test_hash_and_verify_password():
    pw = "SuperSecret99!"
    hashed = hash_password(pw)
    assert hashed != pw
    assert verify_password(pw, hashed)
    assert not verify_password("wrong", hashed)


def test_hash_is_unique():
    pw = "same-password"
    h1 = hash_password(pw)
    h2 = hash_password(pw)
    # bcrypt generates a new salt each time
    assert h1 != h2
    assert verify_password(pw, h1)
    assert verify_password(pw, h2)


def test_token_roundtrip():
    token = create_access_token("user-id-123", "hotel-id-456", "manager")
    payload = decode_token(token)
    assert payload is not None
    assert payload["sub"] == "user-id-123"
    assert payload["hotel_id"] == "hotel-id-456"
    assert payload["role"] == "manager"


def test_invalid_token_returns_none():
    assert decode_token("not.a.valid.token") is None
    assert decode_token("") is None


def test_tampered_token_returns_none():
    token = create_access_token("uid", "hid", "agent")
    # Flip a character in the signature
    tampered = token[:-3] + "xxx"
    assert decode_token(tampered) is None
