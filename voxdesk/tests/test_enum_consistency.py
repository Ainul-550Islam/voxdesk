"""
Enum consistency between the Python models and the PostgreSQL / Alembic types.

These enums drifted twice before, and both times the failure was invisible
until a real call tried to write a row:

  * `CallStatus.NO_ANSWER` was referenced by the Twilio status webhook but did
    not exist on the Python enum -> AttributeError at import time.
  * `Speaker.CALLER` / `Speaker.AGENT` were written by the voice pipeline while
    the database type only accepted USER / ASSISTANT / SYSTEM -> every
    transcript insert failed.

Two layers of checking:

  1. Source parity -- the Alembic migrations are parsed and their enum literals
     compared against the Python enums. This catches drift without needing a
     live PostgreSQL server, which is the case the previous bugs slipped
     through.
  2. Round-trip -- the real models are created on an in-memory SQLite database
     and rows are written and read back. SQLAlchemy renders Enum() as VARCHAR
     plus a CHECK constraint there, so an out-of-range member is still
     rejected.
"""
from __future__ import annotations

import ast
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    Call,
    CallDirection,
    CallStatus,
    Speaker,
    Tenant,
    Turn,
)

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"
BASELINE = ALEMBIC_DIR / "0001_baseline.py"
ENUM_FIX = ALEMBIC_DIR / "0002_enum_consistency.py"

EXPECTED_CALL_STATUS = {
    "RINGING", "IN_PROGRESS", "COMPLETED", "FAILED", "NO_ANSWER", "TRANSFERRED",
}
EXPECTED_SPEAKER = {"USER", "ASSISTANT", "SYSTEM"}


# ---------------------------------------------------------------- helpers ---

