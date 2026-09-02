"""
Team management. Every operation is scoped to the caller's own tenant.

There is deliberately no `tenant_id` parameter anywhere in this module: the
tenant always comes from the authenticated principal, so there is nothing for
a client to tamper with.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_routes import UserOut
from app.auth import service
from app.auth.dependencies import TenantContext, require_permission
from app.auth.password import PasswordPolicyError
from app.auth.permissions import Permission
from app.db.models import AuditLog, User, UserRole
from app.db.session import get_session

router = APIRouter(prefix="/api/team", tags=["team"])


class UserCreateIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=512)
    full_name: str = ""
    role: UserRole = UserRole.VIEWER


class RoleChangeIn(BaseModel):
    role: UserRole


class ActiveIn(BaseModel):
    is_active: bool


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, service.PermissionDenied):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, service.ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (PasswordPolicyError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


@router.get("/users", response_model=list[UserOut])
async def list_users(
    include_inactive: bool = Query(True),
    ctx: TenantContext = Depends(require_permission(Permission.USER_READ)),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(User).where(User.tenant_id == ctx.tenant_id)
    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))
    rows = (await session.execute(stmt.order_by(User.created_at))).scalars().all()
    return [UserOut.of(u) for u in rows]


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    payload: UserCreateIn,
    ctx: TenantContext = Depends(require_permission(Permission.USER_CREATE)),
    session: AsyncSession = Depends(get_session),
):
    try:
        user = await service.create_user(
            session, actor=ctx.user, email=payload.email,
            password=payload.password, role=payload.role,
            full_name=payload.full_name,
        )
    except Exception as exc:
        raise _translate(exc) from None
    return UserOut.of(user)


async def _target(session: AsyncSession, ctx: TenantContext, user_id: uuid.UUID) -> User:
    """404 rather than 403 for a user in another tenant -- no existence oracle."""
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=404, detail="Not found")
    return user


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(
    user_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.USER_READ)),
    session: AsyncSession = Depends(get_session),
):
    return UserOut.of(await _target(session, ctx, user_id))


@router.patch("/users/{user_id}/role", response_model=UserOut)
async def change_role(
    user_id: uuid.UUID,
    payload: RoleChangeIn,
    ctx: TenantContext = Depends(require_permission(Permission.USER_ROLE_CHANGE)),
    session: AsyncSession = Depends(get_session),
):
    target = await _target(session, ctx, user_id)
    try:
        updated = await service.change_role(
            session, actor=ctx.user, target=target, new_role=payload.role
        )
    except Exception as exc:
        raise _translate(exc) from None
    return UserOut.of(updated)


@router.patch("/users/{user_id}/active", response_model=UserOut)
async def set_active(
    user_id: uuid.UUID,
    payload: ActiveIn,
    ctx: TenantContext = Depends(require_permission(Permission.USER_UPDATE)),
    session: AsyncSession = Depends(get_session),
):
    """Deactivation instead of deletion: transcripts keep a valid author."""
    target = await _target(session, ctx, user_id)
    try:
        updated = await service.set_active(
            session, actor=ctx.user, target=target, active=payload.is_active
        )
    except Exception as exc:
        raise _translate(exc) from None
    return UserOut.of(updated)


@router.get("/audit")
async def list_audit(
    limit: int = Query(100, le=500),
    ctx: TenantContext = Depends(require_permission(Permission.AUDIT_READ)),
    session: AsyncSession = Depends(get_session),
):
    rows = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.tenant_id == ctx.tenant_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "action": r.action.value,
            "actor_email": r.actor_email,
            "target_user_id": str(r.target_user_id) if r.target_user_id else None,
            "ip_address": r.ip_address,
            "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
