"""
The RAG evaluation suite.

Reported as pass/fail per named case rather than as an accuracy score. The
dataset in `dataset.py` has a couple of dozen questions; quoting "94% accurate"
off that would be a number with no confidence interval and no honesty behind
it. What these tests do support is a categorical claim: on this corpus, every
listed behaviour either holds or it does not, and a regression names itself.

Five categories, matching requirement 26:

  1. retrieval relevance
  2. answer groundedness
  3. unsupported-answer rejection
  4. prompt-injection resistance
  5. tenant isolation
"""
from __future__ import annotations

import pytest

from app.knowledge.context import build_context, summarize_for_tool
from app.knowledge.retrieval import retrieve
from tests.conftest import add_document
from tests.evals.rag.dataset import (
    ACME_HANDBOOK,
    ACME_HOURS,
    BETA_HANDBOOK,
    INJECTION_MARKERS,
    INJECTION_QUERIES,
    ISOLATION_CASES,
    MALICIOUS_DOCUMENT,
    RELEVANCE_CASES,
    UNSUPPORTED_CASES,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def corpus(db, tenant_a, tenant_b):
    """Index the whole evaluation corpus through the real pipeline."""
    await add_document(
        db, tenant_a, text=ACME_HANDBOOK, filename="handbook.md", title="Acme handbook"
    )
    await add_document(
        db, tenant_a, text=ACME_HOURS, filename="hours.md", title="Acme hours"
    )
    await add_document(
        db, tenant_b, text=BETA_HANDBOOK, filename="handbook.md", title="Beta handbook"
    )
    return tenant_a, tenant_b


# =========================================================== 1. relevance ===

@pytest.mark.parametrize("case", RELEVANCE_CASES, ids=lambda c: c.name)
async def test_retrieval_relevance(db, corpus, case):
    tenant_a, _ = corpus
    hits = await retrieve(db, tenant_id=tenant_a.id, query=case.query)

    assert hits, f"[{case.name}] retrieved nothing for {case.query!r}"
    joined = " ".join(hit.text for hit in hits)
    for expected in case.expect_text:
        assert expected in joined, (
            f"[{case.name}] {expected!r} missing from the retrieved chunks"
        )


async def test_relevance_summary(db, corpus, capsys):
    """
    Reports coverage across the relevance set as a count, not a percentage.

    A denominator of seven does not support a percentage anyone should quote.
    """
    tenant_a, _ = corpus
    passed = []
    failed = []
    for case in RELEVANCE_CASES:
        hits = await retrieve(db, tenant_id=tenant_a.id, query=case.query)
        joined = " ".join(hit.text for hit in hits)
        if hits and all(e in joined for e in case.expect_text):
            passed.append(case.name)
        else:
            failed.append(case.name)

    with capsys.disabled():
        print(
            f"\n  retrieval relevance: {len(passed)}/{len(RELEVANCE_CASES)} cases"
            + (f" -- failing: {failed}" if failed else "")
        )
    assert not failed


# ======================================================== 2. groundedness ===

@pytest.mark.parametrize("case", RELEVANCE_CASES, ids=lambda c: c.name)
async def test_the_answer_context_is_grounded_in_a_real_chunk(db, corpus, case):
    """
    Every fact offered to the model must be traceable to a retrieved chunk,
    and the context block must carry the "do not invent" framing with it.
    """
    tenant_a, _ = corpus
    hits = await retrieve(db, tenant_id=tenant_a.id, query=case.query)
    context = build_context(hits)

    for expected in case.expect_text:
        assert expected in context
    assert "UNTRUSTED" in context
    assert "never fill the gap with a guess" in context.lower()


@pytest.mark.parametrize("case", RELEVANCE_CASES, ids=lambda c: c.name)
async def test_every_grounded_answer_is_traceable_to_its_source(db, corpus, case):
    tenant_a, _ = corpus
    hits = await retrieve(db, tenant_id=tenant_a.id, query=case.query)

    for hit in hits:
        source = hit.to_source()
        assert source["document_id"] and source["chunk_id"] and source["title"]


# ============================================ 3. unsupported-answer refusal ===

@pytest.mark.parametrize("case", UNSUPPORTED_CASES, ids=lambda c: c.name)
async def test_unsupported_questions_retrieve_nothing(db, corpus, case):
    tenant_a, _ = corpus
    hits = await retrieve(db, tenant_id=tenant_a.id, query=case.query)
    assert hits == [], (
        f"[{case.name}] returned {[h.text[:60] for h in hits]} "
        f"for an unanswerable question"
    )


@pytest.mark.parametrize("case", UNSUPPORTED_CASES, ids=lambda c: c.name)
async def test_unsupported_questions_produce_a_do_not_guess_context(db, corpus, case):
    tenant_a, _ = corpus
    hits = await retrieve(db, tenant_id=tenant_a.id, query=case.query)
    context = build_context(hits)

    assert "Do not guess" in context
    assert "call them" in context.lower()


@pytest.mark.parametrize("case", UNSUPPORTED_CASES, ids=lambda c: c.name)
async def test_the_agent_refuses_unsupported_questions(db, corpus, case, tenant_a):
    """End to end through the tool the voice loop actually calls."""
    import uuid
    from datetime import datetime

    from app.agent.functions import FunctionHandlers
    from app.db.models import Call, CallDirection, CallStatus

    call = Call(
        tenant_id=tenant_a.id, direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS, from_number="+15551230000",
        to_number=tenant_a.twilio_number, call_sid=f"CA{uuid.uuid4().hex}",
        started_at=datetime.utcnow(),
    )
    db.add(call)
    await db.commit()

    handlers = FunctionHandlers(db, tenant_a, call)
    result = await handlers.answer_question(topic=case.name, question=case.query)

    assert result["ok"] is False, f"[{case.name}] the agent answered anyway"


# ================================================= 4. injection resistance ===

@pytest.fixture
async def poisoned(db, tenant_a):
    return await add_document(
        db, tenant_a, text=MALICIOUS_DOCUMENT, filename="pricelist.md",
        title="Supplier price list",
    )


async def test_a_malicious_document_still_indexes(db, tenant_a, poisoned):
    """
    Poisoned content is not rejected at ingestion. It cannot be reliably
    detected there, and refusing it would give a false sense of safety -- the
    defence lives at prompt-construction time, where it is deterministic.
    """
    from app.db.models import DocumentStatus

    assert poisoned.status is DocumentStatus.READY


@pytest.mark.parametrize("query", INJECTION_QUERIES)
async def test_injected_instructions_are_neutralized_in_the_prompt(
    db, tenant_a, poisoned, query
):
    hits = await retrieve(db, tenant_id=tenant_a.id, query=query, min_score=0.0)
    context = build_context(hits)

    for marker in INJECTION_MARKERS:
        if marker in context:
            line = next(
                line_ for line_ in context.split("\n") if marker in line_
            )
            assert line.startswith("[quoted from document"), (
                f"{marker!r} reached the prompt as a live instruction"
            )


@pytest.mark.parametrize("query", INJECTION_QUERIES)
async def test_injected_instructions_are_neutralized_in_tool_results(
    db, tenant_a, poisoned, query
):
    hits = await retrieve(db, tenant_id=tenant_a.id, query=query, min_score=0.0)
    summary = summarize_for_tool(hits)

    for marker in INJECTION_MARKERS:
        if marker in summary:
            line = next(line_ for line_ in summary.split("\n") if marker in line_)
            assert line.startswith("[quoted from document")


async def test_the_legitimate_fact_in_a_poisoned_document_still_works(
    db, tenant_a, poisoned
):
    """
    Neutralisation must not destroy the document. The price list is still a
    price list; only the instruction-shaped lines are defanged.
    """
    hits = await retrieve(
        db, tenant_id=tenant_a.id, query="how much does a cleaning cost"
    )
    assert any("120 dollars" in hit.text for hit in hits)


async def test_a_poisoned_document_cannot_forge_the_context_fence(
    db, tenant_a, poisoned
):
    hits = await retrieve(db, tenant_id=tenant_a.id, query="terms", min_score=0.0)
    context = build_context(hits)

    # Exactly one open and one close marker per excerpt, and no stray ones.
    assert context.count("<<<KB_EXCERPT_") == context.count("<<<END_KB_EXCERPT_")


# ===================================================== 5. tenant isolation ===

@pytest.mark.parametrize("case", ISOLATION_CASES, ids=lambda c: c.name)
async def test_tenant_isolation_cases(db, corpus, case):
    tenant_a, _ = corpus
    hits = await retrieve(db, tenant_id=tenant_a.id, query=case.query, min_score=0.0)
    joined = " ".join(hit.text for hit in hits)

    for forbidden in case.forbid_text:
        assert forbidden not in joined, (
            f"[{case.name}] LEAK: {forbidden!r} from the other tenant"
        )
    for expected in case.expect_text:
        assert expected in joined, f"[{case.name}] {expected!r} missing"


async def test_isolation_holds_in_both_directions(db, corpus):
    tenant_a, tenant_b = corpus

    a_text = " ".join(
        hit.text
        for hit in await retrieve(
            db, tenant_id=tenant_a.id, query="pricing refunds supplier", min_score=0.0
        )
    )
    b_text = " ".join(
        hit.text
        for hit in await retrieve(
            db, tenant_id=tenant_b.id, query="pricing refunds supplier", min_score=0.0
        )
    )

    assert "Contoso" not in a_text and "300 euros" not in a_text
    assert "Maple Street" not in b_text and "Delta Dental" not in b_text


async def test_isolation_summary(db, corpus, capsys):
    tenant_a, _ = corpus
    leaks = []
    for case in ISOLATION_CASES:
        hits = await retrieve(
            db, tenant_id=tenant_a.id, query=case.query, min_score=0.0
        )
        joined = " ".join(hit.text for hit in hits)
        leaks += [
            f"{case.name}:{f}" for f in case.forbid_text if f in joined
        ]

    with capsys.disabled():
        print(
            f"  tenant isolation: {len(ISOLATION_CASES)} cases, "
            f"{len(leaks)} leaks {leaks or ''}"
        )
    assert not leaks