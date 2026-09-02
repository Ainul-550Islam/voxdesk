"""
Performance measurements for the knowledge pipeline.

These are guard rails, not a benchmark suite. The thresholds are deliberately
loose -- several times the observed median -- because a test that fails when
CI is busy teaches people to ignore failures. What they actually catch is a
change of *complexity*: an accidental O(n^2) chunker, a per-chunk database
round trip, an embedding call that stopped batching.

The one number that is a real product requirement is the last section:
retrieval on a live call must be bounded, because a caller is waiting.

Run with `-s` to see the measured timings.
"""
from __future__ import annotations

import statistics
import time

import pytest

from app.knowledge.chunking import ChunkingConfig, chunk_sections
from app.knowledge.embeddings import get_embedder
from app.knowledge.extractors import extract
from app.knowledge.retrieval import retrieve, retrieve_with_timeout
from tests.conftest import add_document
from tests.knowledge_fixtures import make_docx, make_pdf

PAGE = "Refund Policy. Refunds are issued within fourteen days of purchase. " * 12


def _median_ms(fn, runs: int = 5) -> float:
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


async def _median_ms_async(fn, runs: int = 5) -> float:
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        await fn()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def _report(capsys, label: str, ms: float, budget: float) -> None:
    with capsys.disabled():
        print(f"\n    {label:<34} {ms:8.2f} ms   (ceiling {budget:.0f} ms)")


# ------------------------------------------------------------- extraction ---

def test_pdf_extraction_time(capsys):
    data = make_pdf([PAGE] * 10)
    ms = _median_ms(lambda: extract(data, filename="p.pdf"))
    _report(capsys, "extract PDF, 10 pages", ms, 2000)
    assert ms < 2000


def test_pdf_extraction_scales_linearly_in_pages(capsys):
    """
    Five times the pages must not cost twenty-five times the work. This is the
    test that would catch someone re-parsing the document once per page.
    """
    small = make_pdf([PAGE] * 10)
    large = make_pdf([PAGE] * 50)

    small_ms = _median_ms(lambda: extract(small, filename="s.pdf"), runs=3)
    large_ms = _median_ms(lambda: extract(large, filename="l.pdf"), runs=3)
    ratio = large_ms / max(small_ms, 0.01)

    with capsys.disabled():
        print(
            f"\n    extract 10p={small_ms:.1f} ms  50p={large_ms:.1f} ms  "
            f"ratio={ratio:.1f}x for 5x the pages"
        )
    # Linear would be 5x. Allow generous constant overhead, reject quadratic.
    assert ratio < 15


def test_docx_extraction_time(capsys):
    data = make_docx([("Heading 1", "Policy")] + [("Normal", PAGE)] * 30)
    ms = _median_ms(lambda: extract(data, filename="d.docx"))
    _report(capsys, "extract DOCX, 30 paragraphs", ms, 2000)
    assert ms < 2000


def test_large_text_extraction_time(capsys):
    data = (PAGE * 300).encode()
    ms = _median_ms(lambda: extract(data, filename="t.txt"))
    _report(capsys, f"extract TXT, {len(data) // 1024} KB", ms, 1000)
    assert ms < 1000


# --------------------------------------------------------------- chunking ---

def test_chunking_time_is_linear_in_document_size(capsys):
    from app.knowledge.extractors import Section

    config = ChunkingConfig(size=3200, overlap=400, min_size=120)
    small = [Section(text=PAGE * 20, metadata={})]
    large = [Section(text=PAGE * 100, metadata={})]

    small_ms = _median_ms(lambda: chunk_sections(small, config))
    large_ms = _median_ms(lambda: chunk_sections(large, config))
    ratio = large_ms / max(small_ms, 0.01)

    with capsys.disabled():
        print(
            f"\n    chunk 1x={small_ms:.2f} ms  5x={large_ms:.2f} ms  "
            f"ratio={ratio:.1f}x for 5x the text"
        )
    assert ratio < 15


# -------------------------------------------------------------- embedding ---

async def test_single_query_embedding_time(capsys):
    """
    This one sits directly in the call path -- every retrieval pays it before
    the search even starts.
    """
    embedder = get_embedder()
    ms = await _median_ms_async(
        lambda: embedder.embed_one("how much does a cleaning cost"), runs=10
    )
    _report(capsys, f"embed 1 query ({embedder.provider})", ms, 200)
    assert ms < 200


