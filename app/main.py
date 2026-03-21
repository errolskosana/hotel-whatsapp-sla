import asyncio
import json
import uuid
from datetime import datetime, date, timedelta
from typing import AsyncGenerator

from fastapi import (
    FastAPI, Request, Depends, HTTPException, UploadFile, File, Form,
    Response,
)
from fastapi.responses import (
    PlainTextResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import settings
from app.db import db_session
from app.models import (
    Base, Hotel, Room, Conversation, Message, GuestStay,
    PushSubscription, StaffUser, Escalation, KnowledgeChunk,
)
from app.auth import (
    get_current_user, require_manager, CurrentUser,
    authenticate_user, create_access_token, hash_password,
)
from app.seed import seed_demo
from app.crypto import decrypt_str, encrypt_str
from app.whatsapp import verify_webhook_signature, send_whatsapp_text
from app.ai import top_k_chunks, should_auto_answer, compose_grounded_answer
from app.csv_import import import_guest_stays_csv
from app.logger import configure_logging, get_logger

configure_logging()
log = get_logger(__name__)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Fallback knowledge when hotel has no DB chunks
_DEFAULT_KNOWLEDGE = [
    "Breakfast is served from 06:30 to 10:30 daily in the main restaurant.",
    "Wi-Fi: Network 'HotelGuest' password is on your keycard sleeve.",
    "Pool hours are 08:00 to 20:00. Towels available at reception.",
    "Check-out is at 11:00. Late check-out is subject to availability; please ask reception.",
]


@app.on_event("startup")
def _startup():
    db = next(db_session())
    try:
        seed_demo(db)
    finally:
        db.close()


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "OK"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    # If already logged in redirect to inbox
    if request.cookies.get("access_token"):
        return RedirectResponse("/dashboard/inbox", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    hotel_id: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(db_session),
):
    user = authenticate_user(db, hotel_id, email, password)
    if not user:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid hotel ID, email, or password."},
            status_code=401,
        )
    token = create_access_token(str(user.id), str(user.hotel_id), user.role)
    response = RedirectResponse("/dashboard/inbox", status_code=302)
    response.set_cookie(
        "access_token",
        token,
        httponly=True,
        samesite="strict",
        secure=settings.app_env != "dev",
        max_age=settings.jwt_expiry_hours * 3600,
    )
    log.info("login_success", user_id=str(user.id), role=user.role)
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("access_token", httponly=True, samesite="strict", secure=settings.app_env != "dev")
    return response


# ---------------------------------------------------------------------------
# WhatsApp Webhook
# ---------------------------------------------------------------------------

