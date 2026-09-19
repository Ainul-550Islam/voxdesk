"""performance indexes + data-policy columns

Adds composite indexes for the hottest query patterns (per-tenant call log,
per-tenant lead lists, per-call transcript reads) and the data-subject-rights
columns on tenants (consent provenance, erasure marker).

Revision ID: 0009_perf_policy
Revises: 0008_billing
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_perf_policy"
down_revision = "0008_billing"
branch_labels = None
depends_on = None

#: New members appended to the existing `auditaction` type (data-subject
#: rights + licensing).
NEW_AUDIT_ACTIONS = (
    "GDPR_EXPORT",
    "GDPR_ERASURE",
    "LICENSE_ISSUED",
)


def upgrade() -> None:
    bind = op.get_bind()

    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older
    # PostgreSQL, and does not exist on SQLite, so it is guarded.
    if bind.dialect.name == "postgresql":
        for value in NEW_AUDIT_ACTIONS:
            op.execute(
                f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{value}'"
            )

    # NOTE: `ix_calls_tenant_started` already exists (created in 0001_baseline
    # for the dashboard's monthly-call query), so it is not re-created here.
    op.create_index("ix_calls_tenant_status", "calls", ["tenant_id", "status"])
    op.create_index("ix_turns_call_created", "turns", ["call_id", "created_at"])
    op.create_index("ix_leads_tenant_status", "leads", ["tenant_id", "status"])
    op.create_index("ix_leads_tenant_created", "leads", ["tenant_id", "created_at"])

    op.add_column(
        "tenants",
        sa.Column("data_consent_recorded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("data_consent_source", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("erasure_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "erasure_requested_at")
    op.drop_column("tenants", "data_consent_source")
    op.drop_column("tenants", "data_consent_recorded_at")

    op.drop_index("ix_leads_tenant_created", table_name="leads")
    op.drop_index("ix_leads_tenant_status", table_name="leads")
    op.drop_index("ix_turns_call_created", table_name="turns")
    op.drop_index("ix_calls_tenant_status", table_name="calls")
