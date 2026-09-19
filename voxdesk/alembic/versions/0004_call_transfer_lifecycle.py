"""call transfer lifecycle

Adds the columns that let the application prove a human transfer actually
happened, plus a failure reason for calls that ended badly.

Purely additive: no existing column is altered or dropped, no row is touched.
Every new column is nullable or carries a server default, so rows written by
the previous version stay valid and a rolling deploy does not break.

Revision ID: 0004_call_transfer_lifecycle
Revises: 0003_auth_rbac
"""
import sqlalchemy as sa

from alembic import op

revision = "0004_call_transfer_lifecycle"
down_revision = "0003_auth_rbac"
branch_labels = None
depends_on = None

# SQLAlchemy's Enum() persists the member NAME, not the value, so this type
# must list the names exactly as they appear in app/db/models.TransferState.
# Getting this wrong is what caused the bugs 0002_enum_consistency had to fix.
transfer_state = sa.Enum(
    "NONE", "REQUESTED", "DIALING", "CONNECTED", "FAILED",
    name="transferstate",
)


def upgrade() -> None:
    bind = op.get_bind()
    transfer_state.create(bind, checkfirst=True)

    op.add_column(
        "calls",
        sa.Column("failure_reason", sa.String(120), nullable=True),
    )
    op.add_column(
        "calls",
        sa.Column(
            "transfer_state",
            transfer_state,
            nullable=False,
            server_default="NONE",
        ),
    )
    op.add_column("calls", sa.Column("transfer_destination", sa.String(64), nullable=True))
    op.add_column("calls", sa.Column("transfer_reason", sa.String(400), nullable=True))
    op.add_column(
        "calls",
        sa.Column("transfer_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("calls", sa.Column("transfer_error", sa.String(300), nullable=True))
    for column in (
        "transfer_requested_at",
        "transfer_started_at",
        "transfer_completed_at",
        "transfer_failed_at",
    ):
        op.add_column(
            "calls", sa.Column(column, sa.DateTime(timezone=True), nullable=True)
        )

    # Supports the dashboard's "show me escalations" filter without a table
    # scan once a tenant has a few hundred thousand calls.
    op.create_index(
        "ix_calls_tenant_transfer_state",
        "calls",
        ["tenant_id", "transfer_state"],
    )


def downgrade() -> None:
    """
    Drops only what this migration added. Calls, turns and tenants are
    untouched, so downgrading loses transfer metadata but no call history.
    """
    bind = op.get_bind()

    op.drop_index("ix_calls_tenant_transfer_state", table_name="calls")
    for column in (
        "transfer_failed_at",
        "transfer_completed_at",
        "transfer_started_at",
        "transfer_requested_at",
        "transfer_error",
        "transfer_attempts",
        "transfer_reason",
        "transfer_destination",
        "transfer_state",
        "failure_reason",
    ):
        op.drop_column("calls", column)

    transfer_state.drop(bind, checkfirst=True)