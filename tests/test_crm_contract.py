"""
Provider contract tests.

Requirement 27: adding a future provider must not accidentally bypass tenant
scoping, error normalization, idempotency, timeout handling or secret
handling.

These tests are parameterized over the **registry**, not over a hand-written
list. A Salesforce adapter added tomorrow is tested by this file the moment it
is registered, and there is no separate list to forget to update. That is the
only design that actually delivers what the requirement asks for — a checklist
in a document does not survive contact with a deadline.
"""
from __future__ import annotations

import inspect

import httpx
import pytest

from app.db.models import CrmProviderType
from app.integrations.crm.base import Capability, CrmProvider, ProviderContext
from app.integrations.crm.errors import (
    CrmAuthError,
    CrmConnectionError,
    CrmError,
    CrmRateLimited,
    CrmServerError,
    CrmTimeout,
    CrmUnsupportedOperation,
    CrmValidationError,
)
from app.integrations.crm.models import CrmResult, HealthResult, NormalizedContact
from app.integrations.crm.registry import PROVIDERS, build, capabilities_of
from tests.conftest import FakeTransport

ALL_PROVIDERS = list(CrmProviderType)

#: Config that satisfies every adapter's required settings, so a contract test
#: exercises the operation rather than tripping over a missing location id.
_CONFIG = {
    CrmProviderType.GOHIGHLEVEL: {
        "location_id": "loc_1", "calendar_id": "cal_1", "base_url": "https://ghl.test",
    },
    CrmProviderType.HUBSPOT: {"base_url": "https://hubspot.test"},
    CrmProviderType.JOBBER: {"base_url": "https://jobber.test/graphql"},
    CrmProviderType.WEBHOOK: {"url": "https://hooks.test/in"},
}
_CREDENTIALS = {
    CrmProviderType.GOHIGHLEVEL: {"access_token": "pit-secret-token-value-123"},
    CrmProviderType.HUBSPOT: {"access_token": "pat-na1-secret-token-value"},
    CrmProviderType.JOBBER: {"access_token": "jobber-secret-token-value"},
    CrmProviderType.WEBHOOK: {"signing_secret": "webhook-secret-value-0123456789"},
}


def make_provider(provider: CrmProviderType, *, timeout: float = 5.0) -> CrmProvider:
    return build(provider, ProviderContext(
        tenant_id="11111111-1111-1111-1111-111111111111",
        credentials=dict(_CREDENTIALS[provider]),
        config=dict(_CONFIG[provider]),
        field_mappings={},
        timeout_seconds=timeout,
    ))


CONTACT = NormalizedContact(
    first_name="Jane", last_name="Doe", phone="+15551230000",
    email="jane@example.com", tags=("voxdesk",),
)


# =========================================================== interface ===

@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_every_provider_type_has_a_registered_adapter(provider):
    assert provider in PROVIDERS


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_adapter_name_matches_the_enum_value(provider):
    """
    A mismatch here means log lines and adapters disagree about what a
    provider is called, which is the kind of thing that only surfaces when
    someone is grepping logs during an incident.
    """
    assert PROVIDERS[provider].name == provider.value


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_adapter_subclasses_the_base(provider):
    assert issubclass(PROVIDERS[provider], CrmProvider)


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_declared_capabilities_are_actually_implemented(provider):
    """
    **The core contract test.**

    Declaring a capability you have not written is worse than not declaring
    it: the service layer routes work to you and the tenant gets a permanent
    failure instead of a clean "unsupported". This walks every declared
    capability and asserts the class actually overrides the base's raising
    stub.
    """
    adapter = PROVIDERS[provider]
    base_methods = {
        name: getattr(CrmProvider, name)
        for name in dir(CrmProvider)
        if not name.startswith("_") and inspect.isfunction(getattr(CrmProvider, name, None))
    }

    for capability in capabilities_of(provider):
        method_name = capability.value
        assert hasattr(adapter, method_name), (
            f"{provider.value} declares {method_name} but has no such method"
        )
        own = getattr(adapter, method_name)
        assert own is not base_methods.get(method_name), (
            f"{provider.value} declares {capability.value} but does not "
            f"override the base implementation, so calling it would raise "
            f"CrmUnsupportedOperation"
        )


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_undeclared_capabilities_raise_unsupported_cleanly(provider):
    """
    The other direction: an operation a provider does not claim must fail as
    `CrmUnsupportedOperation`, not `AttributeError` or `NotImplementedError`.
    The service layer treats that class as "nothing to do here" rather than
    as an outage to alarm on.
    """
    adapter = make_provider(provider)
    missing = set(Capability) - capabilities_of(provider)
    if not missing:
        pytest.skip(f"{provider.value} declares every capability")

    for capability in missing:
        if capability is Capability.HEALTH_CHECK:
            # health_check has a defined non-raising default: it reports
            # not-connected, because a diagnostic must always answer.
            result = await adapter.health_check()
            assert result.connected is False
            continue
        method = getattr(adapter, capability.value)
        with pytest.raises(CrmUnsupportedOperation):
            await _call_with_dummy_args(method, capability)


