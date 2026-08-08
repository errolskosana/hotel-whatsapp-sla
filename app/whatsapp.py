"""WhatsApp Cloud API client.

Owns every outbound call to Meta plus inbound webhook signature checking.

Both an async variant (request path) and a sync variant (Celery workers) are
provided for each operation. They share the payload builders and the retry
policy so the two paths cannot drift apart.
"""
import asyncio
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

import httpx

from app.config import settings
from app.logger import get_logger

log = get_logger(__name__)

GRAPH_URL = f"https://graph.facebook.com/{settings.whatsapp_api_version}"

# Meta rejects a text body longer than 4096 characters outright.
TEXT_BODY_LIMIT = 4096

# A business may send free-form messages only within 24h of the user's last
# inbound message. Outside it, only approved templates are delivered.
SERVICE_WINDOW = timedelta(hours=24)

# Meta error codes worth another attempt: transient throttling / capacity.
RETRYABLE_META_CODES = {
    1,       # unknown API error
    2,       # temporary service problem
    4,       # application request limit reached
    80007,   # rate limit hit
    130429,  # cloud API message throughput reached
    131048,  # spam rate limit hit
    133016,  # temporary account restriction
}
RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}

# Guest is outside the 24h service window — a template is required instead.
ERROR_REENGAGEMENT = 131047

# Template-specific failures: not approved, paused, disabled, bad parameters.
TEMPLATE_ERROR_RANGE = (132000, 132999)


class WhatsAppError(Exception):
    """A Cloud API call that failed, with Meta's diagnostics preserved.

    ``raise_for_status`` throws away the response body, which is where Meta puts
    the only thing that explains the failure. This keeps it.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        code: int | None = None,
        subcode: int | None = None,
        details: str | None = None,
        fbtrace_id: str | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code
        self.subcode = subcode
        self.details = details
        self.fbtrace_id = fbtrace_id
        self.retryable = retryable

    @property
    def needs_template(self) -> bool:
        """True when the send failed only because the 24h window has closed."""
        return self.code == ERROR_REENGAGEMENT

    @property
    def is_template_problem(self) -> bool:
        """True when the template itself is the problem, not the recipient.

        Covers the 132xxx family: not approved yet, paused, disabled, wrong
        parameter count or format. The message may still be deliverable as
        free-form text if the 24h window happens to be open.
        """
        return self.code is not None and TEMPLATE_ERROR_RANGE[0] <= self.code <= TEMPLATE_ERROR_RANGE[1]

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "wa_error": self.message,
            "http_status": self.http_status,
            "meta_code": self.code,
            "meta_subcode": self.subcode,
            "fbtrace_id": self.fbtrace_id,
        }

    def __str__(self) -> str:
        return f"WhatsApp API error (http={self.http_status} code={self.code}): {self.message}"


# ---------------------------------------------------------------------------
# Inbound: webhook signature
# ---------------------------------------------------------------------------

def verify_webhook_signature(app_secret: str, raw_body: bytes, x_hub_sig_256: str | None) -> bool:
    """Validate Meta webhook signature header: x-hub-signature-256 == 'sha256=<hex>'"""
    if not x_hub_sig_256 or not x_hub_sig_256.startswith("sha256="):
        return False
    their_sig = x_hub_sig_256.split("sha256=", 1)[1].strip()
    our_sig = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(our_sig, their_sig)


# ---------------------------------------------------------------------------
# Service window
# ---------------------------------------------------------------------------

def within_service_window(last_inbound_at: datetime | None, *, now: datetime | None = None) -> bool:
    """True when a free-form (non-template) message may still be delivered.

    ``last_inbound_at`` is when the guest last messaged us. Meta closes the
    window 24h after that, after which only approved templates go through.
    """
    if last_inbound_at is None:
        return False
    now = now or datetime.utcnow()
    return (now - last_inbound_at) < SERVICE_WINDOW


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------

def split_text(text: str, limit: int = TEXT_BODY_LIMIT) -> list[str]:
    """Split a body into chunks Meta will accept, preferring line/word breaks."""
    text = text or ""
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("\n"), window.rfind(" "))
        # Only honour a boundary that isn't wastefully early; else hard-cut.
        if cut < int(limit * 0.6):
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def _text_payload(to: str, text: str, *, preview_url: bool, reply_to: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": text, "preview_url": preview_url},
    }
    if reply_to:
        payload["context"] = {"message_id": reply_to}
    return payload


def sanitize_template_param(value: str, *, fallback: str = "-") -> str:
    """Make a value safe to use as a template variable.

    Meta rejects parameters containing newlines, tab characters, or four or
    more consecutive spaces, and rejects empty ones. Guest message text goes
    into escalation templates verbatim, so this is not a theoretical concern:
    one guest pressing enter would otherwise fail the whole alert.
    """
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    return cleaned or fallback


def _template_payload(
    to: str,
    template_name: str,
    language: str,
    body_params: Iterable[str] | None,
) -> dict[str, Any]:
    template: dict[str, Any] = {
        "name": template_name,
        "language": {"code": language},
    }
    params = [sanitize_template_param(p) for p in (body_params or [])]
    if params:
        template["components"] = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params],
            }
        ]
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": template,
    }


def _read_receipt_payload(wa_message_id: str) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": wa_message_id,
    }


def _headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}


def _messages_url(phone_number_id: str) -> str:
    return f"{GRAPH_URL}/{phone_number_id}/messages"


def _merge_sends(responses: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold multi-chunk sends into one response, ids in send order."""
    if len(responses) == 1:
        return responses[0]
    merged: dict[str, Any] = dict(responses[0])
    merged["messages"] = [m for r in responses for m in (r.get("messages") or [])]
    return merged


