"""Integration tests for inbound WhatsApp webhook handling."""
import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

import app.main as main
from app.models import Conversation, Message

SECRET = "test-secret"


def _post(client, payload: dict):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={"x-hub-signature-256": sig, "content-type": "application/json"},
    )


def _change(phone_number_id: str, *, messages=None, statuses=None) -> dict:
    value = {"metadata": {"phone_number_id": phone_number_id}}
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {"value": value, "field": "messages"}


def _text_msg(wa_id: str, msg_id: str, text: str) -> dict:
    return {"from": wa_id, "id": msg_id, "type": "text", "text": {"body": text}}


@pytest.fixture
def wa_client(client, db, monkeypatch):
    """Webhook client with the network and the embedding model stubbed out."""
    monkeypatch.setattr(main, "_open_db_for_phone_number", lambda pnid: db)

    async def _noop_read(**kwargs):
        return {}

    monkeypatch.setattr(main, "mark_message_read", _noop_read)
    # Auto-answer off by default; individual tests turn it on.
    monkeypatch.setattr(main, "should_auto_answer", lambda retrieved: False)
    monkeypatch.setattr(main, "top_k_chunks", lambda q, chunks, k=3: [])
    return client


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------

def test_all_messages_in_a_batch_are_stored(wa_client, db, hotel):
    """Meta batches several messages per delivery — none may be dropped."""
    pnid = hotel.whatsapp_phone_number_id
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {"id": "1", "changes": [_change(pnid, messages=[
                _text_msg("2799900001", "wamid.1", "towels please"),
                _text_msg("2799900002", "wamid.2", "extra pillow"),
            ])]},
            {"id": "2", "changes": [_change(pnid, messages=[
                _text_msg("2799900003", "wamid.3", "late checkout?"),
            ])]},
        ],
    }
    assert _post(wa_client, payload).status_code == 200

    bodies = {m.body for m in db.execute(select(Message)).scalars().all()}
    assert bodies == {"towels please", "extra pillow", "late checkout?"}


def test_multiple_changes_in_one_entry_are_processed(wa_client, db, hotel):
    pnid = hotel.whatsapp_phone_number_id
    payload = {"entry": [{"id": "1", "changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.a", "first")]),
        _change(pnid, messages=[_text_msg("27999", "wamid.b", "second")]),
    ]}]}
    assert _post(wa_client, payload).status_code == 200
    assert db.execute(select(Message)).scalars().all().__len__() == 2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_redelivered_message_is_not_duplicated(wa_client, db, hotel):
    pnid = hotel.whatsapp_phone_number_id
    payload = {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.dup", "hello")])
    ]}]}
    _post(wa_client, payload)
    _post(wa_client, payload)  # Meta retry

    msgs = db.execute(select(Message).where(Message.direction == "in")).scalars().all()
    assert len(msgs) == 1


# ---------------------------------------------------------------------------
# Opt-out / opt-in
# ---------------------------------------------------------------------------

def test_stop_from_unknown_guest_persists_opt_out(wa_client, db, hotel):
    """Regression: STOP used to be dropped when no conversation existed yet."""
    pnid = hotel.whatsapp_phone_number_id
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27991112222", "wamid.stop", "STOP")])
    ]}]})

    conv = db.execute(select(Conversation)).scalar_one()
    assert conv.opted_out is True
    assert conv.opted_out_at is not None


def test_opt_out_is_case_and_space_insensitive(wa_client, db, hotel):
    pnid = hotel.whatsapp_phone_number_id
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27991112222", "wamid.s", "  stop  ")])
    ]}]})
    assert db.execute(select(Conversation)).scalar_one().opted_out is True


def test_message_after_opt_out_is_stored_but_not_escalatable(wa_client, db, hotel):
    pnid = hotel.whatsapp_phone_number_id
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.s", "STOP")])
    ]}]})
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.after", "need towels")])
    ]}]})

    msg = db.execute(
        select(Message).where(Message.wa_message_id == "wamid.after")
    ).scalar_one()
    assert msg.body == "need towels"       # visible to staff, not silently dropped
    assert msg.status == "closed"          # never enters the SLA queue


def test_start_keyword_reopens_conversation(wa_client, db, hotel):
    pnid = hotel.whatsapp_phone_number_id
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.s", "STOP")])
    ]}]})
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.in", "START")])
    ]}]})

    conv = db.execute(select(Conversation)).scalar_one()
    assert conv.opted_out is False


