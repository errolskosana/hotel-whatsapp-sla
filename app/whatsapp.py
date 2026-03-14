import hmac
import hashlib
import json
from typing import Any
import httpx

GRAPH_URL = "https://graph.facebook.com/v21.0"


def verify_webhook_signature(app_secret: str, raw_body: bytes, x_hub_sig_256: str | None) -> bool:
    """Validate Meta webhook signature header: x-hub-signature-256 == 'sha256=<hex>'"""
    if not x_hub_sig_256 or not x_hub_sig_256.startswith("sha256="):
        return False
    their_sig = x_hub_sig_256.split("sha256=", 1)[1].strip()
    our_sig = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(our_sig, their_sig)


async def send_whatsapp_text(*, phone_number_id: str, access_token: str, to_e164_or_waid: str, text: str) -> dict[str, Any]:
    url = f"{GRAPH_URL}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_e164_or_waid,
        "type": "text",
        "text": {"body": text},
    }
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, headers=headers, content=json.dumps(payload))
        r.raise_for_status()
        return r.json()


def send_whatsapp_text_sync(*, phone_number_id: str, access_token: str, to_e164_or_waid: str, text: str) -> dict[str, Any]:
    """Synchronous version for use inside Celery tasks."""
    url = f"{GRAPH_URL}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_e164_or_waid,
        "type": "text",
        "text": {"body": text},
    }
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    with httpx.Client(timeout=15) as client:
        r = client.post(url, headers=headers, content=json.dumps(payload))
        r.raise_for_status()
        return r.json()
