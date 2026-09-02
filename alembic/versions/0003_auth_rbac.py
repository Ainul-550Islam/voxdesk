"""auth: users, refresh tokens, audit log

Adds the authentication and RBAC tables. Purely additive: no existing table is
altered and no row is deleted, so an existing deployment keeps every tenant,
call, transcript and lead exactly as it is.

Bootstrapping note: this migration intentionally does NOT create a default
owner account. Shipping a known email/password pair would be a backdoor. Use
`python -m scripts.create_owner` after upgrading.

Revision ID: 0003_auth_rbac
Revises: 0002_enum_consistency
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_auth_rbac"
down_revision = "0002_enum_consistency"
branch_labels = None
depends_on = None

user_role = sa.Enum(
    "OWNER", "ADMIN", "MANAGER", "AGENT", "VIEWER", name="userrole"
)
audit_action = sa.Enum(
    "LOGIN_SUCCESS", "LOGIN_FAILURE", "LOGOUT", "TOKEN_REFRESH",
    "USER_CREATED", "USER_DEACTIVATED", "USER_REACTIVATED", "ROLE_CHANGED",
    "PASSWORD_CHANGED", "AUTHZ_DENIED",
    name="auditaction",
)


def upgrade() -> None:
    bind = op.get_bind()
    uuid_t = postgresql.UUID(as_uuid=True)

    user_role.create(bind, checkfirst=True)
    audit_action.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column(
            "tenant_id", uuid_t,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("full_name", sa.String(200), server_default=""),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", user_role, nullable=False, server_default="VIEWER"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Global uniqueness: login is by email alone, with no tenant selector, so
    # the same address in two tenants would make authentication ambiguous.
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_index("ix_users_tenant_role", "users", ["tenant_id", "role"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column(
            "user_id", uuid_t,
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        # SHA-256 hex digest only. The plaintext token is never stored.
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by", uuid_t, nullable=True),
        sa.Column("user_agent", sa.String(300), server_default=""),
        sa.Column("ip_address", sa.String(64), server_default=""),
    )
    op.create_index(
        "ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_active", "refresh_tokens", ["user_id", "revoked_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column(
            "tenant_id", uuid_t,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column(
            "actor_user_id", uuid_t,
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("target_user_id", uuid_t, nullable=True),
        sa.Column("action", audit_action, nullable=False),
        sa.Column("actor_email", sa.String(320), server_default=""),
        sa.Column("ip_address", sa.String(64), server_default=""),
        sa.Column("user_agent", sa.String(300), server_default=""),
        sa.Column("detail", sa.JSON(), server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_tenant_time", "audit_logs", ["tenant_id", "created_at"])


def downgrade() -> None:
    """
    Drops only the tables this migration created. Tenants, calls, transcripts
    and leads are untouched, so downgrading loses accounts and audit history
    but no business data.
    """
    bind = op.get_bind()

    op.drop_table("audit_logs")
    op.drop_table("refresh_tokens")
    op.drop_table("users")

    audit_action.drop(bind, checkfirst=True)
    user_role.drop(bind, checkfirst=True)