# ---------------------------------------------------------------------------
# Error translation + retry
# ---------------------------------------------------------------------------

def _error_from_response(response: httpx.Response) -> WhatsAppError:
    code = subcode = None
    details = fbtrace_id = None
    message = f"HTTP {response.status_code}"
    try:
        err = (response.json() or {}).get("error") or {}
        message = err.get("message") or message
        code = err.get("code")
        subcode = err.get("error_subcode")
        fbtrace_id = err.get("fbtrace_id")
        details = (err.get("error_data") or {}).get("details")
    except (ValueError, AttributeError):
        message = f"{message}: {response.text[:200]}"

    retryable = response.status_code in RETRYABLE_HTTP_STATUS or code in RETRYABLE_META_CODES
    return WhatsAppError(
        message,
        http_status=response.status_code,
        code=code,
        subcode=subcode,
        details=details,
        fbtrace_id=fbtrace_id,
        retryable=retryable,
    )


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff: 0.5s, 1s, 2s, capped at 8s."""
    return min(0.5 * (2 ** attempt), 8.0)


@dataclass
class _Attempt:
    """Outcome of one HTTP attempt: either a body or an error to maybe retry."""
    body: dict[str, Any] | None = None
    error: WhatsAppError | None = None


def _interpret(response: httpx.Response) -> _Attempt:
    if response.status_code >= 400:
        return _Attempt(error=_error_from_response(response))
    try:
        return _Attempt(body=response.json())
    except ValueError:
        return _Attempt(body={})


_MAX_ATTEMPTS = max(1, settings.whatsapp_max_retries + 1)


async def _post_async(url: str, access_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    last: WhatsAppError | None = None
    client = await get_async_client()
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = await client.post(
                url, headers=_headers(access_token), content=json.dumps(payload)
            )
            result = _interpret(response)
            if result.error is None:
                return result.body or {}
            last = result.error
        except httpx.HTTPError as exc:
            last = WhatsAppError(f"transport error: {exc}", retryable=True)

        if not last.retryable or attempt == _MAX_ATTEMPTS - 1:
            break
        delay = _backoff_seconds(attempt)
        log.warning("whatsapp_send_retry", attempt=attempt + 1, delay=delay, **last.as_log_fields())
        await asyncio.sleep(delay)

    raise last  # type: ignore[misc]  # unreachable with last=None


def _post_sync(url: str, access_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    last: WhatsAppError | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            with httpx.Client(timeout=settings.whatsapp_timeout_seconds) as client:
                response = client.post(
                    url, headers=_headers(access_token), content=json.dumps(payload)
                )
            result = _interpret(response)
            if result.error is None:
                return result.body or {}
            last = result.error
        except httpx.HTTPError as exc:
            last = WhatsAppError(f"transport error: {exc}", retryable=True)

        if not last.retryable or attempt == _MAX_ATTEMPTS - 1:
            break
        delay = _backoff_seconds(attempt)
        log.warning("whatsapp_send_retry", attempt=attempt + 1, delay=delay, **last.as_log_fields())
        time.sleep(delay)

    raise last  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Shared async client (avoids a TLS handshake per message)
# ---------------------------------------------------------------------------

_async_client: httpx.AsyncClient | None = None


async def get_async_client() -> httpx.AsyncClient:
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = httpx.AsyncClient(
            timeout=settings.whatsapp_timeout_seconds,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _async_client


async def close_async_client() -> None:
    global _async_client
    if _async_client is not None and not _async_client.is_closed:
        await _async_client.aclose()
    _async_client = None


# ---------------------------------------------------------------------------
# Outbound operations
# ---------------------------------------------------------------------------

async def send_whatsapp_text(
    *,
    phone_number_id: str,
    access_token: str,
    to_e164_or_waid: str,
    text: str,
    preview_url: bool = False,
    reply_to_wa_message_id: str | None = None,
) -> dict[str, Any]:
    """Send a free-form text. Only valid inside the 24h service window.

    Bodies over 4096 chars are split; the returned ``messages`` list holds one
    id per chunk, in send order.
    """
    url = _messages_url(phone_number_id)
    chunks = split_text(text)
    responses = []
    for i, chunk in enumerate(chunks):
        payload = _text_payload(
            to_e164_or_waid,
            chunk,
            preview_url=preview_url,
            # Thread only the first chunk to the message being answered.
            reply_to=reply_to_wa_message_id if i == 0 else None,
        )
        responses.append(await _post_async(url, access_token, payload))
    return _merge_sends(responses)


def send_whatsapp_text_sync(
    *,
    phone_number_id: str,
    access_token: str,
    to_e164_or_waid: str,
    text: str,
    preview_url: bool = False,
) -> dict[str, Any]:
    """Synchronous free-form text send, for Celery workers."""
    url = _messages_url(phone_number_id)
    responses = [
        _post_sync(url, access_token, _text_payload(to_e164_or_waid, chunk, preview_url=preview_url, reply_to=None))
        for chunk in split_text(text)
    ]
    return _merge_sends(responses)


async def send_whatsapp_template(
    *,
    phone_number_id: str,
    access_token: str,
    to_e164_or_waid: str,
    template_name: str,
    language: str | None = None,
    body_params: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Send an approved template — the only thing deliverable outside 24h."""
    payload = _template_payload(
        to_e164_or_waid,
        template_name,
        language or settings.whatsapp_template_language,
        body_params,
    )
    return await _post_async(_messages_url(phone_number_id), access_token, payload)


def send_whatsapp_template_sync(
    *,
    phone_number_id: str,
    access_token: str,
    to_e164_or_waid: str,
    template_name: str,
    language: str | None = None,
    body_params: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Synchronous template send, for Celery workers."""
    payload = _template_payload(
        to_e164_or_waid,
        template_name,
        language or settings.whatsapp_template_language,
        body_params,
    )
    return _post_sync(_messages_url(phone_number_id), access_token, payload)


async def mark_message_read(
    *, phone_number_id: str, access_token: str, wa_message_id: str
) -> dict[str, Any]:
    """Send a read receipt so the guest sees blue ticks."""
    return await _post_async(
        _messages_url(phone_number_id), access_token, _read_receipt_payload(wa_message_id)
    )


def first_message_id(response: dict[str, Any] | None) -> str | None:
    """Pull the wa_message_id out of a send response, tolerating odd shapes."""
    if not response:
        return None
    messages = response.get("messages") or []
    if not messages:
        return None
    return messages[0].get("id")