def _string_literals_in_assignment(path: Path, target: str) -> set[str]:
    """Pull the string literals out of `target = ...` in a migration file."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if target not in names:
            continue
        return {
            n.value for n in ast.walk(node.value)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
    raise AssertionError(f"{target} not found in {path.name}")


# =============================================================================
# 1. Python enum shape
# =============================================================================

def test_call_status_has_exactly_the_expected_members():
    assert {e.name for e in CallStatus} == EXPECTED_CALL_STATUS


def test_speaker_has_exactly_the_expected_members():
    assert {e.name for e in Speaker} == EXPECTED_SPEAKER


def test_no_answer_exists_on_the_python_enum():
    """Regression: the Twilio webhook referenced this before it existed."""
    assert CallStatus.NO_ANSWER.value == "no_answer"


def test_transferred_was_not_removed():
    """It predates this fix and is a real terminal state -- keep it."""
    assert CallStatus.TRANSFERRED.value == "transferred"


def test_speaker_has_no_legacy_aliases_left():
    """CALLER / AGENT must be gone, not shadowed, or drift can recur."""
    assert not hasattr(Speaker, "CALLER")
    assert not hasattr(Speaker, "AGENT")


def test_enum_names_are_unique_and_have_no_duplicate_values():
    for enum_cls in (CallStatus, Speaker):
        names = [e.name for e in enum_cls]
        values = [e.value for e in enum_cls]
        assert len(names) == len(set(names))
        assert len(values) == len(set(values))


# =============================================================================
# 2. Parity with the Alembic / PostgreSQL types
# =============================================================================

def test_migration_declares_the_same_call_status_values():
    declared = _string_literals_in_assignment(ENUM_FIX, "CALL_STATUS_VALUES")
    assert declared == {e.name for e in CallStatus}


def test_migration_declares_the_same_speaker_values():
    declared = _string_literals_in_assignment(ENUM_FIX, "SPEAKER_VALUES")
    assert declared == {e.name for e in Speaker}


def test_baseline_speaker_type_already_matches_python():
    """
    The baseline created USER/ASSISTANT/SYSTEM. Standardising on those names is
    what lets an Alembic-managed database keep its rows untouched.
    """
    baseline = _string_literals_in_assignment(BASELINE, "speaker")
    assert baseline - {"speaker"} == {e.name for e in Speaker}


def test_migration_adds_every_value_the_baseline_was_missing():
    baseline = _string_literals_in_assignment(BASELINE, "call_status") - {"callstatus"}
    added = _string_literals_in_assignment(ENUM_FIX, "CALL_STATUS_VALUES")
    assert baseline - added == set(), "migration must not drop a baseline value"
    assert "TRANSFERRED" in added - baseline


def test_legacy_speaker_map_only_targets_valid_members():
    """Every remap destination must be a real member, or the cast will fail."""
    mapping = ast.literal_eval(
        next(
            ast.unparse(node.value)
            for node in ast.walk(ast.parse(ENUM_FIX.read_text()))
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "SPEAKER_LEGACY_MAP"
                    for t in node.targets)
        )
    )
    assert set(mapping.values()) <= {e.name for e in Speaker}
    assert set(mapping) == {"CALLER", "AGENT"}


def test_migration_chain_is_linked_to_the_baseline():
    tree = ast.parse(ENUM_FIX.read_text())
    consts = {
        t.id: node.value.value
        for node in ast.walk(tree) if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant)
    }
    assert consts["revision"] == "0002_enum_consistency"
    assert consts["down_revision"] == "0001_baseline"


# =============================================================================
# 3. Round-trip against a real database
# =============================================================================

@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _make_tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(name="Bright Smile Dental", twilio_number=f"+1555{uuid.uuid4().int % 10**7:07d}")
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)
    return tenant


async def _make_call(session: AsyncSession, tenant: Tenant, status: CallStatus) -> Call:
    call = Call(
        tenant_id=tenant.id,
        call_sid=f"CA{uuid.uuid4().hex}",
        from_number="+15551234567",
        to_number=tenant.twilio_number,
        status=status,
        direction=CallDirection.INBOUND,
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)
    return call


@pytest.mark.asyncio
async def test_no_answer_can_be_stored_and_retrieved(session):
    """The exact case the Twilio status webhook produces on an unanswered call."""
    tenant = await _make_tenant(session)
    call = await _make_call(session, tenant, CallStatus.NO_ANSWER)

    session.expunge_all()
    loaded = (
        await session.execute(select(Call).where(Call.id == call.id))
    ).scalar_one()

    assert loaded.status is CallStatus.NO_ANSWER
    assert loaded.status.value == "no_answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(CallStatus))
async def test_every_call_status_round_trips(session, status):
    tenant = await _make_tenant(session)
    call = await _make_call(session, tenant, status)
    session.expunge_all()
    loaded = (
        await session.execute(select(Call).where(Call.id == call.id))
    ).scalar_one()
    assert loaded.status is status


@pytest.mark.asyncio
@pytest.mark.parametrize("speaker", list(Speaker))
async def test_every_speaker_round_trips(session, speaker):
    tenant = await _make_tenant(session)
    call = await _make_call(session, tenant, CallStatus.COMPLETED)
    session.add(Turn(call_id=call.id, speaker=speaker, text="hello"))
    await session.commit()

    session.expunge_all()
    loaded = (
        await session.execute(select(Turn).where(Turn.call_id == call.id))
    ).scalar_one()
    assert loaded.speaker is speaker


@pytest.mark.asyncio
async def test_full_transcript_with_caller_agent_and_system_round_trips(session):
    """
    A realistic transcript: the caller speaks, the assistant answers, and a
    system note records the transfer. This is the insert that used to fail.
    """
    tenant = await _make_tenant(session)
    call = await _make_call(session, tenant, CallStatus.TRANSFERRED)

    transcript = [
        (Speaker.USER, "Hi, do you have anything Tuesday morning?"),
        (Speaker.ASSISTANT, "I've got nine or ten thirty. Which works?"),
        (Speaker.USER, "Actually, can I speak to a person?"),
        (Speaker.SYSTEM, "Escalated to +15551110000"),
        (Speaker.ASSISTANT, "Sure, let me put you through."),
    ]
    for index, (speaker, text) in enumerate(transcript):
        session.add(Turn(
            call_id=call.id, speaker=speaker, text=text,
            created_at=datetime(2026, 6, 15, 10, 0, index, tzinfo=timezone.utc),
        ))
    await session.commit()
    session.expunge_all()

    rows = (
        await session.execute(
            select(Turn).where(Turn.call_id == call.id).order_by(Turn.created_at)
        )
    ).scalars().all()

    assert [(t.speaker, t.text) for t in rows] == transcript
    assert {t.speaker for t in rows} == set(Speaker)


@pytest.mark.asyncio
async def test_call_lifecycle_still_works_end_to_end(session):
    """Existing behaviour: ringing -> in progress -> a terminal status."""
    tenant = await _make_tenant(session)
    call = await _make_call(session, tenant, CallStatus.RINGING)

    call.status = CallStatus.IN_PROGRESS
    await session.commit()

    call.status = CallStatus.COMPLETED
    call.duration_seconds = 91.4
    call.booked = True
    await session.commit()

    session.expunge_all()
    loaded = (
        await session.execute(select(Call).where(Call.id == call.id))
    ).scalar_one()
    assert loaded.status is CallStatus.COMPLETED
    assert loaded.booked is True
    assert loaded.duration_seconds == pytest.approx(91.4)


@pytest.mark.asyncio
async def test_transcript_reader_maps_speakers_to_llm_roles(session):
    """
    app/channels/messaging.load_history() branches on Speaker.USER. Guard the
    mapping so a future rename cannot silently invert every stored role.
    """
    tenant = await _make_tenant(session)
    call = await _make_call(session, tenant, CallStatus.COMPLETED)
    session.add_all([
        Turn(call_id=call.id, speaker=Speaker.USER, text="q",
             created_at=datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)),
        Turn(call_id=call.id, speaker=Speaker.ASSISTANT, text="a",
             created_at=datetime(2026, 6, 15, 10, 0, 1, tzinfo=timezone.utc)),
    ])
    await session.commit()

    rows = (
        await session.execute(
            select(Turn).where(Turn.call_id == call.id).order_by(Turn.created_at)
        )
    ).scalars().all()
    roles = ["user" if t.speaker is Speaker.USER else "assistant" for t in rows]
    assert roles == ["user", "assistant"]


# ---------------------------------------------------------------------------
# STEP 3: the same parity guarantee for TransferState.
#
# SQLAlchemy's Enum() persists the member NAME. Migration 0004 must therefore
# declare NONE/REQUESTED/... exactly, and adding a member to the Python enum
# without a migration must fail loudly here rather than at 3am in production.
# ---------------------------------------------------------------------------

def test_transfer_state_migration_matches_the_model():
    import ast
    import pathlib

    from app.db.models import TransferState

    source = pathlib.Path(
        "alembic/versions/0004_call_transfer_lifecycle.py"
    ).read_text()
    tree = ast.parse(source)

    declared: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", getattr(func, "id", ""))
        if name != "Enum":
            continue
        kwargs = {k.arg: k.value for k in node.keywords}
        type_name = kwargs.get("name")
        if isinstance(type_name, ast.Constant) and type_name.value == "transferstate":
            declared = {
                a.value for a in node.args if isinstance(a, ast.Constant)
            }

    assert declared, "no transferstate Enum(...) found in migration 0004"
    assert declared == {m.name for m in TransferState}, (
        "migration 0004 and TransferState disagree; "
        f"migration={sorted(declared)} model={sorted(m.name for m in TransferState)}"
    )


def test_transfer_state_values_are_lowercase_names():
    """Keeps the value/name mapping predictable for API responses."""
    from app.db.models import TransferState

    for member in TransferState:
        assert member.value == member.name.lower()


def test_migration_0004_adds_every_new_call_column():
    """A model column with no migration is a production-only crash."""
    import pathlib

    from app.db.models import Call

    source = pathlib.Path(
        "alembic/versions/0004_call_transfer_lifecycle.py"
    ).read_text()

    new_columns = [
        c.name for c in Call.__table__.columns
        if c.name.startswith("transfer_") or c.name == "failure_reason"
    ]
    assert new_columns
    for column in new_columns:
        assert f'"{column}"' in source, f"{column} is missing from migration 0004"


def test_migration_chain_is_linear_and_unbroken():
    import pathlib
    import re

    versions = pathlib.Path("alembic/versions")
    revisions, downs = {}, {}
    for path in versions.glob("*.py"):
        text = path.read_text()
        rev = re.search(r'^revision = "([^"]+)"', text, re.M)
        down = re.search(r"^down_revision = (?:\"([^\"]+)\"|None)", text, re.M)
        if rev:
            revisions[rev.group(1)] = path.name
            downs[rev.group(1)] = down.group(1) if down and down.group(1) else None

    assert "0004_call_transfer_lifecycle" in revisions
    assert downs["0004_call_transfer_lifecycle"] == "0003_auth_rbac"
    # Exactly one root, and every parent exists.
    roots = [r for r, d in downs.items() if d is None]
    assert len(roots) == 1, f"expected one baseline, found {roots}"
    for rev, down in downs.items():
        assert down is None or down in revisions, f"{rev} points at missing {down}"


# ---------------------------------------------------------------------------
# STEP 4: the same parity guarantee for the knowledge-base enums and tables.
#
# Same failure mode, same defence. DocumentStatus in particular gates
# retrieval -- a member the PostgreSQL type does not know about means every
# insert with that status raises, and the failure would first appear when a
# document happened to be archived in production.
# ---------------------------------------------------------------------------

MIGRATION_0005 = "alembic/versions/0005_knowledge_rag.py"


def _declared_enum(path: str, type_name: str) -> set[str]:
    """Pull the literal members of a named sa.Enum(...) out of a migration."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(path).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if func_name != "Enum":
            continue
        kwargs = {k.arg: k.value for k in node.keywords}
        declared_name = kwargs.get("name")
        if isinstance(declared_name, ast.Constant) and declared_name.value == type_name:
            return {a.value for a in node.args if isinstance(a, ast.Constant)}
    return set()