@app.get("/webhooks/whatsapp", response_class=PlainTextResponse)
def whatsapp_verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == settings.whatsapp_verify_token and challenge:
        return challenge
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhooks/whatsapp", response_class=PlainTextResponse)
@limiter.limit("60/minute")
async def whatsapp_webhook(request: Request, db: Session = Depends(db_session)):
    raw = await request.body()
    sig = request.headers.get("x-hub-signature-256")

    if not verify_webhook_signature(settings.meta_app_secret, raw, sig):
        raise HTTPException(status_code=403, detail="Bad signature")

    payload = json.loads(raw.decode("utf-8"))

    # Extract routing info
    try:
        entry = payload["entry"][0]
        value = entry["changes"][0]["value"]
        phone_number_id = value["metadata"]["phone_number_id"]
    except Exception as exc:
        log.warning("webhook_parse_error", error=str(exc), payload=str(payload)[:200])
        return "OK"

    hotel = db.execute(
        select(Hotel).where(Hotel.whatsapp_phone_number_id == phone_number_id)
    ).scalar_one_or_none()
    if not hotel:
        log.warning("unknown_phone_number_id", phone_number_id=phone_number_id)
        return "OK"  # always ack to Meta; 4xx causes retries

    # Handle delivery status updates
    statuses = value.get("statuses") or []
    for s in statuses:
        wa_msg_id = s.get("id")
        new_status = s.get("status")  # sent/delivered/read/failed
        if wa_msg_id and new_status:
            msg = db.execute(
                select(Message).where(
                    Message.hotel_id == hotel.id,
                    Message.wa_message_id == wa_msg_id,
                )
            ).scalar_one_or_none()
            if msg:
                msg.wa_status = new_status
                db.commit()

    # Handle inbound messages
    messages = value.get("messages") or []
    for m in messages:
        wa_id = m["from"]
        msg_type = m.get("type", "unknown")

        # Opt-out handling
        if msg_type == "text":
            body_text = m.get("text", {}).get("body", "")
            if body_text.strip().upper() in ("STOP", "UNSUBSCRIBE", "OPT OUT", "OPTOUT"):
                conv = db.execute(
                    select(Conversation).where(
                        Conversation.hotel_id == hotel.id,
                        Conversation.wa_id == wa_id,
                    )
                ).scalar_one_or_none()
                if conv:
                    conv.opted_out = True
                    db.commit()
                log.info("guest_opted_out", hotel_id=str(hotel.id), wa_id=wa_id)
                continue
            body = body_text
        elif msg_type == "image":
            caption = m.get("image", {}).get("caption", "")
            body = f"[image]{(' — ' + caption) if caption else ''}"
        elif msg_type == "audio":
            body = "[voice message]"
        elif msg_type == "video":
            caption = m.get("video", {}).get("caption", "")
            body = f"[video]{(' — ' + caption) if caption else ''}"
        elif msg_type == "document":
            filename = m.get("document", {}).get("filename", "")
            body = f"[document{': ' + filename if filename else ''}]"
        elif msg_type == "location":
            loc = m.get("location", {})
            body = f"[location: {loc.get('latitude')},{loc.get('longitude')}]"
        else:
            body = f"[{msg_type} message]"

        # Idempotency: skip if we already processed this wa_message_id
        wa_msg_id = m.get("id")
        if wa_msg_id:
            existing = db.execute(
                select(Message).where(
                    Message.hotel_id == hotel.id,
                    Message.wa_message_id == wa_msg_id,
                )
            ).scalar_one_or_none()
            if existing:
                continue

        conv = db.execute(
            select(Conversation).where(
                Conversation.hotel_id == hotel.id, Conversation.wa_id == wa_id
            )
        ).scalar_one_or_none()
        if not conv:
            conv = Conversation(
                hotel_id=hotel.id, wa_id=wa_id, last_message_at=datetime.utcnow()
            )
            db.add(conv)
            db.commit()

        # Skip opted-out guests
        if conv.opted_out:
            log.info("skipped_opted_out_guest", wa_id=wa_id)
            continue

        msg = Message(
            hotel_id=hotel.id,
            conversation_id=conv.id,
            direction="in",
            wa_message_id=wa_msg_id,
            body=body,
            received_at=datetime.utcnow(),
            status="unactioned",
        )
        db.add(msg)
        conv.last_message_at = datetime.utcnow()
        db.commit()

        # Bind room/stay from QR START message
        if isinstance(body, str) and body.startswith("START") and "ROOM=" in body and "HOTEL_ID=" in body:
            try:
                room = body.split("ROOM=", 1)[1].split()[0].strip()
                conv.room_number = room
                today = date.today()
                stay = db.execute(
                    select(GuestStay)
                    .where(GuestStay.hotel_id == hotel.id)
                    .where(GuestStay.room_number == room)
                    .where(GuestStay.arrival_date <= today)
                    .where(GuestStay.departure_date >= today)
                    .order_by(GuestStay.arrival_date.desc())
                ).scalars().first()
                conv.stay_id = stay.id if stay else None
                db.commit()
            except Exception as exc:
                db.rollback()
                log.warning("room_bind_failed", wa_id=wa_id, error=str(exc))

        # Guardrailed AI — use DB knowledge chunks, fall back to defaults
        db_chunks = db.execute(
            select(KnowledgeChunk).where(
                KnowledgeChunk.hotel_id == hotel.id,
                KnowledgeChunk.is_active == True,
            )
        ).scalars().all()
        knowledge_chunks = [c.content for c in db_chunks] if db_chunks else _DEFAULT_KNOWLEDGE
        retrieved = top_k_chunks(body, knowledge_chunks, k=3)
        if should_auto_answer(retrieved):
            answer = compose_grounded_answer(body, retrieved)
            try:
                token = decrypt_str(hotel.whatsapp_access_token_enc)
                await send_whatsapp_text(
                    phone_number_id=hotel.whatsapp_phone_number_id,
                    access_token=token,
                    to_e164_or_waid=wa_id,
                    text=answer,
                )
                # Record outbound AI reply
                out_msg = Message(
                    hotel_id=hotel.id,
                    conversation_id=conv.id,
                    direction="out",
                    body=answer,
                    received_at=datetime.utcnow(),
                    status="auto_replied",
                )
                db.add(out_msg)
                db.commit()
            except Exception as exc:
                log.error("ai_reply_failed", wa_id=wa_id, error=str(exc))

    return "OK"


