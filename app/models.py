import uuid
from datetime import datetime, date
from sqlalchemy import String, DateTime, Date, Text, ForeignKey, UniqueConstraint, Index, Boolean, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


def _uuid():
    return uuid.uuid4()


class Hotel(Base):
    __tablename__ = "hotels"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    whatsapp_phone_number_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    whatsapp_business_phone_e164: Mapped[str] = mapped_column(String(32))
    whatsapp_access_token_enc: Mapped[str] = mapped_column(Text())
    manager_wa_e164: Mapped[str] = mapped_column(String(32))
    sla_seconds: Mapped[int] = mapped_column(Integer(), default=20)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow)


class StaffUser(Base):
    __tablename__ = "staff_users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    hotel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hotels.id"), index=True)
    email: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="agent")
    is_active: Mapped[bool] = mapped_column(Boolean(), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("hotel_id", "email", name="uq_staff_email_per_hotel"),)


class Room(Base):
    __tablename__ = "rooms"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    hotel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hotels.id"), index=True)
    room_number: Mapped[str] = mapped_column(String(32))
    room_type: Mapped[str] = mapped_column(String(64), default="standard")
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("hotel_id", "room_number", name="uq_room_number_per_hotel"),)


class GuestStay(Base):
    __tablename__ = "guest_stays"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    hotel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hotels.id"), index=True)
    guest_name: Mapped[str] = mapped_column(String(255))
    arrival_date: Mapped[date] = mapped_column(Date)
    departure_date: Mapped[date] = mapped_column(Date)
    room_number: Mapped[str] = mapped_column(String(32))
    guest_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reservation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_stay_lookup", "hotel_id", "room_number", "arrival_date", "departure_date"),
    )


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    hotel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hotels.id"), index=True)
    wa_id: Mapped[str] = mapped_column(String(32))
    room_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stay_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("guest_stays.id"), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    opted_out: Mapped[bool] = mapped_column(Boolean(), default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow)

    messages = relationship("Message", back_populates="conversation")
    stay = relationship("GuestStay", foreign_keys=[stay_id])

    __table_args__ = (UniqueConstraint("hotel_id", "wa_id", name="uq_conv_per_hotel"),)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    hotel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hotels.id"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id"), index=True)

    direction: Mapped[str] = mapped_column(String(3))  # in/out
    wa_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    body: Mapped[str] = mapped_column(Text())
    wa_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # sent/delivered/read/failed

    received_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow, index=True)  # SLA start
    actioned_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    actioned_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    actioned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("staff_users.id"), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="unactioned", index=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")

    __table_args__ = (
        Index("ix_sla_scan", "hotel_id", "status", "received_at"),
        UniqueConstraint("hotel_id", "wa_message_id", name="uq_wa_msgid_per_hotel"),
    )


class Escalation(Base):
    __tablename__ = "escalations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    hotel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hotels.id"), index=True)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id"), unique=True)

    triggered_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow, index=True)
    whatsapp_notified_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    push_notified_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="triggered")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    hotel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hotels.id"), index=True)
    staff_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("staff_users.id"), index=True)

    endpoint: Mapped[str] = mapped_column(Text())
    p256dh: Mapped[str] = mapped_column(Text())
    auth: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("staff_user_id", "endpoint", name="uq_push_endpoint_per_user"),)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    hotel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hotels.id"), index=True)
    content: Mapped[str] = mapped_column(Text())
    is_active: Mapped[bool] = mapped_column(Boolean(), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow)