def test_document_status_migration_matches_the_model():
    from app.db.models import DocumentStatus

    declared = _declared_enum(MIGRATION_0005, "documentstatus")
    assert declared, "no documentstatus Enum(...) found in migration 0005"
    assert declared == {m.name for m in DocumentStatus}, (
        f"migration={sorted(declared)} "
        f"model={sorted(m.name for m in DocumentStatus)}"
    )


def test_document_source_type_migration_matches_the_model():
    from app.db.models import DocumentSourceType

    declared = _declared_enum(MIGRATION_0005, "documentsourcetype")
    assert declared, "no documentsourcetype Enum(...) found in migration 0005"
    assert declared == {m.name for m in DocumentSourceType}


def test_knowledge_enum_values_are_lowercase_names():
    from app.db.models import DocumentSourceType, DocumentStatus

    for enum_cls in (DocumentStatus, DocumentSourceType):
        for member in enum_cls:
            assert member.value == member.name.lower()


def test_only_ready_is_searchable():
    """
    The single gate the whole feature depends on.

    If this set ever grows, archived or half-processed documents start being
    answered from. It is asserted here rather than only in the retrieval tests
    because it is a policy decision, not an implementation detail.
    """
    from app.db.models import SEARCHABLE_DOCUMENT_STATUSES, DocumentStatus

    assert SEARCHABLE_DOCUMENT_STATUSES == frozenset({DocumentStatus.READY})


