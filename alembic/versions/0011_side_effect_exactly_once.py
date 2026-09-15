"""side-effect exactly-once: reminder leases + message webhook receipts

Step 6 (scale-compliance) hardens the two remaining external side effects that
lacked a durable exactly-once primitive:

* ``reminders.claimed_at`` / ``reminders.claimed_by`` — a send lease so two
  overlapping ticks (or a crash between the SMS send and the commit) cannot
  text a customer twice.
* ``message_webhook_receipts`` — replay protection for inbound Twilio
  SMS/WhatsApp messages, matching the existing receipt tables for CRM and
  calendar webhooks.

Revision ID: 0011_side_effect_exactly_once
Revises: 0010_e2e_test_tenant
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_side_effect_exactly_once"
down_revision = "0010_e2e_test_tenant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminders",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("claimed_by", sa.String(length=64), nullable=True),
    )

    op.create_table(
        "message_webhook_receipts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "channel", "provider_message_id",
            name="uq_message_receipt_event",
        ),
    )
    op.create_index(
        "ix_message_receipt_received", "message_webhook_receipts", ["received_at"]
    )
    op.create_index(
        "ix_message_webhook_receipts_tenant_id",
        "message_webhook_receipts",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_message_webhook_receipts_tenant_id", table_name="message_webhook_receipts")
    op.drop_index("ix_message_receipt_received", table_name="message_webhook_receipts")
    op.drop_table("message_webhook_receipts")

    op.drop_column("reminders", "claimed_by")
    op.drop_column("reminders", "claimed_at")
