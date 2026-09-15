"""
Conversation memory must never become tenant-wide knowledge.

Two different things share the word "context" in a voice agent, and conflating
them is a privacy incident, not a bug:

* **Knowledge** — documents the business deliberately uploaded and curated.
  Tenant-wide. Any caller of that tenant may be answered from it.
* **Conversation memory** — what one caller said on one call. Scoped to that
  call, and to that caller.

If transcripts were vector-indexed into the knowledge base "for context", then
one customer mentioning their diagnosis, their address, or the discount they
negotiated would become a fact the agent volunteers to the next caller. That
is the failure this file exists to prevent.

The separation is enforced by omission -- there is simply no code path from a
`Turn` to a `KnowledgeChunk` -- which is exactly the kind of guarantee that
quietly disappears when someone later adds "index the transcript for better
recall". These tests make it fail loudly instead.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.db.models import (
    Call,
    CallDirection,
    CallStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    Speaker,
    Turn,
)
from app.knowledge.retrieval import retrieve
from tests.conftest import add_document

#: Things a caller says that must never leak to the next caller.
PRIVATE_UTTERANCES = [
    "My name is Sarah Chen and my number is 555 0147.",
    "I need to reschedule my root canal from last Tuesday.",
    "The manager promised me a fifty percent discount on the whitening.",
    "My insurance member id is XJ-99120.",
]

HANDBOOK = (
    "# Pricing\n\nA standard cleaning costs 120 dollars.\n\n"
    "# Refunds\n\nRefunds are issued within fourteen days.\n"
)


@pytest.fixture
async def call_with_transcript(db, tenant_a):
    call = Call(
        tenant_id=tenant_a.id,
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        from_number="+15551230000",
        to_number=tenant_a.twilio_number,
        call_sid=f"CA{uuid.uuid4().hex}",
        started_at=datetime.utcnow(),
    )
    db.add(call)
    await db.commit()
    await db.refresh(call)

    for index, text in enumerate(PRIVATE_UTTERANCES):
        db.add(
            Turn(
                call_id=call.id,
                speaker=Speaker.USER if index % 2 == 0 else Speaker.ASSISTANT,
                text=text,
            )
        )
    await db.commit()
    return call


async def test_a_transcript_creates_no_knowledge_documents(
    db, tenant_a, call_with_transcript
):
    """Holding a conversation must not add anything to the knowledge base."""
    count = int(
        (
            await db.execute(
                select(func.count(KnowledgeDocument.id)).where(
                    KnowledgeDocument.tenant_id == tenant_a.id
                )
            )
        ).scalar()
    )
    assert count == 0


async def test_a_transcript_creates_no_chunks_or_embeddings(
    db, tenant_a, call_with_transcript
):
    count = int(
        (
            await db.execute(
                select(func.count(KnowledgeChunk.id)).where(
                    KnowledgeChunk.tenant_id == tenant_a.id
                )
            )
        ).scalar()
    )
    assert count == 0


@pytest.mark.parametrize(
    "query",
    [
        "what is Sarah Chen's phone number",
        "who was promised a fifty percent discount",
        "what is the insurance member id",
        "who needs to reschedule a root canal",
    ],
)
async def test_retrieval_cannot_surface_anything_a_caller_said(
    db, tenant_a, call_with_transcript, query
):
    """
    The next caller asks about the previous caller. Nothing may come back --
    not even when the business also has a real, indexed knowledge base.
    """
    await add_document(db, tenant_a, text=HANDBOOK, filename="handbook.md")

    hits = await retrieve(db, tenant_id=tenant_a.id, query=query, min_score=0.0)
    joined = " ".join(hit.text for hit in hits)

    for secret in ("Sarah Chen", "555 0147", "XJ-99120", "root canal",
                   "fifty percent"):
        assert secret not in joined


async def test_indexed_knowledge_still_works_alongside_a_transcript(
    db, tenant_a, call_with_transcript
):
    """
    The counterpart to the test above: proving nothing is retrievable would be
    trivial if retrieval were simply broken. Real knowledge must still answer.
    """
    await add_document(db, tenant_a, text=HANDBOOK, filename="handbook.md")

    hits = await retrieve(db, tenant_id=tenant_a.id, query="how much is a cleaning")
    assert any("120 dollars" in hit.text for hit in hits)


async def test_the_agent_does_not_answer_from_another_callers_conversation(
    db, tenant_a, call_with_transcript
):
    """End to end, through the tool the voice loop actually calls."""
    from app.agent.functions import FunctionHandlers

    await add_document(db, tenant_a, text=HANDBOOK, filename="handbook.md")

    new_call = Call(
        tenant_id=tenant_a.id,
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        from_number="+15559998888",
        to_number=tenant_a.twilio_number,
        call_sid=f"CA{uuid.uuid4().hex}",
        started_at=datetime.utcnow(),
    )
    db.add(new_call)
    await db.commit()

    handlers = FunctionHandlers(db, tenant_a, new_call)
    result = await handlers.answer_question(
        topic="discount", question="was anyone promised a fifty percent discount"
    )

    assert result["ok"] is False
    assert "fifty percent" not in str(result)
    assert "Sarah Chen" not in str(result)


def test_the_chunk_table_has_no_link_to_conversations():
    """
    Structural, not behavioural.

    A foreign key from a chunk to a call or a turn would be the first step
    toward indexing transcripts. There isn't one, and this test is what makes
    adding one a deliberate, visible act.
    """
    columns = set(KnowledgeChunk.__table__.columns.keys())
    assert "call_id" not in columns
    assert "turn_id" not in columns

    referenced = {
        fk.column.table.name
        for fk in KnowledgeChunk.__table__.foreign_keys
    }
    assert referenced == {"knowledge_documents", "tenants"}


def test_no_ingestion_path_accepts_a_transcript():
    """
    `DocumentSourceType` describes deliberate, curated inputs only. A
    `TRANSCRIPT` or `CALL` member appearing here would mean someone built the
    pipeline this file forbids.
    """
    from app.db.models import DocumentSourceType

    names = {member.name for member in DocumentSourceType}
    assert names == {"UPLOAD", "TEXT", "URL"}
    assert not {"TRANSCRIPT", "CALL", "CONVERSATION", "MEMORY"} & names