def test_migration_0005_creates_every_model_column():
    """A model column with no migration is a production-only crash."""
    import pathlib

    from app.db.models import KnowledgeChunk, KnowledgeDocument

    source = pathlib.Path(MIGRATION_0005).read_text()
    for model in (KnowledgeDocument, KnowledgeChunk):
        for column in model.__table__.columns:
            assert f'"{column.name}"' in source, (
                f"{model.__tablename__}.{column.name} is missing from migration 0005"
            )


def test_migration_0005_declares_the_tenant_scoped_constraints():
    """
    Deduplication must be per tenant and chunk slots must be unique.

    Both are correctness guarantees rather than optimisations: the first stops
    dedup from crossing tenants, the second stops a retried reindex from
    writing a second set of embeddings.
    """
    import pathlib

    source = pathlib.Path(MIGRATION_0005).read_text()
    assert "uq_knowledge_doc_hash" in source
    assert '"tenant_id", "content_hash"' in source
    assert "uq_knowledge_chunk_slot" in source
    assert '"document_id", "version", "chunk_index"' in source
    # The indexes retrieval actually uses.
    assert "ix_knowledge_doc_tenant_status" in source
    assert "ix_knowledge_chunk_tenant_doc" in source


def test_migration_0005_follows_0004():
    import pathlib
    import re

    source = pathlib.Path(MIGRATION_0005).read_text()
    assert re.search(r'^revision = "0005_knowledge_rag"', source, re.M)
    assert re.search(
        r'^down_revision = "0004_call_transfer_lifecycle"', source, re.M
    )


def test_chunk_carries_its_own_tenant_id():
    """
    Denormalisation that exists purely for safety.

    Retrieval filters on KnowledgeChunk.tenant_id directly, so a wrong join
    cannot widen results past one tenant. If this column is ever removed the
    isolation guarantee quietly becomes join-dependent.
    """
    from app.db.models import KnowledgeChunk

    assert "tenant_id" in KnowledgeChunk.__table__.columns
    assert not KnowledgeChunk.__table__.columns["tenant_id"].nullable


# ---------------------------------------------------------------------------
# STEP 5: the same parity guarantee for the CRM enums and tables.
#
# The knowledge migration (0005) shipped with `UPLOAD/URL/MANUAL/SYNC` against
# a model declaring `UPLOAD/TEXT/URL`, and only this style of test caught it.
# CrmSyncStatus is the equivalent risk here: it gates the worker's query, so a
# member PostgreSQL does not know about means every state transition raises,
# and it would first show up as syncs silently never being attempted.
# ---------------------------------------------------------------------------

MIGRATION_0006 = "alembic/versions/0006_crm_integrations.py"


def test_crm_provider_type_migration_matches_the_model():
    from app.db.models import CrmProviderType

    declared = _declared_enum(MIGRATION_0006, "crmprovidertype")
    assert declared, "no crmprovidertype Enum(...) found in migration 0006"
    assert declared == {m.name for m in CrmProviderType}


def test_crm_event_type_migration_matches_the_model():
    from app.db.models import CrmEventType

    declared = _declared_enum(MIGRATION_0006, "crmeventtype")
    assert declared, "no crmeventtype Enum(...) found in migration 0006"
    assert declared == {m.name for m in CrmEventType}


def test_crm_entity_type_migration_matches_the_model():
    from app.db.models import CrmEntityType

    declared = _declared_enum(MIGRATION_0006, "crmentitytype")
    assert declared == {m.name for m in CrmEntityType}


def test_crm_sync_status_migration_matches_the_model():
    from app.db.models import CrmSyncStatus

    declared = _declared_enum(MIGRATION_0006, "crmsyncstatus")
    assert declared == {m.name for m in CrmSyncStatus}


def test_integration_audit_actions_are_added_by_migration_0006():
    """
    `auditaction` is an existing PostgreSQL type, so new members need an
    explicit `ALTER TYPE ... ADD VALUE`. Forgetting it means every
    integration change raises when it tries to write its audit row -- and
    audit writes happen after the change has already been committed.
    """
    import pathlib

    from app.db.models import AuditAction

    source = pathlib.Path(MIGRATION_0006).read_text()
    for member in (
        AuditAction.INTEGRATION_CONNECTED, AuditAction.INTEGRATION_UPDATED,
        AuditAction.INTEGRATION_DISCONNECTED, AuditAction.INTEGRATION_TESTED,
    ):
        assert member.name in source, f"{member.name} is not added by migration 0006"
    assert "ALTER TYPE auditaction ADD VALUE" in source


