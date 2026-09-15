"""CRM integration layer: configuration, events, sync state, contact links

Creates five tables:

  crm_integrations       -- one tenant's connection to one provider, with the
                            encrypted credential envelope
  crm_events             -- the normalized business event, recorded once
  crm_syncs              -- delivery state, one row per (event, integration)
  crm_contact_links      -- (tenant, provider, identity) -> external contact id
  crm_webhook_receipts   -- inbound replay protection

and extends the `auditaction` enum with four integration actions.

Purely additive with respect to existing data. Nothing is dropped, no column
is altered, and no row is rewritten. The legacy `tenants.crm_webhook_url`,
`tenants.crm_type` and `tenants.crm_api_key` columns are **deliberately left
in place**:

  * a live tenant's webhook URL is configuration a customer entered, and
    deleting it in the same release that introduces its replacement leaves no
    way back if the new path has a problem;
  * `tests/test_outbound.py` still exercises the legacy helper;
  * a rolling deploy means the previous application version is briefly still
    running, and it reads those columns.

Removing them is a follow-up migration once tenants have been migrated onto
`crm_integrations` and the old code path has been deleted. That is a data
migration with a verification step, not a footnote to this one.

Constraints worth naming, because each prevents a specific failure:

  uq_crm_integration_tenant_provider  (tenant_id, provider)
        Makes "one tenant, one provider" a key rather than a convention.
        Every lookup in the service layer is on this pair -- requirement 5
        states that ids alone are not authorization -- and the constraint is
        what stops a second row from quietly shadowing the first.

  uq_crm_event_idempotency            (tenant_id, idempotency_key)
        The idempotency guarantee. Keys are derived from the business fact,
        so a duplicate Twilio callback computes the same key and the INSERT
        fails instead of producing a second CRM contact. Tenant-scoped so one
        tenant's key can never suppress another's event.

  uq_crm_sync_event_integration       (event_id, integration_id)
        One delivery record per event per provider. Fan-out to three
        providers is three rows, so one provider being down cannot mark the
        others failed, and a duplicate worker pass is a no-op.

  uq_crm_contact_identity             (tenant_id, provider, identity_hash)
        Stops the tenth call from the same number becoming the tenth CRM
        contact. `tenant_id` leads the constraint so cross-tenant contact
        matching is impossible at the schema level, not just in a WHERE
        clause.

  uq_crm_receipt_event                (tenant_id, provider, provider_event_id)
        Inbound replay protection.

Revision ID: 0006_crm_integrations
Revises: 0005_knowledge_rag
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_crm_integrations"
down_revision = "0005_knowledge_rag"
branch_labels = None
depends_on = None

# SQLAlchemy's Enum() persists the member NAME, not the value. These lists
# must match app/db/models exactly. `tests/test_enum_consistency.py` parses
# this file and compares them -- that check caught a real mismatch in
# migration 0005 before it reached a database.
crm_provider_type = sa.Enum(
    "GOHIGHLEVEL", "HUBSPOT", "JOBBER", "WEBHOOK",
    name="crmprovidertype",
)
crm_entity_type = sa.Enum(
    "CALL", "LEAD", "APPOINTMENT",
    name="crmentitytype",
)
crm_event_type = sa.Enum(
    "CALL_COMPLETED", "CALL_MISSED", "LEAD_CREATED", "LEAD_UPDATED",
    "APPOINTMENT_BOOKED", "APPOINTMENT_CANCELLED", "TRANSFER_COMPLETED",
    name="crmeventtype",
)
crm_sync_status = sa.Enum(
    "PENDING", "PROCESSING", "SYNCED", "FAILED", "PERMANENT_FAILURE",
    name="crmsyncstatus",
)

#: New members appended to the existing `auditaction` type.
NEW_AUDIT_ACTIONS = (
    "INTEGRATION_CONNECTED",
    "INTEGRATION_UPDATED",
    "INTEGRATION_DISCONNECTED",
    "INTEGRATION_TESTED",
)


def upgrade() -> None:
    bind = op.get_bind()

    # NOTE: the enum types are created by the `op.create_table` calls below
    # (SQLAlchemy emits `CREATE TYPE` for enum columns). Creating them first
    # and then again via the table DDL emits each type twice and fails on a
    # fresh database with "type ... already exists".

    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older
    # PostgreSQL, and does not exist at all on SQLite, so it is guarded.
    if bind.dialect.name == "postgresql":
        for value in NEW_AUDIT_ACTIONS:
            op.execute(
                f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{value}'"
            )

    # ------------------------------------------------------ integrations ---
    op.create_table(
        "crm_integrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider", crm_provider_type, nullable=False),
        sa.Column(
            "is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        # AES-256-GCM envelope. Opaque to the database, and bound by its AAD
        # to (tenant_id, provider) so a row copied between tenants will not
        # decrypt.
        sa.Column("credentials_encrypted", sa.Text(), nullable=True),
        sa.Column("credentials_key_id", sa.String(64), nullable=True),
        sa.Column("credentials_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("field_mappings", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("subscribed_events", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "share_transcripts", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
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
            "tenant_id", "provider", name="uq_crm_integration_tenant_provider"
        ),
    )
    op.create_index(
        "ix_crm_integrations_tenant_id", "crm_integrations", ["tenant_id"]
    )
    op.create_index(
        "ix_crm_integration_tenant_enabled",
        "crm_integrations", ["tenant_id", "is_enabled"],
    )

    # ------------------------------------------------------------ events ---
    op.create_table(
        "crm_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("event_type", crm_event_type, nullable=False),
        sa.Column("entity_type", crm_entity_type, nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "payload_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_crm_event_idempotency"
        ),
    )
    op.create_index("ix_crm_events_tenant_id", "crm_events", ["tenant_id"])
    op.create_index(
        "ix_crm_event_tenant_created", "crm_events", ["tenant_id", "created_at"]
    )
    op.create_index(
        "ix_crm_event_entity", "crm_events", ["tenant_id", "entity_type", "entity_id"]
    )

    # ------------------------------------------------------------- syncs ---
    op.create_table(
        "crm_syncs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "event_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_events.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "integration_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_integrations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider", crm_provider_type, nullable=False),
        sa.Column("entity_type", crm_entity_type, nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status", crm_sync_status, nullable=False, server_default="PENDING"
        ),
        # Proof of the SYNCED claim. A row is never SYNCED without one.
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        # Scrubbed by errors.safe_message before it is written, because this
        # is shown to tenants and a provider body can echo a token.
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "event_id", "integration_id", name="uq_crm_sync_event_integration"
        ),
    )
    op.create_index("ix_crm_syncs_tenant_id", "crm_syncs", ["tenant_id"])
    op.create_index("ix_crm_syncs_event_id", "crm_syncs", ["event_id"])
    op.create_index("ix_crm_syncs_integration_id", "crm_syncs", ["integration_id"])
    op.create_index("ix_crm_syncs_next_attempt_at", "crm_syncs", ["next_attempt_at"])
    # The worker's query: "everything due, oldest first".
    op.create_index("ix_crm_sync_due", "crm_syncs", ["status", "next_attempt_at"])
    op.create_index(
        "ix_crm_sync_tenant_status", "crm_syncs", ["tenant_id", "status"]
    )
    op.create_index(
        "ix_crm_sync_entity", "crm_syncs", ["tenant_id", "entity_type", "entity_id"]
    )

    # ---------------------------------------------------- contact links ---
    op.create_table(
        "crm_contact_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider", crm_provider_type, nullable=False),
        sa.Column(
            "lead_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="SET NULL"), nullable=True,
        ),
        # sha256 of a tenant-salted normalized phone or email. Salted so this
        # table is not a searchable index of every phone number the platform
        # has seen, and so two tenants sharing a customer produce different
        # hashes.
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("external_contact_id", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "provider", "identity_hash", name="uq_crm_contact_identity"
        ),
    )
    op.create_index(
        "ix_crm_contact_links_tenant_id", "crm_contact_links", ["tenant_id"]
    )
    op.create_index(
        "ix_crm_contact_tenant_provider",
        "crm_contact_links", ["tenant_id", "provider"],
    )
    op.create_index(
        "ix_crm_contact_lead", "crm_contact_links", ["tenant_id", "lead_id"]
    )

    # ------------------------------------------------- webhook receipts ---
    op.create_table(
        "crm_webhook_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider", crm_provider_type, nullable=False),
        sa.Column("provider_event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False, server_default=""),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "provider", "provider_event_id", name="uq_crm_receipt_event"
        ),
    )
    op.create_index(
        "ix_crm_webhook_receipts_tenant_id", "crm_webhook_receipts", ["tenant_id"]
    )
    # Supports the pruning sweep, which deletes by age.
    op.create_index(
        "ix_crm_receipt_received", "crm_webhook_receipts", ["received_at"]
    )


def downgrade() -> None:
    # Reverse dependency order: syncs reference both events and integrations.
    op.drop_index("ix_crm_receipt_received", table_name="crm_webhook_receipts")
    op.drop_index(
        "ix_crm_webhook_receipts_tenant_id", table_name="crm_webhook_receipts"
    )
    op.drop_table("crm_webhook_receipts")

    op.drop_index("ix_crm_contact_lead", table_name="crm_contact_links")
    op.drop_index("ix_crm_contact_tenant_provider", table_name="crm_contact_links")
    op.drop_index("ix_crm_contact_links_tenant_id", table_name="crm_contact_links")
    op.drop_table("crm_contact_links")

    op.drop_index("ix_crm_sync_entity", table_name="crm_syncs")
    op.drop_index("ix_crm_sync_tenant_status", table_name="crm_syncs")
    op.drop_index("ix_crm_sync_due", table_name="crm_syncs")
    op.drop_index("ix_crm_syncs_next_attempt_at", table_name="crm_syncs")
    op.drop_index("ix_crm_syncs_integration_id", table_name="crm_syncs")
    op.drop_index("ix_crm_syncs_event_id", table_name="crm_syncs")
    op.drop_index("ix_crm_syncs_tenant_id", table_name="crm_syncs")
    op.drop_table("crm_syncs")

    op.drop_index("ix_crm_event_entity", table_name="crm_events")
    op.drop_index("ix_crm_event_tenant_created", table_name="crm_events")
    op.drop_index("ix_crm_events_tenant_id", table_name="crm_events")
    op.drop_table("crm_events")

    op.drop_index("ix_crm_integration_tenant_enabled", table_name="crm_integrations")
    op.drop_index("ix_crm_integrations_tenant_id", table_name="crm_integrations")
    op.drop_table("crm_integrations")

    bind = op.get_bind()
    crm_sync_status.drop(bind, checkfirst=True)
    crm_event_type.drop(bind, checkfirst=True)
    crm_entity_type.drop(bind, checkfirst=True)
    crm_provider_type.drop(bind, checkfirst=True)

    # The four `auditaction` members are intentionally NOT removed.
    # PostgreSQL has no `ALTER TYPE ... DROP VALUE`, and rebuilding the type
    # would require rewriting every audit_logs row. Leaving unused members in
    # an enum is harmless; destroying an audit trail to tidy one up is not.