# ---------------------------------------------------------------------------
# SSE — real-time inbox updates
# ---------------------------------------------------------------------------

async def _inbox_event_stream(hotel_id: str) -> AsyncGenerator[str, None]:
    """Poll DB every 3 seconds and push new unactioned message counts."""
    last_count = -1
    while True:
        await asyncio.sleep(3)
        try:
            db = next(db_session())
            try:
                count = db.execute(
                    select(func.count(Message.id)).where(
                        Message.hotel_id == uuid.UUID(hotel_id),
                        Message.status == "unactioned",
                        Message.direction == "in",
                    )
                ).scalar() or 0
            finally:
                db.close()
            if count != last_count:
                last_count = count
                yield f"data: {json.dumps({'unactioned': count})}\n\n"
        except Exception:
            yield "data: {}\n\n"


@app.get("/api/sse/inbox")
async def inbox_sse(user: CurrentUser = Depends(get_current_user)):
    return StreamingResponse(
        _inbox_event_stream(user.hotel_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Dashboard — Inbox
# ---------------------------------------------------------------------------

@app.get("/dashboard/inbox", response_class=HTMLResponse)
def inbox(
    request: Request,
    status_filter: str = "all",
    room_filter: str = "",
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(db_session),
):
    hotel_id = uuid.UUID(user.hotel_id)

    # Load conversations with latest message for this hotel
    conv_stmt = (
        select(Conversation)
        .where(Conversation.hotel_id == hotel_id)
        .order_by(Conversation.last_message_at.desc())
        .limit(50)
    )
    conversations = db.execute(conv_stmt).scalars().all()

    conv_data = []
    for conv in conversations:
        # Load messages for this conversation
        msg_stmt = (
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.received_at.asc())
        )
        msgs = db.execute(msg_stmt).scalars().all()

        # Apply status filter
        if status_filter != "all":
            # Show conversation if any message matches the filter
            if not any(m.status == status_filter for m in msgs if m.direction == "in"):
                continue

        # Apply room filter
        if room_filter and conv.room_number != room_filter:
            continue

        # Load guest stay
        guest = None
        if conv.stay_id:
            guest = db.get(GuestStay, conv.stay_id)

        # Escalation info
        escalated_ids = {
            str(m.id) for m in msgs if m.escalated_at is not None
        }

        conv_data.append({
            "conv": conv,
            "msgs": msgs,
            "guest": guest,
            "escalated_ids": escalated_ids,
        })

    # Rooms for filter dropdown
    rooms = db.execute(
        select(Room).where(Room.hotel_id == hotel_id, Room.status == "active")
        .order_by(Room.room_number)
    ).scalars().all()

    unactioned_count = db.execute(
        select(func.count(Message.id)).where(
            Message.hotel_id == hotel_id,
            Message.status == "unactioned",
            Message.direction == "in",
        )
    ).scalar() or 0

    return templates.TemplateResponse("inbox.html", {
        "request": request,
        "user": user,
        "conv_data": conv_data,
        "rooms": rooms,
        "status_filter": status_filter,
        "room_filter": room_filter,
        "unactioned_count": unactioned_count,
    })


# ---------------------------------------------------------------------------
# Dashboard — Push
# ---------------------------------------------------------------------------

@app.get("/dashboard/push", response_class=HTMLResponse)
def push_page(request: Request, user: CurrentUser = Depends(get_current_user)):
    return templates.TemplateResponse("push.html", {"request": request, "user": user})


# ---------------------------------------------------------------------------
# Dashboard — Knowledge Base
# ---------------------------------------------------------------------------

@app.get("/dashboard/knowledge", response_class=HTMLResponse)
def knowledge_page(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(db_session),
):
    hotel_id = uuid.UUID(user.hotel_id)
    chunks = db.execute(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.hotel_id == hotel_id)
        .order_by(KnowledgeChunk.created_at.desc())
    ).scalars().all()
    return templates.TemplateResponse("knowledge.html", {
        "request": request,
        "user": user,
        "chunks": chunks,
    })