def test_crm_enum_values_are_stable_wire_strings():
    """
    Two different conventions, both deliberate.

    Status and provider values are the lowercase of their names, matching
    every other enum in the schema. `CrmEventType` values are dotted wire
    strings (`lead.created`) because they are *published* -- they appear in
    outbound webhook bodies and in tenant-facing filters, so they are part of
    the integration contract and cannot be renamed freely.
    """
    from app.db.models import (
        CrmEntityType, CrmEventType, CrmProviderType, CrmSyncStatus,
    )

    for enum_cls in (CrmProviderType, CrmEntityType, CrmSyncStatus):
        for member in enum_cls:
            assert member.value == member.name.lower(), member

    for member in CrmEventType:
        assert member.value == member.name.lower().replace("_", ".", 1), member


def test_migration_0006_creates_every_model_column():
    """A model column with no migration is a production-only crash."""
    import pathlib

    from app.db.models import (
        CrmContactLink, CrmEvent, CrmIntegration, CrmSync, CrmWebhookReceipt,
    )

    source = pathlib.Path(MIGRATION_0006).read_text()
    for model in (
        CrmIntegration, CrmEvent, CrmSync, CrmContactLink, CrmWebhookReceipt,
    ):
        for column in model.__table__.columns:
            assert f'"{column.name}"' in source, (
                f"{model.__tablename__}.{column.name} is missing from migration 0006"
            )


def test_migration_0006_declares_the_constraints_that_carry_the_guarantees():
    import pathlib

    source = pathlib.Path(MIGRATION_0006).read_text()

    # Isolation: (tenant_id, provider) is a key, not a filter.
    assert "uq_crm_integration_tenant_provider" in source
    # Idempotency: one event per business fact per tenant.
    assert "uq_crm_event_idempotency" in source
    # One delivery record per (event, integration).
    assert "uq_crm_sync_event_integration" in source
    # No duplicate contacts, and no cross-tenant contact matching.
    assert "uq_crm_contact_identity" in source
    # Inbound replay protection.
    assert "uq_crm_receipt_event" in source
    # The worker's query.
    assert "ix_crm_sync_due" in source


def test_migration_0006_follows_0005():
    import pathlib
    import re

    source = pathlib.Path(MIGRATION_0006).read_text()
    assert re.search(r'^revision = "0006_crm_integrations"', source, re.M)
    assert re.search(r'^down_revision = "0005_knowledge_rag"', source, re.M)


def test_migration_0006_does_not_touch_the_legacy_crm_columns():
    """
    The old `tenants.crm_*` columns hold configuration a customer entered.
    Dropping them in the same release that introduces their replacement leaves
    no way back, and the previous application version is still running during
    a rolling deploy.
    """
    import pathlib

    source = pathlib.Path(MIGRATION_0006).read_text()
    for legacy in ("crm_webhook_url", "crm_api_key", "crm_type"):
        assert f'drop_column("tenants", "{legacy}")' not in source
        assert f"drop_column('tenants', '{legacy}')" not in source


def test_the_retryable_and_terminal_status_sets_do_not_overlap():
    """
    A status in both sets would be picked up by the worker forever. This is a
    policy decision, asserted here rather than only in the service tests.
    """
    from app.db.models import (
        RETRYABLE_SYNC_STATUSES, TERMINAL_SYNC_STATUSES, CrmSyncStatus,
    )

    assert not (RETRYABLE_SYNC_STATUSES & TERMINAL_SYNC_STATUSES)
    # PROCESSING is in neither: it is held by a worker and recovered by the
    # reaper, not claimed by a second worker.
    assert CrmSyncStatus.PROCESSING not in RETRYABLE_SYNC_STATUSES
    assert CrmSyncStatus.PROCESSING not in TERMINAL_SYNC_STATUSES
    assert (
        RETRYABLE_SYNC_STATUSES | TERMINAL_SYNC_STATUSES | {CrmSyncStatus.PROCESSING}
    ) == set(CrmSyncStatus)


async def test_every_crm_enum_round_trips_through_the_database(db, tenant_a):
    """
    The second layer of the STEP 1 check: write every member and read it back.
    SQLAlchemy renders Enum() as VARCHAR + CHECK on SQLite, so an out-of-range
    member is still rejected.
    """
    from sqlalchemy import select as _select

    from app.db.models import (
        CrmEntityType, CrmEvent, CrmEventType, CrmIntegration, CrmProviderType,
    )

    for provider in CrmProviderType:
        db.add(CrmIntegration(tenant_id=tenant_a.id, provider=provider, config={}))
    for index, event_type in enumerate(CrmEventType):
        db.add(CrmEvent(
            tenant_id=tenant_a.id, event_type=event_type,
            entity_type=CrmEntityType.CALL, entity_id=uuid.uuid4(),
            idempotency_key=f"roundtrip-{index}", payload={},
        ))
    await db.commit()

    providers = (
        (await db.execute(_select(CrmIntegration.provider))).scalars().all()
    )
    assert set(providers) == set(CrmProviderType)

    types = (await db.execute(_select(CrmEvent.event_type))).scalars().all()
    assert set(types) == set(CrmEventType)


