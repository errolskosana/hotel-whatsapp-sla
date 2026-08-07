"""Unit tests for the WhatsApp Cloud API client."""
import httpx
import pytest
from datetime import datetime, timedelta

from app import whatsapp as wa


# ---------------------------------------------------------------------------
# Service window
# ---------------------------------------------------------------------------

def test_service_window_open_just_inside_24h():
    assert wa.within_service_window(datetime.utcnow() - timedelta(hours=23, minutes=59))


def test_service_window_closed_after_24h():
    assert not wa.within_service_window(datetime.utcnow() - timedelta(hours=24, seconds=1))


def test_service_window_closed_when_guest_never_messaged():
    assert not wa.within_service_window(None)


# ---------------------------------------------------------------------------
# Text splitting
# ---------------------------------------------------------------------------

def test_short_text_is_one_chunk():
    assert wa.split_text("hello") == ["hello"]


def test_long_text_split_within_limit():
    parts = wa.split_text("word " * 2000)
    assert len(parts) > 1
    assert all(len(p) <= wa.TEXT_BODY_LIMIT for p in parts)


def test_split_prefers_word_boundary():
    text = "a" * 4000 + " " + "b" * 500
    parts = wa.split_text(text)
    assert parts[0] == "a" * 4000
    assert parts[1] == "b" * 500


def test_split_hard_cuts_when_no_boundary():
    text = "x" * 5000
    parts = wa.split_text(text)
    assert len(parts[0]) == wa.TEXT_BODY_LIMIT
    assert "".join(parts) == text


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

def _response(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=body if body is not None else {},
        request=httpx.Request("POST", "https://graph.facebook.com/v25.0/1/messages"),
    )


def test_error_preserves_meta_diagnostics():
    err = wa._error_from_response(_response(400, {
        "error": {
            "message": "Re-engagement message",
            "code": 131047,
            "error_subcode": 2018278,
            "fbtrace_id": "AbC123",
            "error_data": {"details": "Message failed to send"},
        }
    }))
    assert err.code == 131047
    assert err.subcode == 2018278
    assert err.fbtrace_id == "AbC123"
    assert err.needs_template
    assert not err.retryable


def test_rate_limit_is_retryable():
    assert wa._error_from_response(_response(429, {"error": {"code": 130429}})).retryable


def test_server_error_is_retryable():
    assert wa._error_from_response(_response(503)).retryable


def test_bad_token_is_not_retryable():
    err = wa._error_from_response(_response(401, {"error": {"code": 190, "message": "expired"}}))
    assert not err.retryable
    assert not err.needs_template


def test_non_json_error_body_still_yields_error():
    response = httpx.Response(
        status_code=500,
        text="<html>gateway blew up</html>",
        request=httpx.Request("POST", "https://graph.facebook.com/v25.0/1/messages"),
    )
    err = wa._error_from_response(response)
    assert err.http_status == 500
    assert err.retryable


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------

def test_sync_send_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return _response(429, {"error": {"code": 130429}})
            return _response(200, {"messages": [{"id": "wamid.OK"}]})

    monkeypatch.setattr(wa.httpx, "Client", _Client)
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)

    result = wa.send_whatsapp_text_sync(
        phone_number_id="1", access_token="t", to_e164_or_waid="+1555", text="hi"
    )
    assert calls["n"] == 2
    assert wa.first_message_id(result) == "wamid.OK"


def test_sync_send_does_not_retry_permanent_error(monkeypatch):
    calls = {"n": 0}

    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **kw):
            calls["n"] += 1
            return _response(400, {"error": {"code": 131047, "message": "out of window"}})

    monkeypatch.setattr(wa.httpx, "Client", _Client)
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)

    with pytest.raises(wa.WhatsAppError) as exc:
        wa.send_whatsapp_text_sync(
            phone_number_id="1", access_token="t", to_e164_or_waid="+1555", text="hi"
        )
    assert calls["n"] == 1  # one attempt, no pointless retries
    assert exc.value.needs_template


def test_sync_send_gives_up_after_max_retries(monkeypatch):
    calls = {"n": 0}

    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **kw):
            calls["n"] += 1
            return _response(503)

    monkeypatch.setattr(wa.httpx, "Client", _Client)
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)

    with pytest.raises(wa.WhatsAppError):
        wa.send_whatsapp_text_sync(
            phone_number_id="1", access_token="t", to_e164_or_waid="+1555", text="hi"
        )
    assert calls["n"] == wa._MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------

def test_text_payload_threads_reply_context():
    payload = wa._text_payload("+1555", "hi", preview_url=False, reply_to="wamid.ABC")
    assert payload["context"] == {"message_id": "wamid.ABC"}
    assert payload["messaging_product"] == "whatsapp"
    assert payload["type"] == "text"


def test_template_payload_carries_body_params():
    payload = wa._template_payload("+1555", "sla_alert", "en", ["Hotel", "Room 12", "towels"])
    params = payload["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["Hotel", "Room 12", "towels"]
    assert payload["template"]["language"] == {"code": "en"}


@pytest.mark.parametrize("raw,expected", [
    ("shower\nis broken", "shower is broken"),
    ("tab\tseparated", "tab separated"),
    ("four    spaces", "four spaces"),
    ("  padded  ", "padded"),
    ("", "-"),
    ("   \n\t ", "-"),
])
def test_sanitize_template_param(raw, expected):
    assert wa.sanitize_template_param(raw) == expected


def test_template_payload_sanitises_params():
    payload = wa._template_payload("+1555", "sla_alert", "en", ["Hotel", "Room 12", "a\nb    c"])
    texts = [p["text"] for p in payload["template"]["components"][0]["parameters"]]
    assert texts == ["Hotel", "Room 12", "a b c"]


def test_template_error_codes_are_recognised():
    for code in (132000, 132001, 132015, 132016):
        assert wa.WhatsAppError("x", code=code).is_template_problem
    for code in (131047, 190, 130429):
        assert not wa.WhatsAppError("x", code=code).is_template_problem
    assert not wa.WhatsAppError("x").is_template_problem


def test_template_payload_omits_components_when_no_params():
    payload = wa._template_payload("+1555", "sla_alert", "en", None)
    assert "components" not in payload["template"]


def test_read_receipt_payload():
    assert wa._read_receipt_payload("wamid.X") == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.X",
    }


def test_first_message_id_tolerates_empty_response():
    assert wa.first_message_id(None) is None
    assert wa.first_message_id({}) is None
    assert wa.first_message_id({"messages": []}) is None
