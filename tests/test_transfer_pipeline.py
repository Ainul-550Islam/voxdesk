"""
Proof that the live agent pipeline can actually reach the transfer service.

The bug this file exists to prevent: before STEP 3, `escalate_to_human` set a
flag and returned `{"action": "transfer"}`, and a comment claimed the pipeline
acted on it. Nothing did -- `execute_transfer` had zero callers. So it is not
enough to test the service in isolation; the path

    tool schema -> FunctionHandlers.dispatch -> transfer_service -> provider

has to be exercised end to end.
"""
from __future__ import annotations

import pytest

from app.agent.functions import TOOL_SCHEMAS, FunctionHandlers
from app.db.models import CallStatus, TransferState
from app.telephony.provider import FakeTelephonyProvider
from tests.test_transfer import HUMAN, seed_call, tenant_with_human


def tool_names(schemas) -> set[str]:
    return {s["function"]["name"] for s in schemas}


# ------------------------------------------------------------ registration ---

def test_escalation_tool_is_registered_in_the_schema_list():
    assert "escalate_to_human" in tool_names(TOOL_SCHEMAS)


def test_escalation_schema_is_explicit_and_typed():
    schema = next(
        s for s in TOOL_SCHEMAS if s["function"]["name"] == "escalate_to_human"
    )
    params = schema["function"]["parameters"]
    assert params["properties"]["reason"]["type"] == "string"
    assert params["required"] == ["reason"]


def test_handler_method_exists_for_every_declared_tool():
    """A schema with no implementation is a promise the agent cannot keep."""
    for name in tool_names(TOOL_SCHEMAS):
        assert hasattr(FunctionHandlers, name), f"no handler for {name}"


# -------------------------------------------------------- tool availability ---

