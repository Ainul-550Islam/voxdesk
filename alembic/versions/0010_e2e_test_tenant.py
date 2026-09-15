"""real-call E2E test-tenant marker

Adds the ``tenants.is_test_tenant`` boolean that the Step 5 (scale-compliance)
real-call E2E guard trusts to decide whether an inbound Twilio call is an
operator-run test call. Server default is false so no existing tenant is ever
silently marked as a test tenant.

Revision ID: 0010_e2e_test_tenant
Revises: 0009_perf_policy
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_e2e_test_tenant"
down_revision = "0009_perf_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "is_test_tenant",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "is_test_tenant")
