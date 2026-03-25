"""Integration tests for protected API endpoints."""
import pytest


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.text == "OK"


def test_login_page(client):
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 200
    assert "Sign In" in resp.text


def test_login_bad_credentials(client, hotel):
    resp = client.post("/login", data={
        "hotel_id": str(hotel.id),
        "email": "nobody@nowhere.com",
        "password": "wrong",
    }, follow_redirects=False)
    assert resp.status_code == 401


def test_login_success_redirects_to_inbox(client, hotel, manager_user):
    resp = client.post("/login", data={
        "hotel_id": str(hotel.id),
        "email": "manager@test.com",
        "password": "password123",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard/inbox"
    assert "access_token" in resp.cookies


def test_inbox_requires_auth(client):
    resp = client.get("/dashboard/inbox", follow_redirects=False)
    assert resp.status_code == 302
    assert "login" in resp.headers["location"]


def test_inbox_accessible_with_token(client, manager_token):
    client.cookies.set("access_token", manager_token)
    resp = client.get("/dashboard/inbox")
    assert resp.status_code == 200
    assert "Inbox" in resp.text


def test_knowledge_page(client, manager_token):
    client.cookies.set("access_token", manager_token)
    resp = client.get("/dashboard/knowledge")
    assert resp.status_code == 200


def test_rooms_page(client, agent_token):
    client.cookies.set("access_token", agent_token)
    resp = client.get("/dashboard/rooms")
    assert resp.status_code == 200


def test_add_room_requires_manager(client, agent_token):
    client.cookies.set("access_token", agent_token)
    resp = client.post("/dashboard/rooms", data={
        "room_number": "999",
        "room_type": "standard",
    }, follow_redirects=False)
    assert resp.status_code == 403


def test_add_room_as_manager(client, manager_token, hotel):
    client.cookies.set("access_token", manager_token)
    resp = client.post("/dashboard/rooms", data={
        "room_number": "301",
        "room_type": "deluxe",
    }, follow_redirects=False)
    assert resp.status_code == 302


def test_analytics_page(client, manager_token):
    client.cookies.set("access_token", manager_token)
    resp = client.get("/dashboard/analytics")
    assert resp.status_code == 200
    assert "Analytics" in resp.text


def test_settings_requires_manager(client, agent_token):
    client.cookies.set("access_token", agent_token)
    resp = client.get("/dashboard/settings", follow_redirects=False)
    assert resp.status_code == 403


def test_settings_accessible_as_manager(client, manager_token):
    client.cookies.set("access_token", manager_token)
    resp = client.get("/dashboard/settings")
    assert resp.status_code == 200


def test_logout_clears_cookie(client, manager_token):
    client.cookies.set("access_token", manager_token)
    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 302
    # Cookie should be cleared
    assert resp.cookies.get("access_token") == "" or "access_token" not in resp.cookies


def test_tenant_isolation_on_ack(client, agent_token, db):
    """An agent cannot ack a message that belongs to a different hotel."""
    import uuid
    from app.models import Hotel, Conversation, Message
    from app.crypto import encrypt_str
    from datetime import datetime, timezone

    # Create a second hotel
    other = Hotel(
        name="Other Hotel",
        whatsapp_phone_number_id="77777",
        whatsapp_business_phone_e164="+19990000000",
        whatsapp_access_token_enc=encrypt_str("tok"),
        manager_wa_e164="+19990000001",
    )
    db.add(other)
    db.commit()

    conv = Conversation(hotel_id=other.id, wa_id="15550000000")
    db.add(conv)
    db.commit()

    msg = Message(
        hotel_id=other.id,
        conversation_id=conv.id,
        direction="in",
        body="Hello from other hotel",
        received_at=datetime.now(timezone.utc),
        status="unactioned",
    )
    db.add(msg)
    db.commit()

    client.cookies.set("access_token", agent_token)
    resp = client.post(f"/api/messages/{msg.id}/ack")
    assert resp.status_code == 404
