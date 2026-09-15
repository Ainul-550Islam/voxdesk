"""
Authentication and team-management business logic.

Kept out of the route layer so the rules -- lockout, rotation, last-owner
protection, escalation blocking -- can be tested directly and cannot be
bypassed by a second route that forgets one of them.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import jwt as jwt_utils
from app.auth import password as pw
from app.auth.rbac import can_assign_role, can_manage_user
from app.core.config import settings
from app.db.models import AuditAction, AuditLog, RefreshToken, Tenant, User, UserRole

log = structlog.get_logger()


class AuthError(Exception):
    """Authentication failed. The message is deliberately generic."""


class PermissionDenied(Exception):
    pass


class ConflictError(Exception):
    pass


def _now() -> datetime:
    return _utcnow()


def _naive_utc(value: datetime | None) -> datetime | None:
    """Columns are written naive in places; compare consistently."""
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


# ------------------------------------------------------------------- email ---

def _utcnow() -> datetime:
    """Naive UTC, matching every existing timestamp column in this schema."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_email(raw: str) -> str:
    """
    Lowercased, Unicode-normalised, deliverability not checked (that would
    make login depend on DNS). Raises ValueError on a malformed address.
    """
    try:
        result = validate_email(raw, check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc
    return result.normalized.lower()


# ------------------------------------------------------------------- audit ---

async def record_audit(
    session: AsyncSession,
    *,
    action: AuditAction,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID | None = None,
    actor_email: str = "",
    ip_address: str = "",
    user_agent: str = "",
    detail: dict | None = None,
    commit: bool = True,
) -> AuditLog:
    """`detail` must never contain a password, token or API key."""
    entry = AuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        action=action,
        actor_email=actor_email[:320],
        ip_address=ip_address[:64],
        user_agent=user_agent[:300],
        detail=detail or {},
    )
    session.add(entry)
    if commit:
        await session.commit()
    return entry


# ---------------------------------------------------------------- lockout ---

def is_locked(user: User) -> bool:
    locked_until = _naive_utc(user.locked_until)
    return locked_until is not None and locked_until > _utcnow()


async def _register_failure(session: AsyncSession, user: User) -> None:
    user.failed_login_count += 1
    if user.failed_login_count >= settings.max_failed_logins:
        user.locked_until = _utcnow() + timedelta(minutes=settings.lockout_minutes)
        user.failed_login_count = 0
        log.warning("auth.account_locked", user_id=str(user.id))
    await session.commit()


# --------------------------------------------------------------- authenticate ---

async def authenticate(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    ip_address: str = "",
    user_agent: str = "",
) -> User:
    """
    Returns the User or raises AuthError.

    Every failure path raises the SAME message and spends comparable CPU, so a
    caller cannot distinguish "no such account" from "wrong password" and use
    the endpoint to enumerate registered addresses.
    """
    generic = AuthError("Invalid email or password.")

    try:
        normalized = normalize_email(email)
    except ValueError:
        pw.verify_dummy()
        raise generic from None

    user = (
        await session.execute(select(User).where(User.email == normalized))
    ).scalar_one_or_none()

    if user is None:
        pw.verify_dummy()
        await record_audit(
            session, action=AuditAction.LOGIN_FAILURE, actor_email=normalized,
            ip_address=ip_address, user_agent=user_agent,
            detail={"reason": "no_such_user"},
        )
        raise generic

    if is_locked(user):
        pw.verify_dummy()
        await record_audit(
            session, action=AuditAction.LOGIN_FAILURE, tenant_id=user.tenant_id,
            actor_user_id=user.id, actor_email=normalized, ip_address=ip_address,
            user_agent=user_agent, detail={"reason": "locked"},
        )
        raise generic

    if not pw.verify_password(password, user.password_hash):
        await record_audit(
            session, action=AuditAction.LOGIN_FAILURE, tenant_id=user.tenant_id,
            actor_user_id=user.id, actor_email=normalized, ip_address=ip_address,
            user_agent=user_agent, detail={"reason": "bad_password"}, commit=False,
        )
        await _register_failure(session, user)
        raise generic

    # Correct password but disabled: still a generic failure to the caller.
    if not user.is_active:
        await record_audit(
            session, action=AuditAction.LOGIN_FAILURE, tenant_id=user.tenant_id,
            actor_user_id=user.id, actor_email=normalized, ip_address=ip_address,
            user_agent=user_agent, detail={"reason": "inactive"},
        )
        raise generic

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        await record_audit(
            session, action=AuditAction.LOGIN_FAILURE, tenant_id=user.tenant_id,
            actor_user_id=user.id, actor_email=normalized, ip_address=ip_address,
            user_agent=user_agent, detail={"reason": "tenant_inactive"},
        )
        raise generic

    # Transparent upgrade if the cost factor was raised since signup.
    if pw.needs_rehash(user.password_hash):
        user.password_hash = pw.hash_password(password)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = _utcnow()
    await record_audit(
        session, action=AuditAction.LOGIN_SUCCESS, tenant_id=user.tenant_id,
        actor_user_id=user.id, actor_email=normalized, ip_address=ip_address,
        user_agent=user_agent, commit=False,
    )
    await session.commit()
    return user


