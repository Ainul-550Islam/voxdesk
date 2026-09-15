"""Step 9/1 — retention deletion of expired calls and transcripts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.retention import prune_webhook_receipts, purge_expired_calls
from app.db.models import (
    BillingProviderType,
    BillingWebhookReceipt,
    CalendarProviderType,
    CalendarWebhookReceipt,
    Call,
    CrmProviderType,
    CrmWebhookReceipt,
    MessageWebhookReceipt,
)
from tests.conftest import make_tenant
from tests.test_transfer import seed_call


async def _age_call(db, tenant, started_at):
    call = await seed_call(db, tenant)
    call.started_at = started_at
    await db.commit()
    return call


async def _call_count(db, tenant) -> int:
    rows = (
        await db.execute(select(Call).where(Call.tenant_id == tenant.id))
    ).scalars().all()
    return len(rows)


@pytest.mark.asyncio
async def test_recent_calls_are_kept(db):
    tenant = await make_tenant(db, "Recent Co")
    await _age_call(db, tenant, datetime.now(timezone.utc) - timedelta(days=5))

    summary = await purge_expired_calls(db, days=30)

    assert summary["purged_calls"] == 0
    assert await _call_count(db, tenant) == 1


@pytest.mark.asyncio
async def test_expired_calls_are_purged_with_their_turns(db):
    tenant = await make_tenant(db, "Old Co")
    old = datetime.now(timezone.utc) - timedelta(days=400)
    await _age_call(db, tenant, old)

    summary = await purge_expired_calls(db, days=365)

    assert summary["purged_calls"] == 1
    assert await _call_count(db, tenant) == 0


@pytest.mark.asyncio
async def test_purge_is_scoped_by_age_not_tenant(db):
    """One old call and one recent call in the same tenant: the recent call
    must survive a purge aimed at the old one."""
    tenant = await make_tenant(db, "Mixed Co")
    old = datetime.now(timezone.utc) - timedelta(days=400)
    recent = datetime.now(timezone.utc) - timedelta(days=5)
    await _age_call(db, tenant, old)
    await _age_call(db, tenant, recent)

    summary = await purge_expired_calls(db, days=365)

    assert summary["purged_calls"] == 1
    assert await _call_count(db, tenant) == 1


@pytest.mark.asyncio
async def test_repeated_purge_is_idempotent(db):
    tenant = await make_tenant(db, "Idem Co")
    old = datetime.now(timezone.utc) - timedelta(days=400)
    await _age_call(db, tenant, old)

    first = await purge_expired_calls(db, days=365)
    second = await purge_expired_calls(db, days=365)

    assert first["purged_calls"] == 1
    assert second["purged_calls"] == 0
    assert await _call_count(db, tenant) == 0


@pytest.mark.asyncio
async def test_scheduler_retention_loop_invokes_purge_and_prune_once_per_pass(monkeypatch):
    """The daily scheduler loop calls purge_expired_calls and
    prune_webhook_receipts exactly once per pass, then exits cleanly when
    signalled to stop."""
    import scripts.scheduler as scheduler

    from app.core import retention as retention_mod

    calls: list[str] = []

    async def fake_purge(session):
        calls.append("purge")

    async def fake_prune(session):
        calls.append("prune")

    async def fake_sleep(seconds: int) -> None:
        # Simulate shutdown after the first pass.
        scheduler._stop.set()

    class _FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc_info):
            return False

    class _FakeMaker:
        def __call__(self):
            return _FakeSessionContext()

    monkeypatch.setattr(retention_mod, "purge_expired_calls", fake_purge)
    monkeypatch.setattr(retention_mod, "prune_webhook_receipts", fake_prune)
    monkeypatch.setattr(scheduler, "get_sessionmaker", lambda: _FakeMaker())
    monkeypatch.setattr(scheduler, "_sleep", fake_sleep)

    scheduler._stop.clear()
    try:
        await scheduler.retention_loop()
    finally:
        scheduler._stop.clear()

    assert calls == ["purge", "prune"]


@pytest.mark.asyncio
async def test_prune_webhook_receipts_covers_all_four_tables(db):
    """The retention seam prunes every webhook-replay receipt table, keeping
    only rows inside the redelivery window."""
    tenant = await make_tenant(db, "Receipt Co")
    old = datetime.now(timezone.utc) - timedelta(days=400)
    fresh = datetime.now(timezone.utc)

    old_rows = [
        CrmWebhookReceipt(
            tenant_id=tenant.id, provider=CrmProviderType.WEBHOOK,
            provider_event_id="old-crm", event_type="contact",
        ),
        CalendarWebhookReceipt(
            tenant_id=tenant.id, provider=CalendarProviderType.GOOGLE,
            provider_event_id="old-cal",
        ),
        BillingWebhookReceipt(
            provider=BillingProviderType.STRIPE, provider_event_id="old-billing",
            event_type="checkout.session.completed", processed=True,
        ),
        MessageWebhookReceipt(
            tenant_id=tenant.id, channel="sms", provider_message_id="SM-old",
        ),
    ]
    fresh_rows = [
        CrmWebhookReceipt(
            tenant_id=tenant.id, provider=CrmProviderType.WEBHOOK,
            provider_event_id="fresh-crm", event_type="contact",
        ),
        CalendarWebhookReceipt(
            tenant_id=tenant.id, provider=CalendarProviderType.GOOGLE,
            provider_event_id="fresh-cal",
        ),
        BillingWebhookReceipt(
            provider=BillingProviderType.STRIPE,
            provider_event_id="fresh-billing",
            event_type="checkout.session.completed", processed=True,
        ),
        MessageWebhookReceipt(
            tenant_id=tenant.id, channel="sms", provider_message_id="SM-fresh",
        ),
    ]
    for row in old_rows:
        row.received_at = old
    for row in fresh_rows:
        row.received_at = fresh
    db.add_all(old_rows + fresh_rows)
    await db.commit()

    counts = await prune_webhook_receipts(db)

    assert counts == {"calendar": 1, "crm": 1, "billing": 1, "message": 1}

    from sqlalchemy import select

    assert len((await db.execute(select(CrmWebhookReceipt))).scalars().all()) == 1
    assert len((await db.execute(select(CalendarWebhookReceipt))).scalars().all()) == 1
    assert len((await db.execute(select(BillingWebhookReceipt))).scalars().all()) == 1
    assert len((await db.execute(select(MessageWebhookReceipt))).scalars().all()) == 1

    # The surviving rows are the fresh ones.
    kept_crm = (await db.execute(select(CrmWebhookReceipt))).scalars().one()
    assert kept_crm.provider_event_id == "fresh-crm"
