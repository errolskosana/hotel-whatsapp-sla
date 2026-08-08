from datetime import datetime, timedelta
from celery import Celery
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Hotel, Message, Escalation, PushSubscription
from app.crypto import decrypt_str
from app.models import Conversation
from app.push import send_push
from app.whatsapp import (
    send_whatsapp_text_sync, send_whatsapp_template_sync, WhatsAppError,
)
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


def _notify_manager_whatsapp(hotel: Hotel, msg: Message, room_label: str) -> None:
    """Alert the manager on WhatsApp about an SLA breach.

    A manager who has not messaged the business number in the last 24 hours is
    outside Meta's service window, and a free-form text is rejected with error
    131047. That silently loses the alert — which is the one message in this
    system that must not be lost. So prefer the approved template whenever one
    is configured, and fall back to free-form only when it is not.
    """
    token = decrypt_str(hotel.whatsapp_access_token_enc)
    template = settings.whatsapp_escalation_template

    if template:
        # Trim before slicing: a whitespace-only body would otherwise become an
        # empty parameter, which Meta rejects outright.
        excerpt = (msg.body or "").strip()[:120] or "(no text)"
        try:
            send_whatsapp_template_sync(
                phone_number_id=hotel.whatsapp_phone_number_id,
                access_token=token,
                to_e164_or_waid=hotel.manager_wa_e164,
                template_name=template,
                body_params=[hotel.name, room_label, excerpt],
            )
            return
        except WhatsAppError as exc:
            if not exc.is_template_problem:
                raise
            # Template still in review, paused, or misconfigured. Free-form
            # still reaches a manager who is inside the 24h window, and a lost
            # alert is worse than a downgraded one.
            log.warning(
                "escalation_template_unusable",
                hotel_id=str(hotel.id),
                template=template,
                falling_back_to="free-form text",
                **exc.as_log_fields(),
            )

    text = (
        f"SLA breach at {hotel.name}: {room_label} message not actioned in "
        f"{hotel.sla_seconds}s.\n\n\"{msg.body[:200]}\""
    )
    try:
        send_whatsapp_text_sync(
            phone_number_id=hotel.whatsapp_phone_number_id,
            access_token=token,
            to_e164_or_waid=hotel.manager_wa_e164,
            text=text,
        )
    except WhatsAppError as exc:
        if exc.needs_template:
            log.error(
                "escalation_blocked_by_service_window",
                hotel_id=str(hotel.id),
                message_id=str(msg.id),
                remedy=(
                    "approve the template named in WHATSAPP_ESCALATION_TEMPLATE"
                    if template
                    else "set WHATSAPP_ESCALATION_TEMPLATE to an approved template name"
                ),
                **exc.as_log_fields(),
            )
        raise


def _room_label(db: Session, msg: Message) -> str:
    """Human-readable origin of the breaching message, for the alert body."""
    conv = db.get(Conversation, msg.conversation_id)
    if conv and conv.room_number:
        return f"Room {conv.room_number}"
    return "Guest"


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

        try:
            _notify_manager_whatsapp(hotel, msg, _room_label(db, msg))
            esc.whatsapp_notified_at = datetime.utcnow()
            db.commit()
            log.info("manager_notified_whatsapp", message_id=str(msg.id))
        except WhatsAppError as exc:
            db.rollback()
            log.error("manager_whatsapp_notify_failed", message_id=str(msg.id), **exc.as_log_fields())
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
