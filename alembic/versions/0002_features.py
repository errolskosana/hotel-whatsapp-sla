"""add sla_seconds, opted_out, wa_status, knowledge_chunks

Revision ID: 0002_features
Revises: 0001_initial
Create Date: 2026-03-14

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_features"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Hotel: configurable SLA threshold
    op.add_column("hotels", sa.Column("sla_seconds", sa.Integer(), nullable=False, server_default="20"))

    # Conversation: opt-out flag
    op.add_column("conversations", sa.Column("opted_out", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    # Message: WhatsApp delivery status
    op.add_column("messages", sa.Column("wa_status", sa.String(length=16), nullable=True))

    # Knowledge chunks table
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hotel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("hotels.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_knowledge_chunks_hotel_id", "knowledge_chunks", ["hotel_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_hotel_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_column("messages", "wa_status")
    op.drop_column("conversations", "opted_out")
    op.drop_column("hotels", "sla_seconds")