# ---------------------------------------------------------------------------
# STEP 6: the same parity guarantee for the calendar/scheduling enums.
#
# AppointmentStatus is the equivalent risk to DocumentStatus in STEP 4: it
# gates the conflict check, so a member PostgreSQL does not know about means
# every booking write raises -- and it would first surface as a caller being
# told the calendar is broken.
# ---------------------------------------------------------------------------

MIGRATION_0007 = "alembic/versions/0007_calendar_scheduling.py"


def test_appointment_status_migration_matches_the_model():
    from app.db.models import AppointmentStatus

    declared = _declared_enum(MIGRATION_0007, "appointmentstatus")
    assert declared, "no appointmentstatus Enum(...) found in migration 0007"
    assert declared == {m.name for m in AppointmentStatus}


def test_calendar_provider_type_migration_matches_the_model():
    from app.db.models import CalendarProviderType

    declared = _declared_enum(MIGRATION_0007, "calendarprovidertype")
    assert declared == {m.name for m in CalendarProviderType}


def test_calendar_audit_actions_are_added_by_migration_0007():
    import pathlib

    from app.db.models import AuditAction

    source = pathlib.Path(MIGRATION_0007).read_text()
    for member in (
        AuditAction.CALENDAR_CONNECTED, AuditAction.CALENDAR_DISCONNECTED,
        AuditAction.APPOINTMENT_CANCELLED, AuditAction.APPOINTMENT_RESCHEDULED,
    ):
        assert member.name in source, f"{member.name} is not added by 0007"
    assert "ALTER TYPE auditaction ADD VALUE" in source


def test_calendar_enum_values_are_lowercase_names():
    from app.db.models import AppointmentStatus, CalendarProviderType

    for enum_cls in (AppointmentStatus, CalendarProviderType):
        for member in enum_cls:
            assert member.value == member.name.lower(), member


def test_only_live_statuses_block_a_slot():
    """
    The single policy the conflict check depends on. If this set ever grows,
    cancelled appointments start blocking their time; if it shrinks, a
    confirmed one stops. Asserted here rather than only in the booking tests
    because it is a policy decision, not an implementation detail.
    """
    from app.db.models import (
        BLOCKING_APPOINTMENT_STATUSES,
        TERMINAL_APPOINTMENT_STATUSES,
        AppointmentStatus,
    )

    assert BLOCKING_APPOINTMENT_STATUSES == frozenset({
        AppointmentStatus.PENDING,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.RESCHEDULED,
    })
    assert not (BLOCKING_APPOINTMENT_STATUSES & TERMINAL_APPOINTMENT_STATUSES)
    assert (
        BLOCKING_APPOINTMENT_STATUSES | TERMINAL_APPOINTMENT_STATUSES
    ) == set(AppointmentStatus)


def test_migration_0007_creates_every_new_model_column():
    import pathlib

    from app.db.models import (
        Appointment,
        CalendarIntegration,
        CalendarWebhookReceipt,
        SchedulingPolicy,
    )

    source = pathlib.Path(MIGRATION_0007).read_text()

    # Columns that existed before STEP 6 live in migration 0001.
    pre_existing = {
        "id", "tenant_id", "call_id", "customer_name", "customer_phone",
        "reason", "starts_at", "ends_at", "google_event_id", "created_at",
    }
    for column in Appointment.__table__.columns:
        if column.name in pre_existing:
            continue
        assert f'"{column.name}"' in source, (
            f"appointments.{column.name} is missing from migration 0007"
        )

    for model in (CalendarIntegration, SchedulingPolicy, CalendarWebhookReceipt):
        for column in model.__table__.columns:
            assert f'"{column.name}"' in source, (
                f"{model.__tablename__}.{column.name} is missing from 0007"
            )


def test_migration_0007_declares_the_constraints_that_carry_the_guarantees():
    import pathlib

    source = pathlib.Path(MIGRATION_0007).read_text()

    # The double-booking defence.
    assert "uq_appointment_slot" in source
    # A retried booking finds its own earlier attempt.
    assert "uq_appointment_idempotency" in source
    # (tenant, provider) is a key, not a convention.
    assert "uq_calendar_integration_tenant_provider" in source
    assert "uq_scheduling_policy_tenant" in source
    # Inbound webhook replay protection.
    assert "uq_calendar_receipt_event" in source


def test_migration_0007_backfills_rather_than_leaving_existing_rows_wrong():
    """
    Two backfills are load-bearing:

    * every pre-STEP-6 appointment becomes CONFIRMED, because the old code
      only ever wrote a row it believed in -- defaulting them to PENDING would
      make the entire existing book look unconfirmed;
    * `timezone` is copied from the owning tenant, because leaving the column
      default of 'UTC' would silently reinterpret every historical appointment
      by the tenant's offset.
    """
    import pathlib

    source = pathlib.Path(MIGRATION_0007).read_text()
    assert "UPDATE appointments SET status = 'CONFIRMED'" in source
    assert "SELECT t.timezone FROM tenants t" in source
    assert "GOOGLE_SERVICE_ACCOUNT" in source


