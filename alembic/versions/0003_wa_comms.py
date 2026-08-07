"""track 24h service window and WhatsApp send errors

Revision ID: 0003_wa_comms
Revises: 0002_features
Create Date: 2026-08-07

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_wa_comms"
down_revision = "0002_features"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Conversation: start of Meta's 24h free-form messaging window, and when
    # the guest opted out (needed for the audit trail).
    op.add_column("conversations", sa.Column("last_inbound_at", sa.DateTime(), nullable=True))
    op.add_column("conversations", sa.Column("opted_out_at", sa.DateTime(), nullable=True))

    # Backfill the window from the newest inbound message per conversation so
    # existing threads don't all look stale on deploy.
    op.execute(
        """
        UPDATE conversations c
        SET last_inbound_at = sub.max_received
        FROM (
            SELECT conversation_id, MAX(received_at) AS max_received
            FROM messages
            WHERE direction = 'in'
            GROUP BY conversation_id
        ) sub
        WHERE sub.conversation_id = c.id
        """
    )

    # Message: why a send failed, straight from the status callback.
    op.add_column("messages", sa.Column("wa_error_code", sa.Integer(), nullable=True))
    op.add_column("messages", sa.Column("wa_error_title", sa.String(length=255), nullable=True))

    # Status callbacks arrive keyed only by wa_message_id — make that lookup an
    # index seek rather than a scan of the hotel's whole message history.
    op.create_index(
        "ix_messages_wa_message_id", "messages", ["hotel_id", "wa_message_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_messages_wa_message_id", table_name="messages")
    op.drop_column("messages", "wa_error_title")
    op.drop_column("messages", "wa_error_code")
    op.drop_column("conversations", "opted_out_at")
    op.drop_column("conversations", "last_inbound_at")
