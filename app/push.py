import json
from pywebpush import webpush
from app.config import settings


def send_push(subscription: dict, title: str, body: str) -> None:
    payload = json.dumps({"title": title, "body": body})
    webpush(
        subscription_info=subscription,
        data=payload,
        vapid_private_key=settings.vapid_private_key,
        vapid_claims={"sub": settings.vapid_subject},
    )