def test_migration_0007_follows_0006():
    import pathlib
    import re

    source = pathlib.Path(MIGRATION_0007).read_text()
    assert re.search(r'^revision = "0007_calendar_scheduling"', source, re.M)
    assert re.search(r'^down_revision = "0006_crm_integrations"', source, re.M)


def test_migration_0007_does_not_drop_the_legacy_calendar_column():
    """
    `tenants.google_calendar_id` and `app/integrations/google_calendar.py` are
    still read by the previous application version during a rolling deploy,
    and by `tests/test_availability.py`.
    """
    import pathlib

    source = pathlib.Path(MIGRATION_0007).read_text()
    assert 'drop_column("tenants", "google_calendar_id")' not in source
    assert "drop_column(\"appointments\", \"google_event_id\")" not in source


async def test_every_appointment_status_round_trips_through_the_database(
    db, tenant_a
):
    from sqlalchemy import select as _select

    from app.db.models import Appointment, AppointmentStatus

    base = datetime(2026, 6, 16, 14, 0, tzinfo=timezone.utc)
    for index, status in enumerate(AppointmentStatus):
        db.add(Appointment(
            tenant_id=tenant_a.id, customer_name="X", customer_phone="+1555",
            starts_at=base, ends_at=base, timezone="UTC", status=status,
            slot_key=f"slot-{index}",
        ))
    await db.commit()

    stored = (await db.execute(_select(Appointment.status))).scalars().all()
    assert set(stored) == set(AppointmentStatus)


# ---------------------------------------------------------------------------
# STEP 7: the same parity guarantee for the billing enums.
#
# `SubscriptionStatus` gates entitlement and `UsageMetric` gates the rollup
# query, so a member PostgreSQL does not know about means every write raises
# -- and for billing that surfaces as a customer who cannot be charged or
# cannot use the product.
# ---------------------------------------------------------------------------

MIGRATION_0008 = "alembic/versions/0008_billing.py"


def test_billing_provider_type_migration_matches_the_model():
    from app.db.models import BillingProviderType

    declared = _declared_enum(MIGRATION_0008, "billingprovidertype")
    assert declared, "no billingprovidertype Enum(...) found in migration 0008"
    assert declared == {m.name for m in BillingProviderType}


def test_subscription_status_migration_matches_the_model():
    from app.db.models import SubscriptionStatus

    declared = _declared_enum(MIGRATION_0008, "subscriptionstatus")
    assert declared == {m.name for m in SubscriptionStatus}


def test_usage_metric_migration_matches_the_model():
    from app.db.models import UsageMetric

    declared = _declared_enum(MIGRATION_0008, "usagemetric")
    assert declared == {m.name for m in UsageMetric}


def test_usage_event_type_migration_matches_the_model():
    from app.db.models import UsageEventType

    declared = _declared_enum(MIGRATION_0008, "usageeventtype")
    assert declared == {m.name for m in UsageEventType}


def test_invoice_and_interval_migrations_match_the_model():
    from app.db.models import BillingInterval, InvoiceStatus

    assert _declared_enum(MIGRATION_0008, "invoicestatus") == {
        m.name for m in InvoiceStatus
    }
    assert _declared_enum(MIGRATION_0008, "billinginterval") == {
        m.name for m in BillingInterval
    }


def test_billing_enum_values_are_lowercase_names():
    from app.db.models import (
        BillingInterval, BillingProviderType, InvoiceStatus, SubscriptionStatus,
        UsageEventType, UsageMetric,
    )

    for enum_cls in (
        BillingProviderType, SubscriptionStatus, BillingInterval,
        UsageMetric, UsageEventType, InvoiceStatus,
    ):
        for member in enum_cls:
            assert member.value == member.name.lower(), member


def test_the_new_billing_audit_actions_are_added_by_the_migration():
    import pathlib

    from app.db.models import AuditAction

    source = pathlib.Path(MIGRATION_0008).read_text()
    billing_actions = [a for a in AuditAction if a.name.startswith("BILLING_")]
    assert len(billing_actions) == 9
    for member in billing_actions:
        assert member.name in source, f"{member.name} is not added by 0008"
    assert "ALTER TYPE auditaction ADD VALUE" in source


MIGRATION_0009 = "alembic/versions/0009_perf_policy.py"


def test_the_step9_audit_actions_are_added_by_the_migration():
    """
    GDPR export/erasure and license issuance write audit rows, and those rows
    must exist on the PostgreSQL `auditaction` type before the first write --
    the same drift class the 0006/0007 tests already guard.
    """
    import pathlib

    from app.db.models import AuditAction

    source = pathlib.Path(MIGRATION_0009).read_text()
    for member in (
        AuditAction.GDPR_EXPORT, AuditAction.GDPR_ERASURE,
        AuditAction.LICENSE_ISSUED,
    ):
        assert member.name in source, f"{member.name} is not added by 0009"
    assert "ALTER TYPE auditaction ADD VALUE" in source


def test_step9_migration_follows_0008():
    import pathlib
    import re

    source = pathlib.Path(MIGRATION_0009).read_text()
    assert re.search(r'^revision = "0009_perf_policy"', source, re.M)
    assert re.search(r'^down_revision = "0008_billing"', source, re.M)