def test_replayed_stop_does_not_undo_a_later_opt_in(wa_client, db, hotel):
    """Regression: Meta redelivers on timeout, and a replayed STOP used to
    silently re-opt-out a guest who had already opted back in."""
    pnid = hotel.whatsapp_phone_number_id
    stop = {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.stop", "STOP")])
    ]}]}

    _post(wa_client, stop)
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.start", "START")])
    ]}]})
    _post(wa_client, stop)  # Meta retries the original STOP delivery

    conv = db.execute(select(Conversation)).scalar_one()
    assert conv.opted_out is False
    assert conv.opted_out_at is None


def test_replayed_start_does_not_undo_a_later_opt_out(wa_client, db, hotel):
    """The mirror case: a replayed opt-in must not resurrect a stopped guest."""
    pnid = hotel.whatsapp_phone_number_id
    start = {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.start", "START")])
    ]}]}

    _post(wa_client, start)
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.stop", "STOP")])
    ]}]})
    _post(wa_client, start)  # replay

    assert db.execute(select(Conversation)).scalar_one().opted_out is True


def test_stop_is_recorded_so_it_can_be_deduplicated(wa_client, db, hotel):
    """The STOP row is both the audit trail and the idempotency key."""
    pnid = hotel.whatsapp_phone_number_id
    payload = {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.stop", "STOP")])
    ]}]}
    _post(wa_client, payload)
    _post(wa_client, payload)

    msgs = db.execute(
        select(Message).where(Message.wa_message_id == "wamid.stop")
    ).scalars().all()
    assert len(msgs) == 1
    assert msgs[0].status == "closed"  # a control keyword is not staff work


def test_opt_out_sends_no_read_receipt(wa_client, db, hotel, monkeypatch):
    """Honour the request to stop: no further outbound traffic to this guest."""
    called = []

    async def _record(**kwargs):
        called.append(kwargs)
        return {}

    monkeypatch.setattr(main, "mark_message_read", _record)
    monkeypatch.setattr(main.settings, "whatsapp_mark_read", True)

    pnid = hotel.whatsapp_phone_number_id
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.stop", "STOP")])
    ]}]})
    assert called == []


def test_duplicate_message_sends_no_second_read_receipt(wa_client, db, hotel, monkeypatch):
    called = []

    async def _record(**kwargs):
        called.append(kwargs)
        return {}

    monkeypatch.setattr(main, "mark_message_read", _record)
    monkeypatch.setattr(main.settings, "whatsapp_mark_read", True)

    pnid = hotel.whatsapp_phone_number_id
    payload = {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.x", "hello")])
    ]}]}
    _post(wa_client, payload)
    _post(wa_client, payload)
    assert len(called) == 1


def test_qr_start_payload_is_not_treated_as_bare_keyword(wa_client, db, hotel):
    """'START HOTEL_ID=… ROOM=…' is a QR scan, and must bind the room."""
    pnid = hotel.whatsapp_phone_number_id
    text = f"START HOTEL_ID={hotel.id} ROOM=204"
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.qr", text)])
    ]}]})

    conv = db.execute(select(Conversation)).scalar_one()
    assert conv.room_number == "204"
    assert conv.opted_out is False


# ---------------------------------------------------------------------------
# Service window
# ---------------------------------------------------------------------------

def test_inbound_message_opens_service_window(wa_client, db, hotel):
    pnid = hotel.whatsapp_phone_number_id
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.1", "hi")])
    ]}]})

    conv = db.execute(select(Conversation)).scalar_one()
    assert conv.last_inbound_at is not None
    assert main.within_service_window(conv.last_inbound_at)


# ---------------------------------------------------------------------------
# Delivery status callbacks
# ---------------------------------------------------------------------------

def test_status_callback_records_delivery_state(wa_client, db, hotel):
    pnid = hotel.whatsapp_phone_number_id
    conv = Conversation(hotel_id=hotel.id, wa_id="27999")
    db.add(conv)
    db.commit()
    db.add(Message(
        hotel_id=hotel.id, conversation_id=conv.id, direction="out",
        wa_message_id="wamid.out", body="on its way", status="sent",
    ))
    db.commit()

    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, statuses=[{"id": "wamid.out", "status": "delivered"}])
    ]}]})

    msg = db.execute(
        select(Message).where(Message.wa_message_id == "wamid.out")
    ).scalar_one()
    assert msg.wa_status == "delivered"


