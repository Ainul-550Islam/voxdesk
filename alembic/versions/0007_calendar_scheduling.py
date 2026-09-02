"""calendar/scheduling layer: appointment lifecycle, integrations, policies

Extends `appointments` and creates three tables:

  calendar_integrations      -- per (tenant, provider) connection + encrypted OAuth
  scheduling_policies        -- business hours, breaks, holidays, buffers
  calendar_webhook_receipts  -- inbound provider notification dedupe

and extends the `auditaction` enum with four scheduling actions.

**No existing data is destroyed.** Every column added to `appointments` is
nullable or has a server default, and two of them are backfilled in this
migration rather than left wrong:

  * `status`   -> CONFIRMED for every existing row. Those rows were created by
                  the pre-STEP-6 code, which only ever inserted an appointment
                  it believed in. Defaulting them to PENDING would make the
                  whole existing book look unconfirmed and would fire a
                  reconciliation alarm for every historical booking.
  * `timezone` -> copied from the owning tenant. Leaving it at the column
                  default of 'UTC' would silently reinterpret every existing
                  appointment by the tenant's offset -- for America/New_York
                  that is a five-hour error on rows that are currently correct.
  * `provider` -> GOOGLE_SERVICE_ACCOUNT wherever `google_event_id` is set,
                  because that is the only path that could have written one.

The legacy `tenants.google_calendar_id` column and
`app/integrations/google_calendar.py` are deliberately untouched: the previous
application version is still running during a rolling deploy, and
`tests/test_availability.py` exercises that path.

Constraints worth naming, because each prevents a specific failure:

  uq_appointment_slot          (tenant_id, slot_key)
        **The double-booking defence.** Two callers racing for the same slot
        compute the same `slot_key`, so the second INSERT loses at the
        database rather than in application logic that can interleave.
        Nullable, and NULLs are distinct in a UNIQUE index in both PostgreSQL
        and SQLite -- which is what lets pre-STEP-6 rows coexist without all
        colliding, and what lets a cancelled appointment release its slot by
        setting the key back to NULL.

  uq_appointment_idempotency   (tenant_id, idempotency_key)
        A retried booking request finds its own earlier attempt instead of
        creating a second appointment.

  uq_calendar_integration_tenant_provider
        Makes (tenant, provider) a key rather than a convention, the same way
        STEP 5 did for CRM.

  uq_scheduling_policy_tenant
        One policy per tenant.

  uq_calendar_receipt_event    (tenant_id, provider, provider_event_id)
        Inbound webhook replay protection.

Revision ID: 0007_calendar_scheduling
Revises: 0006_crm_integrations
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_calendar_scheduling"
down_revision = "0006_crm_integrations"
branch_labels = None
depends_on = None

# SQLAlchemy's Enum() persists the member NAME, not the value. These must match
# app/db/models exactly; `tests/test_enum_consistency.py` parses this file and
# compares. That check caught a real mismatch in migration 0005.
appointment_status = sa.Enum(
    "PENDING", "CONFIRMED", "RESCHEDULED", "CANCELLED", "NO_SHOW", "FAILED",
    name="appointmentstatus",
)
calendar_provider_type = sa.Enum(
    "GOOGLE", "GOOGLE_SERVICE_ACCOUNT", "MICROSOFT", "CALCOM", "INTERNAL",
    name="calendarprovidertype",
)

NEW_AUDIT_ACTIONS = (
    "CALENDAR_CONNECTED",
    "CALENDAR_DISCONNECTED",
    "APPOINTMENT_CANCELLED",
    "APPOINTMENT_RESCHEDULED",
)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    appointment_status.create(bind, checkfirst=True)
    calendar_provider_type.create(bind, checkfirst=True)

    if is_postgres:
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on
        # older PostgreSQL, and does not exist on SQLite.
        for value in NEW_AUDIT_ACTIONS:
            op.execute(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{value}'")

    # ------------------------------------------------------ appointments ---
    #
    # Added as a batch so SQLite -- which cannot ALTER a column -- rebuilds the
    # table once instead of once per column.
    with op.batch_alter_table("appointments") as batch:
        batch.add_column(sa.Column(
            "status", appointment_status, nullable=False, server_default="PENDING"
        ))
        batch.add_column(sa.Column(
            "timezone", sa.String(64), nullable=False, server_default="UTC"
        ))
        batch.add_column(sa.Column("provider", calendar_provider_type, nullable=True))
        batch.add_column(sa.Column("external_event_id", sa.String(255), nullable=True))
        batch.add_column(sa.Column("calendar_reference", sa.String(255), nullable=True))
        batch.add_column(sa.Column(
            "lead_id", postgresql.UUID(as_uuid=True), nullable=True
        ))
        batch.add_column(sa.Column("attendee_email", sa.String(320), nullable=True))
        batch.add_column(sa.Column(
            "notes", sa.Text(), nullable=False, server_default=""
        ))
        batch.add_column(sa.Column("meeting_url", sa.String(500), nullable=True))
        batch.add_column(sa.Column("slot_key", sa.String(64), nullable=True))
        batch.add_column(sa.Column("idempotency_key", sa.String(128), nullable=True))
        batch.add_column(sa.Column(
            "confirmed_at", sa.DateTime(timezone=True), nullable=True
        ))
        batch.add_column(sa.Column(
            "cancelled_at", sa.DateTime(timezone=True), nullable=True
        ))
        batch.add_column(sa.Column("cancellation_reason", sa.String(500), nullable=True))
        batch.add_column(sa.Column("cancelled_by", sa.String(64), nullable=True))
        batch.add_column(sa.Column(
            "rescheduled_from", sa.DateTime(timezone=True), nullable=True
        ))
        batch.add_column(sa.Column("last_error", sa.String(500), nullable=True))
        batch.add_column(sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ))

    # ---- backfill, before the constraints go on ----
    #
    # Existing rows were written by code that only inserted an appointment it
    # believed in, so CONFIRMED is the truthful status for them.
    op.execute("UPDATE appointments SET status = 'CONFIRMED'")
    op.execute(
        "UPDATE appointments SET provider = 'GOOGLE_SERVICE_ACCOUNT', "
        "external_event_id = google_event_id "
        "WHERE google_event_id IS NOT NULL AND google_event_id <> ''"
    )
    # Take the timezone from the owning tenant. Without this every historical
    # appointment would claim UTC and be read back hours out.
    op.execute(
        "UPDATE appointments SET timezone = ("
        "  SELECT t.timezone FROM tenants t WHERE t.id = appointments.tenant_id"
        ") WHERE EXISTS ("
        "  SELECT 1 FROM tenants t WHERE t.id = appointments.tenant_id"
        ")"
    )

    with op.batch_alter_table("appointments") as batch:
        batch.create_foreign_key(
            "fk_appointment_lead", "leads", ["lead_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_unique_constraint(
            "uq_appointment_slot", ["tenant_id", "slot_key"]
        )
        batch.create_unique_constraint(
            "uq_appointment_idempotency", ["tenant_id", "idempotency_key"]
        )
    op.create_index(
        "ix_appointment_tenant_start", "appointments", ["tenant_id", "starts_at"]
    )
    op.create_index(
        "ix_appointment_tenant_status", "appointments", ["tenant_id", "status"]
    )

    # ----------------------------------------------- calendar_integrations ---
    op.create_table(
        "calendar_integrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider", calendar_provider_type, nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        # AES-256-GCM envelope, same scheme and key ring as CRM credentials.
        sa.Column("credentials_encrypted", sa.Text(), nullable=True),
        sa.Column("credentials_key_id", sa.String(64), nullable=True),
        sa.Column("credentials_updated_at", sa.DateTime(timezone=True), nullable=True),
        # Not secret, and the refresh sweep queries on it.
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_ok", sa.Boolean(), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "provider", name="uq_calendar_integration_tenant_provider"
        ),
    )
    op.create_index(
        "ix_calendar_integrations_tenant_id", "calendar_integrations", ["tenant_id"]
    )
    op.create_index(
        "ix_calendar_integration_tenant_enabled",
        "calendar_integrations", ["tenant_id", "is_enabled"],
    )

    # ------------------------------------------------- scheduling_policies ---
    op.create_table(
        "scheduling_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        # {"mon": [["09:00","12:00"],["13:00","17:00"]], "sun": []}
        sa.Column("weekly_hours", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("holidays", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("blocked_periods", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("slot_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column(
            "slot_interval_minutes", sa.Integer(), nullable=False, server_default="30"
        ),
        sa.Column(
            "buffer_before_minutes", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "buffer_after_minutes", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "minimum_notice_minutes", sa.Integer(), nullable=False, server_default="60"
        ),
        sa.Column(
            "booking_horizon_days", sa.Integer(), nullable=False, server_default="60"
        ),
        sa.Column(
            "max_slots_offered", sa.Integer(), nullable=False, server_default="3"
        ),
        sa.Column(
            "allow_outside_business_hours", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "require_provider_confirmation", sa.Boolean(), nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", name="uq_scheduling_policy_tenant"),
    )
    op.create_index(
        "ix_scheduling_policies_tenant_id", "scheduling_policies", ["tenant_id"]
    )

    # ------------------------------------------- calendar_webhook_receipts ---
    op.create_table(
        "calendar_webhook_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider", calendar_provider_type, nullable=False),
        sa.Column("provider_event_id", sa.String(255), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "provider", "provider_event_id",
            name="uq_calendar_receipt_event",
        ),
    )
    op.create_index(
        "ix_calendar_webhook_receipts_tenant_id",
        "calendar_webhook_receipts", ["tenant_id"],
    )
    op.create_index(
        "ix_calendar_receipt_received", "calendar_webhook_receipts", ["received_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_calendar_receipt_received", table_name="calendar_webhook_receipts")
    op.drop_index(
        "ix_calendar_webhook_receipts_tenant_id", table_name="calendar_webhook_receipts"
    )
    op.drop_table("calendar_webhook_receipts")

    op.drop_index("ix_scheduling_policies_tenant_id", table_name="scheduling_policies")
    op.drop_table("scheduling_policies")

    op.drop_index(
        "ix_calendar_integration_tenant_enabled", table_name="calendar_integrations"
    )
    op.drop_index(
        "ix_calendar_integrations_tenant_id", table_name="calendar_integrations"
    )
    op.drop_table("calendar_integrations")

    op.drop_index("ix_appointment_tenant_status", table_name="appointments")
    op.drop_index("ix_appointment_tenant_start", table_name="appointments")
    with op.batch_alter_table("appointments") as batch:
        batch.drop_constraint("uq_appointment_idempotency", type_="unique")
        batch.drop_constraint("uq_appointment_slot", type_="unique")
        batch.drop_constraint("fk_appointment_lead", type_="foreignkey")
        for column in (
            "updated_at", "last_error", "rescheduled_from", "cancelled_by",
            "cancellation_reason", "cancelled_at", "confirmed_at",
            "idempotency_key", "slot_key", "meeting_url", "notes",
            "attendee_email", "lead_id", "calendar_reference",
            "external_event_id", "provider", "timezone", "status",
        ):
            batch.drop_column(column)

    bind = op.get_bind()
    calendar_provider_type.drop(bind, checkfirst=True)
    appointment_status.drop(bind, checkfirst=True)

    # The four `auditaction` members are intentionally NOT removed.
    # PostgreSQL has no `ALTER TYPE ... DROP VALUE`, and rebuilding the type
    # would mean rewriting every audit_logs row. Unused enum members are
    # harmless; destroying an audit trail to tidy one up is not.
