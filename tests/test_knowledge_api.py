"""
The document API: permissions, validation and response safety.

Authorization here is the STEP 2 permission layer, so these tests assert on
capability rather than on role names -- a viewer is refused because they lack
`knowledge:write`, not because the code compared a string.
"""
from __future__ import annotations

import pytest

from app.db.models import DocumentStatus
from tests.conftest import add_document, auth_headers
from tests.knowledge_fixtures import make_docx, make_pdf

TXT = b"# Hours\n\nWe are open nine to five, Monday to Friday.\n"


def _upload(name: str, data: bytes, mime: str = "text/markdown"):
    return {"file": (name, data, mime)}


# -------------------------------------------------------------- uploading ---

async def test_owner_can_upload(client, owner_a):
    headers = await auth_headers(client, owner_a)
    response = await client.post(
        "/api/knowledge/documents", files=_upload("hours.md", TXT), headers=headers
    )

    assert response.status_code == 202
    body = response.json()
    assert body["title"] == "hours.md"
    assert body["status"] == DocumentStatus.UPLOADED.value
    assert body["file_type"] == "md"


async def test_manager_can_upload(client, manager_a):
    headers = await auth_headers(client, manager_a)
    response = await client.post(
        "/api/knowledge/documents", files=_upload("hours.md", TXT), headers=headers
    )
    assert response.status_code == 202


async def _upload_is_forbidden(client, user):
    headers = await auth_headers(client, user)
    response = await client.post(
        "/api/knowledge/documents", files=_upload("hours.md", TXT), headers=headers
    )
    assert response.status_code == 403
    assert "knowledge:write" in response.json()["detail"]


async def test_viewer_cannot_upload(client, viewer_a):
    await _upload_is_forbidden(client, viewer_a)


async def test_agent_cannot_upload(client, agent_a):
    """
    An agent handles calls; curating the knowledge base is a manager's job.
    Read access is enough for the call path, which only ever retrieves.
    """
    await _upload_is_forbidden(client, agent_a)


async def test_uploading_requires_authentication(client):
    response = await client.post("/api/knowledge/documents", files=_upload("h.md", TXT))
    assert response.status_code in (401, 403)


async def test_a_real_pdf_uploads(client, owner_a):
    headers = await auth_headers(client, owner_a)
    data = make_pdf(["Refunds within fourteen days."])
    response = await client.post(
        "/api/knowledge/documents",
        files=_upload("policy.pdf", data, "application/pdf"),
        headers=headers,
    )
    assert response.status_code == 202
    assert response.json()["file_type"] == "pdf"


async def test_a_real_docx_uploads(client, owner_a):
    headers = await auth_headers(client, owner_a)
    data = make_docx([("Normal", "Open nine to five.")])
    response = await client.post(
        "/api/knowledge/documents",
        files=_upload("hours.docx", data, "application/octet-stream"),
        headers=headers,
    )
    assert response.status_code == 202
    assert response.json()["file_type"] == "docx"


async def test_an_executable_is_rejected_with_400(client, owner_a):
    headers = await auth_headers(client, owner_a)
    response = await client.post(
        "/api/knowledge/documents",
        files=_upload("setup.exe", b"MZ\x90\x00" * 40, "application/pdf"),
        headers=headers,
    )
    assert response.status_code == 400


async def test_an_empty_file_is_rejected_with_400(client, owner_a):
    headers = await auth_headers(client, owner_a)
    response = await client.post(
        "/api/knowledge/documents", files=_upload("empty.txt", b"", "text/plain"),
        headers=headers,
    )
    assert response.status_code == 400


async def test_an_oversized_file_is_rejected_with_413(client, owner_a, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "knowledge_max_file_mb", 1)
    headers = await auth_headers(client, owner_a)
    response = await client.post(
        "/api/knowledge/documents",
        files=_upload("big.txt", b"x" * (2 * 1024 * 1024), "text/plain"),
        headers=headers,
    )
    assert response.status_code == 413


async def test_re_uploading_the_same_file_is_idempotent(client, owner_a):
    headers = await auth_headers(client, owner_a)
    first = await client.post(
        "/api/knowledge/documents", files=_upload("hours.md", TXT), headers=headers
    )
    second = await client.post(
        "/api/knowledge/documents", files=_upload("hours-copy.md", TXT), headers=headers
    )

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


# ----------------------------------------------------------------- reading ---

async def test_viewer_can_read_documents(client, db, tenant_a, viewer_a):
    await add_document(db, tenant_a, text=TXT, filename="hours.md")
    headers = await auth_headers(client, viewer_a)

    response = await client.get("/api/knowledge/documents", headers=headers)
    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_listing_can_be_filtered_by_status(client, db, tenant_a, owner_a):
    await add_document(db, tenant_a, text=TXT, filename="ready.md")
    await add_document(db, tenant_a, text="Another note entirely.", filename="new.md",
                       process=False)
    headers = await auth_headers(client, owner_a)

    ready = await client.get("/api/knowledge/documents?status=ready", headers=headers)
    assert ready.json()["total"] == 1
    assert ready.json()["documents"][0]["status"] == "ready"


async def test_an_unknown_status_filter_is_a_400(client, owner_a):
    headers = await auth_headers(client, owner_a)
    response = await client.get(
        "/api/knowledge/documents?status=banana", headers=headers
    )
    assert response.status_code == 400


async def test_getting_one_document_returns_management_metadata(
    client, db, tenant_a, owner_a
):
    document = await add_document(db, tenant_a, text=TXT, filename="hours.md")
    headers = await auth_headers(client, owner_a)

    body = (
        await client.get(f"/api/knowledge/documents/{document.id}", headers=headers)
    ).json()

    assert body["chunk_count"] > 0
    assert body["char_count"] > 0
    assert body["token_estimate"] > 0
    assert body["version"] == 1
    assert body["indexed_at"]
    assert body["is_searchable"] is True


