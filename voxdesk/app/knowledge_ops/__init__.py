"""Knowledge operations (Phase 4 slice 4): vector math + knowledge graph.

Pure, deterministic, stdlib-only. These are the database-free cores of the
existing ``app/knowledge`` package — retrieval scoring/top-k/deduplication and
a tenant-scoped labelled graph — lifted out so they can run and be tested
without SQLAlchemy, numpy or the async stack.
"""

from app.knowledge_ops.graph import (
    Entity,
    KnowledgeGraph,
    Relation,
    validate_graph,
)
from app.knowledge_ops.vectors import (
    cosine_similarity,
    deduplicate,
    l2_normalize,
    l2_norm,
    top_k,
)

__all__ = [
    "Entity",
    "KnowledgeGraph",
    "Relation",
    "cosine_similarity",
    "deduplicate",
    "l2_norm",
    "l2_normalize",
    "top_k",
    "validate_graph",
]