@app.post("/dashboard/knowledge", response_class=HTMLResponse)
def knowledge_add(
    request: Request,
    content: str = Form(...),
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(db_session),
):
    if content.strip():
        db.add(KnowledgeChunk(
            hotel_id=uuid.UUID(user.hotel_id),
            content=content.strip(),
        ))
        db.commit()
    return RedirectResponse("/dashboard/knowledge", status_code=302)


@app.post("/dashboard/knowledge/{chunk_id}/delete")
def knowledge_delete(
    chunk_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(db_session),
):
    chunk = db.get(KnowledgeChunk, uuid.UUID(chunk_id))
    if not chunk or str(chunk.hotel_id) != user.hotel_id:
        raise HTTPException(status_code=404)
    db.delete(chunk)
    db.commit()
    return RedirectResponse("/dashboard/knowledge", status_code=302)


# ---------------------------------------------------------------------------
# Dashboard — Rooms
# ---------------------------------------------------------------------------

@app.get("/dashboard/rooms", response_class=HTMLResponse)
def rooms_page(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(db_session),
):
    hotel_id = uuid.UUID(user.hotel_id)
    rooms = db.execute(
        select(Room).where(Room.hotel_id == hotel_id).order_by(Room.room_number)
    ).scalars().all()
    return templates.TemplateResponse("rooms.html", {
        "request": request,
        "user": user,
        "rooms": rooms,
    })


@app.post("/dashboard/rooms", response_class=HTMLResponse)
def room_add(
    request: Request,
    room_number: str = Form(...),
    room_type: str = Form("standard"),
    user: CurrentUser = Depends(require_manager),
    db: Session = Depends(db_session),
):
    hotel_id = uuid.UUID(user.hotel_id)
    existing = db.execute(
        select(Room).where(Room.hotel_id == hotel_id, Room.room_number == room_number.strip())
    ).scalar_one_or_none()
    if not existing:
        db.add(Room(hotel_id=hotel_id, room_number=room_number.strip(), room_type=room_type))
        db.commit()
    return RedirectResponse("/dashboard/rooms", status_code=302)


@app.post("/dashboard/rooms/{room_id}/delete")
def room_delete(
    room_id: str,
    user: CurrentUser = Depends(require_manager),
    db: Session = Depends(db_session),
):
    room = db.get(Room, uuid.UUID(room_id))
    if not room or str(room.hotel_id) != user.hotel_id:
        raise HTTPException(status_code=404)
    db.delete(room)
    db.commit()
    return RedirectResponse("/dashboard/rooms", status_code=302)


# ---------------------------------------------------------------------------
# Dashboard — Guest Stays
# ---------------------------------------------------------------------------

@app.get("/dashboard/stays", response_class=HTMLResponse)
def stays_page(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(db_session),
):
    hotel_id = uuid.UUID(user.hotel_id)
    today = date.today()
    stays = db.execute(
        select(GuestStay)
        .where(GuestStay.hotel_id == hotel_id)
        .where(GuestStay.departure_date >= today)
        .order_by(GuestStay.arrival_date.asc())
        .limit(200)
    ).scalars().all()
    return templates.TemplateResponse("stays.html", {
        "request": request,
        "user": user,
        "stays": stays,
    })


@app.post("/dashboard/stays/import", response_class=HTMLResponse)
def stays_import(
    request: Request,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_manager),
    db: Session = Depends(db_session),
):
    try:
        count = import_guest_stays_csv(db, uuid.UUID(user.hotel_id), file.file)
        msg = f"Imported {count} stays successfully."
    except Exception as exc:
        msg = f"Import failed: {exc}"

    hotel_id = uuid.UUID(user.hotel_id)
    today = date.today()
    stays = db.execute(
        select(GuestStay)
        .where(GuestStay.hotel_id == hotel_id)
        .where(GuestStay.departure_date >= today)
        .order_by(GuestStay.arrival_date.asc())
        .limit(200)
    ).scalars().all()
    return templates.TemplateResponse("stays.html", {
        "request": request,
        "user": user,
        "stays": stays,
        "import_msg": msg,
    })


