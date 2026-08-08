"""Tests for outbound WhatsApp: staff replies and SLA escalation alerts."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

import app.main as main
import app.tasks as tasks
from app.models import Conversation, Message
from app.whatsapp import WhatsAppError


@pytest.fixture
def thread(db, hotel):
    """A guest conversation with one unactioned inbound message."""
    conv = Conversation(
        hotel_id=hotel.id,
        wa_id="27991234567",
        room_number="101",
        last_inbound_at=datetime.utcnow(),
        last_message_at=datetime.utcnow(),
    )
    db.add(conv)
    db.commit()
    msg = Message(
        hotel_id=hotel.id,
        conversation_id=conv.id,
        direction="in",
        wa_message_id="wamid.in",
        body="Could I get more towels?",
        received_at=datetime.utcnow(),
        status="unactioned",
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return conv, msg


@pytest.fixture
def auth(client, hotel, manager_user):
    client.post("/login", data={
        "hotel_id": str(hotel.id),
        "email": "manager@test.com",
        "password": "password123",
    }, follow_redirects=False)
    return client


# ---------------------------------------------------------------------------
# Staff reply
# ---------------------------------------------------------------------------

def test_reply_inside_window_succeeds(auth, db, thread, monkeypatch):
    conv, msg = thread

    async def _fake_send(**kwargs):
        assert kwargs["to_e164_or_waid"] == conv.wa_id
        return {"messages": [{"id": "wamid.out"}]}

    monkeypatch.setattr(main, "send_whatsapp_text", _fake_send)

    resp = auth.post(f"/api/messages/{msg.id}/reply", data={"reply": "On the way!"})
    assert resp.status_code == 200

    db.refresh(msg)
    assert msg.status == "replied"
    out = db.execute(select(Message).where(Message.direction == "out")).scalar_one()
    assert out.wa_message_id == "wamid.out"


def test_reply_outside_24h_window_is_refused_before_sending(auth, db, thread, monkeypatch):
    """Regression: we used to send, get a 131047, and mark it replied anyway."""
    conv, msg = thread
    conv.last_inbound_at = datetime.utcnow() - timedelta(hours=25)
    db.commit()

    async def _must_not_send(**kwargs):
        raise AssertionError("should not reach the Cloud API")

    monkeypatch.setattr(main, "send_whatsapp_text", _must_not_send)

    resp = auth.post(f"/api/messages/{msg.id}/reply", data={"reply": "hello?"})
    assert resp.status_code == 409
    assert "24-hour" in resp.json()["detail"]

    db.refresh(msg)
    assert msg.status == "unactioned"  # SLA clock keeps running


def test_failed_send_does_not_mark_message_replied(auth, db, thread, monkeypatch):
    conv, msg = thread

    async def _boom(**kwargs):
        raise WhatsAppError("Recipient unavailable", http_status=400, code=131026)

    monkeypatch.setattr(main, "send_whatsapp_text", _boom)

    resp = auth.post(f"/api/messages/{msg.id}/reply", data={"reply": "hi"})
    assert resp.status_code == 502

    db.refresh(msg)
    assert msg.status == "unactioned"
    assert db.execute(select(Message).where(Message.direction == "out")).scalars().all() == []


# ---------------------------------------------------------------------------
# SLA escalation to the manager
# ---------------------------------------------------------------------------

def test_escalation_uses_template_when_configured(db, hotel, thread, monkeypatch):
    """A template is the only thing that reaches a manager outside 24h."""
    _, msg = thread
    sent = {}

    def _fake_template(**kwargs):
        sent.update(kwargs)
        return {"messages": [{"id": "wamid.tmpl"}]}

    def _must_not_text(**kwargs):
        raise AssertionError("free-form text would be rejected outside the window")

    monkeypatch.setattr(tasks.settings, "whatsapp_escalation_template", "sla_alert")
    monkeypatch.setattr(tasks, "send_whatsapp_template_sync", _fake_template)
    monkeypatch.setattr(tasks, "send_whatsapp_text_sync", _must_not_text)

    tasks._notify_manager_whatsapp(hotel, msg, tasks._room_label(db, msg))

    assert sent["template_name"] == "sla_alert"
    assert sent["to_e164_or_waid"] == hotel.manager_wa_e164
    assert sent["body_params"][0] == hotel.name
    assert sent["body_params"][1] == "Room 101"


def test_escalation_falls_back_to_text_without_template(db, hotel, thread, monkeypatch):
    _, msg = thread
    sent = {}

    monkeypatch.setattr(tasks.settings, "whatsapp_escalation_template", None)
    monkeypatch.setattr(tasks, "send_whatsapp_text_sync", lambda **kw: sent.update(kw) or {})

    tasks._notify_manager_whatsapp(hotel, msg, tasks._room_label(db, msg))

    assert "Room 101" in sent["text"]
    assert "Could I get more towels?" in sent["text"]


def test_escalation_surfaces_window_closure(db, hotel, thread, monkeypatch):
    """Without a template, an out-of-window alert must surface as 131047."""
    _, msg = thread

    def _reengagement(**kwargs):
        raise WhatsAppError("Re-engagement message", http_status=400, code=131047)

    monkeypatch.setattr(tasks.settings, "whatsapp_escalation_template", None)
    monkeypatch.setattr(tasks, "send_whatsapp_text_sync", _reengagement)

    with pytest.raises(WhatsAppError) as exc:
        tasks._notify_manager_whatsapp(hotel, msg, "Room 101")
    assert exc.value.needs_template


def test_pending_template_falls_back_to_free_form(db, hotel, thread, monkeypatch):
    """A template awaiting Meta review must not swallow the alert."""
    _, msg = thread
    sent = {}

    def _not_approved(**kwargs):
        raise WhatsAppError("template does not exist", http_status=400, code=132001)

    monkeypatch.setattr(tasks.settings, "whatsapp_escalation_template", "sla_breach_alert")
    monkeypatch.setattr(tasks, "send_whatsapp_template_sync", _not_approved)
    monkeypatch.setattr(tasks, "send_whatsapp_text_sync", lambda **kw: sent.update(kw) or {})

    tasks._notify_manager_whatsapp(hotel, msg, "Room 101")
    assert "Room 101" in sent["text"]  # the manager still gets told


def test_paused_template_falls_back_to_free_form(db, hotel, thread, monkeypatch):
    _, msg = thread
    sent = {}
    monkeypatch.setattr(tasks.settings, "whatsapp_escalation_template", "sla_breach_alert")
    monkeypatch.setattr(tasks, "send_whatsapp_template_sync", lambda **kw: (_ for _ in ()).throw(
        WhatsAppError("template paused", http_status=400, code=132015)))
    monkeypatch.setattr(tasks, "send_whatsapp_text_sync", lambda **kw: sent.update(kw) or {})

    tasks._notify_manager_whatsapp(hotel, msg, "Room 101")
    assert sent  # fell through


def test_non_template_error_is_not_masked_by_fallback(db, hotel, thread, monkeypatch):
    """A bad token must surface, not be retried as free-form."""
    _, msg = thread

    def _bad_token(**kwargs):
        raise WhatsAppError("invalid oauth token", http_status=401, code=190)

    def _must_not_run(**kwargs):
        raise AssertionError("must not fall back on a non-template error")

    monkeypatch.setattr(tasks.settings, "whatsapp_escalation_template", "sla_breach_alert")
    monkeypatch.setattr(tasks, "send_whatsapp_template_sync", _bad_token)
    monkeypatch.setattr(tasks, "send_whatsapp_text_sync", _must_not_run)

    with pytest.raises(WhatsAppError) as exc:
        tasks._notify_manager_whatsapp(hotel, msg, "Room 101")
    assert exc.value.code == 190


def test_template_params_are_sanitised(db, hotel, thread, monkeypatch):
    """Meta rejects params with newlines, tabs, or 4+ consecutive spaces."""
    conv, msg = thread
    msg.body = "Hi there\n\nthe shower    is\tbroken"
    db.commit()

    sent = {}
    monkeypatch.setattr(tasks.settings, "whatsapp_escalation_template", "sla_breach_alert")
    monkeypatch.setattr(tasks, "send_whatsapp_template_sync", lambda **kw: sent.update(kw) or {})

    tasks._notify_manager_whatsapp(hotel, msg, "Room 101")

    from app.whatsapp import _template_payload
    payload = _template_payload("+27999", "sla_breach_alert", "en", sent["body_params"])
    for param in payload["template"]["components"][0]["parameters"]:
        text = param["text"]
        assert "\n" not in text and "\t" not in text and "    " not in text
        assert text.strip() == text and text != ""


def test_whitespace_only_body_does_not_become_empty_param(db, hotel, thread, monkeypatch):
    conv, msg = thread
    msg.body = "   \n\t  "
    db.commit()

    sent = {}
    monkeypatch.setattr(tasks.settings, "whatsapp_escalation_template", "sla_breach_alert")
    monkeypatch.setattr(tasks, "send_whatsapp_template_sync", lambda **kw: sent.update(kw) or {})

    tasks._notify_manager_whatsapp(hotel, msg, "Room 101")
    assert sent["body_params"][2] == "(no text)"


def test_room_label_falls_back_when_room_unknown(db, hotel, thread):
    conv, msg = thread
    conv.room_number = None
    db.commit()
    assert tasks._room_label(db, msg) == "Guest"
