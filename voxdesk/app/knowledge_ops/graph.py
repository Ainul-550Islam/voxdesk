"""A small, deterministic knowledge graph (Phase 4, knowledge slice).

This is an in-memory labelled property graph aimed at *support* content
(solutions → articles) and the FAQ/intent side of the knowledge base — the
shapes the retrieval pipeline already reasons about — not a replacement for
the vector store or the structured DB. It answers two questions:

* What does a support ticket about ``X`` most likely relate to?
* What are the top related/aliased entities for a phrase?

It is intentionally single-tenant: an ``id`` namespace must be provided and
every node/edge is stamped with it, so cross-tenant leakage is impossible at
the data-structure level.
"""

from __future__ import annotations

import heapq
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.knowledge_ops.vectors import cosine_similarity


class GraphError(ValueError):
    """Raised for malformed graph input or unresolvable references."""


# --------------------------------------------------------------- primitives --

@dataclass(frozen=True)
class Entity:
    """A node: an id unique within its tenant namespace, a label, and attrs."""

    id: str
    label: str
    attrs: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.id or not self.id.strip() or len(self.id) > 128:
            problems.append("entity id must be 1-128 characters")
        if not self.label or not self.label.strip() or len(self.label) > 64:
            problems.append("entity label must be 1-64 characters")
        return problems


@dataclass(frozen=True)
class Relation:
    """A directed, typed edge between two entities in the same graph."""

    source: str
    target: str
    type: str
    weight: float = 1.0
    attrs: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.source or not self.target:
            problems.append("relation requires source and target")
        if not self.type or not self.type.strip() or len(self.type) > 64:
            problems.append("relation type must be 1-64 characters")
        if not (0.0 <= self.weight <= 10.0):
            problems.append("relation weight must be in [0, 10]")
        return problems


# ------------------------------------------------------------------- graph --