async def _call_with_dummy_args(method, capability: Capability):
    from datetime import datetime, timedelta

    from app.integrations.crm.models import NormalizedActivity, NormalizedAppointment

    activity = NormalizedActivity(title="t", body="b")
    now = datetime.utcnow()
    appointment = NormalizedAppointment(
        title="t", starts_at=now, ends_at=now + timedelta(minutes=30), contact=CONTACT
    )
    args = {
        Capability.UPSERT_CONTACT: (CONTACT,),
        Capability.CREATE_CONTACT: (CONTACT,),
        Capability.UPDATE_CONTACT: ("ext-1", CONTACT),
        Capability.GET_CONTACT: (),
        Capability.CREATE_NOTE: ("ext-1", activity),
        Capability.CREATE_ACTIVITY: ("ext-1", activity),
        Capability.CREATE_APPOINTMENT: (appointment,),
        Capability.CANCEL_APPOINTMENT: ("ext-1",),
        Capability.ADD_TAG: ("ext-1", ["a"]),
        Capability.ADD_CUSTOM_FIELDS: ("ext-1", {"a": "b"}),
    }[capability]
    return await method(*args)


# ====================================================== error normalization ===

#: Every adapter must map these identically. This is what lets the retry
#: policy read one boolean instead of knowing four providers' conventions.
STATUS_EXPECTATIONS = [
    (401, CrmAuthError, False),
    (403, CrmAuthError, False),
    (404, CrmError, False),
    (422, CrmValidationError, False),
    (429, CrmRateLimited, True),
    (500, CrmServerError, True),
    (502, CrmServerError, True),
    (503, CrmServerError, True),
]


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
@pytest.mark.parametrize("status,expected,retryable", STATUS_EXPECTATIONS)
async def test_http_status_maps_to_the_same_error_class_for_every_provider(
    provider, status, expected, retryable, monkeypatch
):
    FakeTransport((status, {"message": "nope"})).install(monkeypatch)
    adapter = make_provider(provider)

    with pytest.raises(CrmError) as caught:
        await adapter.request("POST", "https://example.test/x", json_body={})

    assert isinstance(caught.value, expected), (
        f"{provider.value} mapped HTTP {status} to {type(caught.value).__name__}, "
        f"expected {expected.__name__}"
    )
    assert caught.value.retryable is retryable


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_timeouts_become_retryable_crm_timeouts(provider, monkeypatch):
    FakeTransport(httpx.ReadTimeout("too slow")).install(monkeypatch)
    adapter = make_provider(provider)

    with pytest.raises(CrmTimeout) as caught:
        await adapter.request("POST", "https://example.test/x", json_body={})
    assert caught.value.retryable is True


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_connection_errors_become_retryable(provider, monkeypatch):
    FakeTransport(httpx.ConnectError("reset")).install(monkeypatch)
    adapter = make_provider(provider)

    with pytest.raises(CrmConnectionError) as caught:
        await adapter.request("POST", "https://example.test/x", json_body={})
    assert caught.value.retryable is True


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_no_httpx_exception_ever_escapes_an_adapter(provider, monkeypatch):
    """
    The service layer catches `CrmError`. An `httpx` exception leaking through
    would skip retry classification entirely and land in the generic handler,
    which marks the sync permanently failed — so a transient network blip
    would become a lost lead.
    """
    for failure in (
        httpx.ConnectError("x"), httpx.ReadTimeout("x"),
        httpx.PoolTimeout("x"), httpx.RemoteProtocolError("x"),
    ):
        FakeTransport(failure).install(monkeypatch)
        adapter = make_provider(provider)
        try:
            await adapter.request("GET", "https://example.test/x")
        except CrmError:
            pass
        except httpx.HTTPError as exc:
            pytest.fail(f"{provider.value} leaked {type(exc).__name__}")


# ================================================================= timeout ===

