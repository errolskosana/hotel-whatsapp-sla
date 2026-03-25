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

def _celery_backend_url(url: str) -> str:
    """Use Redis DB 1 for Celery results (separate from broker on DB 0)."""
    import re
    return re.sub(r"/\d+$", "/1", url) if re.search(r"/\d+$", url) else url + "/1"

celery_app = Celery(
    "hotel_sla",
    broker=settings.redis_url,
    backend=_celery_backend_url(settings.redis_url),
)

celery_app.conf.beat_schedule = {
    "sla-scan-every-2s": {
        "task": "app.tasks.scan_sla_and_escalate",
        "schedule": 2.0,
    }
}


def _db() -> Session:
    return SessionLocal()


def _scan_db(db: Session) -> None:
    """Run the SLA scan on a single DB session (one hotel namespace)."""
    hotels = db.execute(select(Hotel)).scalars().all()
    if not hotels:
        return
    hotel_map = {h.id: h for h in hotels}

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

        hotel_cutoff = datetime.utcnow() - timedelta(seconds=hotel.sla_seconds)
        if msg.received_at > hotel_cutoff:
            continue

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

        subs = db.execute(
            select(PushSubscription).where(PushSubscription.hotel_id == hotel.id)
        ).scalars().all()
        push_succeeded = False
        for s in subs:
            try:
                send_push(
                    {"endpoint": s.endpoint, "keys": {"p256dh": s.p256dh, "auth": s.auth}},
                    title="SLA Escalation",
                    body=f"Guest request not actioned within {hotel.sla_seconds}s.",
                )
                push_succeeded = True
            except Exception as exc:
                log.error("push_notify_failed", subscription_id=str(s.id), error=str(exc))
        if push_succeeded:
            try:
                esc.push_notified_at = datetime.utcnow()
                db.commit()
            except Exception as exc:
                db.rollback()
                log.error("push_notified_at_save_failed", message_id=str(msg.id), error=str(exc))


@celery_app.task(name="app.tasks.scan_sla_and_escalate")
def scan_sla_and_escalate():
    if settings.control_plane_db_url:
        # Multi-tenant: scan every active tenant's DB
        from app.control_plane import get_cp_session_direct, TenantHotel
        from app.tenant_db import get_session_for_tenant
        cp = get_cp_session_direct()
        if cp is None:
            return
        try:
            tenants = cp.execute(
                select(TenantHotel).where(TenantHotel.is_active == True)
            ).scalars().all()
        finally:
            cp.close()

        for tenant in tenants:
            tenant_db = None
            try:
                tenant_db = get_session_for_tenant(str(tenant.id))
                _scan_db(tenant_db)
            except Exception as exc:
                log.error("sla_scan_tenant_failed", tenant_id=str(tenant.id), error=str(exc))
            finally:
                if tenant_db:
                    tenant_db.close()
    else:
        # Single-tenant: scan the main DB
        db = _db()
        try:
            _scan_db(db)
        finally:
            db.close()
