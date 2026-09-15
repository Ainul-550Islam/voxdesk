"""GDPR / data-subject rights (Step 9 compliance).

Right of access (export) and right to erasure, tenant-scoped and owner-gated.
Everything is derived from the verified JWT principal (ctx.tenant_id) -- there
is no way to request another tenant's data. The audit trail records every
access/erasure request, which is itself a SOC 2 control.

    GET  /api/gdpr/export     machine-readable JSON of the tenant's data
    POST /api/gdpr/erasure    delete call/transcript/lead data, mark the request
    GET  /api/gdpr/status     consent + erasure state
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, record_audit, require_role
from app.core.config import settings
from app.core.data_policy import compliance_summary
from app.db.models import AuditAction, Call, Lead, Turn, UserRole
from app.db.session import get_session

router = APIRouter(prefix="/api/gdpr", tags=["gdpr"])


class ErasureResult(BaseModel):
    ok: bool
    purged_calls: int
    purged_turns: int
    purged_leads: int
    erasure_requested_at: str


@router.get("/export")
async def export(
    ctx: TenantContext = Depends(require_role(UserRole.OWNER)),
    session: AsyncSession = Depends(get_session),
):
    """Right of access: a complete, machine-readable copy of the tenant's data."""
    calls = (
        await session.execute(
            select(Call).where(Call.tenant_id == ctx.tenant_id).order_by(Call.started_at)
        )
    ).scalars().all()
    turns = (
        await session.execute(
            select(Turn).join(Call).where(Call.tenant_id == ctx.tenant_id).order_by(Turn.created_at)
        )
    ).scalars().all()
    leads = (
        await session.execute(select(Lead).where(Lead.tenant_id == ctx.tenant_id))
    ).scalars().all()

    await record_audit(
        session,
        action=AuditAction.GDPR_EXPORT,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        detail={"calls": len(calls)},
    )

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tenant": {"id": str(ctx.tenant_id), "name": ctx.tenant.name},
        "calls": [
            {
                "id": str(c.id),
                "from": c.from_number,
                "to": c.to_number,
                "status": c.status.value,
                "started_at": c.started_at.isoformat() if c.started_at else None,
                "duration_seconds": c.duration_seconds,
                "summary": c.summary,
            }
            for c in calls
        ],
        "transcripts": [
            {
                "call_id": str(t.call_id),
                "speaker": t.speaker.value if hasattr(t.speaker, "value") else str(t.speaker),
                "text": t.text,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in turns
        ],
        "leads": [
            {
                "id": str(lead.id),
                "name": lead.name,
                "phone": lead.phone,
                "email": lead.email,
                "status": lead.status.value,
                "notes": lead.notes,
            }
            for lead in leads
        ],
    }


@router.post("/erasure", response_model=ErasureResult)
async def erasure(
    ctx: TenantContext = Depends(require_role(UserRole.OWNER)),
    session: AsyncSession = Depends(get_session),
):
    """Right to erasure: delete transcripts, calls and leads for the tenant.

    The tenant row itself is retained (billing/audit obligations) but marked
    with ``erasure_requested_at`` so the operator can complete account closure.
    """
    call_ids = select(Call.id).where(Call.tenant_id == ctx.tenant_id)
    turns_deleted = await session.execute(
        delete(Turn).where(Turn.call_id.in_(call_ids))
    )
    calls_deleted = await session.execute(delete(Call).where(Call.tenant_id == ctx.tenant_id))
    leads_deleted = await session.execute(delete(Lead).where(Lead.tenant_id == ctx.tenant_id))

    ctx.tenant.erasure_requested_at = datetime.now(timezone.utc)
    await record_audit(
        session,
        action=AuditAction.GDPR_ERASURE,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        detail={},
        commit=False,
    )
    await session.commit()

    return ErasureResult(
        ok=True,
        purged_calls=calls_deleted.rowcount or 0,
        purged_turns=turns_deleted.rowcount or 0,
        purged_leads=leads_deleted.rowcount or 0,
        erasure_requested_at=ctx.tenant.erasure_requested_at.isoformat(),
    )


@router.get("/status")
async def status(
    ctx: TenantContext = Depends(require_role(UserRole.OWNER)),
):
    """Consent + erasure state for the tenant's compliance dashboard."""
    disclosure = compliance_summary(
        greeting=ctx.tenant.greeting,
        disclosure_required=settings.ai_disclosure_required,
    )
    return {
        "erasure_requested_at": (
            ctx.tenant.erasure_requested_at.isoformat()
            if ctx.tenant.erasure_requested_at
            else None
        ),
        "data_consent_recorded_at": (
            ctx.tenant.data_consent_recorded_at.isoformat()
            if ctx.tenant.data_consent_recorded_at
            else None
        ),
        "data_consent_source": ctx.tenant.data_consent_source,
        "ai_disclosure": disclosure,
    }
