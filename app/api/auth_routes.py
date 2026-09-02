"""
Authentication endpoints.

Response models are explicit allow-lists. There is no `from_attributes` model
over `User` that could start leaking a new sensitive column the day somebody
adds one -- every exposed field is written out by hand in `UserOut`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.dependencies import TenantContext, _client_ip, get_context
from app.auth.rbac import describe_roles, permissions_for
from app.core.config import settings
from app.db.models import AuditAction, User
from app.db.session import get_session

log = structlog.get_logger()
router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "voxdesk_refresh"


# ----------------------------------------------------------------- schemas ---

class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=512)


class RefreshIn(BaseModel):
    """Optional: the refresh token normally travels in an HttpOnly cookie."""
    refresh_token: str | None = None


class UserOut(BaseModel):
    """Safe public projection. Never add a secret field to this model."""
    id: uuid.UUID
    email: str
    full_name: str
    role: str
    tenant_id: uuid.UUID
    is_active: bool
    created_at: datetime | None = None
    last_login_at: datetime | None = None

    @classmethod
    def of(cls, user: User) -> UserOut:
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role.value,
            tenant_id=user.tenant_id,
            is_active=user.is_active,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
        )


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class MeOut(BaseModel):
    user: UserOut
    tenant: dict
    permissions: list[str]


# ----------------------------------------------------------------- helpers ---

def _set_refresh_cookie(response: Response, token: str) -> None:
    """
    HttpOnly so browser JavaScript cannot read it, which keeps the long-lived
    credential out of localStorage and out of reach of XSS.
    """
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=settings.is_production,   # plain http is needed for local dev
        samesite="lax",
        max_age=settings.refresh_token_days * 24 * 3600,
        path="/auth",                    # never sent to /api or /telephony
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/auth")


# ------------------------------------------------------------------ routes ---

@router.post("/login", response_model=TokenOut)
async def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """
    Returns a short-lived access token in the body and sets the refresh token
    as an HttpOnly cookie. Failure is always the same 401 and the same
    message, whether or not the address exists.
    """
    ip = _client_ip(request)
    agent = request.headers.get("user-agent", "")[:300]

    try:
        user = await service.authenticate(
            session, email=payload.email, password=payload.password,
            ip_address=ip, user_agent=agent,
        )
    except service.AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from None

    tokens = await service.issue_tokens(session, user, ip_address=ip, user_agent=agent)
    _set_refresh_cookie(response, tokens["refresh_token"])

    return TokenOut(
        access_token=tokens["access_token"],
        expires_in=tokens["expires_in"],
        user=UserOut.of(user),
    )


@router.post("/refresh", response_model=TokenOut)
async def refresh(
    request: Request,
    response: Response,
    payload: RefreshIn | None = None,
    session: AsyncSession = Depends(get_session),
):
    token = (payload.refresh_token if payload else None) or request.cookies.get(
        REFRESH_COOKIE
    )
    if not token:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    try:
        tokens = await service.rotate_refresh_token(
            session, token,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:300],
        )
    except service.AuthError as exc:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail=str(exc)) from None

    from app.auth.jwt import decode_access_token
    claims = decode_access_token(tokens["access_token"])
    user = await session.get(User, claims.user_id)

    _set_refresh_cookie(response, tokens["refresh_token"])
    return TokenOut(
        access_token=tokens["access_token"],
        expires_in=tokens["expires_in"],
        user=UserOut.of(user),
    )


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    payload: RefreshIn | None = None,
    ctx: TenantContext = Depends(get_context),
    session: AsyncSession = Depends(get_session),
):
    """Revokes the presented refresh token; the access token expires on its own."""
    token = (payload.refresh_token if payload else None) or request.cookies.get(
        REFRESH_COOKIE
    )
    if token:
        await service.revoke_refresh_token(session, token)

    await service.record_audit(
        session, action=AuditAction.LOGOUT, tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id, actor_email=ctx.user.email,
        ip_address=_client_ip(request),
    )
    _clear_refresh_cookie(response)
    return Response(status_code=204)


@router.post("/logout-all", status_code=204)
async def logout_everywhere(
    response: Response,
    ctx: TenantContext = Depends(get_context),
    session: AsyncSession = Depends(get_session),
):
    """Revokes every session and invalidates outstanding access tokens."""
    await service.revoke_all_for_user(session, ctx.user_id, bump_version=True)
    _clear_refresh_cookie(response)
    return Response(status_code=204)


@router.get("/me", response_model=MeOut)
async def me(ctx: TenantContext = Depends(get_context)):
    return MeOut(
        user=UserOut.of(ctx.user),
        tenant={
            "id": str(ctx.tenant.id),
            "name": ctx.tenant.name,
            "industry": ctx.tenant.industry,
            "plan": ctx.tenant.plan,
            "language": ctx.tenant.language,
        },
        permissions=sorted(p.value for p in permissions_for(ctx.role)),
    )


@router.get("/roles")
async def roles(ctx: TenantContext = Depends(get_context)):
    """The policy itself, so the dashboard can render role pickers correctly."""
    return {"roles": describe_roles()}
