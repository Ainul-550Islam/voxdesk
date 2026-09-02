"""
The permission vocabulary.

Every capability in the product is named exactly once here. Route code asks
for a `Permission`, never for a role string -- so adding a role, or moving a
capability between roles, is a one-line change in `rbac.ROLE_PERMISSIONS`
instead of a repository-wide grep for `if user.role == "admin"`.
"""
from __future__ import annotations

import enum


class Permission(str, enum.Enum):
    # ---- tenant configuration -------------------------------------------
    TENANT_READ = "tenant:read"
    TENANT_UPDATE = "tenant:update"          # voice tuning, IVR, business hours
    TENANT_CREATE = "tenant:create"          # provisioning a brand new business
    TENANT_DELETE = "tenant:delete"

    # ---- team -----------------------------------------------------------
    USER_READ = "user:read"
    USER_CREATE = "user:create"
    USER_UPDATE = "user:update"              # rename, deactivate, reactivate
    USER_ROLE_CHANGE = "user:role_change"
    USER_DELETE = "user:delete"

    # ---- operational data -----------------------------------------------
    CALL_READ = "call:read"
    CALL_READ_ALL = "call:read_all"          # not just calls assigned to me
    TRANSCRIPT_READ = "transcript:read"
    RECORDING_READ = "recording:read"

    LEAD_READ = "lead:read"
    LEAD_CREATE = "lead:create"
    LEAD_UPDATE = "lead:update"
    LEAD_DELETE = "lead:delete"

    APPOINTMENT_READ = "appointment:read"
    APPOINTMENT_WRITE = "appointment:write"

    CAMPAIGN_READ = "campaign:read"
    CAMPAIGN_WRITE = "campaign:write"
    CAMPAIGN_RUN = "campaign:run"            # spends real money on calls

    # ---- knowledge base / RAG -------------------------------------------
    KNOWLEDGE_READ = "knowledge:read"        # list/inspect documents
    KNOWLEDGE_WRITE = "knowledge:write"      # upload, reindex, archive
    KNOWLEDGE_DELETE = "knowledge:delete"    # permanent removal

    # ---- reporting -------------------------------------------------------
    ANALYTICS_READ = "analytics:read"
    AUDIT_READ = "audit:read"

    # ---- sensitive -------------------------------------------------------
    INTEGRATION_READ = "integration:read"    # CRM / calendar wiring
    INTEGRATION_WRITE = "integration:write"
    # Firing a sync spends the tenant's provider rate limit and writes to
    # their CRM, so it is separated from merely reading the configuration.
    INTEGRATION_SYNC = "integration:sync"
    COMPLIANCE_READ = "compliance:read"
    COMPLIANCE_WRITE = "compliance:write"    # A2P registration, DNC handling
    SECURITY_SETTINGS = "security:settings"
    BILLING_READ = "billing:read"
    BILLING_WRITE = "billing:write"


# Convenience bundles used when composing roles. Kept here so `rbac.py` reads
# as a policy document rather than a wall of enum members.
READ_ONLY_OPERATIONAL: frozenset[Permission] = frozenset({
    Permission.TENANT_READ,
    Permission.CALL_READ,
    Permission.CALL_READ_ALL,
    Permission.TRANSCRIPT_READ,
    Permission.LEAD_READ,
    Permission.APPOINTMENT_READ,
    Permission.CAMPAIGN_READ,
    Permission.KNOWLEDGE_READ,
    Permission.ANALYTICS_READ,
})

OPERATIONAL_WRITE: frozenset[Permission] = frozenset({
    Permission.LEAD_CREATE,
    Permission.LEAD_UPDATE,
    Permission.APPOINTMENT_WRITE,
    Permission.CAMPAIGN_WRITE,
    Permission.CAMPAIGN_RUN,
    Permission.RECORDING_READ,
})

TEAM_MANAGEMENT: frozenset[Permission] = frozenset({
    Permission.USER_READ,
    Permission.USER_CREATE,
    Permission.USER_UPDATE,
    Permission.USER_ROLE_CHANGE,
})

