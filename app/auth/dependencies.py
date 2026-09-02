"""
Reusable security dependencies.

Route handlers never parse a token, never read a role string, and never take
a tenant id from the client. They declare what they need:

    @router.get("/tenants/{tenant_id}/leads")
    async def list_leads(
        ctx: TenantContext = Depends(require_permission(Permission.LEAD_READ)),
    ): ...

`ctx.tenant_id` comes from the signed JWT. When a route also carries
`{tenant_id}` in its path, `tenant_path_guard` compares the two and rejects a
mismatch, so an authenticated user cannot reach another tenant by editing the
URL.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import Depends, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import TokenError, decode_access_token
from app.auth.permissions import Permission
from app.auth.rbac import has_permission, role_level
from app.auth.service import record_audit
from app.db.models import AuditAction, Tenant, User, UserRole
from app.db.session import get_session

log = structlog.get_logger()

# auto_error=False so a missing header produces our own 401 with a
# WWW-Authenticate hint rather than FastAPI's default 403.
_bearer = HTTPBearer(auto_error=False)

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def _forbidden(detail: str = "Insufficient permissions") -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


@dataclass(frozen=True)
class TenantContext:
    """Everything a protected handler is allowed to trust."""
    user: User
    tenant: Tenant

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def role(self) -> UserRole:
        return self.user.role

    def can(self, permission: Permission) -> bool:
        return has_permission(self.user.role, permission)


# ------------------------------------------------------------ current user ---

async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    if credentials is None or not credentials.credentials:
        raise _UNAUTHENTICATED
    if (credentials.scheme or "").lower() != "bearer":
        raise _UNAUTHENTICATED

    try:
        claims = decode_access_token(credentials.credentials)
    except TokenError:
        raise _UNAUTHENTICATED from None

    user = await session.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise _UNAUTHENTICATED

    # The tenant is re-read from the row, not taken from the token, so a
    # user moved between tenants cannot keep using an old token.
    if user.tenant_id != claims.tenant_id:
        log.warning("auth.tenant_claim_mismatch", user_id=str(user.id))
        raise _UNAUTHENTICATED

    # Deactivation, role change and refresh-reuse all bump this.
    if user.token_version != claims.token_version:
        raise _UNAUTHENTICATED

    request.state.user_id = str(user.id)
    request.state.tenant_id = str(user.tenant_id)
    return user


async def get_current_tenant(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Tenant:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise _forbidden("Tenant is inactive")
    return tenant


async def get_context(
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
) -> TenantContext:
    return TenantContext(user=user, tenant=tenant)


# ------------------------------------------------------------- authorization ---

def require_permission(
    *permissions: Permission, require_all: bool = True
) -> Callable[..., Awaitable[TenantContext]]:
    """
    Dependency factory. Default is AND; pass require_all=False for OR.

    A denial is written to the audit log so repeated probing is visible.
    """
    async def _dependency(
        request: Request,
        ctx: TenantContext = Depends(get_context),
        session: AsyncSession = Depends(get_session),
    ) -> TenantContext:
        checks = [has_permission(ctx.role, p) for p in permissions]
        ok = all(checks) if require_all else any(checks)
        if not ok:
            missing = [
                p.value for p, granted in zip(permissions, checks) if not granted
            ]
            await record_audit(
                session, action=AuditAction.AUTHZ_DENIED, tenant_id=ctx.tenant_id,
                actor_user_id=ctx.user_id, actor_email=ctx.user.email,
                ip_address=_client_ip(request),
                detail={"missing": missing, "path": request.url.path},
            )
            raise _forbidden(f"Requires permission: {', '.join(missing)}")
        return ctx

    return _dependency


def require_role(*roles: UserRole) -> Callable[..., Awaitable[TenantContext]]:
    """Prefer require_permission. Use this only for genuinely role-shaped rules."""
    allowed = set(roles)

    async def _dependency(ctx: TenantContext = Depends(get_context)) -> TenantContext:
        if ctx.role not in allowed:
            raise _forbidden(
                f"Requires role: {', '.join(sorted(r.value for r in allowed))}"
            )
        return ctx

    return _dependency


def require_min_role(minimum: UserRole) -> Callable[..., Awaitable[TenantContext]]:
    threshold = role_level(minimum)

    async def _dependency(ctx: TenantContext = Depends(get_context)) -> TenantContext:
        if role_level(ctx.role) < threshold:
            raise _forbidden(f"Requires at least the {minimum.value} role")
        return ctx

    return _dependency


# ---------------------------------------------------------- tenant isolation ---

async def tenant_path_guard(
    tenant_id: uuid.UUID = Path(...),
    ctx: TenantContext = Depends(get_context),
) -> TenantContext:
    """
    For routes that keep `{tenant_id}` in the URL.

    Returns 404 rather than 403 on a mismatch: confirming that some other
    tenant id exists is itself an information leak.
    """
    if tenant_id != ctx.tenant_id:
        log.warning(
            "authz.cross_tenant_path_blocked",
            user_id=str(ctx.user_id), requested=str(tenant_id),
        )
        raise HTTPException(status_code=404, detail="Not found")
    return ctx


def scoped_permission(
    *permissions: Permission,
) -> Callable[..., Awaitable[TenantContext]]:
    """
    The combination used by almost every route: the path tenant must match the
    token tenant AND the caller must hold the permission.
    """
    permission_dep = require_permission(*permissions)

    async def _dependency(
        tenant_id: uuid.UUID = Path(...),
        ctx: TenantContext = Depends(permission_dep),
    ) -> TenantContext:
        if tenant_id != ctx.tenant_id:
            log.warning(
                "authz.cross_tenant_path_blocked",
                user_id=str(ctx.user_id), requested=str(tenant_id),
            )
            raise HTTPException(status_code=404, detail="Not found")
        return ctx

    return _dependency


async def get_owned(
    session: AsyncSession,
    model: Any,
    object_id: uuid.UUID,
    ctx: TenantContext,
    *,
    tenant_field: str = "tenant_id",
) -> Any:
    """
    Fetch a row by id and prove it belongs to the caller's tenant.

    Always 404 on both "missing" and "belongs to someone else", so object ids
    cannot be probed for existence across tenants.
    """
    obj = await session.get(model, object_id)
    if obj is None or getattr(obj, tenant_field, None) != ctx.tenant_id:
        raise HTTPException(status_code=404, detail="Not found")
    return obj


def tenant_filter(model: Any, ctx: TenantContext):
    """Mandatory WHERE clause for every list query."""
    return model.tenant_id == ctx.tenant_id


# ------------------------------------------------------------------ helpers ---

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


async def get_platform_admin(
    ctx: TenantContext = Depends(get_context),
) -> TenantContext:
    """
    Guard for operator-only actions such as creating a whole new tenant.

    There is no platform-superuser concept yet, so this denies everyone and
    those endpoints are served by an out-of-band script instead. Documented as
    a known limitation rather than left as an open endpoint.
    """
    raise _forbidden("Platform administration is not available through this API")