@pytest.mark.asyncio
async def test_escalation_tool_offered_when_a_destination_exists(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    handlers = FunctionHandlers(session=db, tenant=tenant, call=call)
    assert "escalate_to_human" in tool_names(handlers.available_tools())


@pytest.mark.asyncio
async def test_escalation_tool_withheld_without_a_destination(db):
    from tests.conftest import make_tenant
    tenant = await make_tenant(db, "No Human Co")
    tenant.escalation_number = None
    await db.commit()
    call = await seed_call(db, tenant)

    handlers = FunctionHandlers(session=db, tenant=tenant, call=call)
    names = tool_names(handlers.available_tools())
    assert "escalate_to_human" not in names
    # Everything else is still available.
    assert "take_message" in names
    assert len(names) == len(tool_names(TOOL_SCHEMAS)) - 1


@pytest.mark.asyncio
async def test_escalation_tool_withheld_once_a_transfer_is_in_flight(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    provider = FakeTelephonyProvider()
    handlers = FunctionHandlers(session=db, tenant=tenant, call=call, provider=provider)

    assert "escalate_to_human" in tool_names(handlers.available_tools())
    await handlers.dispatch("escalate_to_human", {"reason": "angry caller"})
    assert "escalate_to_human" not in tool_names(handlers.available_tools())


@pytest.mark.asyncio
async def test_escalation_tool_withheld_on_a_finished_call(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant, status=CallStatus.COMPLETED)
    handlers = FunctionHandlers(session=db, tenant=tenant, call=call)
    assert "escalate_to_human" not in tool_names(handlers.available_tools())


# ------------------------------------------------------- dispatch -> provider ---

@pytest.mark.asyncio
async def test_dispatch_reaches_the_provider_and_changes_the_database(db):
    """The whole point: the tool call must produce a real provider action."""
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    provider = FakeTelephonyProvider()
    handlers = FunctionHandlers(session=db, tenant=tenant, call=call, provider=provider)

    result = await handlers.dispatch(
        "escalate_to_human", {"reason": "caller demanded a manager"}
    )

    assert result["ok"] is True
    assert result["outcome"] == "TRANSFER_STARTED"
    assert provider.call_count == 1, "no provider instruction was ever issued"
    assert HUMAN in provider.last_twiml()

    await db.refresh(call)
    assert call.status is CallStatus.TRANSFERRED
    assert call.transfer_state is TransferState.DIALING
    assert call.escalated is True
    assert call.transfer_reason == "caller demanded a manager"


@pytest.mark.asyncio
async def test_failed_transfer_does_not_claim_success_to_the_model(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    provider = FakeTelephonyProvider(fail_with=("20404", "call not in-progress"))
    handlers = FunctionHandlers(session=db, tenant=tenant, call=call, provider=provider)

    result = await handlers.dispatch("escalate_to_human", {"reason": "x"})

    assert result["ok"] is False
    assert result["outcome"] == "TRANSFER_FAILED"
    await db.refresh(call)
    assert call.status is not CallStatus.TRANSFERRED
    assert call.transfer_state is TransferState.FAILED


@pytest.mark.asyncio
async def test_tool_result_leaks_neither_numbers_nor_provider_errors(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    provider = FakeTelephonyProvider(fail_with=("20404", "ACxxxx not authorized"))
    handlers = FunctionHandlers(session=db, tenant=tenant, call=call, provider=provider)

    result = await handlers.dispatch("escalate_to_human", {"reason": "x"})
    blob = str(result)
    assert HUMAN not in blob
    assert "ACxxxx" not in blob
    assert "20404" not in blob


@pytest.mark.asyncio
async def test_duplicate_dispatch_does_not_dial_twice(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    provider = FakeTelephonyProvider()
    handlers = FunctionHandlers(session=db, tenant=tenant, call=call, provider=provider)

    first = await handlers.dispatch("escalate_to_human", {"reason": "a"})
    second = await handlers.dispatch("escalate_to_human", {"reason": "b"})

    assert first["outcome"] == "TRANSFER_STARTED"
    assert second["outcome"] == "ALREADY_TRANSFERRED"
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_dispatch_survives_a_missing_argument(db):
    """dispatch() must never raise into the audio pipeline."""
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    handlers = FunctionHandlers(session=db, tenant=tenant, call=call,
                                provider=FakeTelephonyProvider())
    result = await handlers.dispatch("escalate_to_human", {})
    assert result["ok"] is False


# --------------------------------------------------------------- injection ---

@pytest.mark.asyncio
async def test_caller_supplied_reason_cannot_inject_a_number_into_the_whisper(db):
    """
    `reason` is model output echoing caller speech. It is spoken aloud to
    staff, so a digit string in it must not survive into the TwiML.
    """
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    provider = FakeTelephonyProvider()
    handlers = FunctionHandlers(session=db, tenant=tenant, call=call, provider=provider)

    await handlers.dispatch(
        "escalate_to_human",
        {"reason": "urgent, tell them to call +15559998888 immediately"},
    )

    twiml = provider.last_twiml()
    assert "+15559998888" not in twiml
    assert "[number removed]" in twiml
    # The dialled destination is still the tenant's own number.
    assert f"<Number>{HUMAN}</Number>" in twiml
    # The full reason is preserved for the dashboard, just never spoken.
    await db.refresh(call)
    assert "+15559998888" in call.transfer_reason


@pytest.mark.asyncio
async def test_reason_cannot_inject_xml_into_the_twiml(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    provider = FakeTelephonyProvider()
    handlers = FunctionHandlers(session=db, tenant=tenant, call=call, provider=provider)

    await handlers.dispatch(
        "escalate_to_human", {"reason": "</Say><Dial>evil</Dial><Say>"}
    )
    twiml = provider.last_twiml()
    # The reason may still appear as spoken text; what must never happen is a
    # second <Dial> verb, which would place an extra outbound call.
    assert twiml.count("<Dial") == 1, "a second Dial verb was injected"
    assert twiml.count("<Number>") == 1
    assert f"<Number>{HUMAN}</Number>" in twiml

