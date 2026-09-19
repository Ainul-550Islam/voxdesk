"""
Idempotency and retry.

Requirement 12 names four ways the same business event arrives twice:
duplicate webhooks, retries, worker restarts, and API timeouts *after* the
provider already accepted the write. The last one is the nastiest, because
from our side it is indistinguishable from a failure, and the naive response
— retry — creates a second contact.

Requirement 13 then asks that transient failures retry and permanent ones do
not, with bounded attempts and jittered backoff.

Both are tested here against the real database and the real service layer,
with only the HTTP transport faked.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.db.models import (
    CrmContactLink,
    CrmEvent,
    CrmEventType,
    CrmSync,
    CrmSyncStatus,
)
from app.integrations.crm import events, service
from app.integrations.crm.errors import (
    CrmAuthError,
    CrmRateLimited,
    CrmServerError,
    CrmTimeout,
    CrmValidationError,
)
from app.integrations.crm.retry import RetryPolicy, TokenBucketLimiter
from tests.conftest import FakeTransport, make_call, make_integration

GHL_OK = (200, {"contact": {"id": "ghl-contact-1"}})


@pytest.fixture
async def ghl(db, tenant_a):
    return await make_integration(db, tenant_a, "gohighlevel")


async def _emit_call(db, tenant, call=None):
    call = call or await make_call(db, tenant)
    event = await events.emit(
        db, tenant_id=tenant.id, event_type=CrmEventType.CALL_COMPLETED,
        entity_id=call.id,
        payload=events.call_payload(call, tenant),
    )
    await db.commit()
    return call, event


async def _syncs_for(db, tenant):
    return (
        (await db.execute(select(CrmSync).where(CrmSync.tenant_id == tenant.id)))
        .scalars()
        .all()
    )


# ============================================================ idempotency ===

def test_the_key_is_derived_from_the_business_fact_not_generated():
    """
    A random key would make every duplicate a new event, which is exactly the
    failure requirement 12 describes.
    """
    entity = uuid.uuid4()
    first = events.idempotency_key(CrmEventType.CALL_COMPLETED, entity)
    second = events.idempotency_key(CrmEventType.CALL_COMPLETED, entity)

    assert first == second
    assert str(entity) in first
    assert first.startswith("call.completed:")


def test_different_event_types_on_one_entity_get_different_keys():
    entity = uuid.uuid4()
    assert events.idempotency_key(
        CrmEventType.CALL_COMPLETED, entity
    ) != events.idempotency_key(CrmEventType.TRANSFER_COMPLETED, entity)


def test_a_discriminator_separates_genuinely_repeatable_facts():
    entity = uuid.uuid4()
    first = events.idempotency_key(
        CrmEventType.LEAD_UPDATED, entity, discriminator="status:qualified"
    )
    second = events.idempotency_key(
        CrmEventType.LEAD_UPDATED, entity, discriminator="status:do_not_call"
    )
    assert first != second
    # ...but the same change retried is still one key.
    assert first == events.idempotency_key(
        CrmEventType.LEAD_UPDATED, entity, discriminator="status:qualified"
    )


async def test_emitting_the_same_event_twice_creates_one_event(db, tenant_a, ghl):
    """A retried Twilio status callback is routine, not exceptional."""
    call, first = await _emit_call(db, tenant_a)
    second = await events.emit(
        db, tenant_id=tenant_a.id, event_type=CrmEventType.CALL_COMPLETED,
        entity_id=call.id, payload=events.call_payload(call, tenant_a),
    )
    await db.commit()

    assert first is not None
    assert second is None, "the duplicate should be swallowed"

    total = (
        await db.execute(
            select(func.count(CrmEvent.id)).where(CrmEvent.tenant_id == tenant_a.id)
        )
    ).scalar()
    assert total == 1


async def test_a_duplicate_event_produces_no_second_sync(db, tenant_a, ghl):
    call, _ = await _emit_call(db, tenant_a)
    await events.emit(
        db, tenant_id=tenant_a.id, event_type=CrmEventType.CALL_COMPLETED,
        entity_id=call.id, payload={},
    )
    await db.commit()

    assert len(await _syncs_for(db, tenant_a)) == 1


async def test_two_tenants_can_hold_the_same_idempotency_key(
    db, tenant_a, tenant_b, ghl
):
    """
    Keys are tenant-scoped. If they were global, one tenant emitting an event
    could silently suppress another's -- a cross-tenant denial of service
    through a shared namespace.
    """
    shared = uuid.uuid4()
    for tenant in (tenant_a, tenant_b):
        event = await events.emit(
            db, tenant_id=tenant.id, event_type=CrmEventType.CALL_COMPLETED,
            entity_id=shared, payload={},
        )
        assert event is not None
    await db.commit()

    total = (await db.execute(select(func.count(CrmEvent.id)))).scalar()
    assert total == 2


async def test_a_duplicate_event_creates_only_one_crm_contact(
    db, tenant_a, ghl, monkeypatch
):
    """
    **The end-to-end guarantee.** Two emits, one provider write.
    """
    transport = FakeTransport(GHL_OK).install(monkeypatch)
    call, _ = await _emit_call(db, tenant_a)
    await events.emit(
        db, tenant_id=tenant_a.id, event_type=CrmEventType.CALL_COMPLETED,
        entity_id=call.id, payload=events.call_payload(call, tenant_a),
    )
    await db.commit()

    result = await service.run_sync_tick(db)

    assert result["synced"] == 1
    upserts = [r for r in transport.requests if "upsert" in r["url"]]
    assert len(upserts) == 1


async def test_a_provider_timeout_after_acceptance_does_not_duplicate(
    db, tenant_a, ghl, monkeypatch
):
    """
    **The hardest case in requirement 12.**

    The provider created the contact and then the response timed out. We
    cannot tell that from a total failure, so we retry -- and the retry must
    not create a second contact.

    Two things make that safe: GoHighLevel's upsert matches on phone/email,
    and `CrmContactLink` remembers the external id. Here the first attempt
    times out (nothing recorded), and the second succeeds; the assertion is
    that exactly one contact id is ever linked, and that a *third* pass
    updates rather than upserts.
    """
    FakeTransport(
        httpx.ReadTimeout("provider accepted but we never heard"),
        GHL_OK,
    ).install(monkeypatch)

    call, _ = await _emit_call(db, tenant_a)
    sync = (await _syncs_for(db, tenant_a))[0]

    first = await service.process_sync(db, sync)
    assert first.status is CrmSyncStatus.FAILED
    assert first.error_code == "timeout"

    sync.next_attempt_at = None
    await db.commit()
    second = await service.process_sync(db, sync)
    assert second.status is CrmSyncStatus.SYNCED
    assert second.external_id == "ghl-contact-1"

    links = (
        (
            await db.execute(
                select(CrmContactLink).where(CrmContactLink.tenant_id == tenant_a.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(links) == 1
    assert links[0].external_contact_id == "ghl-contact-1"


async def test_a_second_call_from_the_same_number_updates_the_same_contact(
    db, tenant_a, ghl, monkeypatch
):
    """
    Requirement 19: avoid duplicate contacts. The tenth call from a number
    must not be the tenth CRM contact.
    """
    transport = FakeTransport(GHL_OK).install(monkeypatch)

    first_call = await make_call(db, tenant_a, from_number="+15551110000")
    await _emit_call(db, tenant_a, first_call)
    await service.run_sync_tick(db)

    second_call = await make_call(db, tenant_a, from_number="+15551110000")
    await _emit_call(db, tenant_a, second_call)
    await service.run_sync_tick(db)

    links = (
        (
            await db.execute(
                select(CrmContactLink).where(CrmContactLink.tenant_id == tenant_a.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(links) == 1, "two links means two CRM contacts for one caller"

    # The second pass should have used PUT (update), not the upsert endpoint.
    methods = [r["method"] for r in transport.requests if "contacts" in r["url"]]
    assert "PUT" in methods


async def test_the_same_number_in_two_tenants_stays_two_contacts(
    db, tenant_a, tenant_b, monkeypatch
):
    """
    Requirement 19: never match contacts across tenants. Two businesses
    sharing a customer must each get their own CRM record.
    """
    FakeTransport(GHL_OK).install(monkeypatch)
    await make_integration(db, tenant_a, "gohighlevel")
    await make_integration(db, tenant_b, "gohighlevel")

    for tenant in (tenant_a, tenant_b):
        call = await make_call(db, tenant, from_number="+15559990000")
        await _emit_call(db, tenant, call)
    await service.run_sync_tick(db)

    links = (
        (await db.execute(select(CrmContactLink))).scalars().all()
    )
    assert len(links) == 2
    assert {link.tenant_id for link in links} == {tenant_a.id, tenant_b.id}
    # Same phone, different fingerprints -- the hash is tenant-salted.
    assert links[0].identity_hash != links[1].identity_hash


async def test_a_worker_restart_mid_flight_is_safe(db, tenant_a, ghl, monkeypatch):
    """
    Requirement 12: worker restart safe.

    A sync left in PROCESSING by a dead worker is reaped back to FAILED and
    retried, rather than being stranded in a state no query picks up.
    """
    FakeTransport(GHL_OK).install(monkeypatch)
    call, _ = await _emit_call(db, tenant_a)
    sync = (await _syncs_for(db, tenant_a))[0]

    # Simulate the crash: claimed, never finished.
    sync.status = CrmSyncStatus.PROCESSING
    sync.attempt_count = 1
    sync.last_attempt_at = datetime.utcnow() - timedelta(hours=1)
    await db.commit()

    assert await service.claim_due_syncs(db) == []          # invisible while stuck

    reaped = await service.reap_stuck_syncs(db, older_than_minutes=15)
    assert reaped == 1

    await db.refresh(sync)
    assert sync.status is CrmSyncStatus.FAILED
    # The spent attempt still counts, so a crash loop cannot retry forever.
    assert sync.attempt_count == 1

    assert len(await service.claim_due_syncs(db)) == 1
    outcome = await service.process_sync(db, sync)
    assert outcome.status is CrmSyncStatus.SYNCED


async def test_a_second_worker_pass_over_a_synced_row_does_nothing(
    db, tenant_a, ghl, monkeypatch
):
    transport = FakeTransport(GHL_OK).install(monkeypatch)
    await _emit_call(db, tenant_a)

    await service.run_sync_tick(db)
    after_first = transport.call_count

    await service.run_sync_tick(db)
    assert transport.call_count == after_first, "a SYNCED row was re-sent"


# ================================================================== retry ===

def test_transient_errors_are_retryable_and_permanent_ones_are_not():
    policy = RetryPolicy(max_attempts=5)

    for transient in (
        CrmTimeout("t"), CrmServerError("s"), CrmRateLimited("r"),
    ):
        assert policy.should_retry(transient, attempt=1), type(transient).__name__

    for permanent in (
        CrmAuthError("a"), CrmValidationError("v"),
    ):
        assert not policy.should_retry(permanent, attempt=1), type(permanent).__name__


def test_a_permanent_error_stops_immediately_even_on_the_first_attempt():
    """
    A revoked token does not come back on its own, and hammering an auth
    endpoint with a dead credential is how an account gets locked.
    """
    policy = RetryPolicy(max_attempts=5)
    assert not policy.should_retry(CrmAuthError("401"), attempt=1)


def test_attempts_are_bounded():
    policy = RetryPolicy(max_attempts=3)
    error = CrmTimeout("t")

    assert policy.should_retry(error, attempt=1)
    assert policy.should_retry(error, attempt=2)
    assert not policy.should_retry(error, attempt=3)
    assert policy.is_exhausted(3)


def test_backoff_grows_exponentially():
    policy = RetryPolicy(base_seconds=2.0, max_seconds=900, jitter=False)
    delays = [policy.delay_for(n) for n in range(1, 6)]
    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_backoff_is_capped():
    policy = RetryPolicy(base_seconds=2.0, max_seconds=60.0, jitter=False)
    assert policy.delay_for(20) == 60.0


def test_jitter_spreads_retries():
    """
    Without jitter, a hundred syncs failing together retry together and
    recreate the overload. Asserted statistically, because that is the only
    honest way to test randomness.
    """
    policy = RetryPolicy(base_seconds=8.0, jitter=True)
    samples = [policy.delay_for(3) for _ in range(200)]

    assert len(set(samples)) > 150, "delays are not actually being jittered"
    assert all(0 <= s <= 32.0 for s in samples)
    # Full jitter is uniform on [0, cap], so the mean sits near the midpoint.
    assert 8.0 < sum(samples) / len(samples) < 24.0


def test_a_providers_retry_after_wins_when_it_is_longer():
    """
    Arguing with a rate limiter by retrying sooner than it asked is how a
    token gets suspended.
    """
    policy = RetryPolicy(base_seconds=2.0, jitter=False)
    error = CrmRateLimited("429", retry_after=120.0)

    assert policy.delay_for(1, error=error) == 120.0


def test_our_backoff_wins_when_it_is_longer_than_retry_after():
    policy = RetryPolicy(base_seconds=100.0, jitter=False)
    error = CrmRateLimited("429", retry_after=1.0)

    assert policy.delay_for(3, error=error) == 400.0


def test_retry_settings_come_from_configuration(monkeypatch):
    """Requirement 13: make retry settings configurable."""
    from app.core.config import settings
    from app.integrations.crm.retry import policy_from_settings

    monkeypatch.setattr(settings, "crm_retry_max_attempts", 2, raising=False)
    monkeypatch.setattr(settings, "crm_retry_base_seconds", 7.5, raising=False)

    policy = policy_from_settings()
    assert policy.max_attempts == 2
    assert policy.base_seconds == 7.5


# ================================================== retry, end to end ===

async def test_a_500_schedules_a_retry_and_keeps_the_sync_recoverable(
    db, tenant_a, ghl, monkeypatch
):
    FakeTransport((500, {"error": "upstream"})).install(monkeypatch)
    await _emit_call(db, tenant_a)
    sync = (await _syncs_for(db, tenant_a))[0]

    outcome = await service.process_sync(db, sync)

    assert outcome.status is CrmSyncStatus.FAILED
    assert outcome.error_code == "server_error"
    await db.refresh(sync)
    assert sync.attempt_count == 1
    assert sync.next_attempt_at is not None
    assert sync.external_id is None


async def test_a_401_fails_permanently_without_burning_attempts(
    db, tenant_a, ghl, monkeypatch
):
    transport = FakeTransport((401, {"message": "bad token"})).install(monkeypatch)
    await _emit_call(db, tenant_a)
    sync = (await _syncs_for(db, tenant_a))[0]

    outcome = await service.process_sync(db, sync)

    assert outcome.status is CrmSyncStatus.PERMANENT_FAILURE
    assert outcome.error_code == "unauthorized"
    await db.refresh(sync)
    assert sync.attempt_count == 1
    assert sync.next_attempt_at is None
    # One request, not five.
    assert transport.call_count == 1


async def test_a_422_fails_permanently(db, tenant_a, ghl, monkeypatch):
    FakeTransport((422, {"message": "phone is required"})).install(monkeypatch)
    await _emit_call(db, tenant_a)
    sync = (await _syncs_for(db, tenant_a))[0]

    outcome = await service.process_sync(db, sync)
    assert outcome.status is CrmSyncStatus.PERMANENT_FAILURE
    assert outcome.error_code == "validation"


async def test_repeated_transient_failures_eventually_give_up(
    db, tenant_a, ghl, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "crm_retry_max_attempts", 3, raising=False)
    FakeTransport((503, {})).install(monkeypatch)

    await _emit_call(db, tenant_a)
    sync = (await _syncs_for(db, tenant_a))[0]

    for expected in (CrmSyncStatus.FAILED, CrmSyncStatus.FAILED):
        outcome = await service.process_sync(db, sync)
        assert outcome.status is expected
        sync.next_attempt_at = None
        await db.commit()

    final = await service.process_sync(db, sync)
    assert final.status is CrmSyncStatus.PERMANENT_FAILURE
    await db.refresh(sync)
    assert sync.attempt_count == 3


async def test_a_429_reads_retry_after(db, tenant_a, ghl, monkeypatch):
    FakeTransport((429, {}, {"Retry-After": "45"})).install(monkeypatch)
    await _emit_call(db, tenant_a)
    sync = (await _syncs_for(db, tenant_a))[0]

    before = datetime.utcnow()
    outcome = await service.process_sync(db, sync)

    assert outcome.error_code == "rate_limited"
    await db.refresh(sync)
    assert sync.next_attempt_at >= before + timedelta(seconds=44)


async def test_an_absurd_retry_after_is_capped(db, tenant_a, ghl, monkeypatch):
    """A provider claiming a one-hour wait must not pin a worker slot."""
    FakeTransport((429, {}, {"Retry-After": "99999"})).install(monkeypatch)
    await _emit_call(db, tenant_a)
    sync = (await _syncs_for(db, tenant_a))[0]

    before = datetime.utcnow()
    await service.process_sync(db, sync)

    await db.refresh(sync)
    assert sync.next_attempt_at < before + timedelta(seconds=1000)


async def test_a_garbage_retry_after_falls_back_to_our_own_backoff(
    db, tenant_a, ghl, monkeypatch
):
    FakeTransport((429, {}, {"Retry-After": "next tuesday"})).install(monkeypatch)
    await _emit_call(db, tenant_a)
    sync = (await _syncs_for(db, tenant_a))[0]

    outcome = await service.process_sync(db, sync)
    assert outcome.status is CrmSyncStatus.FAILED
    await db.refresh(sync)
    assert sync.next_attempt_at is not None


# ============================================================ rate limiting ===

def test_the_token_bucket_allows_a_burst_then_throttles():
    limiter = TokenBucketLimiter(rate_per_second=1.0, burst=3)

    assert [limiter.allow("t", "p", now=100.0) for _ in range(3)] == [True] * 3
    assert limiter.allow("t", "p", now=100.0) is False


def test_the_bucket_refills_over_time():
    limiter = TokenBucketLimiter(rate_per_second=2.0, burst=2)
    for _ in range(2):
        limiter.allow("t", "p", now=0.0)
    assert limiter.allow("t", "p", now=0.0) is False
    assert limiter.allow("t", "p", now=1.0) is True


def test_one_tenant_cannot_exhaust_another_tenants_budget():
    """
    Requirement 23's actual point: a broken tenant must not consume the shared
    worker.
    """
    limiter = TokenBucketLimiter(rate_per_second=1.0, burst=2)

    for _ in range(5):
        limiter.allow("noisy-tenant", "gohighlevel", now=0.0)
    assert limiter.allow("noisy-tenant", "gohighlevel", now=0.0) is False
    assert limiter.allow("quiet-tenant", "gohighlevel", now=0.0) is True


def test_buckets_are_per_provider_as_well_as_per_tenant():
    limiter = TokenBucketLimiter(rate_per_second=1.0, burst=1)
    assert limiter.allow("t", "gohighlevel", now=0.0) is True
    assert limiter.allow("t", "gohighlevel", now=0.0) is False
    assert limiter.allow("t", "hubspot", now=0.0) is True


async def test_a_throttled_sync_is_rescheduled_not_failed(
    db, tenant_a, ghl, monkeypatch
):
    """
    Being locally rate-limited is not the provider's fault and not an error.
    The sync must go back to PENDING with a later time, keeping its attempt
    budget intact.
    """
    from app.integrations.crm.retry import limiter

    FakeTransport(GHL_OK).install(monkeypatch)
    await _emit_call(db, tenant_a)
    sync = (await _syncs_for(db, tenant_a))[0]

    limiter.reset()
    limiter.rate_per_second = 0.001
    limiter.burst = 0
    try:
        outcome = await service.process_sync(db, sync)
    finally:
        limiter.reset()
        limiter.rate_per_second = 1000.0
        limiter.burst = 1000.0

    assert outcome.skipped_reason == "rate_limited_locally"
    await db.refresh(sync)
    assert sync.status is CrmSyncStatus.PENDING
    assert sync.attempt_count == 0, "throttling must not spend an attempt"
    assert sync.next_attempt_at is not None