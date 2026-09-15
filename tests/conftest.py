"""
Shared fixtures for the auth / RBAC integration tests.

These are real integration tests: a real FastAPI app, a real router stack, a
real SQLAlchemy session against an in-memory database, real bcrypt hashing and
real JWT signing. The authorization layer is never mocked -- mocking it would
make the tenant-isolation tests prove nothing.
"""
from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import password as pw
from app.core.config import settings
from app.db.models import Base, Tenant, User, UserRole
from app.db.session import get_session

# bcrypt at 12 rounds costs ~250ms per hash. The suite creates dozens of
# users, so drop the cost for tests only -- the algorithm under test is
# unchanged, only the work factor.
pw.BCRYPT_ROUNDS = 4

TEST_PASSWORD = "Correct-Horse-Battery-9!"


@pytest.fixture(autouse=True)
def _twilio_webhook_bypass_for_route_tests():
    """Twilio webhook routes are exercised here with plain form posts and no
    X-Twilio-Signature. The harness runs under development configuration, so it
    enables the explicit dev-only bypass flag (TWILIO_SKIP_WEBHOOK_VERIFY).
    Tests that assert the fail-closed behaviour disable the flag with
    monkeypatch."""
    previous = settings.twilio_skip_webhook_verify
    settings.twilio_skip_webhook_verify = True
    yield
    settings.twilio_skip_webhook_verify = previous


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker_(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(sessionmaker_) -> AsyncSession:
    async with sessionmaker_() as session:
        yield session


@pytest_asyncio.fixture
async def app(sessionmaker_):
    """The real application, with only the database dependency redirected."""
    from app.main import app as fastapi_app

    async def _override():
        async with sessionmaker_() as session:
            yield session

    fastapi_app.dependency_overrides[get_session] = _override
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ------------------------------------------------------------- factories ---

async def make_tenant(db: AsyncSession, name: str = "Acme Dental") -> Tenant:
    tenant = Tenant(
        name=name,
        twilio_number=f"+1555{uuid.uuid4().int % 10**7:07d}",
        business_open=time(9, 0),
        business_close=time(17, 0),
        outbound_window_open=time(9, 0),
        outbound_window_close=time(20, 0),
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


async def make_user(
    db: AsyncSession,
    tenant: Tenant,
    role: UserRole,
    *,
    email: str | None = None,
    active: bool = True,
    password: str = TEST_PASSWORD,
) -> User:
    user = User(
        tenant_id=tenant.id,
        email=email or f"{role.value}-{uuid.uuid4().hex[:8]}@example.com",
        full_name=f"Test {role.value}",
        password_hash=pw.hash_password(password),
        role=role,
        is_active=active,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def login(client: AsyncClient, email: str, password: str = TEST_PASSWORD):
    return await client.post("/auth/login", json={"email": email, "password": password})


async def auth_headers(client: AsyncClient, user: User, password: str = TEST_PASSWORD):
    resp = await login(client, user.email, password)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest_asyncio.fixture
async def tenant_a(db):
    return await make_tenant(db, "Tenant A")


@pytest_asyncio.fixture
async def tenant_b(db):
    return await make_tenant(db, "Tenant B")


@pytest_asyncio.fixture
async def owner_a(db, tenant_a):
    return await make_user(db, tenant_a, UserRole.OWNER)


@pytest_asyncio.fixture
async def admin_a(db, tenant_a):
    return await make_user(db, tenant_a, UserRole.ADMIN)


@pytest_asyncio.fixture
async def manager_a(db, tenant_a):
    return await make_user(db, tenant_a, UserRole.MANAGER)


@pytest_asyncio.fixture
async def agent_a(db, tenant_a):
    return await make_user(db, tenant_a, UserRole.AGENT)


@pytest_asyncio.fixture
async def viewer_a(db, tenant_a):
    return await make_user(db, tenant_a, UserRole.VIEWER)


@pytest_asyncio.fixture
async def owner_b(db, tenant_b):
    return await make_user(db, tenant_b, UserRole.OWNER)


# ------------------------------------------------------- STEP 4: knowledge ---
#
# The knowledge tests are integration tests too. Real extraction of real PDF
# and DOCX bytes, real chunking, real embedding, real SQL. The only thing
# swapped out is object storage, which points at a temp directory so a test
# run cannot touch a developer's ./var/knowledge, and cannot leak files
# between tests.

@pytest_asyncio.fixture(autouse=True)
async def knowledge_storage(tmp_path):
    """Point document storage at a per-test temp directory."""
    from app.knowledge.storage import set_storage
    from app.knowledge.storage.local import LocalStorage

    set_storage(LocalStorage(str(tmp_path / "knowledge")))
    yield
    set_storage(None)


@pytest_asyncio.fixture(autouse=True)
async def knowledge_embedder():
    """
    Reset the cached embedder around every test.

    It is a process-level singleton, so a test that overrides the provider
    would otherwise poison every test that ran after it.
    """
    from app.knowledge.embeddings import reset_embedder

    reset_embedder()
    yield
    reset_embedder()


async def add_document(
    db: AsyncSession,
    tenant,
    *,
    text: str | bytes = "",
    filename: str = "notes.md",
    title: str | None = None,
    process: bool = True,
):
    """
    Upload and (by default) fully index a document, returning the row.

    Goes through the real `create_document` / `process_document` path rather
    than inserting chunks directly -- a fixture that fabricates chunks would
    let a broken pipeline pass the retrieval tests.
    """
    from app.knowledge import ingest

    data = text.encode() if isinstance(text, str) else text
    result = await ingest.create_document(
        db, tenant_id=tenant.id, data=data, filename=filename, title=title
    )
    if process:
        result = await ingest.process_document(db, result.document)
    return result.document


# ===========================================================================
# STEP 5 -- CRM integration fixtures
# ===========================================================================

@pytest.fixture(autouse=True)
def crm_encryption_key(monkeypatch):
    """
    A real AES-256 key for every test.

    Autouse and real rather than opt-in and faked, because "credentials are
    encrypted" is a property the tests must be able to assert against actual
    ciphertext. A stub cipher would let a bug that stores plaintext pass.
    """
    from app.core.config import settings
    from app.integrations.crm.crypto import generate_key

    monkeypatch.setattr(
        settings, "crm_encryption_keys", f"test-key:{generate_key()}", raising=False
    )
    yield


@pytest.fixture(autouse=True)
def crm_limiter_reset():
    """
    The token bucket is a module global, so one test exhausting it would
    starve the next. Reset around every test.
    """
    from app.integrations.crm.retry import limiter

    limiter.reset()
    limiter.rate_per_second = 1000.0
    limiter.burst = 1000.0
    yield
    limiter.reset()


async def make_integration(
    db: AsyncSession,
    tenant,
    provider="webhook",
    *,
    credentials: dict | None = None,
    config: dict | None = None,
    enabled: bool = True,
    field_mappings: dict | None = None,
    subscribed_events: list | None = None,
    share_transcripts: bool = False,
):
    """
    Create a CRM integration with genuinely encrypted credentials.

    Goes through `crypto.encrypt_credentials` rather than writing the column
    directly, so every test that reads credentials back is exercising the real
    envelope, key id and AAD binding.
    """
    from app.core.config import settings
    from app.db.models import CrmIntegration, CrmProviderType
    from app.integrations.crm import crypto

    provider_type = (
        provider if isinstance(provider, CrmProviderType)
        else CrmProviderType(provider)
    )

    defaults = {
        CrmProviderType.WEBHOOK: (
            {"signing_secret": "test-signing-secret-0123456789abcdef"},
            {"url": "https://hooks.example.com/voxdesk"},
        ),
        CrmProviderType.GOHIGHLEVEL: (
            {"access_token": "pit-test-token-abcdef123456"},
            {"location_id": "loc_test_123", "base_url": "https://ghl.test"},
        ),
        CrmProviderType.HUBSPOT: (
            {"access_token": "pat-na1-test-token-abcdef"},
            {"base_url": "https://hubspot.test"},
        ),
        CrmProviderType.JOBBER: (
            {"access_token": "jobber-test-token-abcdef"},
            {"base_url": "https://jobber.test/api/graphql"},
        ),
    }
    default_credentials, default_config = defaults[provider_type]

    integration = CrmIntegration(
        tenant_id=tenant.id,
        provider=provider_type,
        is_enabled=enabled,
        config=config if config is not None else dict(default_config),
        field_mappings=field_mappings or {},
        subscribed_events=subscribed_events or [],
        share_transcripts=share_transcripts,
    )

    bundle = credentials if credentials is not None else default_credentials
    if bundle:
        key_ring = crypto.parse_key_ring(settings.crm_encryption_keys)
        envelope, key_id = crypto.encrypt_credentials(
            bundle, tenant_id=str(tenant.id), provider=provider_type.value,
            key_ring=key_ring,
        )
        integration.credentials_encrypted = envelope
        integration.credentials_key_id = key_id
        integration.credentials_updated_at = datetime.utcnow()

    db.add(integration)
    await db.commit()
    await db.refresh(integration)
    return integration


async def make_call(db: AsyncSession, tenant, **overrides):
    """A completed inbound call, ready to emit a CRM event about."""
    from app.db.models import Call, CallDirection, CallStatus

    fields = {
        "tenant_id": tenant.id,
        "direction": CallDirection.INBOUND,
        "status": CallStatus.COMPLETED,
        "from_number": "+15551230000",
        "to_number": tenant.twilio_number,
        "call_sid": f"CA{uuid.uuid4().hex}",
        "started_at": datetime.utcnow(),
        "duration_seconds": 92.5,
        "intent": "booking",
        "summary": "Caller asked about a cleaning and booked Tuesday.",
        "booked": True,
        "lead_score": 80,
    }
    fields.update(overrides)
    call = Call(**fields)
    db.add(call)
    await db.commit()
    await db.refresh(call)
    return call


async def make_lead(db: AsyncSession, tenant, **overrides):
    from app.db.models import Lead

    fields = {
        "tenant_id": tenant.id,
        "name": "Jane Doe",
        "phone": "+15557778888",
        "email": "jane@example.com",
        "company": "Doe Plumbing",
        "notes": "Called about a burst pipe.",
    }
    fields.update(overrides)
    lead = Lead(**fields)
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return lead


async def make_appointment(db: AsyncSession, tenant, **overrides):
    from app.db.models import Appointment

    starts = datetime.utcnow() + timedelta(days=1)
    fields = {
        "tenant_id": tenant.id,
        "customer_name": "Jane Doe",
        "customer_phone": "+15557778888",
        "reason": "Drain cleaning",
        "starts_at": starts,
        "ends_at": starts + timedelta(minutes=30),
    }
    fields.update(overrides)
    appointment = Appointment(**fields)
    db.add(appointment)
    await db.commit()
    await db.refresh(appointment)
    return appointment


class FakeTransport:
    """
    A scripted HTTP transport for provider adapters.

    Adapters call `httpx.AsyncClient(...).request(...)` inside
    `CrmProvider.request`. Patching at the `httpx` layer rather than stubbing
    the adapter method means the tests exercise the real header assembly, the
    real timeout handling and the real status classification -- which is the
    part most likely to be wrong.

    `responses` is a list of either `(status, json_body)` tuples or exception
    instances to raise. They are consumed in order, and the last one repeats
    so a retry test does not have to enumerate every attempt.
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self._index = 0

    def _next(self):
        if self._index < len(self.responses):
            item = self.responses[self._index]
            self._index += 1
            return item
        return self.responses[-1] if self.responses else (200, {})

    def install(self, monkeypatch):
        import httpx

        transport = self
        original_request = httpx.AsyncClient.request

        async def fake_request(self_client, method, url, **kwargs):
            # The `client` fixture is also an httpx.AsyncClient -- one wired to
            # the ASGI app. Patching the class would swallow the test's own
            # requests to VoxDesk and answer them with a provider's canned
            # response, which produces baffling 401s in API tests. Only
            # intercept clients talking to the outside world.
            if isinstance(
                getattr(self_client, "_transport", None), httpx.ASGITransport
            ):
                return await original_request(self_client, method, url, **kwargs)

            transport.requests.append({
                "method": method,
                "url": str(url),
                "json": kwargs.get("json"),
                # Stripe is form-encoded, so a test asserting on what was sent
                # has to see `data` as well as `json`.
                "form": kwargs.get("data"),
                "params": kwargs.get("params"),
                "headers": dict(kwargs.get("headers") or {}),
            })
            item = transport._next()
            if isinstance(item, Exception):
                raise item
            status, body, *rest = (*item, None)
            headers = rest[0] if rest and rest[0] else {}
            return httpx.Response(
                status_code=status,
                json=body if body is not None else {},
                headers=headers,
                request=httpx.Request(method, url),
            )

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        return self

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def last(self) -> dict:
        return self.requests[-1]


# ===========================================================================
# STEP 6 -- calendar / scheduling fixtures
# ===========================================================================

async def make_calendar_integration(
    db: AsyncSession,
    tenant,
    provider="internal",
    *,
    credentials: dict | None = None,
    config: dict | None = None,
    enabled: bool = True,
    primary: bool = True,
):
    """
    Connect a calendar provider with genuinely encrypted credentials.

    Goes through `crypto.encrypt_credentials` rather than writing the column,
    so every test that reads credentials back exercises the real envelope,
    key id and `(tenant, provider)` AAD binding.
    """
    from app.core.config import settings
    from app.db.models import CalendarIntegration, CalendarProviderType
    from app.integrations.crm import crypto

    provider_type = (
        provider if isinstance(provider, CalendarProviderType)
        else CalendarProviderType(provider)
    )

    defaults = {
        CalendarProviderType.GOOGLE: (
            {
                "access_token": "ya29-test-token-abcdef",
                "refresh_token": "1//test-refresh-abcdef",
                "client_id": "test-client-id",
                "client_secret": "test-client-secret",
            },
            {"calendar_id": "primary", "base_url": "https://gcal.test/v3"},
        ),
        CalendarProviderType.MICROSOFT: (
            {
                "access_token": "eyJ0test.microsoft.token",
                "refresh_token": "M.R3-test-refresh",
                "client_id": "test-client-id",
                "client_secret": "test-client-secret",
            },
            {"mailbox": "diary@example.com", "base_url": "https://graph.test/v1.0"},
        ),
        CalendarProviderType.CALCOM: (
            {"api_key": "cal_live_test_abcdef123456"},
            {"event_type_id": 4242, "base_url": "https://cal.test/v2"},
        ),
        CalendarProviderType.GOOGLE_SERVICE_ACCOUNT: (
            {}, {"calendar_id": "shared@example.com"},
        ),
        CalendarProviderType.INTERNAL: ({}, {}),
    }
    default_credentials, default_config = defaults[provider_type]

    integration = CalendarIntegration(
        tenant_id=tenant.id,
        provider=provider_type,
        is_enabled=enabled,
        is_primary=primary,
        config=config if config is not None else dict(default_config),
    )

    bundle = credentials if credentials is not None else default_credentials
    if bundle:
        key_ring = crypto.parse_key_ring(settings.crm_encryption_keys)
        envelope, key_id = crypto.encrypt_credentials(
            bundle, tenant_id=str(tenant.id), provider=provider_type.value,
            key_ring=key_ring,
        )
        integration.credentials_encrypted = envelope
        integration.credentials_key_id = key_id
        integration.credentials_updated_at = datetime.utcnow()

    db.add(integration)
    await db.commit()
    await db.refresh(integration)
    return integration


async def make_scheduling_policy(db: AsyncSession, tenant, **overrides):
    """A policy row. Defaults to weekdays 09:00-17:00 with a lunch break."""
    from app.db.models import SchedulingPolicy

    fields = {
        "tenant_id": tenant.id,
        "weekly_hours": {
            "mon": [["09:00", "12:00"], ["13:00", "17:00"]],
            "tue": [["09:00", "12:00"], ["13:00", "17:00"]],
            "wed": [["09:00", "12:00"], ["13:00", "17:00"]],
            "thu": [["09:00", "12:00"], ["13:00", "17:00"]],
            "fri": [["09:00", "12:00"], ["13:00", "17:00"]],
            "sat": [],
            "sun": [],
        },
        "holidays": [],
        "blocked_periods": [],
        "slot_minutes": 30,
        "slot_interval_minutes": 30,
        "minimum_notice_minutes": 60,
        "booking_horizon_days": 60,
        "max_slots_offered": 3,
    }
    fields.update(overrides)
    policy = SchedulingPolicy(**fields)
    db.add(policy)
    await db.commit()
    await db.refresh(policy)
    return policy


def next_weekday(reference, weekday: int, *, weeks: int = 0):
    """The next occurrence of `weekday` strictly after `reference`'s date."""
    delta = (weekday - reference.weekday()) % 7 or 7
    return reference.date() + timedelta(days=delta + 7 * weeks)


@pytest_asyncio.fixture
async def concurrent_sessionmaker(tmp_path):
    """
    A sessionmaker whose sessions get genuinely separate connections.

    The default `engine` fixture uses `sqlite+aiosqlite:///:memory:`, which
    SQLAlchemy backs with a single shared connection -- so two "concurrent"
    sessions are really one, and one session's rollback tears down the
    other's uncommitted work. That is an artifact of the test database, not
    of the code, but it makes a genuine race impossible to test.

    A file-backed database gives each session its own connection, which is
    what PostgreSQL does in production. This is the only way to test
    requirement 8 for real rather than by mocking the lock.
    """
    from app.db.models import Base

    url = f"sqlite+aiosqlite:///{tmp_path}/concurrency.db"
    eng = create_async_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


# ===========================================================================
# STEP 7 -- billing fixtures
# ===========================================================================

@pytest_asyncio.fixture
async def billing_plans(db):
    """
    The plan catalogue, seeded for every test.

    Autouse because entitlements fall back to the default plan, so a test that
    never mentions billing still resolves one -- and a missing catalogue would
    make every entitlement check deny.

    Built with a single `add_all` and one commit rather than through
    `sync_seed_plans`, which does a read-modify-write per plan. That is the
    right shape for production (it must not clobber an operator's prices) and
    the wrong shape for a fixture that runs 1500 times: measured at ~60ms per
    test, which is 90 seconds across the suite. `sync_seed_plans` itself is
    covered by its own tests.
    """
    from app.billing.plans import SEED_PLANS, validate_feature_entitlements
    from app.db.models import BillingPlan

    db.add_all([
        BillingPlan(
            code=seed.code,
            name=seed.name,
            description=seed.description,
            is_active=True,
            display_order=seed.display_order,
            currency="usd",
            monthly_price_cents=seed.monthly_price_cents,
            annual_price_cents=seed.annual_price_cents,
            included_voice_minutes=seed.included_voice_minutes,
            included_sms_segments=seed.included_sms_segments,
            included_llm_tokens=seed.included_llm_tokens,
            included_tts_characters=seed.included_tts_characters,
            overage_voice_minute_millicents=seed.overage_voice_minute_millicents,
            overage_sms_millicents=seed.overage_sms_millicents,
            overage_llm_token_millicents=seed.overage_llm_token_millicents,
            overage_tts_character_millicents=seed.overage_tts_character_millicents,
            overage_enabled=seed.overage_enabled,
            trial_days=seed.trial_days,
            feature_entitlements=validate_feature_entitlements(
                seed.feature_entitlements
            ),
            # Obviously fake, so `resolve_price_id` has something to return
            # and no test has to invent its own.
            provider_price_ids={
                "month": f"price_FAKE_{seed.code}_month",
                "year": f"price_FAKE_{seed.code}_year",
            },
        )
        for seed in SEED_PLANS
    ])
    await db.commit()


@pytest.fixture(autouse=True)
def billing_provider_manual(monkeypatch):
    """
    Default every test to the manual provider.

    Manual is a real mode, not a stub: plans, entitlements, metering, periods
    and overage all work. A test that wants Stripe opts in with
    `stripe_settings`.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "billing_provider", "manual", raising=False)
    monkeypatch.setattr(
        settings, "billing_unlimited_entitlements", False, raising=False
    )
    monkeypatch.setattr(settings, "billing_enforce_entitlements", True, raising=False)
    yield


@pytest.fixture
def stripe_settings(monkeypatch):
    """Switch to the Stripe adapter with fake credentials."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "billing_provider", "stripe", raising=False)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_FAKE", raising=False)
    monkeypatch.setattr(
        settings, "stripe_webhook_secret", "whsec_FAKE_TEST_SECRET", raising=False
    )
    monkeypatch.setattr(
        settings, "billing_checkout_success_url", "https://app.test/ok", raising=False
    )
    monkeypatch.setattr(
        settings, "billing_checkout_cancel_url", "https://app.test/no", raising=False
    )
    monkeypatch.setattr(
        settings, "billing_portal_return_url", "https://app.test/back", raising=False
    )
    return "whsec_FAKE_TEST_SECRET"


async def subscribe(db: AsyncSession, tenant, plan_code="pro", **overrides):
    """Give a tenant an active subscription on a plan."""
    from app.billing.periods import add_months, now_utc
    from app.billing.plans import get_plan_by_code
    from app.db.models import (
        BillingInterval,
        BillingProviderType,
        Subscription,
        SubscriptionStatus,
    )

    plan = await get_plan_by_code(db, plan_code, active_only=False)
    start = overrides.pop("current_period_start", now_utc())

    fields = {
        "tenant_id": tenant.id,
        "plan_id": plan.id if plan else None,
        "provider": BillingProviderType.MANUAL,
        "status": SubscriptionStatus.ACTIVE,
        "interval": BillingInterval.MONTH,
        "current_period_start": start,
        "current_period_end": add_months(start, 1),
        "external_customer_id": f"manual_cus_{tenant.id}",
        "external_subscription_id": f"manual_sub_{tenant.id}",
    }
    fields.update(overrides)

    subscription = Subscription(**fields)
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


async def add_usage(db: AsyncSession, tenant, metric, quantity, *, key=None, period=None):
    """Record usage directly, for tests that need a starting position."""
    from app.billing import metering
    from app.billing.periods import period_for
    from app.billing.service import get_subscription

    if period is None:
        period = period_for(await get_subscription(db, tenant.id))

    result = await metering.record_usage(
        db,
        tenant_id=tenant.id,
        metric=metric,
        quantity=quantity,
        idempotency_key=key or f"test:{metric.value}:{uuid.uuid4()}",
        period=period,
    )
    await db.commit()
    return result


def stripe_signature(body: bytes, secret: str, *, timestamp: int | None = None) -> str:
    """
    Build a valid `Stripe-Signature` for `body`.

    The scheme is documented and deterministic, so a test can produce a real
    signature without calling Stripe -- which is what lets the whole webhook
    path be exercised offline.
    """
    import hashlib
    import hmac
    import time as _t

    ts = timestamp if timestamp is not None else int(_t.time())
    mac = hmac.new(secret.encode(), b"%d." % ts + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def stripe_event(event_type: str, obj: dict, *, event_id=None, created=None) -> bytes:
    """A Stripe event envelope, as raw bytes."""
    import json
    import time as _t

    return json.dumps({
        "id": event_id or f"evt_{uuid.uuid4().hex[:16]}",
        "object": "event",
        "type": event_type,
        "created": created if created is not None else int(_t.time()),
        "data": {"object": obj},
    }).encode()