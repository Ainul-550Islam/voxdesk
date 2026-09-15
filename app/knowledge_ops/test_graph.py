"""Unit tests for app.knowledge_ops.graph.

Co-located with the package; run with: python -m pytest app/knowledge_ops/ -q
"""

from __future__ import annotations

import pytest

from app.knowledge_ops.graph import (
    Entity,
    GraphError,
    KnowledgeGraph,
    Relation,
    validate_graph,
)


def graph():
    g = KnowledgeGraph(namespace="tenant-a")
    for eid, label in [("art-1", "article"), ("art-2", "article"),
                       ("sol-1", "solution"), ("sym-1", "symptom"),
                       ("prod-1", "product")]:
        g.add_entity(Entity(eid, label))
    g.add_relation(Relation("sym-1", "sol-1", "resolved_by", weight=2.0))
    g.add_relation(Relation("sym-1", "art-1", "mentioned_in"))
    g.add_relation(Relation("sol-1", "art-1", "documented_in"))
    g.add_relation(Relation("prod-1", "sol-1", "has_solution"))
    return g


def test_entity_and_relation_validation():
    assert Entity("", "x").validate()
    assert Entity("a", "").validate()
    assert Relation("", "b", "t").validate()
    assert Relation("a", "b", "").validate()
    assert Relation("a", "b", "t", weight=11).validate()


def test_add_relation_requires_known_entities():
    g = KnowledgeGraph(namespace="t")
    g.add_entity(Entity("a", "article"))
    with pytest.raises(GraphError):
        g.add_relation(Relation("a", "missing", "related_to"))


def test_duplicate_entity_rejected():
    g = KnowledgeGraph(namespace="t")
    g.add_entity(Entity("a", "article"))
    with pytest.raises(GraphError):
        g.add_entity(Entity("a", "article"))


def test_namespace_required():
    with pytest.raises(GraphError):
        KnowledgeGraph(namespace="")


def test_alias_resolution_is_case_insensitive():
    g = graph()
    g.add_alias("WiFi drops", "sym-1")
    assert g.resolve_alias("wifi drops") == "sym-1"
    assert g.resolve_alias("WIFI DROPS") == "sym-1"
    assert g.resolve_alias("unknown") is None
    with pytest.raises(GraphError):
        g.add_alias("x", "missing")


def test_related_entities_ranked_by_weight():
    g = graph()
    g.add_relation(Relation("sym-1", "art-2", "mentioned_in", weight=5.0))
    related = g.related_entities("sym-1", limit=10)
    assert related[0] == ("art-2", "mentioned_in", 5.0)
    assert ("sol-1", "resolved_by", 2.0) in related


def test_find_path_shortest_and_deterministic():
    g = graph()
    # sym-1 links directly to art-1, so the shortest path is one edge.
    assert g.find_path("sym-1", "art-1") == ["sym-1", "art-1"]
    # A path through an intermediate: prod-1 -> sol-1 -> art-1.
    assert g.find_path("prod-1", "art-1") == ["prod-1", "sol-1", "art-1"]
    assert g.find_path("sym-1", "sym-1") == ["sym-1"]
    assert g.find_path("sym-1", "missing") is None
    assert g.find_path("missing", "sym-1") is None


def test_related_by_vector_orders_by_similarity():
    g = graph()
    emb = {
        "art-1": [1.0, 0.0],
        "art-2": [0.9, 0.1],
        "sym-1": [0.0, 1.0],
        "sol-1": [0.0, 1.0],
        "prod-1": [0.5, 0.5],
    }
    results = g.related_by_vector([1.0, 0.0], embedding=lambda e: emb[e.id], limit=2)
    assert [entity.id for entity, _ in results] == ["art-1", "art-2"]


def test_suggested_links_finds_shared_neighbours():
    g = KnowledgeGraph(namespace="t")
    for eid in ["a", "b", "c"]:
        g.add_entity(Entity(eid, "article"))
    # a and b both point at c → high Jaccard, so (a, b) is suggested.
    g.add_relation(Relation("a", "c", "mentions"))
    g.add_relation(Relation("b", "c", "mentions"))
    suggestions = g.suggested_links(threshold=0.5, limit=10)
    assert ("a", "b", 1.0) in suggestions


def test_suggested_links_excludes_existing_edges():
    g = KnowledgeGraph(namespace="t")
    for eid in ["a", "b", "c"]:
        g.add_entity(Entity(eid, "article"))
    g.add_relation(Relation("a", "c", "mentions"))
    g.add_relation(Relation("b", "c", "mentions"))
    g.add_relation(Relation("a", "b", "mentions"))  # already linked
    suggestions = g.suggested_links(threshold=0.5, limit=10)
    assert ("a", "b", 1.0) not in suggestions


def test_suggested_links_validates_threshold():
    g = graph()
    with pytest.raises(GraphError):
        g.suggested_links(threshold=0.0, limit=5)


def test_adjacency_snapshot_grouped_and_sorted():
    g = graph()
    g.add_relation(Relation("sym-1", "art-2", "mentioned_in", weight=3.0))
    snapshot = g.adjacency_snapshot("sym-1")
    assert snapshot["resolved_by"] == [("sol-1", 2.0)]
    assert snapshot["mentioned_in"] == [("art-2", 3.0), ("art-1", 1.0)]


def test_validate_graph_standalone():
    problems = validate_graph(
        "t",
        [Entity("a", "article"), Entity("a", "article")],
        [Relation("a", "missing", "related_to")],
    )
    assert any("duplicate" in p for p in problems)
    assert any("not an entity" in p for p in problems)
    assert not validate_graph("t", [Entity("a", "article")], [])
