"""
Realtime publisher, in isolation: the never-raises contract, the ingest
request shape, event-id determinism, and the no-PII payload boundary.

No database and no network are involved — httpx.MockTransport stands in for
the gateway, and the module-global publish client is swapped for one backed
by that transport (the global exists precisely so tests can do this without
reaching into httpx internals).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from app.core.config import settings
from app.db.models import (
    Call,
    CallDirection,
    CallStatus,
    TransferState,
)
from app.realtime import events, publisher


# ---------------------------------------------------------------- helpers ---


def _enable_realtime(monkeypatch, *, url="http://gateway:8790", secret="s" * 32):
    monkeypatch.setattr(settings, "realtime_gateway_url", url, raising=False)
    monkeypatch.setattr(
        settings, "realtime_gateway_ingest_secret", secret, raising=False
    )


def _swap_client(monkeypatch, handler):
    """Install a MockTransport-backed client; return a recorder of requests."""
    recorder = []

    def tracking_handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(tracking_handler))
    # `raising=False`: the global is created lazily, so it may not exist yet.
    monkeypatch.setattr(publisher, "_client", client, raising=False)
    return recorder


def _call(**overrides) -> Call:
    """An in-memory Call row with every content field set — so a payload that
    leaks ANY of them fails the PII test, not just the ones the author thought
    to check."""
    call = Call(
        tenant_id=uuid.uuid4(),
        call_sid="CA" + uuid.uuid4().hex[:30],
        from_number="+15551112222",
        to_number="+15553334444",
        status=CallStatus.IN_PROGRESS,
        direction=CallDirection.INBOUND,
        started_at=datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc),
    )
    call.id = uuid.uuid4()
    call.summary = "Customer wants a boiler replaced before Friday"
    call.recording_url = "https://api.twilio.com/recordings/REdeadbeef"
    call.transfer_state = TransferState.DIALING
    call.lead_score = 42
    call.escalated = True
    for key, value in overrides.items():
        setattr(call, key, value)
    return call


def _published_json(request):
    return json.loads(request.content)


# ------------------------------------------------------------- enablement ---


async def test_disabled_without_a_gateway_url_silently_does_nothing(monkeypatch):
    _enable_realtime(monkeypatch, url="")
    recorded = _swap_client(
        monkeypatch,
        lambda request: (_ for _ in ()).throw(AssertionError("must not publish")),
    )

    assert publisher.enabled() is False
    ok = await publisher.publish_event(
        tenant_id="t", room="calls", kind="call.updated", payload={}
    )
    assert ok is False
    assert recorded == []


async def test_disabled_without_a_secret_silently_does_nothing(monkeypatch):
    _enable_realtime(monkeypatch, secret="")
    recorded = _swap_client(
        monkeypatch,
        lambda request: (_ for _ in ()).throw(AssertionError("must not publish")),
    )

    assert publisher.enabled() is False
    ok = await publisher.publish_event(
        tenant_id="t", room="calls", kind="call.updated", payload={}
    )
    assert ok is False
    assert recorded == []


# ------------------------------------------------------------ publish path --


async def test_publish_ok_posts_the_ingest_contract(monkeypatch):
    _enable_realtime(monkeypatch, secret="top-secret-ingest-token-123456")
    recorded = _swap_client(
        monkeypatch, lambda request: httpx.Response(200, json={"delivered": 2})
    )

    ok = await publisher.publish_event(
        tenant_id="tenant-1",
        room="calls",
        kind="call.updated",
        payload={"call_id": "c1", "status": "completed"},
        event_id="0f8fad5b-d9cb-469f-a165-70867728950e",
    )

    assert ok is True
    assert len(recorded) == 1
    request = recorded[0]
    assert request.method == "POST"
    assert request.url.path == "/ingest/v1/publish"
    assert request.headers["authorization"] == "Bearer top-secret-ingest-token-123456"
    body = _published_json(request)
    assert body == {
        "tenant_id": "tenant-1",
        "room": "calls",
        "kind": "call.updated",
        "payload": {"call_id": "c1", "status": "completed"},
        "event_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
    }


async def test_duplicate_acknowledgement_counts_as_success(monkeypatch):
    """Gateway 200 + duplicate=true means "already happened once" — the exact
    outcome the deterministic event id is FOR. It must be a success."""
    _enable_realtime(monkeypatch)
    _swap_client(monkeypatch, lambda request: httpx.Response(200, json={"duplicate": True}))

    ok = await publisher.publish_event(
        tenant_id="t", room="calls", kind="call.updated",
        payload={}, event_id="0f8fad5b-d9cb-469f-a165-70867728950e",
    )
    assert ok is True


@pytest.mark.parametrize("status_code", [401, 422, 500])
async def test_rejections_and_gateway_errors_return_false(monkeypatch, status_code):
    _enable_realtime(monkeypatch)
    _swap_client(monkeypatch, lambda request: httpx.Response(status_code))

    ok = await publisher.publish_event(
        tenant_id="t", room="calls", kind="call.updated", payload={}
    )
    assert ok is False


async def test_a_gateway_timeout_returns_false_and_never_raises(monkeypatch):
    _enable_realtime(monkeypatch)

    def raise_timeout(request):
        raise httpx.ConnectTimeout("gateway unreachable")

    _swap_client(monkeypatch, raise_timeout)
    ok = await publisher.publish_event(
        tenant_id="t", room="calls", kind="call.updated", payload={}
    )
    assert ok is False


# --------------------------------------------------------------- payloads ---


def test_call_payload_excludes_every_content_field():
    call = _call(status=CallStatus.COMPLETED, duration_seconds=95)
    payload = events.call_payload(call)
    rendered = json.dumps(payload)

    for forbidden_fragment in (
        "from_number", "to_number", "summary", "transcript", "recording_url",
        "+15551112222", "+15553334444", "boiler", "REdeadbeef",
    ):
        assert forbidden_fragment not in rendered, forbidden_fragment

    # ...while keeping exactly the routing + display facts a wallboard needs.
    assert payload["call_id"] == str(call.id)
    assert payload["call_sid"] == call.call_sid
    assert payload["status"] == "completed"
    assert payload["direction"] == "inbound"
    assert payload["duration_seconds"] == 95
    assert payload["lead_score"] == 42
    assert payload["transfer_state"] == "dialing"
    assert payload["started_at"] == "2026-09-16T10:00:00+00:00"
    assert payload["ended_at"] is None


async def test_emit_publishes_to_both_rooms_with_per_room_event_ids(monkeypatch):
    _enable_realtime(monkeypatch)
    recorded = _swap_client(
        monkeypatch, lambda request: httpx.Response(200, json={"delivered": 1})
    )
    call = _call()

    ok = await events.emit_call_event(call, kind="call.updated")

    assert ok is True
    assert len(recorded) == 2
    rooms = {_published_json(r)["room"] for r in recorded}
    assert rooms == {"calls", f"call:{call.id}"}
    ids = [_published_json(r)["event_id"] for r in recorded]
    # Per-room ids differ — a partial outage that delivered ONE room must
    # stay retryable for the other.
    assert ids[0] != ids[1]
    # Both are gateway-valid UUIDs (the ingest schema requires them).
    for event_id in ids:
        uuid.UUID(event_id)


async def test_event_ids_are_deterministic_and_state_material(monkeypatch):
    """uuid5 over <call_id>:<kind>:<scope>:<status>:<transfer_state>, pinned to
    the documented namespace — a regression here silently breaks replay
    suppression across API restarts."""
    _enable_realtime(monkeypatch)
    recorded = _swap_client(monkeypatch, lambda request: httpx.Response(200))
    call = _call(status=CallStatus.COMPLETED)
    call.transfer_state = None

    await events.emit_call_event(call, kind="call.updated")
    await events.emit_call_event(call, kind="call.updated")

    assert len(recorded) == 4  # 2 emissions × 2 rooms
    first_pair = [_published_json(r) for r in recorded[:2]]
    second_pair = [_published_json(r) for r in recorded[2:]]
    for first, second in zip(first_pair, second_pair):
        assert first["event_id"] == second["event_id"]

    namespace = uuid.UUID("7e9f6d3a-2b1c-4f5e-9a8d-0c1b2a3f4e5d")
    expected_all = str(
        uuid.uuid5(namespace, f"{call.id}:call.updated:all:completed:none")
    )
    by_room = {b["room"]: b["event_id"] for b in first_pair}
    assert by_room["calls"] == expected_all


async def test_extra_outcome_rides_along_inside_the_payload(monkeypatch):
    _enable_realtime(monkeypatch)
    recorded = _swap_client(monkeypatch, lambda request: httpx.Response(200))
    call = _call(transfer_state=TransferState.CONNECTED)

    await events.emit_call_event(call, kind="call.transfer", extra={"transfer_outcome": "connected"})

    body = _published_json(recorded[0])
    assert body["payload"]["transfer_outcome"] == "connected"
    assert body["payload"]["transfer_state"] == "connected"


async def test_one_failed_room_degrades_the_whole_event(monkeypatch):
    _enable_realtime(monkeypatch)
    attempts = []

    def flaky(request):
        attempts.append(request)
        # First room fine, second room refused (partial outage).
        return httpx.Response(200 if len(attempts) == 1 else 500)

    _swap_client(monkeypatch, flaky)
    ok = await events.emit_call_event(_call(), kind="call.updated")
    assert ok is False
    assert len(attempts) == 2