@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_the_configured_timeout_reaches_the_http_client(provider, monkeypatch):
    """
    Requirement 14: nothing waits on a CRM indefinitely. Asserted by watching
    what the adapter passes to `AsyncClient`, because a timeout that is
    configured but never applied looks identical from the outside until a
    provider hangs.
    """
    seen = {}
    original_init = httpx.AsyncClient.__init__

    def spy(self, *args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
    FakeTransport((200, {})).install(monkeypatch)

    adapter = make_provider(provider, timeout=3.5)
    await adapter.request("GET", "https://example.test/x")

    assert seen["timeout"] == 3.5


# ========================================================= secret handling ===

@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_the_provider_context_repr_hides_credentials(provider):
    """
    A `ProviderContext` in a traceback, a debugger or a log line must not
    print the token. Tracebacks are the most common accidental exfiltration
    path there is — they get pasted into issue trackers.
    """
    adapter = make_provider(provider)
    printed = repr(adapter.context)
    for secret in _CREDENTIALS[provider].values():
        assert secret not in printed
    # It should still be useful for debugging.
    assert "credential_keys" in printed


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_credentials_never_appear_in_a_raised_error(provider, monkeypatch):
    """
    A provider that echoes the submitted credential in its error body — which
    misconfigured auth endpoints genuinely do — must not have that echoed
    into an exception the service layer then writes to the database.
    """
    secret = list(_CREDENTIALS[provider].values())[0]
    FakeTransport(
        (400, {"error": "invalid", "access_token": secret, "echo": f"Bearer {secret}"})
    ).install(monkeypatch)

    adapter = make_provider(provider)
    with pytest.raises(CrmError) as caught:
        await adapter.request("POST", "https://example.test/x", json_body={})

    assert secret not in str(caught.value)
    assert secret not in caught.value.safe_message


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_a_401_body_is_never_included_in_the_error_message(
    provider, monkeypatch
):
    """
    A 401 body is the single most likely place for a token to be reflected,
    so it is excluded entirely rather than merely scrubbed. Belt and braces:
    the scrubber is a regex and regexes miss things.
    """
    FakeTransport(
        (401, {"detail": "token abcdef0123456789 is invalid"})
    ).install(monkeypatch)

    adapter = make_provider(provider)
    with pytest.raises(CrmAuthError) as caught:
        await adapter.request("GET", "https://example.test/x")

    assert "abcdef0123456789" not in str(caught.value)


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_a_health_check_never_raises_and_never_leaks(provider, monkeypatch):
    """
    Requirement 24. A tenant pressing "Test" with a dead token gets a
    normalized answer, not a 500 and not the provider's raw response.
    """
    secret = list(_CREDENTIALS[provider].values())[0]
    FakeTransport((401, {"token": secret})).install(monkeypatch)

    adapter = make_provider(provider)
    result = await adapter.health_check()

    assert isinstance(result, HealthResult)
    assert result.connected is False
    assert result.provider == provider.value
    assert secret not in result.safe_message
    assert result.latency_ms >= 0


# ============================================================ tenant scoping ===

@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_an_adapter_holds_no_handle_capable_of_widening_its_scope(provider):
    """
    Structural, and the strongest tenant-isolation guarantee in the layer.

    An adapter is constructed with a `ProviderContext` and nothing else: no
    database session, no `Tenant` row, no request. It therefore *cannot* read
    another tenant's data, because it holds no object that could. This is a
    guarantee by construction rather than by discipline, and this test is
    what stops someone "just passing the session in" for convenience.
    """
    adapter = make_provider(provider)

    for name, value in vars(adapter).items():
        assert not _looks_like_a_session(value), (
            f"{provider.value} holds {name}={type(value).__name__}, which can "
            f"reach other tenants' rows"
        )
    assert set(vars(adapter)) == {"context"}


def _looks_like_a_session(value) -> bool:
    return any(
        hasattr(value, attr) for attr in ("execute", "get", "add", "commit")
    ) and not isinstance(value, (dict, list, str, bytes))


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_the_context_carries_exactly_one_tenant(provider):
    adapter = make_provider(provider)
    assert adapter.context.tenant_id == "11111111-1111-1111-1111-111111111111"


# =============================================================== idempotency ===

@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_a_successful_write_always_returns_an_external_id(
    provider, monkeypatch
):
    """
    Requirement 12 and the standing rule that a sync is never claimed without
    proof. Whatever a provider's success shape is, the adapter must extract an
    id from it — the service layer refuses to write SYNCED without one.
    """
    success_bodies = {
        CrmProviderType.GOHIGHLEVEL: {"contact": {"id": "ghl-1"}},
        CrmProviderType.HUBSPOT: {"results": [{"id": "hs-1", "new": True}]},
        CrmProviderType.JOBBER: {
            "data": {"clientCreate": {
                "client": {"id": "jb-1"}, "userErrors": [],
            }}
        },
        CrmProviderType.WEBHOOK: {"id": "wh-1"},
    }
    FakeTransport((200, success_bodies[provider])).install(monkeypatch)

    adapter = make_provider(provider)
    if not adapter.supports(Capability.UPSERT_CONTACT):
        pytest.skip(f"{provider.value} cannot upsert")

    result = await adapter.upsert_contact(CONTACT)
    assert isinstance(result, CrmResult)
    assert result.external_id


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_a_success_response_with_no_id_is_a_failure_not_a_success(
    provider, monkeypatch
):
    """
    The trap this closes: a provider returning 200 with an empty body. Silence
    is not acknowledgement, and recording SYNCED here would mean claiming a
    CRM record exists that nobody can point to.
    """
    FakeTransport((200, {})).install(monkeypatch)

    adapter = make_provider(provider)
    if not adapter.supports(Capability.UPSERT_CONTACT):
        pytest.skip(f"{provider.value} cannot upsert")

    if provider is CrmProviderType.WEBHOOK:
        # A webhook legitimately has no id to return; it falls back to the
        # idempotency key, which is still a stable identifier.
        result = await adapter.upsert_contact(CONTACT)
        assert result.external_id
        return

    with pytest.raises(CrmError):
        await adapter.upsert_contact(CONTACT)
