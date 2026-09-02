"""knowledge base documents and chunks (RAG)

Creates the two tables behind tenant-scoped retrieval:

  knowledge_documents  -- one uploaded file, its lifecycle status and metadata
  knowledge_chunks     -- the retrievable passages and their embeddings

Purely additive. No existing table, column or row is touched, so a rolling
deploy is safe in both directions: the previous application version simply
ignores the new tables, and `downgrade()` drops only what this revision made.

Two indexes exist specifically to keep retrieval honest and fast:

  ix_knowledge_doc_tenant_status  -- every listing and every retrieval query
                                     filters on (tenant_id, status)
  ix_knowledge_chunk_tenant_doc   -- the vector scan's WHERE clause

Two unique constraints exist to make the system idempotent:

  uq_knowledge_doc_hash    (tenant_id, content_hash)
        Deduplication is scoped to a tenant. Two businesses uploading the
        identical supplier price list must each get their own document; a
        global hash index would leak the fact that another tenant has the
        same file, and would be a cross-tenant coupling in the data model.

  uq_knowledge_chunk_slot  (document_id, version, chunk_index)
        Makes reindex retries safe. A worker that dies halfway and restarts
        rewrites the same rows instead of creating a second set of embeddings.

Note on pgvector: embeddings are stored in a JSON column so the same code runs
on SQLite in tests and PostgreSQL in production. Moving to a native `vector`
column is a later, separate revision -- it needs `CREATE EXTENSION vector`,
which requires superuser and should not be buried in a table migration.

Revision ID: 0005_knowledge_rag
Revises: 0004_call_transfer_lifecycle
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005_knowledge_rag"
down_revision = "0004_call_transfer_lifecycle"
branch_labels = None
depends_on = None

# SQLAlchemy's Enum() persists the member NAME, not the value. These lists must
# match app/db/models.DocumentStatus and DocumentSourceType exactly -- the STEP
# 1 enum audit exists because this went wrong before.
document_status = sa.Enum(
    "UPLOADED", "PROCESSING", "READY", "FAILED", "ARCHIVED",
    name="documentstatus",
)
document_source_type = sa.Enum(
    "UPLOAD", "TEXT", "URL",
    name="documentsourcetype",
)


def upgrade() -> None:
    bind = op.get_bind()
    document_status.create(bind, checkfirst=True)
    document_source_type.create(bind, checkfirst=True)

    op.create_table(
        "knowledge_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column(
            "source_type", document_source_type,
            nullable=False, server_default="UPLOAD",
        ),
        # A storage key, resolved by app/knowledge/storage. Never a filesystem
        # path, so nothing here is useful to an attacker who reads the table.
        sa.Column("source_uri", sa.String(500), nullable=True),
        sa.Column("original_filename", sa.String(300), nullable=True),
        sa.Column("mime_type", sa.String(120), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "status", document_status, nullable=False, server_default="UPLOADED"
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("embedding_model", sa.String(120), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_estimate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("doc_metadata", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "content_hash", name="uq_knowledge_doc_hash"),
    )
    op.create_index(
        "ix_knowledge_documents_tenant_id", "knowledge_documents", ["tenant_id"]
    )
    op.create_index(
        "ix_knowledge_documents_content_hash", "knowledge_documents", ["content_hash"]
    )
    op.create_index(
        "ix_knowledge_doc_tenant_status",
        "knowledge_documents", ["tenant_id", "status"],
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalised from the document on purpose: retrieval filters on this
        # column directly, so a mistake in a join cannot widen a result set
        # past one tenant.
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("chunk_metadata", sa.JSON(), nullable=True),
        sa.Column("embedding", sa.JSON(), nullable=True),
        # The full identity string "provider:model:dimensions". Vectors from a
        # different model are filtered out of retrieval rather than compared.
        sa.Column("embedding_model", sa.String(120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "document_id", "version", "chunk_index", name="uq_knowledge_chunk_slot"
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"]
    )
    op.create_index(
        "ix_knowledge_chunks_tenant_id", "knowledge_chunks", ["tenant_id"]
    )
    op.create_index(
        "ix_knowledge_chunk_tenant_doc",
        "knowledge_chunks", ["tenant_id", "document_id"],
    )


def downgrade() -> None:
    # Chunks first: they hold the foreign key.
    op.drop_index("ix_knowledge_chunk_tenant_doc", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_tenant_id", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_document_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")

    op.drop_index("ix_knowledge_doc_tenant_status", table_name="knowledge_documents")
    op.drop_index(
        "ix_knowledge_documents_content_hash", table_name="knowledge_documents"
    )
    op.drop_index("ix_knowledge_documents_tenant_id", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")

    bind = op.get_bind()
    document_source_type.drop(bind, checkfirst=True)
    document_status.drop(bind, checkfirst=True)