# ------------------------------------------------------------------ tokens ---

async def issue_tokens(
    session: AsyncSession, user: User, *, ip_address: str = "", user_agent: str = ""
) -> dict:
    access, expires_in = jwt_utils.create_access_token(
        user_id=user.id, tenant_id=user.tenant_id,
        role=user.role.value, token_version=user.token_version,
    )
    plaintext, digest = jwt_utils.generate_refresh_token()
    session.add(RefreshToken(
        user_id=user.id,
        token_hash=digest,
        expires_at=jwt_utils.refresh_expiry().replace(tzinfo=None),
        user_agent=user_agent[:300],
        ip_address=ip_address[:64],
    ))
    await session.commit()
    return {
        "access_token": access,
        "refresh_token": plaintext,
        "token_type": "bearer",
        "expires_in": expires_in,
    }


async def rotate_refresh_token(
    session: AsyncSession, plaintext: str, *, ip_address: str = "", user_agent: str = ""
) -> dict:
    """
    Single-use rotation with reuse detection.

    Presenting an already-used token means the token leaked, so every live
    refresh token for that user is revoked and the access tokens are
    invalidated by bumping token_version.
    """
    generic = AuthError("Invalid refresh token.")
    digest = jwt_utils.hash_refresh_token(plaintext)

    record = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))
    ).scalar_one_or_none()
    if record is None:
        raise generic

    user = await session.get(User, record.user_id)

    if record.used_at is not None or record.revoked_at is not None:
        log.warning("auth.refresh_reuse_detected", user_id=str(record.user_id))
        await revoke_all_for_user(session, record.user_id, bump_version=True)
        if user is not None:
            await record_audit(
                session, action=AuditAction.AUTHZ_DENIED, tenant_id=user.tenant_id,
                actor_user_id=user.id, actor_email=user.email, ip_address=ip_address,
                detail={"reason": "refresh_token_reuse"},
            )
        raise generic

    if _naive_utc(record.expires_at) <= _utcnow():
        raise generic

    if user is None or not user.is_active:
        raise generic

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise generic

    record.used_at = _utcnow()
    tokens = await issue_tokens(session, user, ip_address=ip_address, user_agent=user_agent)

    replacement = (
        await session.execute(
            select(RefreshToken)
            .where(RefreshToken.token_hash == jwt_utils.hash_refresh_token(
                tokens["refresh_token"]
            ))
        )
    ).scalar_one_or_none()
    if replacement is not None:
        record.replaced_by = replacement.id

    await record_audit(
        session, action=AuditAction.TOKEN_REFRESH, tenant_id=user.tenant_id,
        actor_user_id=user.id, actor_email=user.email, ip_address=ip_address,
        commit=False,
    )
    await session.commit()
    return tokens


async def revoke_refresh_token(session: AsyncSession, plaintext: str) -> bool:
    digest = jwt_utils.hash_refresh_token(plaintext)
    record = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))
    ).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return False
    record.revoked_at = _utcnow()
    await session.commit()
    return True