# ---------------------------------------------------------------------------
# Dashboard — Analytics
# ---------------------------------------------------------------------------

@app.get("/dashboard/analytics", response_class=HTMLResponse)
def analytics_page(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(db_session),
):
    hotel_id = uuid.UUID(user.hotel_id)
    last_7 = datetime.utcnow() - timedelta(days=7)

    total_in = db.execute(
        select(func.count(Message.id)).where(
            Message.hotel_id == hotel_id,
            Message.direction == "in",
            Message.received_at >= last_7,
        )
    ).scalar() or 0

    total_escalated = db.execute(
        select(func.count(Message.id)).where(
            Message.hotel_id == hotel_id,
            Message.direction == "in",
            Message.escalated_at.is_not(None),
            Message.received_at >= last_7,
        )
    ).scalar() or 0

    total_replied = db.execute(
        select(func.count(Message.id)).where(
            Message.hotel_id == hotel_id,
            Message.status.in_(["replied", "auto_replied"]),
            Message.received_at >= last_7,
        )
    ).scalar() or 0

    # Recent escalations
    escalations = db.execute(
        select(Escalation)
        .where(Escalation.hotel_id == hotel_id)
        .order_by(Escalation.triggered_at.desc())
        .limit(20)
    ).scalars().all()

    hotel = db.get(Hotel, hotel_id)

    return templates.TemplateResponse("analytics.html", {
        "request": request,
        "user": user,
        "total_in": total_in,
        "total_escalated": total_escalated,
        "total_replied": total_replied,
        "breach_rate": round(total_escalated / total_in * 100, 1) if total_in else 0,
        "escalations": escalations,
        "hotel": hotel,
    })


# ---------------------------------------------------------------------------
# Dashboard — Hotel Settings (manager only)
# ---------------------------------------------------------------------------

@app.get("/dashboard/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: CurrentUser = Depends(require_manager),
    db: Session = Depends(db_session),
):
    hotel = db.get(Hotel, uuid.UUID(user.hotel_id))
    staff = db.execute(
        select(StaffUser).where(StaffUser.hotel_id == hotel.id).order_by(StaffUser.email)
    ).scalars().all()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "hotel": hotel,
        "staff": staff,
        "msg": None,
    })


@app.post("/dashboard/settings/sla", response_class=HTMLResponse)
def update_sla(
    request: Request,
    sla_seconds: int = Form(...),
    user: CurrentUser = Depends(require_manager),
    db: Session = Depends(db_session),
):
    hotel = db.get(Hotel, uuid.UUID(user.hotel_id))
    if sla_seconds < 5:
        sla_seconds = 5
    hotel.sla_seconds = sla_seconds
    db.commit()
    staff = db.execute(
        select(StaffUser).where(StaffUser.hotel_id == hotel.id).order_by(StaffUser.email)
    ).scalars().all()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "hotel": hotel,
        "staff": staff,
        "msg": f"SLA updated to {sla_seconds}s.",
    })


@app.post("/dashboard/settings/staff/add", response_class=HTMLResponse)
def staff_add(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("agent"),
    user: CurrentUser = Depends(require_manager),
    db: Session = Depends(db_session),
):
    hotel_id = uuid.UUID(user.hotel_id)
    existing = db.execute(
        select(StaffUser).where(StaffUser.hotel_id == hotel_id, StaffUser.email == email.strip())
    ).scalar_one_or_none()
    if not existing:
        db.add(StaffUser(
            hotel_id=hotel_id,
            email=email.strip(),
            password_hash=hash_password(password),
            role=role if role in ("agent", "manager") else "agent",
        ))
        db.commit()
    return RedirectResponse("/dashboard/settings", status_code=302)


@app.post("/dashboard/settings/staff/{staff_id}/deactivate")
def staff_deactivate(
    staff_id: str,
    user: CurrentUser = Depends(require_manager),
    db: Session = Depends(db_session),
):
    staff = db.get(StaffUser, uuid.UUID(staff_id))
    if not staff or str(staff.hotel_id) != user.hotel_id:
        raise HTTPException(status_code=404)
    staff.is_active = False
    db.commit()
    return RedirectResponse("/dashboard/settings", status_code=302)


# ---------------------------------------------------------------------------
# Message APIs
# ---------------------------------------------------------------------------

