"""
Realtime emission points, wired into the real webhook routes.

The routes run against the real router stack and a real database; only the
publish call itself is patched out (a recorder). What is asserted is the
wiring contract, not the network: which kind is announced, how many times,
and — for the callback routes, which Twilio retries aggressively — that a
retried callback still announces exactly once.
"""
from __future__ import annotations

import pytest

from app.db.models import CallStatus, TransferState
from tests.conftest import make_tenant
from tests.test_call_callbacks import post_dial, post_status
from tests.test_e2e_guard import post_voice
from tests.test_transfer import seed_call, tenant_with_human


class EmissionRecorder:
    """Stands in for events.emit_call_event; records (kind, extra) per call."""

    def __init__(self):
        self.calls = []

    async def __call__(self, call, *, kind, extra=None):
        self.calls.append(
            {"call_id": str(call.id), "kind": kind, "extra": extra or {}}
        )
        return True

    def kinds(self):
        return [c["kind"] for c in self.calls]


@pytest.fixture
def emissions(monkeypatch):
    recorder = EmissionRecorder()
    # The handler holds a module reference (`from app.realtime import events
    # as realtime_events`), so patching the attribute on the module object is
    # what the routes actually call.
    monkeypatch.setattr(
        "app.realtime.events.emit_call_event", recorder, raising=False
    )
    return recorder


# ---------------------------------------------------------------- /voice ----


async def test_a_new_incoming_call_announces_call_created_once(client, db, emissions):
    tenant = await make_tenant(db, "Realtime Co")

    resp = await post_voice(
        client, "CAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01", "+15559990000", tenant.twilio_number
    )
    assert resp.status_code == 200
    assert emissions.kinds() == ["call.created"]


async def test_a_redelivered_voice_webhook_does_not_reannounce(client, db, emissions):
    """Twilio retries webhooks; a retry hits the 'already exists' branch and
    must not double-animate a wallboard."""
    tenant = await make_tenant(db, "Realtime Co")

    for _ in range(2):
        resp = await post_voice(
            client, "CAbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb02", "+15559990000", tenant.twilio_number
        )
        assert resp.status_code == 200

    assert emissions.kinds() == ["call.created"]


# --------------------------------------------------------------- /status ----


async def test_an_applied_status_change_announces_call_updated(client, db, emissions):
    tenant = await make_tenant(db, "Realtime Co")
    call = await seed_call(db, tenant)

    resp = await post_status(client, call.call_sid, "completed")

    assert resp.status_code == 200
    assert emissions.kinds() == ["call.updated"]


async def test_duplicate_completed_callbacks_announce_call_updated_exactly_once(
    client, db, emissions
):
    """Mirrors test_duplicate_completed_callbacks_bill_the_minutes_once from
    the money side: idempotency isn't real until the realtime side honours it
    too."""
    tenant = await make_tenant(db, "Realtime Co")
    call = await seed_call(db, tenant)

    for _ in range(2):
        resp = await post_status(client, call.call_sid, "completed")
        assert resp.status_code == 200

    assert emissions.kinds() == ["call.updated"]


async def test_an_unknown_callsid_announces_nothing(client, db, emissions):
    resp = await post_status(client, "CAnonexistent000000000000000000", "completed")

    assert resp.status_code == 200
    assert emissions.kinds() == []


# ------------------------------------------------------- /transfer-status ---


async def test_a_connected_transfer_announces_call_transfer(client, db, emissions):
    tenant = await tenant_with_human(db)
    call = await seed_call(
        db, tenant, status=CallStatus.IN_PROGRESS, transfer_state=TransferState.DIALING
    )

    resp = await post_dial(client, call.call_sid, "completed")

    assert resp.status_code == 200
    assert emissions.kinds() == ["call.transfer"]
    assert emissions.calls[0]["extra"]["transfer_outcome"] == "connected"


async def test_a_busy_transfer_announces_the_failure_outcome(client, db, emissions):
    tenant = await tenant_with_human(db)
    call = await seed_call(
        db, tenant, status=CallStatus.IN_PROGRESS, transfer_state=TransferState.DIALING
    )

    resp = await post_dial(client, call.call_sid, "busy")

    assert resp.status_code == 200
    assert emissions.kinds() == ["call.transfer"]
    assert emissions.calls[0]["extra"]["transfer_outcome"] == "busy"


async def test_a_retried_dial_callback_annotates_exactly_once(client, db, emissions):
    """mark_transfer_* returning False on a duplicate must also suppress the
    second announcement — realtime duplicates are display bugs, not data bugs,
    but they are still bugs."""
    tenant = await tenant_with_human(db)
    call = await seed_call(
        db, tenant, status=CallStatus.IN_PROGRESS, transfer_state=TransferState.DIALING
    )

    for _ in range(2):
        resp = await post_dial(client, call.call_sid, "busy")
        assert resp.status_code == 200

    assert emissions.kinds() == ["call.transfer"]


async def test_disabled_realtime_keeps_webhooks_untouched(client, db, monkeypatch):
    """The off-by-default path: with REALTIME_GATEWAY_URL unset, the real
    emission path runs end to end, the publisher's enabled() gate returns
    False before any network client even exists, and the webhook contract is
    exactly what it was. Nothing here is patched — that IS the assertion."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "realtime_gateway_url", "", raising=False)
    monkeypatch.setattr(
        settings, "realtime_gateway_ingest_secret", "", raising=False
    )

    tenant = await make_tenant(db, "Realtime Co")
    resp = await post_voice(
        client, "CAcccccccccccccccccccccccccccccc03", "+15559990000", tenant.twilio_number
    )
    assert resp.status_code == 200
