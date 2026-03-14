"""initial tables

Revision ID: 0001_initial
Revises: 
Create Date: 2026-01-11

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hotels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("whatsapp_phone_number_id", sa.String(length=64), nullable=False),
        sa.Column("whatsapp_business_phone_e164", sa.String(length=32), nullable=False),
        sa.Column("whatsapp_access_token_enc", sa.Text(), nullable=False),
        sa.Column("manager_wa_e164", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_hotels_whatsapp_phone_number_id", "hotels", ["whatsapp_phone_number_id"], unique=True)

    op.create_table(
        "staff_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hotel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("hotels.id"), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_staff_users_hotel_id", "staff_users", ["hotel_id"], unique=False)
    op.create_index("ix_staff_users_role", "staff_users", ["hotel_id", "role"], unique=False)
    op.create_unique_constraint("uq_staff_email_per_hotel", "staff_users", ["hotel_id", "email"])

    op.create_table(
        "rooms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hotel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("hotels.id"), nullable=False),
        sa.Column("room_number", sa.String(length=32), nullable=False),
        sa.Column("room_type", sa.String(length=64), nullable=False, server_default="standard"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_rooms_hotel_id", "rooms", ["hotel_id"], unique=False)
    op.create_unique_constraint("uq_room_number_per_hotel", "rooms", ["hotel_id", "room_number"])

    op.create_table(
        "guest_stays",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hotel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("hotels.id"), nullable=False),
        sa.Column("guest_name", sa.String(length=255), nullable=False),
        sa.Column("arrival_date", sa.Date(), nullable=False),
        sa.Column("departure_date", sa.Date(), nullable=False),
        sa.Column("room_number", sa.String(length=32), nullable=False),
        sa.Column("guest_phone", sa.String(length=32), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("reservation_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_stay_lookup", "guest_stays", ["hotel_id", "room_number", "arrival_date", "departure_date"], unique=False)

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hotel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("hotels.id"), nullable=False),
        sa.Column("wa_id", sa.String(length=32), nullable=False),
        sa.Column("room_number", sa.String(length=32), nullable=True),
        sa.Column("stay_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("guest_stays.id"), nullable=True),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_unique_constraint("uq_conv_per_hotel", "conversations", ["hotel_id", "wa_id"])
    op.create_index("ix_conversations_hotel_id", "conversations", ["hotel_id"], unique=False)

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hotel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("hotels.id"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("direction", sa.String(length=3), nullable=False),
        sa.Column("wa_message_id", sa.String(length=128), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("actioned_at", sa.DateTime(), nullable=True),
        sa.Column("actioned_type", sa.String(length=16), nullable=True),
        sa.Column("actioned_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("staff_users.id"), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("escalated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_messages_hotel_id", "messages", ["hotel_id"], unique=False)
    op.create_index("ix_messages_status", "messages", ["status"], unique=False)
    op.create_index("ix_messages_received_at", "messages", ["received_at"], unique=False)
    op.create_index("ix_messages_escalated_at", "messages", ["escalated_at"], unique=False)
    op.create_index("ix_sla_scan", "messages", ["hotel_id", "status", "received_at"], unique=False)
    op.create_unique_constraint("uq_wa_msgid_per_hotel", "messages", ["hotel_id", "wa_message_id"])

    op.create_table(
        "escalations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hotel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("hotels.id"), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("triggered_at", sa.DateTime(), nullable=False),
        sa.Column("whatsapp_notified_at", sa.DateTime(), nullable=True),
        sa.Column("push_notified_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_escalations_hotel_id", "escalations", ["hotel_id"], unique=False)
    op.create_index("ix_escalations_triggered_at", "escalations", ["triggered_at"], unique=False)
    op.create_unique_constraint("uq_escalation_message_id", "escalations", ["message_id"])

    op.create_table(
        "push_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hotel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("hotels.id"), nullable=False),
        sa.Column("staff_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("staff_users.id"), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_push_subscriptions_hotel_id", "push_subscriptions", ["hotel_id"], unique=False)
    op.create_index("ix_push_subscriptions_staff_user_id", "push_subscriptions", ["staff_user_id"], unique=False)
    op.create_unique_constraint("uq_push_endpoint_per_user", "push_subscriptions", ["staff_user_id", "endpoint"])


def downgrade() -> None:
    op.drop_table("push_subscriptions")
    op.drop_table("escalations")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("guest_stays")
    op.drop_table("rooms")
    op.drop_table("staff_users")
    op.drop_index("ix_hotels_whatsapp_phone_number_id", table_name="hotels")
    op.drop_table("hotels")
