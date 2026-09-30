"""
tests/test_activation_events.py

Unit tests for api.routers.retrieval._build_activation_events() -- pure edge
derivation from a hand-built core.retrieval.HybridQueryResult, no live stack
required (unlike tests/test_retrieval.py and tests/test_api.py, which drive
the real Postgres/Neo4j/Qdrant path with the priya_kubernetes_mixed fixture).

This file only checks the shape of the derived activation_events list: that
edges point at real candidates, that multi-seed convergence produces one
event per matching seed, that non-graph channels source from the transient
"query" root, and that "query" never appears as a target (it is a UI-only
visualization root -- see feedback_visualization_query_node memory -- never
a canonical node with a source_type/source_id).

Run:
    python tests/test_activation_events.py

Exit codes: 0 all passed, 1 failures present.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.routers.retrieval import _build_activation_events
from core.dynamics import Candidate
from core.retrieval import HybridQueryResult, MergedCandidate, RetrievalCandidate

FAILURES: list[str] = []
PASSES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSES.append(label)
        print(f"PASS  {label}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILURES.append(label)
        print(f"FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _mc(ref: str, source_type: str, source_id: str, discoveries: list[RetrievalCandidate]) -> tuple[MergedCandidate, float]:
    dyn = Candidate(ref=ref)
    mc = MergedCandidate(ref=ref, source_type=source_type, source_id=source_id,
                          discoveries=discoveries, dynamics_candidate=dyn)
    return mc, 0.5


# ===========================================================================
section("EMPTY RESULT")
# ===========================================================================

empty = HybridQueryResult(
    query="nothing", seed_entities=[], raw_candidates=[], scored=[], kept=[],
    trace_id=None, all_seed_ids=[],
)
check("E.1 empty result produces no events", _build_activation_events(empty, []) == [])


# ===========================================================================
section("SEED EVENTS")
# ===========================================================================

seed_only = HybridQueryResult(
    query="kubernetes", seed_entities=[{"id": "ent-1", "canonical_name": "Kubernetes", "kind": "concept", "centrality": 0.5}],
    raw_candidates=[], scored=[], kept=[], trace_id=None, all_seed_ids=["ent-1"],
)
events = _build_activation_events(seed_only, seed_only.seed_entities)
check("S.1 one seed produces exactly one seed event", len(events) == 1, str(events))
check("S.2 seed event source is 'query'", events[0]["source"] == "query")
check("S.3 seed event target is entity:<id>", events[0]["target"] == "entity:ent-1")
check("S.4 seed event channel is 'seed'", events[0]["channel"] == "seed")


# ===========================================================================
section("SINGLE-SEED GRAPH DISCOVERY")
# ===========================================================================

d1 = RetrievalCandidate(
    source_id="mem-1", source_type="memory", retrieval_channel="graph",
    raw_score=0.8, normalized_score=0.8, reason="via Kubernetes",
    hops=1, entity_ids=["ent-1"],
)
result_single = HybridQueryResult(
    query="kubernetes", seed_entities=[{"id": "ent-1", "canonical_name": "Kubernetes", "kind": "concept", "centrality": 0.5}],
    raw_candidates=[d1], scored=[_mc("memory:mem-1", "memory", "mem-1", [d1])], kept=[],
    trace_id=None, all_seed_ids=["ent-1"],
)
events = _build_activation_events(result_single, result_single.seed_entities)
graph_events = [e for e in events if e["channel"] == "graph"]
check("G.1 single-seed graph discovery produces exactly one graph edge", len(graph_events) == 1, str(graph_events))
check("G.2 edge is sourced from the matching seed entity", graph_events[0]["source"] == "entity:ent-1")
check("G.3 edge targets the candidate ref", graph_events[0]["target"] == "memory:mem-1")
check("G.4 edge carries hops through unmodified", graph_events[0]["hops"] == 1)


# ===========================================================================
section("MULTI-SEED CONVERGENCE (spec section 11: A->C, B->C, D->C)")
# ===========================================================================

d2 = RetrievalCandidate(
    source_id="mem-2", source_type="memory", retrieval_channel="graph",
    raw_score=0.9, normalized_score=0.9, reason="via Kubernetes and Priya",
    hops=1, entity_ids=["ent-1", "ent-2", "ent-3"],
)
result_multi = HybridQueryResult(
    query="kubernetes rollout", seed_entities=[
        {"id": "ent-1", "canonical_name": "Kubernetes", "kind": "concept", "centrality": 0.5},
        {"id": "ent-2", "canonical_name": "Priya", "kind": "person", "centrality": 0.3},
    ],
    raw_candidates=[d2], scored=[_mc("memory:mem-2", "memory", "mem-2", [d2])], kept=[],
    trace_id=None, all_seed_ids=["ent-1", "ent-2"],  # ent-3 deliberately not a seed
)
events = _build_activation_events(result_multi, result_multi.seed_entities)
graph_events = [e for e in events if e["channel"] == "graph"]
check("M.1 candidate reached from 2 seeds produces 2 distinct convergence edges",
      len(graph_events) == 2, str(graph_events))
sources = {e["source"] for e in graph_events}
check("M.2 both edges come from real seed entities, not the non-seed entity ent-3",
      sources == {"entity:ent-1", "entity:ent-2"}, str(sources))
check("M.3 both edges converge on the same target", all(e["target"] == "memory:mem-2" for e in graph_events))


# ===========================================================================
section("SEMANTIC-EXPANDED SEED (not resolved by resolve_seed_entities, only via all_seed_ids)")
# ===========================================================================

# Mirrors core.retrieval.hybrid_query()'s extra_seed_ids: an entity pulled in
# from a top semantic hit's entity_ids, which never went through the
# query-text name/alias match resolve_seed_entities() does. The router must
# still resolve it (via _resolve_entities against Postgres) and pass it in
# all_seed_entities so it gets a seed node + a query->entity edge here --
# this is the exact gap tests/test_api.py's V.6 caught against the live
# Kubernetes fixture (0 name-matched seed_entities, but real graph edges).
d5 = RetrievalCandidate(
    source_id="mem-5", source_type="memory", retrieval_channel="graph",
    raw_score=0.7, normalized_score=0.7, reason="via semantically-expanded entity",
    hops=1, entity_ids=["ent-9"],
)
result_expanded = HybridQueryResult(
    query="kubernetes rollout", seed_entities=[],  # nothing name-matched
    raw_candidates=[d5], scored=[_mc("memory:mem-5", "memory", "mem-5", [d5])], kept=[],
    trace_id=None, all_seed_ids=["ent-9"],  # only reached via semantic expansion
)
resolved_extra = [{"id": "ent-9", "canonical_name": "Kubernetes", "kind": "concept", "centrality": 0.4}]
events = _build_activation_events(result_expanded, resolved_extra)
check("X.1 a semantic-expanded seed still gets a query->entity seed event",
      any(e["channel"] == "seed" and e["target"] == "entity:ent-9" for e in events), str(events))
check("X.2 the graph edge is sourced from the semantic-expanded seed, not left dangling on 'query'",
      any(e["channel"] == "graph" and e["source"] == "entity:ent-9" for e in events), str(events))


# ===========================================================================
section("NON-GRAPH CHANNELS SOURCE FROM 'query'")
# ===========================================================================

d3 = RetrievalCandidate(
    source_id="mem-3", source_type="memory", retrieval_channel="semantic",
    raw_score=0.6, normalized_score=0.6, reason="cosine similarity",
)
d4 = RetrievalCandidate(
    source_id="state-1", source_type="state_item", retrieval_channel="state",
    raw_score=0.4, normalized_score=0.4, reason="open goal",
)
result_nongraph = HybridQueryResult(
    query="kubernetes", seed_entities=[], raw_candidates=[d3, d4],
    scored=[_mc("memory:mem-3", "memory", "mem-3", [d3]), _mc("state_item:state-1", "state_item", "state-1", [d4])],
    kept=[], trace_id=None, all_seed_ids=[],
)
events = _build_activation_events(result_nongraph, result_nongraph.seed_entities)
check("N.1 semantic-channel event is sourced from 'query'",
      any(e["channel"] == "semantic" and e["source"] == "query" for e in events))
check("N.2 state-channel event is sourced from 'query'",
      any(e["channel"] == "state" and e["source"] == "query" for e in events))
check("N.3 'query' never appears as a target -- it is a transient UI root, never a canonical node",
      all(e["target"] != "query" for e in events))


# ===========================================================================
print()
print("=" * 62)
print(f"  {len(PASSES)} passed, {len(FAILURES)} failed")
print("=" * 62)
for f in FAILURES:
    print(f"  FAILED:  {f}")

sys.exit(1 if FAILURES else 0)
