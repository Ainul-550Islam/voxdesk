"""
Tenant isolation for the knowledge base.

This is the file that matters most. One tenant's documents, chunks or
embeddings reaching another tenant is the failure that ends the product, so
isolation is tested at two independent layers:

* **The API layer** -- can Tenant A's authenticated user touch Tenant B's
  document through any route?
* **The retrieval and DB layer** -- if a future route bug did leak an id or
  drop a filter, would the query itself still refuse?

Testing only the first would mean a single careless route change silently
removes the guarantee. The second layer is what makes it structural.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import KnowledgeChunk, KnowledgeDocument
from app.knowledge.retrieval import retrieve
from app.knowledge.vectorstore import count_searchable_chunks
from tests.conftest import add_document, auth_headers

A_SECRET = (
    "# Pricing\n\nAcme charges 120 dollars for a standard cleaning.\n\n"
    "# Suppliers\n\nOur supplier is Northwind Dental Supply.\n"
)
B_SECRET = (
    "# Pricing\n\nBeta Clinic charges 300 euros for whitening.\n\n"
    "# Suppliers\n\nOur supplier is Contoso Medical.\n"
)


@pytest.fixture
async def doc_a(db, tenant_a):
    return await add_document(db, tenant_a, text=A_SECRET, filename="a.md", title="Acme handbook")


@pytest.fixture
async def doc_b(db, tenant_b):
    return await add_document(db, tenant_b, text=B_SECRET, filename="b.md", title="Beta handbook")


# ====================================================== retrieval / DB layer ===

async def test_a_query_scoped_to_a_never_returns_bs_content(db, tenant_a, doc_a, doc_b):
    hits = await retrieve(db, tenant_id=tenant_a.id, query="pricing supplier", min_score=0.0)
    joined = " ".join(hit.text for hit in hits)
    assert "Contoso" not in joined
    assert "Beta Clinic" not in joined
    assert "300 euros" not in joined


async def test_a_query_scoped_to_b_never_returns_as_content(db, tenant_b, doc_a, doc_b):
    hits = await retrieve(db, tenant_id=tenant_b.id, query="pricing supplier", min_score=0.0)
    joined = " ".join(hit.text for hit in hits)
    assert "Northwind" not in joined
    assert "Acme" not in joined
    assert "120 dollars" not in joined


async def test_every_returned_chunk_belongs_to_the_asking_tenant(db, tenant_a, doc_a, doc_b):
    hits = await retrieve(db, tenant_id=tenant_a.id, query="supplier pricing", min_score=0.0)
    assert hits
    for hit in hits:
        chunk = await db.get(KnowledgeChunk, uuid.UUID(hit.chunk_id))
        assert chunk.tenant_id == tenant_a.id


async def test_a_document_id_filter_cannot_reach_another_tenant(db, tenant_b, doc_a, doc_b):
    """
    The metadata-filter probe. Even holding Tenant A's real document id, a
    query scoped to Tenant B must come back empty -- the tenant predicate is
    ANDed inside the query, so a known id buys nothing.
    """
    hits = await retrieve(
        db, tenant_id=tenant_b.id, query="Northwind supplier",
        document_ids=[doc_a.id], min_score=0.0,
    )
    assert hits == []


async def test_chunk_counts_do_not_leak_across_tenants(db, tenant_a, tenant_b, doc_a, doc_b):
    """Even an aggregate must not reveal how much another tenant has stored."""
    a_count = await count_searchable_chunks(db, tenant_id=tenant_a.id)
    b_count = await count_searchable_chunks(db, tenant_id=tenant_b.id)
    total = len((await db.execute(select(KnowledgeChunk))).scalars().all())

    assert a_count > 0 and b_count > 0
    assert a_count + b_count == total


async def test_identical_documents_stay_separate_rows(db, tenant_a, tenant_b):
    """
    Deduplication is per tenant. Both tenants uploading the same file must get
    their own document and their own chunks -- sharing them would be a leak.
    """
    a = await add_document(db, tenant_a, text=A_SECRET, filename="shared.md")
    b = await add_document(db, tenant_b, text=A_SECRET, filename="shared.md")

    assert a.id != b.id
    a_chunks = (
        (await db.execute(select(KnowledgeChunk).where(KnowledgeChunk.document_id == a.id)))
        .scalars().all()
    )
    b_chunks = (
        (await db.execute(select(KnowledgeChunk).where(KnowledgeChunk.document_id == b.id)))
        .scalars().all()
    )
    assert {c.id for c in a_chunks}.isdisjoint({c.id for c in b_chunks})
    assert all(c.tenant_id == tenant_a.id for c in a_chunks)
    assert all(c.tenant_id == tenant_b.id for c in b_chunks)


async def test_the_tenant_filter_is_inside_the_query_not_applied_afterwards():
    """
    Inspect the generated SQL directly.

    A Python-side filter over the results would pass every behavioural test in
    this file and still leak the moment someone adds a `.limit()` above it.
    The predicate has to be in the WHERE clause, so that is what is asserted.
    """
    from app.knowledge.vectorstore import _base_query

    sql = str(_base_query(uuid.uuid4()).compile(compile_kwargs={"literal_binds": False}))
    normalized = " ".join(sql.split()).lower()

    assert "where" in normalized
    assert "knowledge_chunks.tenant_id = " in normalized
    assert "knowledge_documents.tenant_id = " in normalized
    assert "knowledge_documents.status in" in normalized


# ================================================================ API layer ===

async def test_a_cannot_list_bs_documents(client, db, owner_a, doc_a, doc_b):
    headers = await auth_headers(client, owner_a)
    response = await client.get("/api/knowledge/documents", headers=headers)

    assert response.status_code == 200
    ids = {d["id"] for d in response.json()["documents"]}
    assert str(doc_a.id) in ids
    assert str(doc_b.id) not in ids
    assert response.json()["total"] == 1


async def test_a_cannot_fetch_bs_document(client, owner_a, doc_b):
    """404, not 403 -- a 403 would confirm the id exists somewhere."""
    headers = await auth_headers(client, owner_a)
    response = await client.get(f"/api/knowledge/documents/{doc_b.id}", headers=headers)
    assert response.status_code == 404


async def test_a_cannot_delete_bs_document(client, db, owner_a, doc_b):
    headers = await auth_headers(client, owner_a)
    response = await client.delete(f"/api/knowledge/documents/{doc_b.id}", headers=headers)

    assert response.status_code == 404
    await db.refresh(doc_b)
    assert doc_b.is_searchable


async def test_a_cannot_hard_delete_bs_document(client, db, owner_a, doc_b):
    headers = await auth_headers(client, owner_a)
    response = await client.delete(
        f"/api/knowledge/documents/{doc_b.id}?hard=true", headers=headers
    )
    assert response.status_code == 404
    assert await db.get(KnowledgeDocument, doc_b.id) is not None


async def test_a_cannot_reindex_bs_document(client, owner_a, doc_b):
    headers = await auth_headers(client, owner_a)
    response = await client.post(
        f"/api/knowledge/documents/{doc_b.id}/reindex", headers=headers
    )
    assert response.status_code == 404


async def test_a_cannot_restore_bs_document(client, owner_a, doc_b):
    headers = await auth_headers(client, owner_a)
    response = await client.post(
        f"/api/knowledge/documents/{doc_b.id}/restore", headers=headers
    )
    assert response.status_code == 404


async def test_search_never_crosses_tenants(client, owner_a, doc_a, doc_b):
    headers = await auth_headers(client, owner_a)
    response = await client.post(
        "/api/knowledge/search",
        json={"query": "supplier pricing whitening euros"},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.text
    assert "Contoso" not in body
    assert "Beta Clinic" not in body
    for hit in response.json()["results"]:
        assert hit["document_id"] == str(doc_a.id)


async def test_stats_only_count_the_callers_own_tenant(client, owner_a, doc_a, doc_b):
    headers = await auth_headers(client, owner_a)
    response = await client.get("/api/knowledge/stats", headers=headers)

    assert response.status_code == 200
    assert response.json()["total_documents"] == 1


async def test_uploading_the_same_file_as_another_tenant_is_allowed(
    client, db, owner_a, doc_b
):
    """Tenant B's content hash must not block Tenant A's upload."""
    headers = await auth_headers(client, owner_a)
    response = await client.post(
        "/api/knowledge/documents",
        files={"file": ("b.md", B_SECRET.encode(), "text/markdown")},
        headers=headers,
    )
    assert response.status_code in (200, 202)
    assert response.json()["id"] != str(doc_b.id)


# ============================================== metadata filters as an oracle ===
#
# Requirement 22 calls this out separately from ordinary reads, and rightly so:
# a filter does not have to *return* another tenant's row to leak. If a caller
# can vary a filter and watch the result count move, they can enumerate what
# exists. The defence is that the tenant predicate is ANDed into the same WHERE
# clause, so a filter can only ever narrow rows the caller already owns.

async def test_metadata_filters_cannot_reach_another_tenant(db, tenant_b, doc_a, doc_b):
    """Filtering on Tenant A's exact title, from Tenant B, returns nothing."""
    hits = await retrieve(
        db, tenant_id=tenant_b.id, query="pricing supplier",
        filters={"title": "Acme handbook"}, min_score=0.0,
    )
    assert hits == []


