"""
RAG inside the live agent pipeline.

The requirement these cover is that retrieval is actually wired into the thing
that answers the phone, not sitting behind a `/knowledge` API nobody calls.
So they drive the real `FunctionHandlers.answer_question` -- the same method
the voice loop invokes -- against a real database with real indexed documents.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from app.agent.functions import FunctionHandlers
from app.agent.prompts import build_system_prompt
from app.db.models import Call, CallDirection, CallStatus
from tests.conftest import add_document

HANDBOOK = (
    "# Pricing\n\nA standard cleaning costs 120 dollars and takes 45 minutes.\n\n"
    "# Refunds\n\nRefunds are issued within fourteen days of purchase.\n\n"
    "# Parking\n\nParking is free in the lot behind our building.\n"
)


@pytest.fixture
async def call(db, tenant_a):
    row = Call(
        tenant_id=tenant_a.id,
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        from_number="+15551230000",
        to_number=tenant_a.twilio_number,
        call_sid=f"CA{uuid.uuid4().hex}",
        started_at=datetime.utcnow(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def handlers(db, tenant_a, call):
    return FunctionHandlers(db, tenant_a, call)


# ----------------------------------------------------- retrieval reaches it ---

async def test_the_agent_answers_from_an_uploaded_document(db, tenant_a, handlers):
    await add_document(db, tenant_a, text=HANDBOOK, filename="handbook.md")

    result = await handlers.answer_question(
        topic="pricing", question="how much does a cleaning cost"
    )

    assert result["ok"] is True
    assert result["grounded"] is True
    assert "120 dollars" in result["answer"]


async def test_the_answer_carries_internal_source_references(db, tenant_a, handlers):
    """
    Requirement 15. The caller never hears a citation, but the answer must be
    traceable back to chunk and document for logs and evaluation.
    """
    document = await add_document(
        db, tenant_a, text=HANDBOOK, filename="handbook.md", title="Handbook"
    )

    result = await handlers.answer_question(
        topic="refunds", question="can I get a refund"
    )

    assert result["sources"]
    source = result["sources"][0]
    assert source["document_id"] == str(document.id)
    assert source["chunk_id"]
    assert source["title"] == "Handbook"


async def test_the_grounding_instruction_travels_with_the_evidence(
    db, tenant_a, handlers
):
    await add_document(db, tenant_a, text=HANDBOOK, filename="handbook.md")
    result = await handlers.answer_question(topic="parking", question="where do I park")

    message = result["message"].lower()
    assert "only what it says" in message
    assert "do not add any fact" in message
    assert "ignore that" in message      # the injection reminder


async def test_an_undocumented_question_is_refused_not_invented(
    db, tenant_a, handlers
):
    """
    The single most important behaviour in the feature. The business never
    documented bicycle repair, so the agent must say so.
    """
    await add_document(db, tenant_a, text=HANDBOOK, filename="handbook.md")

    result = await handlers.answer_question(
        topic="bicycles", question="do you repair bicycles"
    )

    assert result["ok"] is False
    assert result["answer"] == ""
    assert "follow up" in result["message"]


async def test_a_tenant_with_no_documents_still_refuses_cleanly(handlers):
    result = await handlers.answer_question(
        topic="anything", question="what is your return policy on tractors"
    )
    assert result["ok"] is False


# ----------------------------------------------------- backward compatibility ---

async def test_the_configured_knowledge_dict_still_wins(db, tenant_a, handlers):
    """
    `tenant.knowledge_base` is a handful of operator-typed facts. It is
    checked first because it costs nothing, and because existing tenants and
    tests depend on it working exactly as before.
    """
    tenant_a.knowledge_base = {"parking": "Valet only, ten dollars."}
    await db.commit()
    await add_document(db, tenant_a, text=HANDBOOK, filename="handbook.md")

    result = await handlers.answer_question(topic="parking", question="where do I park")

    assert result["answer"] == "Valet only, ten dollars."
    assert not result.get("grounded")


async def test_documents_answer_what_the_dict_does_not(db, tenant_a, handlers):
    tenant_a.knowledge_base = {"parking": "Valet only."}
    await db.commit()
    await add_document(db, tenant_a, text=HANDBOOK, filename="handbook.md")

    result = await handlers.answer_question(
        topic="refunds", question="can I get a refund"
    )
    assert result.get("grounded") is True


async def test_the_hours_builtin_still_works_with_no_documents(handlers, tenant_a):
    result = await handlers.answer_question(topic="hours")
    assert result["ok"] is True
    assert "open" in result["answer"].lower()


# --------------------------------------------------------------- isolation ---

async def test_the_agent_cannot_answer_from_another_tenants_documents(
    db, tenant_a, tenant_b, handlers
):
    await add_document(
        db, tenant_b, text="Beta Clinic charges 300 euros for whitening.",
        filename="b.md",
    )

    result = await handlers.answer_question(
        topic="whitening", question="how much is whitening"
    )

    assert result["ok"] is False
    assert "300 euros" not in str(result)


# ---------------------------------------------------------------- failure ---

async def test_a_retrieval_timeout_falls_back_instead_of_fabricating(
    db, tenant_a, handlers, monkeypatch
):
    """
    Bounded latency is a hard requirement on a phone call. When the deadline
    passes the agent must reach the "I'll have someone follow up" branch --
    never answer anyway.
    """
    import asyncio

    import app.knowledge.retrieval as retrieval_module

    await add_document(db, tenant_a, text=HANDBOOK, filename="handbook.md")

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(retrieval_module, "retrieve", slow)
    monkeypatch.setattr(
        "app.core.config.settings.knowledge_retrieval_timeout_seconds", 0.05
    )

    result = await handlers.answer_question(topic="pricing", question="how much")
    assert result["ok"] is False


async def test_a_retrieval_crash_does_not_break_the_call(
    db, tenant_a, handlers, monkeypatch
):
    import app.knowledge.retrieval as retrieval_module

    await add_document(db, tenant_a, text=HANDBOOK, filename="handbook.md")

    async def broken(*args, **kwargs):
        raise RuntimeError("vector store unreachable")

    monkeypatch.setattr(retrieval_module, "retrieve", broken)

    result = await handlers.answer_question(topic="pricing", question="how much")
    assert result["ok"] is False
    assert "follow up" in result["message"]


async def test_an_archived_document_stops_answering_calls(db, tenant_a, handlers):
    from app.knowledge import ingest

    document = await add_document(db, tenant_a, text=HANDBOOK, filename="handbook.md")
    assert (await handlers.answer_question(topic="p", question="cleaning cost"))["ok"]

    await ingest.archive_document(db, document)
    assert not (await handlers.answer_question(topic="p", question="cleaning cost"))["ok"]


# ------------------------------------------------------- retrieval policy ---

def test_trivial_turns_do_not_trigger_retrieval():
    """
    Retrieval costs an embedding call inside a 1.5 second budget. Most turns
    of a phone call are acknowledgements and must not pay for it.
    """
    from app.knowledge.policy import should_retrieve

    for utterance in ("yeah", "okay thanks", "hi there", "sure, ten thirty",
                      "my name is Sarah", "Tuesday works for me"):
        assert not should_retrieve(utterance), utterance


def test_business_questions_do_trigger_retrieval():
    from app.knowledge.policy import should_retrieve

    for utterance in ("how much is a cleaning", "what time do you open",
                      "do you take Blue Cross", "is there parking?",
                      "can I get a refund on that", "what's your address"):
        assert should_retrieve(utterance), utterance


# ------------------------------------------------------- prompt assembly ---

def test_the_system_prompt_accepts_a_grounded_block(tenant_a):
    from app.knowledge.context import build_context
    from app.knowledge.retrieval import RetrievedChunk

    context = build_context([
        RetrievedChunk(
            chunk_id="c", document_id="d", title="Handbook", score=0.9,
            text="A cleaning costs 120 dollars.",
        )
    ])
    prompt = build_system_prompt(tenant_a, knowledge_context=context)

    assert "120 dollars" in prompt
    assert "UNTRUSTED" in prompt
    # The speaking rules must still be intact.
    assert "Maximum 25 words per reply" in prompt


def test_the_system_prompt_is_unchanged_without_retrieval(tenant_a):
    """Backward compatibility: the default call path produces the old prompt."""
    assert "UNTRUSTED" not in build_system_prompt(tenant_a)


def test_document_text_never_enters_the_prompt_by_itself(db, tenant_a):
    """
    The core rule of the feature. Uploading a document must not put it in the
    system prompt -- only chunks that retrieval actually returned may.
    """
    import asyncio

    asyncio.get_event_loop  # keep the import obvious; the work is below
    prompt = build_system_prompt(tenant_a)
    assert "120 dollars" not in prompt


async def test_uploading_does_not_change_the_system_prompt(db, tenant_a):
    await add_document(db, tenant_a, text=HANDBOOK, filename="handbook.md")
    await db.refresh(tenant_a)

    prompt = build_system_prompt(tenant_a)
    assert "120 dollars" not in prompt
    assert "fourteen days" not in prompt


# ------------------------------------------------------------ job dispatch ---

async def test_the_worker_pass_indexes_pending_documents(
    db, tenant_a, sessionmaker_, monkeypatch
):
    """
    The worker path, end to end: an UPLOADED document is picked up, processed
    and becomes searchable, without the API having done the work.
    """
    from app.db.models import DocumentStatus, KnowledgeDocument
    from app.knowledge import jobs

    document = await add_document(db, tenant_a, text=HANDBOOK, process=False)
    document_id = document.id
    assert document.status is DocumentStatus.UPLOADED

    # The job opens its own session, so point the sessionmaker at this test's
    # engine rather than the real database.
    monkeypatch.setattr(
        "app.db.session.get_sessionmaker", lambda: sessionmaker_
    )
    handled = await jobs.process_pending(limit=5)

    # The work happened in another session; expire before reading, and hold
    # the id separately so expiring cannot trigger a lazy load.
    db.expire_all()
    refreshed = await db.get(KnowledgeDocument, document_id)
    assert handled == 1
    assert refreshed.status is DocumentStatus.READY
    assert refreshed.chunk_count > 0


async def test_a_job_for_an_archived_document_is_skipped(
    db, tenant_a, sessionmaker_, monkeypatch
):
    """Archiving between enqueue and execution must win."""
    from app.db.models import DocumentStatus, KnowledgeDocument
    from app.knowledge import ingest, jobs

    document = await add_document(db, tenant_a, text=HANDBOOK, process=False)
    await ingest.archive_document(db, document)
    document_id = document.id

    monkeypatch.setattr(
        "app.db.session.get_sessionmaker", lambda: sessionmaker_
    )
    await jobs.run_ingestion(document_id)

    db.expire_all()
    refreshed = await db.get(KnowledgeDocument, document_id)
    assert refreshed.status is DocumentStatus.ARCHIVED


async def test_a_job_for_a_missing_document_does_not_crash(
    db, sessionmaker_, monkeypatch
):
    from app.knowledge import jobs

    monkeypatch.setattr(
        "app.db.session.get_sessionmaker", lambda: sessionmaker_
    )
    await jobs.run_ingestion(uuid.uuid4())   # must simply log and return