"""Step 5 (scale-compliance): the real-call E2E safety guard.

Deterministic tests for the guard's classification logic and its wiring into
the `/telephony/voice` webhook. Nothing here places a real call, touches the
network, or uses real credentials. The Twilio webhook signature is bypassed by
the shared dev harness (see tests/conftest.py) so the route can be exercised
with plain form posts; every state change is asserted in the in-memory DB.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import Settings, settings
from app.db.models import Call, CallStatus
from app.telephony import e2e_guard
from tests.conftest import make_tenant

TEST_NUMBER = "+15551112222"
ALLOWED_CALLER = "+15551110000"


def make_settings(**overrides) -> Settings:
    defaults = dict(
        app_env="development",
        e2e_enabled=False,
        e2e_test_number=TEST_NUMBER,
        e2e_allowed_callers=ALLOWED_CALLER,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def fake_tenant(*, is_test: bool, number: str = TEST_NUMBER, name: str = "E2E Co"):
    return SimpleNamespace(is_test_tenant=is_test, twilio_number=number, name=name)


# ======================================================== classification ===


def test_disarmed_is_a_noop_for_normal_traffic():
    assert e2e_guard.check_inbound(
        make_settings(),
        from_number="+15559990000",
        to_number=TEST_NUMBER,
        tenant=fake_tenant(is_test=False),
    ) is None


def test_disarmed_is_a_noop_even_in_production():
    assert e2e_guard.check_inbound(
        make_settings(app_env="production"),
        from_number=ALLOWED_CALLER,
        to_number=TEST_NUMBER,
        tenant=fake_tenant(is_test=True),
    ) is None


def test_armed_in_production_is_rejected():
    with pytest.raises(e2e_guard.E2ERejected):
        e2e_guard.check_inbound(
            make_settings(app_env="production", e2e_enabled=True),
            from_number=ALLOWED_CALLER,
            to_number=TEST_NUMBER,
            tenant=fake_tenant(is_test=True),
        )


def test_armed_without_test_number_is_a_configuration_error():
    with pytest.raises(e2e_guard.E2EConfigurationError):
        e2e_guard.check_inbound(
            make_settings(e2e_enabled=True, e2e_test_number=""),
            from_number=ALLOWED_CALLER,
            to_number=TEST_NUMBER,
            tenant=fake_tenant(is_test=True),
        )


def test_armed_without_allowlisted_callers_is_a_configuration_error():
    with pytest.raises(e2e_guard.E2EConfigurationError):
        e2e_guard.check_inbound(
            make_settings(e2e_enabled=True, e2e_allowed_callers=""),
            from_number=ALLOWED_CALLER,
            to_number=TEST_NUMBER,
            tenant=fake_tenant(is_test=True),
        )


def test_armed_rejects_a_non_test_dialed_number():
    with pytest.raises(e2e_guard.E2ERejected):
        e2e_guard.check_inbound(
            make_settings(e2e_enabled=True),
            from_number=ALLOWED_CALLER,
            to_number="+15559991234",
            tenant=fake_tenant(is_test=True),
        )


def test_armed_rejects_a_non_allowlisted_caller():
    with pytest.raises(e2e_guard.E2ERejected):
        e2e_guard.check_inbound(
            make_settings(e2e_enabled=True),
            from_number="+15559990000",
            to_number=TEST_NUMBER,
            tenant=fake_tenant(is_test=True),
        )


def test_armed_rejects_a_tenant_that_is_not_a_test_tenant():
    with pytest.raises(e2e_guard.E2ERejected):
        e2e_guard.check_inbound(
            make_settings(e2e_enabled=True),
            from_number=ALLOWED_CALLER,
            to_number=TEST_NUMBER,
            tenant=fake_tenant(is_test=False),
        )


def test_armed_rejects_a_missing_tenant():
    with pytest.raises(e2e_guard.E2ERejected):
        e2e_guard.check_inbound(
            make_settings(e2e_enabled=True),
            from_number=ALLOWED_CALLER,
            to_number=TEST_NUMBER,
            tenant=None,
        )


def test_armed_rejects_when_test_tenant_answers_a_different_number():
    with pytest.raises(e2e_guard.E2ERejected):
        e2e_guard.check_inbound(
            make_settings(e2e_enabled=True),
            from_number=ALLOWED_CALLER,
            to_number=TEST_NUMBER,
            tenant=fake_tenant(is_test=True, number="+15559994321"),
        )


def test_armed_accepts_an_allowlisted_test_tenant_call():
    ctx = e2e_guard.check_inbound(
        make_settings(e2e_enabled=True),
        from_number=ALLOWED_CALLER,
        to_number=TEST_NUMBER,
        tenant=fake_tenant(is_test=True, name="E2E Clinic"),
    )
    assert ctx is not None
    assert ctx.from_number == ALLOWED_CALLER
    assert ctx.to_number == TEST_NUMBER
    assert ctx.test_tenant_name == "E2E Clinic"


def test_allowlist_matching_is_format_insensitive():
    # Twilio may deliver "From" formatted ("+1 (555) 111-0000") vs the E.164
    # allowlist ("+15551110000"). The guard must normalize both sides.
    ctx = e2e_guard.check_inbound(
        make_settings(e2e_enabled=True),
        from_number="+1 (555) 111-0000",
        to_number=TEST_NUMBER,
        tenant=fake_tenant(is_test=True),
    )
    assert ctx is not None


def test_rejection_messages_never_expose_numbers_or_secrets():
    scenarios = [
        dict(from_number="+15559990000", to_number=TEST_NUMBER,
             tenant=fake_tenant(is_test=True)),          # caller not allowed
        dict(from_number=ALLOWED_CALLER, to_number="+15559991234",
             tenant=fake_tenant(is_test=True)),          # wrong dialed number
        dict(from_number=ALLOWED_CALLER, to_number=TEST_NUMBER,
             tenant=fake_tenant(is_test=False)),         # not a test tenant
        dict(from_number=ALLOWED_CALLER, to_number=TEST_NUMBER,
             tenant=fake_tenant(is_test=True, number="+15559994321")),
    ]
    for kw in scenarios:
        with pytest.raises(e2e_guard.E2ERejected) as exc_info:
            e2e_guard.check_inbound(make_settings(e2e_enabled=True), **kw)
        message = str(exc_info.value)
        assert "1555" not in message
        assert "E2E_TEST" not in message.upper()
        assert "TOKEN" not in message.upper()


def test_mode_label_reflects_armed_state():
    assert e2e_guard.mode_label(make_settings()) == "production"
    assert e2e_guard.mode_label(make_settings(e2e_enabled=True)) == "e2e-test"


# ================================================== settings validation ===


def test_validate_security_refuses_e2e_in_production():
    problems = make_settings(app_env="production", e2e_enabled=True).validate_security()
    assert any("E2E_ENABLED" in p for p in problems)


def test_validate_security_requires_a_test_number_when_armed():
    problems = make_settings(e2e_enabled=True, e2e_test_number="").validate_security()
    assert any("E2E_TEST_NUMBER" in p for p in problems)


def test_validate_security_requires_callers_when_armed():
    problems = make_settings(e2e_enabled=True, e2e_allowed_callers="").validate_security()
    assert any("E2E_ALLOWED_CALLERS" in p for p in problems)


def test_validate_security_is_silent_when_disarmed_or_complete():
    disarmed = make_settings().validate_security()
    assert not any("E2E" in p for p in disarmed)
    complete = make_settings(e2e_enabled=True).validate_security()
    assert not any("E2E" in p for p in complete)


# ================================================== /telephony/voice wiring ===


async def post_voice(client, sid, frm, to):
    return await client.post(
        "/telephony/voice",
        data={"CallSid": sid, "From": frm, "To": to},
    )


async def count_calls(db, sid):
    rows = (await db.execute(select(Call).where(Call.call_sid == sid))).scalars().all()
    return rows


@pytest.mark.asyncio
async def test_disarmed_voice_answers_normal_traffic_and_creates_a_call(client, db):
    tenant = await make_tenant(db, "Normal Clinic")
    resp = await post_voice(client, "CA11111111111111111111111111111111",
                            "+15559990000", tenant.twilio_number)

    assert resp.status_code == 200
    assert "Stream" in resp.text
    rows = await count_calls(db, "CA11111111111111111111111111111111")
    assert len(rows) == 1
    assert rows[0].status == CallStatus.IN_PROGRESS
    assert rows[0].tenant_id == tenant.id


@pytest.mark.asyncio
async def test_armed_voice_rejects_non_allowlisted_caller_without_a_call(
    client, db, monkeypatch
):
    tenant = await make_tenant(db, "E2E Clinic")
    tenant.twilio_number = TEST_NUMBER
    tenant.is_test_tenant = True
    await db.commit()

    monkeypatch.setattr(settings, "e2e_enabled", True)
    monkeypatch.setattr(settings, "e2e_test_number", TEST_NUMBER)
    monkeypatch.setattr(settings, "e2e_allowed_callers", ALLOWED_CALLER)

    resp = await post_voice(client, "CA22222222222222222222222222222222",
                            "+15559990000", TEST_NUMBER)

    assert resp.status_code == 200
    assert "test mode" in resp.text
    assert await count_calls(db, "CA22222222222222222222222222222222") == []


@pytest.mark.asyncio
async def test_armed_voice_rejects_a_tenant_not_marked_as_test(client, db, monkeypatch):
    tenant = await make_tenant(db, "Real Tenant")
    tenant.twilio_number = TEST_NUMBER  # a real tenant answering the test number
    await db.commit()

    monkeypatch.setattr(settings, "e2e_enabled", True)
    monkeypatch.setattr(settings, "e2e_test_number", TEST_NUMBER)
    monkeypatch.setattr(settings, "e2e_allowed_callers", ALLOWED_CALLER)

    resp = await post_voice(client, "CA33333333333333333333333333333333",
                            ALLOWED_CALLER, TEST_NUMBER)

    assert resp.status_code == 200
    assert "test mode" in resp.text
    assert await count_calls(db, "CA33333333333333333333333333333333") == []


@pytest.mark.asyncio
async def test_armed_voice_accepts_an_allowlisted_test_tenant_call(
    client, db, monkeypatch
):
    tenant = await make_tenant(db, "E2E Clinic")
    tenant.twilio_number = TEST_NUMBER
    tenant.is_test_tenant = True
    await db.commit()

    monkeypatch.setattr(settings, "e2e_enabled", True)
    monkeypatch.setattr(settings, "e2e_test_number", TEST_NUMBER)
    monkeypatch.setattr(settings, "e2e_allowed_callers", ALLOWED_CALLER)

    resp = await post_voice(client, "CA44444444444444444444444444444444",
                            ALLOWED_CALLER, TEST_NUMBER)

    assert resp.status_code == 200
    assert "Stream" in resp.text
    rows = await count_calls(db, "CA44444444444444444444444444444444")
    assert len(rows) == 1
    assert rows[0].tenant_id == tenant.id


@pytest.mark.asyncio
async def test_armed_voice_with_missing_config_fails_closed(client, db, monkeypatch):
    monkeypatch.setattr(settings, "e2e_enabled", True)
    monkeypatch.setattr(settings, "e2e_test_number", "")
    monkeypatch.setattr(settings, "e2e_allowed_callers", ALLOWED_CALLER)

    resp = await post_voice(client, "CA55555555555555555555555555555555",
                            ALLOWED_CALLER, TEST_NUMBER)

    assert resp.status_code == 503
    assert await count_calls(db, "CA55555555555555555555555555555555") == []


@pytest.mark.asyncio
async def test_duplicate_voice_delivery_does_not_create_a_second_call(client, db):
    """Twilio may re-deliver /voice for the same CallSid on a network retry."""
    tenant = await make_tenant(db, "Retry Clinic")
    sid = "CA66666666666666666666666666666666"

    first = await post_voice(client, sid, "+15559990000", tenant.twilio_number)
    second = await post_voice(client, sid, "+15559990000", tenant.twilio_number)

    assert first.status_code == 200
    assert second.status_code == 200
    rows = await count_calls(db, sid)
    assert len(rows) == 1