async def test_a_filter_only_narrows_the_callers_own_tenant(db, tenant_a, doc_a, doc_b):
    unfiltered = await retrieve(
        db, tenant_id=tenant_a.id, query="pricing supplier", min_score=0.0
    )
    matching = await retrieve(
        db, tenant_id=tenant_a.id, query="pricing supplier",
        filters={"title": "Acme handbook"}, min_score=0.0,
    )
    non_matching = await retrieve(
        db, tenant_id=tenant_a.id, query="pricing supplier",
        filters={"title": "Beta handbook"}, min_score=0.0,
    )

    assert unfiltered
    assert len(matching) == len(unfiltered)
    # Tenant A filtering for Tenant B's title gets nothing -- not B's rows.
    assert non_matching == []


async def test_filter_result_counts_do_not_reveal_another_tenants_documents(
    db, tenant_b, doc_a, doc_b
):
    """
    The oracle test proper. Probing Tenant A's real titles and filenames from
    Tenant B must be indistinguishable from probing values that do not exist
    anywhere -- every probe returns zero.
    """
    # Values that identify Tenant A specifically. Each must be
    # indistinguishable from a value that exists nowhere at all.
    a_specific = [
        {"title": "Acme handbook"},
        {"original_filename": "a.md"},
    ]
    nonexistent = {"title": "does-not-exist-anywhere"}

    baseline = len(
        await retrieve(
            db, tenant_id=tenant_b.id, query="Northwind Acme pricing",
            filters=nonexistent, min_score=0.0,
        )
    )
    for probe in a_specific:
        hits = await retrieve(
            db, tenant_id=tenant_b.id, query="Northwind Acme pricing",
            filters=probe, min_score=0.0,
        )
        assert len(hits) == baseline == 0, (
            f"probe {probe} leaked a signal about Tenant A"
        )

    # A value both tenants happen to share ("md") legitimately returns the
    # *caller's own* rows -- that is a narrowed read, not a leak. What matters
    # is that not one of them belongs to the other tenant.
    shared = await retrieve(
        db, tenant_id=tenant_b.id, query="Northwind Acme pricing",
        filters={"file_type": "md"}, min_score=0.0,
    )
    assert shared, "the shared-value probe should return the caller's own rows"
    for hit in shared:
        assert hit.document_id == str(doc_b.id)
        assert "Northwind" not in hit.text and "Acme" not in hit.text


async def test_an_unknown_filter_field_is_rejected_not_ignored(db, tenant_a, doc_a):
    """
    Silently dropping an unrecognised filter would widen the result set past
    what the caller asked for -- for a knowledge base, that means answering
    from documents they meant to exclude.
    """
    from app.knowledge.vectorstore import UnknownFilter

    with pytest.raises(UnknownFilter):
        await retrieve(
            db, tenant_id=tenant_a.id, query="pricing",
            filters={"tenant_id": str(uuid.uuid4())},
        )


async def test_a_filter_cannot_override_the_tenant_predicate(db, tenant_b, doc_a):
    """
    The obvious attack: name the filter `tenant_id` and hope it replaces the
    real one. It is not on the allowlist, so it cannot even be expressed.
    """
    from app.knowledge.vectorstore import UnknownFilter

    with pytest.raises(UnknownFilter):
        await retrieve(
            db, tenant_id=tenant_b.id, query="Northwind",
            filters={"tenant_id": str(doc_a.tenant_id)},
        )
