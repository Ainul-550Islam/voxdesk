"""
Tenant-scoped retrieval-augmented generation.

Layout:

    storage/      where raw uploaded bytes live (local disk or S3)
    extractors/   bytes -> normalized text + metadata, one module per format
    embeddings/   text -> vectors, behind a provider interface
    chunking.py   text -> deterministic, overlapping, metadata-carrying chunks
    vectorstore.py similarity search, tenant filter inside the query
    retrieval.py  the read path used by the agent
    rerank.py     optional second-pass ordering
    context.py    retrieved chunks -> a grounded, injection-resistant prompt
    ingest.py     the write path: validate -> extract -> chunk -> embed -> READY
    jobs.py       run ingestion inline now, in a worker later

The one rule that outranks everything else: a chunk belonging to tenant A must
never be returned to tenant B. Every query filters on tenant_id in its own
WHERE clause rather than filtering results afterwards.
"""