async def test_batch_embedding_is_not_slower_per_item_than_single(capsys):
    """Guards against a regression that drops batching."""
    embedder = get_embedder()
    texts = [f"Service number {i} costs {100 + i} dollars." for i in range(32)]

    batch_ms = await _median_ms_async(lambda: embedder.embed(texts), runs=3)
    per_item = batch_ms / len(texts)

    with capsys.disabled():
        print(
            f"\n    embed batch of 32               {batch_ms:8.2f} ms "
            f"({per_item:.2f} ms/item)"
        )
    assert per_item < 50


# -------------------------------------------------------------- ingestion ---

async def test_full_ingestion_time_for_a_ten_page_pdf(db, tenant_a, capsys):
    """Extract + clean + chunk + embed + persist, end to end."""
    data = make_pdf([PAGE] * 10)

    started = time.perf_counter()
    document = await add_document(db, tenant_a, text=data, filename="perf.pdf")
    elapsed = (time.perf_counter() - started) * 1000

    with capsys.disabled():
        print(
            f"\n    ingest PDF 10 pages             {elapsed:8.2f} ms   "
            f"-> {document.chunk_count} chunks "
            f"({elapsed / max(document.chunk_count, 1):.1f} ms/chunk)"
        )
    assert document.chunk_count > 0
    assert elapsed < 10_000


# -------------------------------------------------------------- retrieval ---

@pytest.fixture
async def corpus(db, tenant_a):
    """Thirty documents, so retrieval has something to scan."""
    for index in range(30):
        text = (
            f"# Service {index}\n\n"
            + f"Service number {index} costs {100 + index} dollars "
            f"and takes {index} minutes. " * 20
        )
        await add_document(db, tenant_a, text=text, filename=f"doc{index}.md")
    return tenant_a


async def test_retrieval_latency(db, corpus, capsys):
    from app.knowledge.vectorstore import count_searchable_chunks

    chunks = await count_searchable_chunks(db, tenant_id=corpus.id)
    ms = await _median_ms_async(
        lambda: retrieve(
            db, tenant_id=corpus.id, query="how much does service number 7 cost"
        ),
        runs=5,
    )
    with capsys.disabled():
        print(f"\n    retrieve over {chunks} chunks        {ms:8.2f} ms")
    assert chunks >= 30
    assert ms < 3000


async def test_reranking_is_not_the_bottleneck(db, corpus, capsys):
    """
    Reranking is lexical and local, so it should be nearly free next to the
    vector scan. If it ever isn't, it should be turned off by default.
    """
    query = "how much does service number 7 cost"
    with_rerank = await _median_ms_async(
        lambda: retrieve(db, tenant_id=corpus.id, query=query, rerank=True), runs=5
    )
    without = await _median_ms_async(
        lambda: retrieve(db, tenant_id=corpus.id, query=query, rerank=False), runs=5
    )
    overhead = with_rerank - without

    with capsys.disabled():
        print(
            f"\n    rerank overhead                 {overhead:8.2f} ms "
            f"(on={with_rerank:.1f}, off={without:.1f})"
        )
    assert overhead < max(without, 50)


async def test_live_call_retrieval_stays_inside_its_budget(db, corpus, capsys):
    """
    **The one measurement that is a product requirement.**

    A caller will not wait. `retrieve_with_timeout` must return inside
    `knowledge_retrieval_timeout_seconds` whether it succeeds or not.
    """
    from app.core.config import settings

    budget_ms = settings.knowledge_retrieval_timeout_seconds * 1000
    started = time.perf_counter()
    await retrieve_with_timeout(
        db, tenant_id=corpus.id, query="how much does service number 7 cost"
    )
    elapsed = (time.perf_counter() - started) * 1000

    _report(capsys, "live-call retrieval", elapsed, budget_ms)
    assert elapsed < budget_ms


async def test_the_timeout_is_enforced_even_when_retrieval_hangs(db, corpus, capsys):
    """
    The bound must hold against a backend that never returns -- otherwise it
    is a hope, not a timeout.
    """
    import asyncio

    import app.knowledge.retrieval as retrieval_module

    async def never_returns(*args, **kwargs):
        await asyncio.sleep(30)

    original = retrieval_module.retrieve
    retrieval_module.retrieve = never_returns
    try:
        started = time.perf_counter()
        results = await retrieve_with_timeout(
            db, tenant_id=corpus.id, query="anything", timeout=0.2
        )
        elapsed = (time.perf_counter() - started) * 1000
    finally:
        retrieval_module.retrieve = original

    with capsys.disabled():
        print(f"\n    hung backend, 200 ms timeout    {elapsed:8.2f} ms -> []")
    assert results == []
    # Generous ceiling: the assertion is "it returned", not "it was instant".
    assert elapsed < 2000