def test_failed_status_records_meta_error(wa_client, db, hotel):
    """Regression: failures used to be recorded with no reason attached."""
    pnid = hotel.whatsapp_phone_number_id
    conv = Conversation(hotel_id=hotel.id, wa_id="27999")
    db.add(conv)
    db.commit()
    db.add(Message(
        hotel_id=hotel.id, conversation_id=conv.id, direction="out",
        wa_message_id="wamid.bad", body="hi", status="sent",
    ))
    db.commit()

    _post(wa_client, {"entry": [{"changes": [_change(pnid, statuses=[{
        "id": "wamid.bad",
        "status": "failed",
        "errors": [{"code": 131047, "title": "Re-engagement message"}],
    }])]}]})

    msg = db.execute(
        select(Message).where(Message.wa_message_id == "wamid.bad")
    ).scalar_one()
    assert msg.wa_status == "failed"
    assert msg.wa_error_code == 131047
    assert msg.wa_error_title == "Re-engagement message"


# ---------------------------------------------------------------------------
# Auto-answer
# ---------------------------------------------------------------------------

def test_auto_answer_actions_the_inbound_message(wa_client, db, hotel, monkeypatch):
    """Regression: an AI-answered message used to still breach its SLA."""
    monkeypatch.setattr(main, "should_auto_answer", lambda retrieved: True)
    monkeypatch.setattr(main, "compose_grounded_answer", lambda q, r: "Breakfast is 06:30-10:30.")

    async def _fake_send(**kwargs):
        return {"messages": [{"id": "wamid.auto"}]}

    monkeypatch.setattr(main, "send_whatsapp_text", _fake_send)

    pnid = hotel.whatsapp_phone_number_id
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.q", "breakfast time?")])
    ]}]})

    inbound = db.execute(
        select(Message).where(Message.direction == "in")
    ).scalar_one()
    assert inbound.status == "auto_replied"
    assert inbound.actioned_at is not None

    outbound = db.execute(
        select(Message).where(Message.direction == "out")
    ).scalar_one()
    assert outbound.wa_message_id == "wamid.auto"  # so status callbacks can match


def test_failed_auto_answer_leaves_message_for_staff(wa_client, db, hotel, monkeypatch):
    monkeypatch.setattr(main, "should_auto_answer", lambda retrieved: True)
    monkeypatch.setattr(main, "compose_grounded_answer", lambda q, r: "answer")

    async def _boom(**kwargs):
        raise main.WhatsAppError("nope", http_status=400, code=131047)

    monkeypatch.setattr(main, "send_whatsapp_text", _boom)

    pnid = hotel.whatsapp_phone_number_id
    _post(wa_client, {"entry": [{"changes": [
        _change(pnid, messages=[_text_msg("27999", "wamid.q", "breakfast?")])
    ]}]})

    inbound = db.execute(select(Message).where(Message.direction == "in")).scalar_one()
    assert inbound.status == "unactioned"  # SLA clock keeps running
    assert db.execute(select(Message).where(Message.direction == "out")).scalars().all() == []


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_unknown_phone_number_id_is_acked(wa_client, hotel):
    """Never 4xx a valid-signature delivery: Meta retries it forever."""
    resp = _post(wa_client, {"entry": [{"changes": [
        _change("999999999999", messages=[_text_msg("27999", "wamid.x", "hi")])
    ]}]})
    assert resp.status_code == 200


def test_non_message_change_is_ignored(wa_client, hotel):
    resp = _post(wa_client, {"entry": [{"changes": [
        {"value": {"event": "PARTNER_ADDED"}, "field": "account_update"}
    ]}]})
    assert resp.status_code == 200


def test_one_bad_message_does_not_drop_the_batch(wa_client, db, hotel):
    pnid = hotel.whatsapp_phone_number_id
    payload = {"entry": [{"changes": [_change(pnid, messages=[
        {"id": "wamid.broken", "type": "text"},  # no 'from' → KeyError
        _text_msg("27999", "wamid.good", "still delivered"),
    ])]}]}
    assert _post(wa_client, payload).status_code == 200

    bodies = [m.body for m in db.execute(select(Message)).scalars().all()]
    assert bodies == ["still delivered"]


@pytest.mark.parametrize("message,expected", [
    ({"type": "audio", "audio": {}}, "[voice message]"),
    ({"type": "image", "image": {"caption": "leak"}}, "[image] — leak"),
    ({"type": "document", "document": {"filename": "id.pdf"}}, "[document: id.pdf]"),
    ({"type": "location", "location": {"latitude": 1.0, "longitude": 2.0}}, "[location: 1.0,2.0]"),
    ({"type": "button", "button": {"text": "Yes"}}, "Yes"),
    ({"type": "interactive", "interactive": {"button_reply": {"title": "Room service"}}}, "Room service"),
    ({"type": "sticker", "sticker": {}}, "[sticker]"),
])
def test_media_types_are_labelled(message, expected):
    assert main._describe_inbound(message) == expected