@app.post("/api/messages/{message_id}/ack", response_class=PlainTextResponse)
def ack_message(
    message_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(db_session),
):
    msg = db.get(Message, uuid.UUID(message_id))
    if not msg or str(msg.hotel_id) != user.hotel_id:
        raise HTTPException(status_code=404, detail="Not found")
    if msg.status == "unactioned":
        msg.status = "acknowledged"
        msg.actioned_at = datetime.utcnow()
        msg.actioned_type = "ack"
        msg.actioned_by_user_id = uuid.UUID(user.staff_user_id)
        db.commit()
    return "OK"


@app.post("/api/messages/{message_id}/reply", response_class=PlainTextResponse)
async def reply_message(
    message_id: str,
    reply: str = Form(...),
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(db_session),
):
    msg = db.get(Message, uuid.UUID(message_id))
    if not msg or str(msg.hotel_id) != user.hotel_id:
        raise HTTPException(status_code=404, detail="Not found")

    hotel = db.get(Hotel, msg.hotel_id)
    conv = db.get(Conversation, msg.conversation_id)
    if not (hotel and conv):
        raise HTTPException(status_code=400, detail="Bad state")

    if conv.opted_out:
        raise HTTPException(status_code=400, detail="Guest has opted out")

    try:
        token = decrypt_str(hotel.whatsapp_access_token_enc)
    except Exception as exc:
        log.error("token_decrypt_failed", hotel_id=str(hotel.id), error=str(exc))
        raise HTTPException(status_code=500, detail="WhatsApp token unavailable")
    sent = await send_whatsapp_text(
        phone_number_id=hotel.whatsapp_phone_number_id,
        access_token=token,
        to_e164_or_waid=conv.wa_id,
        text=reply,
    )

    msg.status = "replied"
    msg.actioned_at = datetime.utcnow()
    msg.actioned_type = "reply"
    msg.actioned_by_user_id = uuid.UUID(user.staff_user_id)
    db.commit()

    # Record outbound message
    wa_out_id = sent.get("messages", [{}])[0].get("id") if sent else None
    out_msg = Message(
        hotel_id=hotel.id,
        conversation_id=conv.id,
        direction="out",
        wa_message_id=wa_out_id,
        body=reply,
        received_at=datetime.utcnow(),
        status="sent",
        actioned_by_user_id=uuid.UUID(user.staff_user_id),
    )
    db.add(out_msg)
    db.commit()
    return "OK"


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------

@app.get("/push/vapid_public_key", response_class=PlainTextResponse)
def vapid_key():
    return settings.vapid_public_key


@app.post("/push/subscribe", response_class=PlainTextResponse)
async def push_subscribe(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(db_session),
):
    data = await request.json()
    sub = data["subscription"]
    keys = sub["keys"]

    # Remove existing subscription for this endpoint to avoid duplicates
    existing = db.execute(
        select(PushSubscription).where(
            PushSubscription.staff_user_id == uuid.UUID(user.staff_user_id),
            PushSubscription.endpoint == sub["endpoint"],
        )
    ).scalar_one_or_none()
    if not existing:
        ps = PushSubscription(
            hotel_id=uuid.UUID(user.hotel_id),
            staff_user_id=uuid.UUID(user.staff_user_id),
            endpoint=sub["endpoint"],
            p256dh=keys["p256dh"],
            auth=keys["auth"],
        )
        db.add(ps)
        db.commit()
    return "OK"


# ---------------------------------------------------------------------------
# Legacy demo_ids endpoint (kept for backwards compat with pwa.js)
# ---------------------------------------------------------------------------

@app.get("/api/demo_ids", response_class=JSONResponse)
def demo_ids(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(db_session),
):
    return {"hotel_id": user.hotel_id, "staff_user_id": user.staff_user_id}


# ---------------------------------------------------------------------------
# Legacy CSV import API endpoint
# ---------------------------------------------------------------------------

@app.post("/api/guests/import_csv", response_class=PlainTextResponse)
def import_csv(
    hotel_id: str = Form(...),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_manager),
    db: Session = Depends(db_session),
):
    if hotel_id != user.hotel_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    count = import_guest_stays_csv(db, uuid.UUID(hotel_id), file.file)
    return f"Imported {count} stays"