async def revoke_all_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, bump_version: bool = False
) -> int:
    rows = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
            )
        )
    ).scalars().all()
    now = _utcnow()
    for row in rows:
        row.revoked_at = now
    if bump_version:
        user = await session.get(User, user_id)
        if user is not None:
            user.token_version += 1
    await session.commit()
    return len(rows)


# --------------------------------------------------------- team management ---

async def count_active_owners(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count(User.id)).where(
                User.tenant_id == tenant_id,
                User.role == UserRole.OWNER,
                User.is_active.is_(True),
            )
        )
    ).scalar_one()


async def create_user(
    session: AsyncSession,
    *,
    actor: User,
    email: str,
    password: str,
    role: UserRole,
    full_name: str = "",
    tenant_id: uuid.UUID | None = None,
) -> User:
    """
    The new user is always created inside the ACTOR's tenant. `tenant_id` is
    accepted only so a caller can be explicit, and a mismatch is rejected --
    it can never be used to plant a user in someone else's tenant.
    """
    if tenant_id is not None and tenant_id != actor.tenant_id:
        raise PermissionDenied("Cannot create a user in another tenant.")

    if not can_assign_role(actor.role, role):
        raise PermissionDenied(
            f"A {actor.role.value} cannot grant the {role.value} role."
        )

    normalized = normalize_email(email)
    pw.validate_policy(password, email=normalized)

    existing = (
        await session.execute(select(User).where(User.email == normalized))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("That email address is already registered.")

    user = User(
        tenant_id=actor.tenant_id,
        email=normalized,
        full_name=full_name.strip()[:200],
        password_hash=pw.hash_password(password),
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    await record_audit(
        session, action=AuditAction.USER_CREATED, tenant_id=actor.tenant_id,
        actor_user_id=actor.id, target_user_id=user.id, actor_email=actor.email,
        detail={"role": role.value}, commit=False,
    )
    await session.commit()
    await session.refresh(user)
    return user


async def change_role(
    session: AsyncSession, *, actor: User, target: User, new_role: UserRole
) -> User:
    if target.tenant_id != actor.tenant_id:
        raise PermissionDenied("Cross-tenant modification is not allowed.")
    if target.id == actor.id:
        raise PermissionDenied("You cannot change your own role.")
    if not can_manage_user(actor.role, target.role):
        raise PermissionDenied("You cannot modify a user at or above your level.")
    if not can_assign_role(actor.role, new_role):
        raise PermissionDenied(f"You cannot grant the {new_role.value} role.")

    # Demoting the last owner would lock the tenant out of its own settings.
    if target.role is UserRole.OWNER and new_role is not UserRole.OWNER:
        if await count_active_owners(session, target.tenant_id) <= 1:
            raise ConflictError("A tenant must always have at least one active owner.")

    previous = target.role
    target.role = new_role
    target.token_version += 1          # old access tokens stop working now
    await revoke_all_for_user(session, target.id)
    await record_audit(
        session, action=AuditAction.ROLE_CHANGED, tenant_id=actor.tenant_id,
        actor_user_id=actor.id, target_user_id=target.id, actor_email=actor.email,
        detail={"from": previous.value, "to": new_role.value}, commit=False,
    )
    await session.commit()
    await session.refresh(target)
    return target


async def set_active(
    session: AsyncSession, *, actor: User, target: User, active: bool
) -> User:
    if target.tenant_id != actor.tenant_id:
        raise PermissionDenied("Cross-tenant modification is not allowed.")
    if target.id == actor.id and not active:
        raise PermissionDenied("You cannot deactivate yourself.")
    if not can_manage_user(actor.role, target.role):
        raise PermissionDenied("You cannot modify a user at or above your level.")

    if not active and target.role is UserRole.OWNER:
        if await count_active_owners(session, target.tenant_id) <= 1:
            raise ConflictError("A tenant must always have at least one active owner.")

    target.is_active = active
    target.token_version += 1
    if not active:
        await revoke_all_for_user(session, target.id)
    await record_audit(
        session,
        action=AuditAction.USER_DEACTIVATED if not active else AuditAction.USER_REACTIVATED,
        tenant_id=actor.tenant_id, actor_user_id=actor.id, target_user_id=target.id,
        actor_email=actor.email, commit=False,
    )
    await session.commit()
    await session.refresh(target)
    return target