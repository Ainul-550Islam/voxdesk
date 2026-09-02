"""v0.3 baseline: full schema

Creates every table from scratch. If you were running v0.1 or v0.2 with the
auto-create lifespan, drop the database and start from this migration -- the
old auto-create never added the new columns to existing tables.

Revision ID: 0001_baseline
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


call_status = sa.Enum(
    "RINGING", "IN_PROGRESS", "COMPLETED", "FAILED", "NO_ANSWER", name="callstatus"
)
call_direction = sa.Enum("INBOUND", "OUTBOUND", name="calldirection")
lead_status = sa.Enum(
    "NEW", "QUEUED", "CALLED", "QUALIFIED", "UNQUALIFIED", "FAILED", "DNC",
    name="leadstatus",
)
speaker = sa.Enum("USER", "ASSISTANT", "SYSTEM", name="speaker")


def upgrade() -> None:
    uuid_t = postgresql.UUID(as_uuid=True)

    op.create_table(
        "tenants",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("industry", sa.String(80), server_default="general"),
        sa.Column("twilio_number", sa.String(32), nullable=False, unique=True),
        sa.Column("agent_name", sa.String(80), server_default="Alex"),
        sa.Column("greeting", sa.Text()),
        sa.Column("system_prompt_extra", sa.Text(), server_default=""),
        sa.Column("knowledge_base", sa.JSON(), server_default="{}"),
        # LLM
        sa.Column("llm_preset", sa.String(32), server_default="natural"),
        sa.Column("llm_provider", sa.String(32), nullable=True),
        sa.Column("llm_model", sa.String(80), nullable=True),
        sa.Column("temperature", sa.Float(), server_default="0.65"),
        # humanize
        sa.Column("humanize", sa.Boolean(), server_default=sa.true()),
        sa.Column("vad_stop_secs", sa.Float(), server_default="0.45"),
        sa.Column("speech_speed", sa.Float(), server_default="1.0"),
        sa.Column("voice_id", sa.String(64), nullable=True),
        sa.Column("language", sa.String(16), server_default="en-US"),
        # behaviour
        sa.Column("timezone", sa.String(64), server_default="America/New_York"),
        sa.Column("business_open", sa.Time()),
        sa.Column("business_close", sa.Time()),
        sa.Column("appointment_minutes", sa.Integer(), server_default="30"),
        sa.Column("escalation_number", sa.String(32), nullable=True),
        sa.Column("notify_sms_number", sa.String(32), nullable=True),
        # integrations
        sa.Column("google_calendar_id", sa.String(255), nullable=True),
        sa.Column("crm_webhook_url", sa.String(500), nullable=True),
        sa.Column("crm_type", sa.String(32), server_default="webhook"),
        sa.Column("crm_api_key", sa.String(255), nullable=True),
        # outbound
        sa.Column("outbound_enabled", sa.Boolean(), server_default=sa.false()),
        sa.Column("outbound_caller_id", sa.String(32), nullable=True),
        sa.Column("outbound_window_open", sa.Time()),
        sa.Column("outbound_window_close", sa.Time()),
        sa.Column("max_call_attempts", sa.Integer(), server_default="3"),
        # reminders
        sa.Column("reminder_hours_before", sa.Integer(), server_default="24"),
        sa.Column("reminder_enabled", sa.Boolean(), server_default=sa.false()),
        # ivr
        sa.Column("ivr_enabled", sa.Boolean(), server_default=sa.false()),
        sa.Column("ivr_flow", sa.JSON(), server_default="{}"),
        # channels
        sa.Column("sms_enabled", sa.Boolean(), server_default=sa.true()),
        sa.Column("whatsapp_enabled", sa.Boolean(), server_default=sa.false()),
        sa.Column("whatsapp_number", sa.String(32), nullable=True),
        # a2p
        sa.Column("a2p_brand_sid", sa.String(64), nullable=True),
        sa.Column("a2p_campaign_sid", sa.String(64), nullable=True),
        sa.Column("a2p_status", sa.String(32), server_default="not_started"),
        # recording
        sa.Column("record_calls", sa.Boolean(), server_default=sa.false()),
        sa.Column("recording_disclaimer", sa.Text()),
        # billing
        sa.Column("plan", sa.String(32), server_default="starter"),
        sa.Column("included_minutes", sa.Integer(), server_default="500"),
        sa.Column("minutes_used", sa.Float(), server_default="0"),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    op.create_index("ix_tenants_twilio_number", "tenants", ["twilio_number"])

    op.create_table(
        "campaigns",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("tenant_id", uuid_t, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("goal", sa.String(32), server_default="qualify"),
        sa.Column("script_prompt", sa.Text(), server_default=""),
        sa.Column("opening_line", sa.Text()),
        sa.Column("is_active", sa.Boolean(), server_default=sa.false()),
        sa.Column("calls_per_minute", sa.Integer(), server_default="2"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    op.create_index("ix_campaigns_tenant_id", "campaigns", ["tenant_id"])

    op.create_table(
        "leads",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("tenant_id", uuid_t, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("campaign_id", uuid_t, sa.ForeignKey("campaigns.id"), nullable=True),
        sa.Column("name", sa.String(200), server_default=""),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("company", sa.String(200), nullable=True),
        sa.Column("notes", sa.Text(), server_default=""),
        sa.Column("custom_fields", sa.JSON(), server_default="{}"),
        sa.Column("status", lead_status, server_default="NEW"),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    op.create_index("ix_leads_tenant_id", "leads", ["tenant_id"])
    op.create_index("ix_leads_phone", "leads", ["phone"])
    op.create_index("ix_leads_campaign_id", "leads", ["campaign_id"])
    # The dialer queries this constantly; without it, campaigns crawl.
    op.create_index("ix_leads_dialer", "leads", ["tenant_id", "status", "next_attempt_at"])

    op.create_table(
        "calls",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("tenant_id", uuid_t, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("call_sid", sa.String(64), nullable=False, unique=True),
        sa.Column("from_number", sa.String(32), nullable=False),
        sa.Column("to_number", sa.String(32), nullable=False),
        sa.Column("status", call_status, server_default="RINGING"),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), server_default="0"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("intent", sa.String(80), nullable=True),
        sa.Column("booked", sa.Boolean(), server_default=sa.false()),
        sa.Column("escalated", sa.Boolean(), server_default=sa.false()),
        sa.Column("avg_response_ms", sa.Float(), nullable=True),
        sa.Column("llm_used", sa.String(80), nullable=True),
        sa.Column("direction", call_direction, server_default="INBOUND"),
        sa.Column("recording_url", sa.String(500), nullable=True),
        sa.Column("lead_score", sa.Integer(), nullable=True),
        sa.Column("crm_synced", sa.Boolean(), server_default=sa.false()),
        sa.Column("lead_id", uuid_t, sa.ForeignKey("leads.id"), nullable=True),
    )
    op.create_index("ix_calls_tenant_id", "calls", ["tenant_id"])
    op.create_index("ix_calls_call_sid", "calls", ["call_sid"])
    op.create_index("ix_calls_lead_id", "calls", ["lead_id"])
    # Powers the dashboard's "calls this month" query.
    op.create_index("ix_calls_tenant_started", "calls", ["tenant_id", "started_at"])

    op.create_table(
        "turns",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("call_id", uuid_t, sa.ForeignKey("calls.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("speaker", speaker, nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    op.create_index("ix_turns_call_id", "turns", ["call_id"])

    op.create_table(
        "appointments",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("tenant_id", uuid_t, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("call_id", uuid_t, sa.ForeignKey("calls.id"), nullable=True),
        sa.Column("customer_name", sa.String(200)),
        sa.Column("customer_phone", sa.String(32)),
        sa.Column("reason", sa.Text(), server_default=""),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("google_event_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    op.create_index("ix_appointments_tenant_id", "appointments", ["tenant_id"])
    op.create_index("ix_appointments_starts_at", "appointments", ["starts_at"])

    op.create_table(
        "reminders",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("tenant_id", uuid_t, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("appointment_id", uuid_t, sa.ForeignKey("appointments.id"),
                  nullable=False),
        sa.Column("channel", sa.String(16), server_default="sms"),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent", sa.Boolean(), server_default=sa.false()),
        sa.Column("confirmed", sa.Boolean(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_reminders_tenant_id", "reminders", ["tenant_id"])
    op.create_index("ix_reminders_due", "reminders", ["sent", "send_at"])


def downgrade() -> None:
    for table in ("reminders", "appointments", "turns", "calls",
                  "leads", "campaigns", "tenants"):
        op.drop_table(table)
    for enum in (speaker, lead_status, call_direction, call_status):
        enum.drop(op.get_bind(), checkfirst=True)

