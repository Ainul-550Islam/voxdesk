"""billing: plans, subscriptions, usage metering, invoices, webhook receipts

Creates six tables and extends the `auditaction` enum with nine billing
actions.

**Purely additive.** No existing table, column or row is touched. In
particular the four pre-STEP-7 billing columns on `tenants` are deliberately
kept:

    plan              a free-text string
    included_minutes  a per-tenant integer
    minutes_used      a lifetime float counter
    is_active         the manual off switch

`minutes_used` is demoted from *authority* to *cache* by the application
(see `docs/BILLING-AUDIT.md` F3): the number that decides an invoice is now
derived from immutable `usage_events`, but the float is kept in step because
the dashboard reads it and because dropping a column external tooling may
read is a separate, announced change — not something to bundle into the
release that fixes the metering.

`tenants.plan` is likewise kept and is actively used: it is the fallback that
lets a pre-STEP-7 tenant with `plan="pro"` and no subscription row keep the
Pro entitlements they were sold. Requirement 25 forbids suddenly blocking
existing tenants, and this column is how that promise is honoured.

**No plan rows are inserted here.** Seeding lives in `app/billing/plans.py`
and runs at startup, because prices are business data that changes and a
migration is the wrong home for it — repricing should be an operator action
against a running system, not a schema change followed by a deploy.

Constraints, and the specific failure each prevents:

  uq_usage_event_idempotency   (tenant_id, idempotency_key)
        **The money constraint.** Requirement 14 asks for a database backstop
        behind the application logic, and this is it: no interleaving of
        workers, webhook retries or duplicate Twilio callbacks can produce two
        billable rows for one fact.

  uq_usage_summary_slot        (tenant_id, billing_period, metric)
        One rollup per bucket, so a concurrent rebuild cannot fork the cache.

  uq_subscription_tenant_provider   (tenant_id, provider)
        Makes "the tenant's subscription" a well-defined phrase and stops a
        concurrent checkout creating a second row.

  uq_subscription_external_id       (provider, external_subscription_id)
        A provider subscription belongs to exactly one tenant. Without it a
        webhook carrying an id could be matched to the wrong row — a
        cross-tenant billing error.

  uq_billing_plan_code         (code)
        Plan codes are the trust boundary for checkout; two rows with the
        same code would make `require_plan` non-deterministic.

  uq_billing_receipt_event     (provider, provider_event_id)
        Webhook dedupe. Not tenant-scoped, because provider event ids are
        globally unique and an event can arrive before we know the tenant.

  uq_billing_invoice_external  (provider, external_invoice_id)
        Idempotent invoice mirroring.

Revision ID: 0008_billing
Revises: 0007_calendar_scheduling
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008_billing"
down_revision = "0007_calendar_scheduling"
branch_labels = None
depends_on = None

# SQLAlchemy's Enum() persists the member NAME, not the value. These must
# match app/db/models exactly; `tests/test_enum_consistency.py` parses this
# file and compares. That check caught a real mismatch in migration 0005.
billing_provider_type = sa.Enum("STRIPE", "MANUAL", name="billingprovidertype")
subscription_status = sa.Enum(
    "TRIALING", "ACTIVE", "PAST_DUE", "CANCELING", "CANCELED",
    "INCOMPLETE", "INCOMPLETE_EXPIRED", "PAUSED",
    name="subscriptionstatus",
)
billing_interval = sa.Enum("MONTH", "YEAR", name="billinginterval")
usage_metric = sa.Enum(
    "VOICE_MINUTE", "SMS_SEGMENT", "LLM_TOKEN", "TTS_CHARACTER",
    name="usagemetric",
)
usage_event_type = sa.Enum(
    "VOICE_MINUTE_USED", "SMS_SEGMENT_USED", "LLM_TOKEN_USED",
    "TTS_CHARACTER_USED", "MANUAL_ADJUSTMENT",
    name="usageeventtype",
)
invoice_status = sa.Enum(
    "DRAFT", "OPEN", "PAID", "UNCOLLECTIBLE", "VOID", name="invoicestatus"
)

NEW_AUDIT_ACTIONS = (
    "BILLING_CHECKOUT_STARTED",
    "BILLING_SUBSCRIPTION_CREATED",
    "BILLING_SUBSCRIPTION_CHANGED",
    "BILLING_CANCELLATION_REQUESTED",
    "BILLING_CANCELLATION_COMPLETED",
    "BILLING_PAYMENT_FAILED",
    "BILLING_PLAN_CHANGED",
    "BILLING_LIMIT_HIT",
    "BILLING_ADJUSTMENT",
)


def upgrade() -> None:
    bind = op.get_bind()

    for enum_type in (
        billing_provider_type, subscription_status, billing_interval,
        usage_metric, usage_event_type, invoice_status,
    ):
        enum_type.create(bind, checkfirst=True)

    if bind.dialect.name == "postgresql":
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on
        # older PostgreSQL, and does not exist on SQLite.
        for value in NEW_AUDIT_ACTIONS:
            op.execute(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{value}'")

    # ------------------------------------------------------ billing_plans ---
    op.create_table(
        "billing_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="usd"),
        # Integer minor units, never floats. A float 19.99 is not exactly
        # 19.99, and accumulating fractional overage in binary floating point
        # produces invoices that do not reconcile with themselves.
        sa.Column(
            "monthly_price_cents", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("annual_price_cents", sa.Integer(), nullable=True),
        sa.Column(
            "included_voice_minutes", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "included_sms_segments", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "included_llm_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "included_tts_characters", sa.Integer(), nullable=False, server_default="0"
        ),
        # Hundredths of a cent. Token pricing is genuinely below one cent per
        # unit; rounding each unit to a cent would overcharge by orders of
        # magnitude.
        sa.Column(
            "overage_voice_minute_millicents", sa.Integer(), nullable=False,
            server_default="0",
        ),
        sa.Column(
            "overage_sms_millicents", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "overage_llm_token_millicents", sa.Integer(), nullable=False,
            server_default="0",
        ),
        sa.Column(
            "overage_tts_character_millicents", sa.Integer(), nullable=False,
            server_default="0",
        ),
        sa.Column(
            "overage_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "feature_entitlements", sa.JSON(), nullable=False, server_default="{}"
        ),
        # The trust boundary: a checkout request names a plan code, and the
        # price id comes from here.
        sa.Column(
            "provider_price_ids", sa.JSON(), nullable=False, server_default="{}"
        ),
        sa.Column("trial_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("code", name="uq_billing_plan_code"),
    )
    op.create_index("ix_billing_plan_active", "billing_plans", ["is_active"])

    # ------------------------------------------------------ subscriptions ---
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "plan_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_plans.id", ondelete="RESTRICT"), nullable=True,
        ),
        sa.Column("provider", billing_provider_type, nullable=False),
        sa.Column("external_customer_id", sa.String(255), nullable=True),
        sa.Column("external_subscription_id", sa.String(255), nullable=True),
        sa.Column("external_price_id", sa.String(255), nullable=True),
        sa.Column(
            "status", subscription_status, nullable=False, server_default="INCOMPLETE"
        ),
        sa.Column(
            "interval", billing_interval, nullable=False, server_default="MONTH"
        ),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_at_period_end", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        # A downgrade waiting for the period end. Upgrades are immediate;
        # downgrades are scheduled -- the policy is in service.change_plan.
        sa.Column(
            "pending_plan_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_plans.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("pending_interval", billing_interval, nullable=True),
        # Ordering authority for out-of-order webhooks.
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_invoice_status", invoice_status, nullable=True),
        sa.Column("last_payment_failed_at", sa.DateTime(timezone=True), nullable=True),
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
            "tenant_id", "provider", name="uq_subscription_tenant_provider"
        ),
        sa.UniqueConstraint(
            "provider", "external_subscription_id", name="uq_subscription_external_id"
        ),
    )
    op.create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"])
    op.create_index("ix_subscriptions_plan_id", "subscriptions", ["plan_id"])
    op.create_index(
        "ix_subscription_tenant_status", "subscriptions", ["tenant_id", "status"]
    )
    op.create_index(
        "ix_subscription_period_end", "subscriptions", ["current_period_end"]
    )

    # -------------------------------------------------------- usage_events ---
    op.create_table(
        "usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        # `YYYY-MM` of the *billing* period, not the calendar month.
        sa.Column("billing_period", sa.String(16), nullable=False),
        sa.Column("metric", usage_metric, nullable=False),
        sa.Column("event_type", usage_event_type, nullable=False),
        sa.Column("source_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_reference", sa.String(255), nullable=True),
        # Integer, in the metric's smallest unit: seconds for voice. Signed,
        # but negative only for MANUAL_ADJUSTMENT.
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column(
            "event_metadata", sa.JSON(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_usage_event_idempotency"
        ),
    )
    op.create_index("ix_usage_events_tenant_id", "usage_events", ["tenant_id"])
    op.create_index(
        "ix_usage_event_rollup", "usage_events",
        ["tenant_id", "billing_period", "metric"],
    )
    op.create_index(
        "ix_usage_event_source", "usage_events", ["tenant_id", "source_entity_id"]
    )
    op.create_index("ix_usage_event_created", "usage_events", ["created_at"])

    # ----------------------------------------------------- usage_summaries ---
    op.create_table(
        "usage_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("billing_period", sa.String(16), nullable=False),
        sa.Column("metric", usage_metric, nullable=False),
        sa.Column(
            "included_quantity", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("used_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "overage_quantity", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "estimated_overage_millicents", sa.Integer(), nullable=False,
            server_default="0",
        ),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        # Once true the figures are what was invoiced and are not silently
        # rewritten by a late event.
        sa.Column(
            "finalized", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "warned_at_percent", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "billing_period", "metric", name="uq_usage_summary_slot"
        ),
    )
    op.create_index("ix_usage_summaries_tenant_id", "usage_summaries", ["tenant_id"])
    op.create_index("ix_usage_summary_period", "usage_summaries", ["billing_period"])

    # ---------------------------------------------------- billing_invoices ---
    op.create_table(
        "billing_invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider", billing_provider_type, nullable=False),
        sa.Column("external_invoice_id", sa.String(255), nullable=False),
        sa.Column("status", invoice_status, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="usd"),
        sa.Column(
            "amount_due_cents", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "amount_paid_cents", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hosted_invoice_url", sa.String(1000), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "provider", "external_invoice_id", name="uq_billing_invoice_external"
        ),
    )
    op.create_index("ix_billing_invoices_tenant_id", "billing_invoices", ["tenant_id"])
    op.create_index(
        "ix_billing_invoice_tenant_created",
        "billing_invoices", ["tenant_id", "created_at"],
    )

    # -------------------------------------------- billing_webhook_receipts ---
    op.create_table(
        "billing_webhook_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Nullable: a Stripe event can arrive before the customer is linked to
        # a tenant, and the receipt must still be recorded so a retry of that
        # same event is recognised.
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("provider", billing_provider_type, nullable=False),
        sa.Column("provider_event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False, server_default=""),
        sa.Column("provider_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "processed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "provider", "provider_event_id", name="uq_billing_receipt_event"
        ),
    )
    op.create_index(
        "ix_billing_webhook_receipts_tenant_id",
        "billing_webhook_receipts", ["tenant_id"],
    )
    op.create_index(
        "ix_billing_receipt_received", "billing_webhook_receipts", ["received_at"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_receipt_received", table_name="billing_webhook_receipts"
    )
    op.drop_index(
        "ix_billing_webhook_receipts_tenant_id",
        table_name="billing_webhook_receipts",
    )
    op.drop_table("billing_webhook_receipts")

    op.drop_index("ix_billing_invoice_tenant_created", table_name="billing_invoices")
    op.drop_index("ix_billing_invoices_tenant_id", table_name="billing_invoices")
    op.drop_table("billing_invoices")

    op.drop_index("ix_usage_summary_period", table_name="usage_summaries")
    op.drop_index("ix_usage_summaries_tenant_id", table_name="usage_summaries")
    op.drop_table("usage_summaries")

    op.drop_index("ix_usage_event_created", table_name="usage_events")
    op.drop_index("ix_usage_event_source", table_name="usage_events")
    op.drop_index("ix_usage_event_rollup", table_name="usage_events")
    op.drop_index("ix_usage_events_tenant_id", table_name="usage_events")
    op.drop_table("usage_events")

    op.drop_index("ix_subscription_period_end", table_name="subscriptions")
    op.drop_index("ix_subscription_tenant_status", table_name="subscriptions")
    op.drop_index("ix_subscriptions_plan_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_tenant_id", table_name="subscriptions")
    op.drop_table("subscriptions")

    op.drop_index("ix_billing_plan_active", table_name="billing_plans")
    op.drop_table("billing_plans")

    bind = op.get_bind()
    for enum_type in (
        invoice_status, usage_event_type, usage_metric, billing_interval,
        subscription_status, billing_provider_type,
    ):
        enum_type.drop(bind, checkfirst=True)

    # The nine `auditaction` members are intentionally NOT removed.
    # PostgreSQL has no `ALTER TYPE ... DROP VALUE`, and rebuilding the type
    # would mean rewriting every audit_logs row. Unused enum members are
    # harmless; destroying a billing audit trail to tidy one up is not.

