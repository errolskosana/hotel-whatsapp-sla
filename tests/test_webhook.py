"""Tests for WhatsApp webhook signature verification and opt-out."""
import hmac
import hashlib
import json
import pytest
from app.whatsapp import verify_webhook_signature


SECRET = "my-app-secret"


def _sign(body: bytes) -> str:
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def test_valid_signature():
    body = b'{"test": true}'
    assert verify_webhook_signature(SECRET, body, _sign(body))


def test_invalid_signature():
    body = b'{"test": true}'
    assert not verify_webhook_signature(SECRET, body, "sha256=badhash")


def test_missing_signature():
    assert not verify_webhook_signature(SECRET, b"body", None)
    assert not verify_webhook_signature(SECRET, b"body", "")


def test_wrong_prefix():
    body = b"body"
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert not verify_webhook_signature(SECRET, body, f"md5={sig}")


def test_webhook_get_verify(client):
    resp = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-token",
            "hub.challenge": "abc123",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "abc123"


def test_webhook_get_bad_token(client):
    resp = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "abc123",
        },
    )
    assert resp.status_code == 403


def test_webhook_post_bad_signature(client):
    resp = client.post(
        "/webhooks/whatsapp",
        content=b'{}',
        headers={"x-hub-signature-256": "sha256=badhash"},
    )
    assert resp.status_code == 403
