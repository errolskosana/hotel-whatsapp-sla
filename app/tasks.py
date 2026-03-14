from datetime import datetime, timedelta
from celery import Celery
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Hotel, Message, Escalation, PushSubscription
from app.crypto import decrypt_str
from app.push import send_push
from app.whatsapp import send_whatsapp_text_sync
from app.logger import get_logger

log = get_logger(__name__)

celery_app = Celery(
    "hotel_sla",
    broker=settings.redis_url,
    backend=settings.redis_url.replace("/0", "/1") if settings.redis_url.endswith("/0") else settings.redis_url + "_results",
)

celery_app.conf.beat_schedule = {
    "sla-scan-every-2s": {
        "task": "app.tasks.scan_sla_and_escalate",
        "schedule": 2.0,
    }
}


def _db() -> Session:
    return SessionLocal()


@celery_app.task(name="app.tasks.scan_sla_and_escalate")
def scan_sla_and_escalate():
    db = _db()
    try:
        # Fetch all hotels so we can use per-hotel SLA seconds
        hotels = db.execute(select(Hotel)).scalars().all()
        hotel_map = {h.id: h for h in hotels}

        # Use the minimum SLA across hotels as the outer cutoff, then filter per message
        min_sla = min((h.sla_seconds for h in hotels), default=20)
        cutoff = datetime.utcnow() - timedelta(seconds=min_sla)

        stmt = (
            select(Message)
            .where(Message.status == "unactioned")
            .where(Message.direction == "in")
            .where(Message.received_at <= cutoff)
            .where(Message.escalated_at.is_(None))
            .limit(50)
        )
        msgs = db.execute(stmt).scalars().all()

        for msg in msgs:
            hotel = hotel_map.get(msg.hotel_id)
            if not hotel:
                continue

            # Check per-hotel SLA
            hotel_cutoff = datetime.utcnow() - timedelta(seconds=hotel.sla_seconds)
            if msg.received_at > hotel_cutoff:
                continue

            # Create escalation record (idempotent via unique message_id)
            try:
                esc = Escalation(hotel_id=hotel.id, message_id=msg.id)
                db.add(esc)
                msg.escalated_at = datetime.utcnow()
                db.commit()
            except Exception as exc:
                db.rollback()
                log.warning("escalation_insert_failed", message_id=str(msg.id), error=str(exc))
                continue

            token = decrypt_str(hotel.whatsapp_access_token_enc)

            # WhatsApp notify manager (sync client — no asyncio.run())
            try:
                text = f"SLA breach: guest message not actioned in {hotel.sla_seconds}s. message_id={msg.id}"
                send_whatsapp_text_sync(
                    phone_number_id=hotel.whatsapp_phone_number_id,
                    access_token=token,
                    to_e164_or_waid=hotel.manager_wa_e164,
                    text=text,
                )
                esc.whatsapp_notified_at = datetime.utcnow()
                db.commit()
                log.info("manager_notified_whatsapp", message_id=str(msg.id))
            except Exception as exc:
                db.rollback()
                log.error("manager_whatsapp_notify_failed", message_id=str(msg.id), error=str(exc))

            # Web Push notify (all subscriptions for hotel)
            subs = db.execute(
                select(PushSubscription).where(PushSubscription.hotel_id == hotel.id)
            ).scalars().all()
            for s in subs:
                try:
                    send_push(
                        {
                            "endpoint": s.endpoint,
                            "keys": {"p256dh": s.p256dh, "auth": s.auth},
                        },
                        title="SLA Escalation",
                        body=f"Guest request not actioned within {hotel.sla_seconds}s.",
                    )
                    esc.push_notified_at = datetime.utcnow()
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    log.error("push_notify_failed", subscription_id=str(s.id), error=str(exc))

    finally:
        db.close()