async def test_a_missing_document_is_a_404(client, owner_a):
    import uuid

    headers = await auth_headers(client, owner_a)
    response = await client.get(
        f"/api/knowledge/documents/{uuid.uuid4()}", headers=headers
    )
    assert response.status_code == 404


# --------------------------------------------------------- response safety ---

async def test_responses_never_expose_storage_or_vectors(client, db, tenant_a, owner_a):
    """
    Requirement 28, asserted on the raw response text rather than on parsed
    fields -- a nested dict could smuggle any of these back in.
    """
    document = await add_document(db, tenant_a, text=TXT, filename="hours.md")
    headers = await auth_headers(client, owner_a)

    for url in (
        "/api/knowledge/documents",
        f"/api/knowledge/documents/{document.id}",
        "/api/knowledge/stats",
    ):
        body = (await client.get(url, headers=headers)).text
        assert "source_uri" not in body
        assert "embedding\"" not in body      # the raw vector field
        assert "/tmp" not in body
        assert "tenant/" not in body          # the storage key format
        assert "secret" not in body.lower()


async def test_search_returns_scores_but_never_vectors(client, db, tenant_a, owner_a):
    await add_document(db, tenant_a, text=TXT, filename="hours.md")
    headers = await auth_headers(client, owner_a)

    response = await client.post(
        "/api/knowledge/search", json={"query": "what time do you open"},
        headers=headers,
    )
    assert response.status_code == 200
    assert "embedding" not in response.text
    for hit in response.json()["results"]:
        assert set(hit) <= {
            "document_id", "chunk_id", "title", "text", "score", "page", "heading"
        }


# --------------------------------------------------- delete and reindex ---

async def test_delete_archives_by_default(client, db, tenant_a, owner_a):
    document = await add_document(db, tenant_a, text=TXT, filename="hours.md")
    headers = await auth_headers(client, owner_a)

    response = await client.delete(
        f"/api/knowledge/documents/{document.id}", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "archived"

    await db.refresh(document)
    assert document.status is DocumentStatus.ARCHIVED


async def test_manager_can_archive_but_not_hard_delete(client, db, tenant_a, manager_a):
    """
    Archiving is curation; destroying audit history is not. The split is the
    reason `knowledge:delete` exists as a separate permission.
    """
    document = await add_document(db, tenant_a, text=TXT, filename="hours.md")
    headers = await auth_headers(client, manager_a)

    assert (
        await client.delete(
            f"/api/knowledge/documents/{document.id}", headers=headers
        )
    ).status_code == 200

    hard = await client.delete(
        f"/api/knowledge/documents/{document.id}?hard=true", headers=headers
    )
    assert hard.status_code == 403
    assert "knowledge:delete" in hard.json()["detail"]


async def test_admin_can_hard_delete(client, db, tenant_a, admin_a):
    from app.db.models import KnowledgeDocument

    document = await add_document(db, tenant_a, text=TXT, filename="hours.md")
    headers = await auth_headers(client, admin_a)

    document_id = document.id
    response = await client.delete(
        f"/api/knowledge/documents/{document_id}?hard=true", headers=headers
    )
    assert response.status_code == 200

    # The request ran in its own session, so this one still holds the deleted
    # object in its identity map. Expire it to force a real SELECT.
    db.expire_all()
    assert await db.get(KnowledgeDocument, document_id) is None


async def test_viewer_cannot_delete(client, db, tenant_a, viewer_a):
    document = await add_document(db, tenant_a, text=TXT, filename="hours.md")
    headers = await auth_headers(client, viewer_a)

    response = await client.delete(
        f"/api/knowledge/documents/{document.id}", headers=headers
    )
    assert response.status_code == 403


async def test_reindexing_an_archived_document_is_a_409(client, db, tenant_a, owner_a):
    from app.knowledge import ingest

    document = await add_document(db, tenant_a, text=TXT, filename="hours.md")
    await ingest.archive_document(db, document)
    headers = await auth_headers(client, owner_a)

    response = await client.post(
        f"/api/knowledge/documents/{document.id}/reindex", headers=headers
    )
    assert response.status_code == 409
    assert "archived" in response.json()["detail"].lower()


async def test_restore_then_reindex_works(client, db, tenant_a, owner_a):
    from app.knowledge import ingest

    document = await add_document(db, tenant_a, text=TXT, filename="hours.md")
    await ingest.archive_document(db, document)
    headers = await auth_headers(client, owner_a)

    restored = await client.post(
        f"/api/knowledge/documents/{document.id}/restore", headers=headers
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "ready"

    response = await client.post(
        f"/api/knowledge/documents/{document.id}/reindex", headers=headers
    )
    assert response.status_code == 200


async def test_viewer_cannot_reindex(client, db, tenant_a, viewer_a):
    document = await add_document(db, tenant_a, text=TXT, filename="hours.md")
    headers = await auth_headers(client, viewer_a)

    response = await client.post(
        f"/api/knowledge/documents/{document.id}/reindex", headers=headers
    )
    assert response.status_code == 403


async def test_stats_report_the_embedding_configuration(client, db, tenant_a, owner_a):
    await add_document(db, tenant_a, text=TXT, filename="hours.md")
    headers = await auth_headers(client, owner_a)

    body = (await client.get("/api/knowledge/stats", headers=headers)).json()
    assert body["total_documents"] == 1
    assert body["searchable_chunks"] > 0
    assert body["embedding_model"]
    assert body["embedding_dimensions"] > 0
