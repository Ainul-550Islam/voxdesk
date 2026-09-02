"""
The role policy: which role holds which permissions, and who may grant what.

This module is pure data plus pure functions -- no database, no request, no
FastAPI. That makes the policy directly unit-testable and means a reviewer can
read the whole authorization model in one screen.
"""
from __future__ import annotations

from app.auth.permissions import (
    OPERATIONAL_WRITE,
    READ_ONLY_OPERATIONAL,
    TEAM_MANAGEMENT,
    Permission,
)
from app.db.models import UserRole

# Privilege ordering. Used to stop a role granting something above itself.
ROLE_LEVEL: dict[UserRole, int] = {
    UserRole.OWNER: 100,
    UserRole.ADMIN: 80,
    UserRole.MANAGER: 60,
    UserRole.AGENT: 40,
    UserRole.VIEWER: 20,
}


_VIEWER: frozenset[Permission] = READ_ONLY_OPERATIONAL | {Permission.COMPLIANCE_READ}

# An agent works their own queue: same reads as a viewer, minus the ability to
# see every call in the tenant, plus the day-to-day write actions.
_AGENT: frozenset[Permission] = (
    (_VIEWER - {Permission.CALL_READ_ALL})
    | {
        Permission.LEAD_CREATE,
        Permission.LEAD_UPDATE,
        Permission.APPOINTMENT_WRITE,
        Permission.RECORDING_READ,
    }
)

# A manager curates the knowledge base: it is business content, not a security
# setting. Deleting a document outright stays with admin, because retrieval
# history and audits reference it.
_MANAGER: frozenset[Permission] = _VIEWER | OPERATIONAL_WRITE | {
    Permission.LEAD_DELETE,
    Permission.KNOWLEDGE_WRITE,
}

_ADMIN: frozenset[Permission] = _MANAGER | TEAM_MANAGEMENT | {
    Permission.TENANT_UPDATE,
    Permission.INTEGRATION_READ,
    Permission.INTEGRATION_WRITE,
    # Triggering a resync writes to the tenant's CRM and spends their provider
    # rate limit, so it sits with the role that owns the connection rather
    # than with anyone who can see that it exists.
    Permission.INTEGRATION_SYNC,
    # An admin may *see* the bill -- they need to know why a limit was hit --
    # but only an owner may change what the company is charged.
    # `_OWNER` is `set(Permission) - _PLATFORM_ONLY`, so BILLING_WRITE stays
    # owner-only by simply not being listed here.
    Permission.BILLING_READ,
    Permission.COMPLIANCE_WRITE,
    Permission.AUDIT_READ,
    Permission.KNOWLEDGE_DELETE,
}

# Owner gets everything defined, including future permissions, except the
# platform-level ability to create or delete whole tenants -- that is an
# operator action, not a customer action. See `is_platform_permission`.
_PLATFORM_ONLY: frozenset[Permission] = frozenset({
    Permission.TENANT_CREATE,
    Permission.TENANT_DELETE,
})

_OWNER: frozenset[Permission] = frozenset(set(Permission) - _PLATFORM_ONLY)


ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.OWNER: _OWNER,
    UserRole.ADMIN: _ADMIN,
    UserRole.MANAGER: _MANAGER,
    UserRole.AGENT: _AGENT,
    UserRole.VIEWER: _VIEWER,
}


def is_platform_permission(permission: Permission) -> bool:
    return permission in _PLATFORM_ONLY


def permissions_for(role: UserRole) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(role: UserRole, permission: Permission) -> bool:
    return permission in permissions_for(role)


def role_level(role: UserRole) -> int:
    return ROLE_LEVEL.get(role, 0)


def outranks(actor: UserRole, target: UserRole) -> bool:
    """Strictly higher privilege. Peers cannot administer each other."""
    return role_level(actor) > role_level(target)


def can_assign_role(actor: UserRole, target_role: UserRole) -> bool:
    """
    Privilege-escalation guard.

    A user may only grant a role strictly below their own. An admin therefore
    cannot mint another admin or an owner, and nobody but an owner can create
    an owner. This is checked again in the service layer against the database.
    """
    if not has_permission(actor, Permission.USER_ROLE_CHANGE):
        return False
    return outranks(actor, target_role)


def can_manage_user(actor: UserRole, target: UserRole) -> bool:
    """Whether `actor` may modify a user currently holding `target`."""
    if not has_permission(actor, Permission.USER_UPDATE):
        return False
    # An owner is allowed to manage other owners (co-founder case); everyone
    # else must strictly outrank the person they are touching.
    if actor is UserRole.OWNER:
        return True
    return outranks(actor, target)


def describe_roles() -> list[dict]:
    """Machine-readable policy, surfaced at GET /auth/roles for the dashboard."""
    return [
        {
            "role": role.value,
            "level": role_level(role),
            "permissions": sorted(p.value for p in permissions_for(role)),
        }
        for role in sorted(UserRole, key=role_level, reverse=True)
    ]
