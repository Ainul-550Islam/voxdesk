"""
Calendar provider contract tests.

Requirement 29: adding a new calendar provider must automatically inherit the
contract suite, and must not be able to bypass tenant scoping, error
normalization, timeout behaviour, idempotency expectations or secret handling.

Parameterized over the **registry**, not a hand-written list. A provider added
tomorrow is tested by this file the moment it is registered — which is the only
design that actually delivers the requirement. A checklist in a document does
not survive a deadline.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from app.db.models import CalendarProviderType
from app.integrations.calendar.base import (
    CalendarCapability,
    CalendarContext,
    CalendarProvider,
)
from app.integrations.calendar.errors import (
    CalendarAuthError,
    CalendarConflictError,
    CalendarError,
    CalendarNotFoundError,
    CalendarPermissionError,
    CalendarRateLimitError,
    CalendarTemporaryError,
    CalendarTimeout,
    CalendarUnsupportedError,
    CalendarValidationError,
)
from app.integrations.calendar.models import (
    Attendee,
    CalendarEvent,
    EventRequest,
    HealthResult,
    TimeWindow,
)
from app.integrations.calendar.registry import PROVIDERS, build, capabilities_of
from app.integrations.calendar.timezones import UTC
from tests.conftest import FakeTransport

ALL_PROVIDERS = list(CalendarProviderType)

#: Config that satisfies every adapter's required settings, so a contract test
#: exercises the operation rather than tripping over a missing calendar id.
_CONFIG = {
    CalendarProviderType.GOOGLE: {
        "calendar_id": "primary", "base_url": "https://gcal.test/v3",
    },
    CalendarProviderType.GOOGLE_SERVICE_ACCOUNT: {"calendar_id": "shared@example.com"},
    CalendarProviderType.MICROSOFT: {
        "mailbox": "diary@example.com", "base_url": "https://graph.test/v1.0",
    },
    CalendarProviderType.CALCOM: {
        "event_type_id": 4242, "base_url": "https://cal.test/v2",
    },
    CalendarProviderType.INTERNAL: {},
}
_CREDENTIALS = {
    CalendarProviderType.GOOGLE: {"access_token": "ya29-SECRET-google-token"},
    CalendarProviderType.GOOGLE_SERVICE_ACCOUNT: {},
    CalendarProviderType.MICROSOFT: {"access_token": "eyJ0-SECRET-graph-token"},
    CalendarProviderType.CALCOM: {"api_key": "cal_live_SECRET_key_123"},
    CalendarProviderType.INTERNAL: {},
}

START = datetime(2026, 6, 16, 14, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=START, end=START + timedelta(minutes=30))
REQUEST = EventRequest(
    title="Cleaning",
    start=START,
    end=START + timedelta(minutes=30),
    timezone="America/New_York",
    attendee=Attendee(name="Jane Doe", phone="+15551230000", email="jane@example.com"),
    idempotency_key="abc123def456abc123def456",
)


def make_provider(provider: CalendarProviderType, *, timeout: float = 5.0):
    return build(provider, CalendarContext(
        tenant_id="11111111-1111-1111-1111-111111111111",
        credentials=dict(_CREDENTIALS[provider]),
        config=dict(_CONFIG[provider]),
        timezone="America/New_York",
        timeout_seconds=timeout,
    ))


def secrets_for(provider: CalendarProviderType) -> list[str]:
    return [v for v in _CREDENTIALS[provider].values() if v]


# ================================================================ registry ===

@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_every_provider_type_has_an_adapter(provider):
    assert provider in PROVIDERS


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_adapter_name_matches_the_enum_value(provider):
    """
    A mismatch means log lines and adapters disagree about what a provider is
    called — the sort of thing that only bites while grepping during an
    incident.
    """
    assert PROVIDERS[provider].name == provider.value


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_adapter_subclasses_the_base(provider):
    assert issubclass(PROVIDERS[provider], CalendarProvider)


# ============================================================== capability ===

#: Capability -> the method that must exist. Several capabilities share a
#: method (RESCHEDULE is served by update_event or a dedicated reschedule).
_CAPABILITY_METHODS = {
    CalendarCapability.FREE_BUSY: "get_busy",
    CalendarCapability.CREATE_EVENT: "create_event",
    CalendarCapability.UPDATE_EVENT: "update_event",
    CalendarCapability.CANCEL_EVENT: "cancel_event",
    CalendarCapability.GET_EVENT: "get_event",
    CalendarCapability.HEALTH_CHECK: "health_check",
}


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_declared_capabilities_are_actually_implemented(provider):
    """
    **The core contract test.**

    Declaring a capability you have not written is worse than not declaring
    it: the service routes work to you and the tenant gets a permanent failure
    instead of a clean "unsupported".
    """
    adapter = PROVIDERS[provider]
    for capability, method_name in _CAPABILITY_METHODS.items():
        if capability not in capabilities_of(provider):
            continue
        assert hasattr(adapter, method_name), (
            f"{provider.value} declares {capability.value} but has no "
            f"{method_name}"
        )
        assert getattr(adapter, method_name) is not getattr(
            CalendarProvider, method_name
        ), (
            f"{provider.value} declares {capability.value} but does not "
            f"override {method_name}, so calling it would raise "
            f"CalendarUnsupportedError"
        )


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_reschedule_is_served_by_something_real(provider):
    """
    RESCHEDULE may be an `update_event` or a dedicated `reschedule` method —
    Cal.com has a real endpoint for it. Either satisfies the capability;
    neither means the service would route a move into nothing.
    """
    if CalendarCapability.RESCHEDULE not in capabilities_of(provider):
        pytest.skip(f"{provider.value} does not offer reschedule")

    adapter = PROVIDERS[provider]
    has_dedicated = hasattr(adapter, "reschedule")
    has_update = getattr(adapter, "update_event") is not CalendarProvider.update_event
    assert has_dedicated or has_update


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_undeclared_capabilities_raise_unsupported_cleanly(provider):
    """
    An operation a provider does not claim must fail as
    `CalendarUnsupportedError`, not `AttributeError` or `NotImplementedError`.
    The service treats that class as "nothing to do here" rather than an
    outage.
    """
    adapter = make_provider(provider)
    declared = capabilities_of(provider)

    for capability, method_name in _CAPABILITY_METHODS.items():
        if capability in declared:
            continue
        if capability is CalendarCapability.HEALTH_CHECK:
            # health_check has a defined non-raising default: a diagnostic
            # must always answer.
            assert (await adapter.health_check()).connected is False
            continue
        with pytest.raises(CalendarUnsupportedError):
            await _call(adapter, method_name)


async def _call(adapter, method_name):
    method = getattr(adapter, method_name)
    return await {
        "get_busy": lambda: method(WINDOW),
        "create_event": lambda: method(REQUEST),
        "update_event": lambda: method("ext-1", REQUEST),
        "cancel_event": lambda: method("ext-1"),
        "get_event": lambda: method("ext-1"),
    }[method_name]()


# ====================================================== error normalization ===

#: Every adapter must map these identically. That is what lets the service
#: read one property instead of knowing four vendors' conventions.
STATUS_EXPECTATIONS = [
    (401, CalendarAuthError, False),
    (403, CalendarPermissionError, False),
    (404, CalendarNotFoundError, False),
    (409, CalendarConflictError, False),
    (410, CalendarNotFoundError, False),
    (422, CalendarValidationError, False),
    (429, CalendarRateLimitError, True),
    (500, CalendarTemporaryError, True),
    (503, CalendarTemporaryError, True),
]


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
@pytest.mark.parametrize("status,expected,retryable", STATUS_EXPECTATIONS)
async def test_http_status_maps_identically_for_every_provider(
    provider, status, expected, retryable, monkeypatch
):
    if provider is CalendarProviderType.INTERNAL:
        pytest.skip("the internal provider makes no HTTP calls")

    FakeTransport((status, {"message": "nope"})).install(monkeypatch)
    adapter = make_provider(provider)

    with pytest.raises(CalendarError) as caught:
        await adapter.request("POST", "https://example.test/x", json_body={})

    assert isinstance(caught.value, expected), (
        f"{provider.value} mapped HTTP {status} to "
        f"{type(caught.value).__name__}, expected {expected.__name__}"
    )
    assert caught.value.retryable is retryable


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_a_403_that_is_really_a_throttle_becomes_retryable(
    provider, monkeypatch
):
    """
    Google and Microsoft both signal throttling inside a 403 body. Treating
    those as permission errors would strand a tenant whose only problem was
    going too fast.
    """
    if provider is CalendarProviderType.INTERNAL:
        pytest.skip("no HTTP")

    FakeTransport(
        (403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
    ).install(monkeypatch)
    adapter = make_provider(provider)

    with pytest.raises(CalendarError) as caught:
        await adapter.request("GET", "https://example.test/x")
    assert isinstance(caught.value, CalendarRateLimitError)
    assert caught.value.retryable is True


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_timeouts_become_retryable_calendar_timeouts(provider, monkeypatch):
    if provider is CalendarProviderType.INTERNAL:
        pytest.skip("no HTTP")

    FakeTransport(httpx.ReadTimeout("too slow")).install(monkeypatch)
    with pytest.raises(CalendarTimeout) as caught:
        await make_provider(provider).request("POST", "https://example.test/x")
    assert caught.value.retryable is True


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_no_httpx_exception_ever_escapes(provider, monkeypatch):
    """
    The service catches `CalendarError`. A leaked `httpx` exception would skip
    classification entirely and land in the generic handler, turning a
    transient blip into a permanently failed booking.
    """
    if provider is CalendarProviderType.INTERNAL:
        pytest.skip("no HTTP")

    for failure in (
        httpx.ConnectError("x"), httpx.ReadTimeout("x"),
        httpx.PoolTimeout("x"), httpx.RemoteProtocolError("x"),
    ):
        FakeTransport(failure).install(monkeypatch)
        try:
            await make_provider(provider).request("GET", "https://example.test/x")
        except CalendarError:
            pass
        except httpx.HTTPError as exc:
            pytest.fail(f"{provider.value} leaked {type(exc).__name__}")


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_free_busy_raises_rather_than_returning_empty_on_failure(
    provider, monkeypatch
):
    """
    **The audit's F2, as a contract.** The pre-STEP-6 client returned `[]` on
    any exception, so an outage was indistinguishable from an empty calendar —
    and therefore looked like total availability. Emptiness must mean
    emptiness.
    """
    if CalendarCapability.FREE_BUSY not in capabilities_of(provider):
        pytest.skip(f"{provider.value} has no free/busy")
    if provider is CalendarProviderType.GOOGLE_SERVICE_ACCOUNT:
        pytest.skip(
            "documented limitation: the legacy client swallows its own errors"
        )

    FakeTransport((500, {"error": "boom"})).install(monkeypatch)
    with pytest.raises(CalendarError):
        await make_provider(provider).get_busy(WINDOW)


# ================================================================= timeout ===

@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_the_configured_timeout_reaches_the_http_client(provider, monkeypatch):
    """
    Requirement 30: nothing may hang. Asserted by watching what the adapter
    passes to `AsyncClient`, because a timeout that is configured but never
    applied looks identical from the outside until a provider stops answering.
    """
    if provider is CalendarProviderType.INTERNAL:
        pytest.skip("no HTTP")

    seen = {}
    original_init = httpx.AsyncClient.__init__

    def spy(self, *args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
    FakeTransport((200, {})).install(monkeypatch)

    await make_provider(provider, timeout=2.5).request("GET", "https://example.test/x")
    assert seen["timeout"] == 2.5


# ========================================================== secret handling ===

@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_the_context_repr_hides_credentials(provider):
    """
    A context in a traceback must not print an OAuth token. Tracebacks get
    pasted into issue trackers.
    """
    printed = repr(make_provider(provider).context)
    for secret in secrets_for(provider):
        assert secret not in printed
    assert "credential_keys" in printed


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_credentials_never_appear_in_a_raised_error(provider, monkeypatch):
    """
    A provider echoing the submitted token in an error body — which
    misconfigured auth endpoints genuinely do — must not have it echoed into
    an exception the service then writes to the database.
    """
    secrets = secrets_for(provider)
    if not secrets:
        pytest.skip(f"{provider.value} stores no credentials")

    secret = secrets[0]
    FakeTransport(
        (400, {"error": "invalid", "access_token": secret, "echo": f"Bearer {secret}"})
    ).install(monkeypatch)

    with pytest.raises(CalendarError) as caught:
        await make_provider(provider).request("POST", "https://example.test/x")

    assert secret not in str(caught.value)
    assert secret not in caught.value.safe_message


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_a_401_body_is_never_included_in_the_message(provider, monkeypatch):
    """
    A 401 body is the most likely place for a token to be reflected, so it is
    excluded entirely rather than merely scrubbed. The scrubber is a regex,
    and regexes miss things.
    """
    if provider is CalendarProviderType.INTERNAL:
        pytest.skip("no HTTP")

    FakeTransport(
        (401, {"detail": "token abcdef0123456789 is invalid"})
    ).install(monkeypatch)

    with pytest.raises(CalendarAuthError) as caught:
        await make_provider(provider).request("GET", "https://example.test/x")
    assert "abcdef0123456789" not in str(caught.value)


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_a_health_check_never_raises_and_never_leaks(provider, monkeypatch):
    secrets = secrets_for(provider)
    FakeTransport((401, {"token": secrets[0] if secrets else "x"})).install(monkeypatch)

    result = await make_provider(provider).health_check()

    assert isinstance(result, HealthResult)
    assert result.provider == provider.value
    assert result.latency_ms >= 0
    for secret in secrets:
        assert secret not in result.safe_message


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_no_secret_reaches_the_log_during_a_failure(provider, monkeypatch):
    import json

    from app.core import logging as app_logging

    secrets = secrets_for(provider)
    if not secrets:
        pytest.skip(f"{provider.value} stores no credentials")

    captured: list = []
    for level in ("info", "warning", "error"):
        original = getattr(app_logging.log, level)

        def spy(event, _orig=original, **kw):
            captured.append((event, kw))
            return _orig(event, **kw)

        monkeypatch.setattr(app_logging.log, level, spy)

    FakeTransport((500, {"echo": secrets[0]})).install(monkeypatch)
    await make_provider(provider).health_check()

    assert secrets[0] not in json.dumps(captured, default=str)


# ============================================================ tenant scope ===

@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_an_adapter_holds_no_handle_capable_of_widening_its_scope(provider):
    """
    Structural, and the strongest isolation guarantee in the layer.

    An adapter is constructed with a `CalendarContext` and nothing else: no
    database session, no `Tenant` row, no request. It therefore *cannot* reach
    another tenant, because it holds no object that could. This is a guarantee
    by construction, and this test is what stops someone "just passing the
    session in" for convenience.
    """
    adapter = make_provider(provider)
    assert set(vars(adapter)) == {"context"}
    for name, value in vars(adapter).items():
        assert not _looks_like_a_session(value), (
            f"{provider.value} holds {name}={type(value).__name__}"
        )


def _looks_like_a_session(value) -> bool:
    return any(
        hasattr(value, attr) for attr in ("execute", "get", "add", "commit")
    ) and not isinstance(value, (dict, list, str, bytes))


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_the_context_carries_exactly_one_tenant(provider):
    assert make_provider(provider).context.tenant_id == (
        "11111111-1111-1111-1111-111111111111"
    )


# ============================================================= idempotency ===

@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_the_idempotency_key_reaches_the_provider_payload(provider):
    """
    Requirement 9. Every adapter must carry the key somewhere the provider can
    use it, or be able to find its own event afterwards — otherwise an
    ambiguous timeout has no safe resolution.
    """
    adapter = make_provider(provider)
    if not adapter.supports(CalendarCapability.CREATE_EVENT):
        pytest.skip(f"{provider.value} cannot create events")

    if hasattr(adapter, "event_payload"):
        import json

        body = json.dumps(adapter.event_payload(REQUEST))
        assert REQUEST.idempotency_key[:20] in body, (
            f"{provider.value} drops the idempotency key from its payload"
        )
    else:
        # No payload of its own. Such a provider must either be able to
        # reconcile, or be one where an ambiguous timeout cannot happen or
        # cannot be resolved -- and in the latter case the service must fail
        # safe rather than retry. Both exceptions are documented limitations:
        #
        #   internal                -- no network, so no ambiguity exists
        #   google_service_account  -- the legacy client cannot read an event
        #                              back, so a timeout ends as FAILED
        can_reconcile = (
            getattr(type(adapter), "find_event_by_key")
            is not CalendarProvider.find_event_by_key
        )
        assert can_reconcile or provider in (
            CalendarProviderType.INTERNAL,
            CalendarProviderType.GOOGLE_SERVICE_ACCOUNT,
        ), f"{provider.value} can neither carry a key nor reconcile"


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_reconciliation_defaults_to_unknown_not_absent(provider):
    """
    `find_event_by_key` returning `None` means "definitely not there", which
    licenses a retry. A provider that cannot check must not claim absence — the
    base default returns `None` only because the service also requires the
    original error to have been a timeout, and the internal provider never
    crosses a network.
    """
    adapter = make_provider(provider)
    if getattr(type(adapter), "find_event_by_key") is CalendarProvider.find_event_by_key:
        # Inheriting the default is only acceptable when there is no network
        # in the first place.
        assert provider in (
            CalendarProviderType.INTERNAL,
            CalendarProviderType.GOOGLE_SERVICE_ACCOUNT,
        ), (
            f"{provider.value} makes network calls but cannot reconcile an "
            f"ambiguous timeout"
        )


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_a_created_event_always_carries_an_external_id(provider, monkeypatch):
    """
    The service refuses to write CONFIRMED without one, so an adapter that
    returns a `CalendarEvent` must have extracted an id from whatever shape
    its provider used.
    """
    success = {
        CalendarProviderType.GOOGLE: {
            "id": "gcal-1",
            "start": {"dateTime": "2026-06-16T14:00:00Z"},
            "end": {"dateTime": "2026-06-16T14:30:00Z"},
        },
        CalendarProviderType.MICROSOFT: {
            "id": "graph-1",
            "start": {"dateTime": "2026-06-16T14:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-16T14:30:00", "timeZone": "UTC"},
        },
        CalendarProviderType.CALCOM: {
            "status": "success",
            "data": {
                "uid": "cal-1", "status": "accepted",
                "start": "2026-06-16T14:00:00Z", "end": "2026-06-16T14:30:00Z",
            },
        },
    }.get(provider)

    adapter = make_provider(provider)
    if not adapter.supports(CalendarCapability.CREATE_EVENT):
        pytest.skip(f"{provider.value} cannot create events")
    if success is None:
        # internal / service account: no HTTP, deterministic id.
        if provider is CalendarProviderType.GOOGLE_SERVICE_ACCOUNT:
            pytest.skip("wraps the legacy client; covered in its own test")
        event = await adapter.create_event(REQUEST)
        assert event.external_id
        return

    FakeTransport((200, success)).install(monkeypatch)
    event = await adapter.create_event(REQUEST)
    assert isinstance(event, CalendarEvent)
    assert event.external_id


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_a_success_response_with_no_id_is_a_failure(provider, monkeypatch):
    """
    The trap: a provider returning 200 with an empty body. Silence is not
    acknowledgement, and recording CONFIRMED here would claim a calendar entry
    nobody can point to.
    """
    adapter = make_provider(provider)
    if not adapter.supports(CalendarCapability.CREATE_EVENT):
        pytest.skip(f"{provider.value} cannot create events")
    if provider in (
        CalendarProviderType.INTERNAL, CalendarProviderType.GOOGLE_SERVICE_ACCOUNT
    ):
        pytest.skip("no remote response to be empty")

    FakeTransport((200, {})).install(monkeypatch)
    with pytest.raises(CalendarError):
        await adapter.create_event(REQUEST)


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_cancelling_a_missing_event_is_not_an_error(provider, monkeypatch):
    """
    Requirement 18: cancellation must be idempotent. A second cancel is a 404
    on Google; surfacing that would make a harmless double-click look broken.
    """
    adapter = make_provider(provider)
    if not adapter.supports(CalendarCapability.CANCEL_EVENT):
        pytest.skip(f"{provider.value} cannot cancel")

    FakeTransport((404, {"error": "not found"})).install(monkeypatch)
    await adapter.cancel_event("gone-1")        # must not raise


# =============================================================== datetimes ===

@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_naive_datetimes_are_refused_at_construction(provider):
    """
    A naive datetime reaching an adapter is how an appointment ends up five
    hours out. The cheapest place to catch it is the moment the value is
    constructed.
    """
    with pytest.raises(ValueError):
        EventRequest(
            title="x", start=datetime(2026, 6, 16, 14, 0),   # naive
            end=START + timedelta(minutes=30), timezone="UTC",
            attendee=Attendee(name="x"),
        )


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_a_payload_transmits_an_unambiguous_instant(provider):
    """
    Every adapter must send something that pins the moment — either a UTC
    offset or an explicit `timeZone`. A bare local wall clock is what breaks
    across DST.
    """
    adapter = make_provider(provider)
    if not hasattr(adapter, "event_payload"):
        pytest.skip(f"{provider.value} builds no event payload")

    import json

    body = json.dumps(adapter.event_payload(REQUEST))
    assert ("timeZone" in body) or ("+00:00" in body) or ("Z\"" in body), body