@dataclass
class KnowledgeGraph:
    """Tenant-scoped labelled graph with adjacency + alias lookup."""

    namespace: str
    entities: dict[str, Entity] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    _outgoing: dict[str, list[Relation]] = field(default_factory=dict)
    _aliases: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.namespace or not self.namespace.strip():
            raise GraphError("namespace is required")
        if self.entities:
            self.rebuild()

    # ---------------------------------------------------------- mutators ----

    def add_entity(self, entity: Entity) -> None:
        problems = entity.validate()
        if problems:
            raise GraphError("; ".join(problems))
        if entity.id in self.entities:
            raise GraphError(f"entity {entity.id!r} already exists")
        self.entities[entity.id] = entity

    def add_relation(self, relation: Relation) -> None:
        problems = relation.validate()
        if problems:
            raise GraphError("; ".join(problems))
        if relation.source not in self.entities:
            raise GraphError(f"relation source {relation.source!r} is not an entity")
        if relation.target not in self.entities:
            raise GraphError(f"relation target {relation.target!r} is not an entity")
        self.relations.append(relation)
        self._outgoing.setdefault(relation.source, []).append(relation)

    def add_alias(self, alias: str, entity_id: str) -> None:
        if not alias.strip():
            raise GraphError("alias must not be empty")
        if entity_id not in self.entities:
            raise GraphError(f"alias target {entity_id!r} is not an entity")
        self._aliases[alias.lower()] = entity_id

    def resolve_alias(self, alias: str) -> str | None:
        return self._aliases.get(alias.lower())

    def rebuild(self) -> None:
        """Re-derive adjacency from ``relations`` (for caller-constructed graphs)."""
        self._outgoing = defaultdict(list)
        for relation in self.relations:
            self._outgoing[relation.source].append(relation)

    # ------------------------------------------------------------ queries ----

    def outgoing(self, entity_id: str) -> list[Relation]:
        return list(self._outgoing.get(entity_id, []))

    def neighbours(self, entity_id: str) -> list[tuple[Relation, Entity]]:
        return [(relation, self.entities[relation.target])
                for relation in self._outgoing.get(entity_id, [])]

    def related_entities(self, entity_id: str, limit: int) -> list[tuple[str, str, float]]:
        """The ``limit`` entities most strongly related to ``entity_id`` by
        outgoing edge weight, as ``(entity_id, relation_type, weight)``.
        Excludes the entity itself and returns at most ``limit`` entries."""
        ranked = sorted(
            ((rel.target, rel.type, rel.weight) for rel in self._outgoing.get(entity_id, [])),
            key=lambda entry: (-entry[2], entry[0]),
        )
        return ranked[:limit]

    def find_path(self, source: str, target: str) -> list[str] | None:
        """Shortest path (fewest edges) as a list of entity ids, or ``None``.

        Ties are broken deterministically by lexicographic id order, so the
        returned path is stable across runs for a given graph."""
        if source == target and source in self.entities:
            return [source]
        if source not in self.entities or target not in self.entities:
            return None

        queue: deque[str] = deque([source])
        previous: dict[str, str | None] = {source: None}
        while queue:
            current = queue.popleft()
            neighbours = sorted(rel.target for rel in self._outgoing.get(current, []))
            for neighbour in neighbours:
                if neighbour in previous:
                    continue
                previous[neighbour] = current
                if neighbour == target:
                    path = [neighbour]
                    step = current
                    while step is not None:
                        path.append(step)
                        step = previous[step]
                    path.reverse()
                    return path
                queue.append(neighbour)
        return None

    def related_by_vector(
        self,
        vector: Sequence[float],
        embedding: Callable[[Entity], Sequence[float]],
        limit: int,
    ) -> list[tuple[Entity, float]]:
        """Top-``limit`` entities by cosine similarity to ``vector``, most
        similar first. Ties keep insertion order (stable sort)."""
        scored = [(cosine_similarity(vector, embedding(entity)), order, entity)
                  for order, entity in enumerate(self.entities.values())]
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return [(entity, score) for score, _order, entity in scored[:limit]]

    def suggested_links(self, threshold: float, limit: int) -> list[tuple[str, str, float]]:
        """Suggest entity pairs that are *not* directly linked but share many
        neighbours — the classic "co-occurrence" signal for a KB graph.

        Returns up to ``limit`` ``(source, target, jaccard)`` triples whose
        neighbour-set Jaccard similarity is >= ``threshold``, most similar
        first, ties broken lexicographically."""
        if not (0.0 < threshold <= 1.0):
            raise GraphError("threshold must be in (0, 1]")
        if limit <= 0:
            return []

        neighbour_sets: dict[str, set[str]] = {}
        for entity_id in self.entities:
            neighbour_sets[entity_id] = {rel.target for rel in self._outgoing.get(entity_id, [])}

        existing = {(rel.source, rel.target) for rel in self.relations}

        candidates: list[tuple[float, str, str]] = []
        ids = sorted(self.entities)
        for index, source in enumerate(ids):
            source_neighbours = neighbour_sets[source]
            if not source_neighbours:
                continue
            for target in ids[index + 1:]:
                if (source, target) in existing:
                    continue
                target_neighbours = neighbour_sets[target]
                if not target_neighbours:
                    continue
                union = source_neighbours | target_neighbours
                if not union:
                    continue
                jaccard = len(source_neighbours & target_neighbours) / len(union)
                if jaccard >= threshold:
                    heapq.heappush(candidates, (-jaccard, source, target))

        results: list[tuple[str, str, float]] = []
        while candidates and len(results) < limit:
            neg_jaccard, source, target = heapq.heappop(candidates)
            results.append((source, target, -neg_jaccard))
        return results

    def adjacency_snapshot(self, entity_id: str) -> dict[str, list[tuple[str, float]]]:
        """A plain, serialisable view of an entity's outgoing edges:
        ``{relation_type: [(target_id, weight), ...]}``, weight order desc."""
        grouped: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for relation in self._outgoing.get(entity_id, []):
            grouped[relation.type].append((relation.target, relation.weight))
        for edges in grouped.values():
            edges.sort(key=lambda entry: (-entry[1], entry[0]))
        return dict(grouped)


# -------------------------------------------------------------- validation --

def validate_graph(namespace: str, entities: Iterable[Entity], relations: Iterable[Relation]) -> list[str]:
    """Standalone structural validation for callers that build graphs from
    storage rows rather than :class:`KnowledgeGraph` directly."""
    problems: list[str] = []
    if not namespace or not namespace.strip():
        problems.append("namespace is required")

    entity_list = list(entities)
    seen_ids: set[str] = set()
    for entity in entity_list:
        problems += entity.validate()
        if entity.id in seen_ids:
            problems.append(f"duplicate entity id {entity.id!r}")
        seen_ids.add(entity.id)

    relation_list = list(relations)
    for relation in relation_list:
        problems += relation.validate()
        if relation.source not in seen_ids:
            problems.append(f"relation source {relation.source!r} is not an entity")
        if relation.target not in seen_ids:
            problems.append(f"relation target {relation.target!r} is not an entity")

    return problems