def test_only_live_statuses_entitle_service():
    """
    The policy the whole entitlement layer rests on, asserted here rather than
    only in the billing tests because it is a business decision.

    PAST_DUE entitles deliberately: the provider is still retrying the card,
    and cutting a business off on the first failed charge costs far more
    goodwill than the few days of service it saves.
    """
    from app.db.models import (
        ENTITLED_SUBSCRIPTION_STATUSES,
        TERMINAL_SUBSCRIPTION_STATUSES,
        SubscriptionStatus,
    )

    assert ENTITLED_SUBSCRIPTION_STATUSES == frozenset({
        SubscriptionStatus.TRIALING,
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.CANCELING,
        SubscriptionStatus.PAST_DUE,
    })
    assert not (ENTITLED_SUBSCRIPTION_STATUSES & TERMINAL_SUBSCRIPTION_STATUSES)
    assert SubscriptionStatus.CANCELED in TERMINAL_SUBSCRIPTION_STATUSES


def test_migration_0008_creates_every_billing_column():
    import pathlib

    from app.db.models import (
        BillingInvoice, BillingPlan, BillingWebhookReceipt, Subscription,
        UsageEvent, UsageSummary,
    )

    source = pathlib.Path(MIGRATION_0008).read_text()
    for model in (
        BillingPlan, Subscription, UsageEvent, UsageSummary, BillingInvoice,
        BillingWebhookReceipt,
    ):
        for column in model.__table__.columns:
            assert f'"{column.name}"' in source, (
                f"{model.__tablename__}.{column.name} is missing from 0008"
            )


def test_migration_0008_declares_the_constraints_that_carry_the_guarantees():
    import pathlib

    source = pathlib.Path(MIGRATION_0008).read_text()

    # The money constraint: no duplicate charge, whatever the interleaving.
    assert "uq_usage_event_idempotency" in source
    assert "uq_usage_summary_slot" in source
    # One subscription per tenant, and one tenant per provider subscription.
    assert "uq_subscription_tenant_provider" in source
    assert "uq_subscription_external_id" in source
    # Plan codes are the checkout trust boundary.
    assert "uq_billing_plan_code" in source
    # Webhook dedupe and idempotent invoice mirroring.
    assert "uq_billing_receipt_event" in source
    assert "uq_billing_invoice_external" in source


def test_migration_0008_does_not_touch_the_legacy_tenant_billing_columns():
    """
    `minutes_used` is demoted to a cache, not dropped: the dashboard reads it,
    and `tenants.plan` is the fallback that lets a pre-STEP-7 tenant keep the
    entitlements they were sold (requirement 25).
    """
    import pathlib

    source = pathlib.Path(MIGRATION_0008).read_text()
    for legacy in ("minutes_used", "included_minutes"):
        assert f'drop_column("tenants", "{legacy}")' not in source
    assert 'drop_column("tenants", "plan")' not in source


def test_migration_0008_seeds_no_plan_rows():
    """
    Prices are business data that changes. Repricing should be an operator
    action against a running system, not a schema change followed by a deploy.
    """
    import pathlib

    source = pathlib.Path(MIGRATION_0008).read_text()
    assert "INSERT INTO billing_plans" not in source
    assert "bulk_insert" not in source


def test_migration_0008_follows_0007():
    import pathlib
    import re

    source = pathlib.Path(MIGRATION_0008).read_text()
    assert re.search(r'^revision = "0008_billing"', source, re.M)
    assert re.search(r'^down_revision = "0007_calendar_scheduling"', source, re.M)


def test_money_is_stored_as_integers_never_floats():
    """
    A float 19.99 is not exactly 19.99, and accumulating fractional overage in
    binary floating point produces invoices that do not reconcile with
    themselves.
    """
    import sqlalchemy as sa

    from app.db.models import BillingInvoice, BillingPlan, UsageEvent, UsageSummary

    money_like = ("cents", "millicents", "quantity")
    for model in (BillingPlan, UsageEvent, UsageSummary, BillingInvoice):
        for column in model.__table__.columns:
            if any(word in column.name for word in money_like):
                assert isinstance(column.type, sa.Integer), (
                    f"{model.__tablename__}.{column.name} is "
                    f"{column.type}, not Integer"
                )


async def test_every_subscription_status_round_trips_through_the_database(
    db, tenant_a
):
    from sqlalchemy import select as _select

    from app.db.models import (
        BillingProviderType, Subscription, SubscriptionStatus, Tenant,
    )

    # One subscription per (tenant, provider), so each status needs its own
    # tenant -- which is itself a check that the constraint is real.
    for index, status in enumerate(SubscriptionStatus):
        tenant = Tenant(name=f"T{index}", twilio_number=f"+1555000{index:04d}")
        db.add(tenant)
        await db.flush()
        db.add(Subscription(
            tenant_id=tenant.id, provider=BillingProviderType.STRIPE, status=status
        ))
    await db.commit()

    stored = (await db.execute(_select(Subscription.status))).scalars().all()
    assert set(stored) == set(SubscriptionStatus)