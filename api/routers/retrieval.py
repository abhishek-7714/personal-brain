"""
api/routers/retrieval.py

Section 5 (Retrieval Playground) backend. A pure wrapper over
core.retrieval.hybrid_query() -- the exact same function
tests/test_retrieval.py and core/query_inspector.py already call. No
ranking math, no candidate construction, no seed resolution happens in
this file; it only serializes hybrid_query()'s result to JSON. No LLM call
anywhere in this path.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.deps import get_conn, get_embedder
from core.retrieval import RetrievalCandidate, hybrid_query

router = APIRouter()


class RetrievalRequest(BaseModel):
    query: str
    owner_id: Optional[str] = None
    record_trace: bool = True


def _serialize_candidate(rc: RetrievalCandidate) -> dict:
    return {
        "source_id": rc.source_id,
        "source_type": rc.source_type,
        "retrieval_channel": rc.retrieval_channel,
        "raw_score": rc.raw_score,
        "normalized_score": rc.normalized_score,
        "reason": rc.reason,
        "content_preview": rc.content_preview,
        "hops": rc.hops,
        "association_strength": rc.association_strength,
        "entity_ids": rc.entity_ids,
    }


def _resolve_entities(cur, entity_ids: list[str]) -> dict[str, dict]:
    """Resolves entity_ids to {id, canonical_name, kind, centrality} -- the
    same shape resolve_seed_entities() already returns, so the frontend
    renders every seed node identically regardless of how it was found.
    Needed because hybrid_query()'s all_seed_ids includes entities pulled
    from top semantic hits (extra_seed_ids in core.retrieval.hybrid_query),
    which never go through resolve_seed_entities() and so never get a
    canonical_name/kind on their own."""
    if not entity_ids:
        return {}
    cur.execute(
        "SELECT id, canonical_name, kind, centrality FROM entities WHERE id = ANY(%s::uuid[])",
        (entity_ids,),
    )
    return {
        str(r[0]): {"id": str(r[0]), "canonical_name": r[1], "kind": r[2], "centrality": r[3]}
        for r in cur.fetchall()
    }


def _build_activation_events(res, all_seed_entities: list[dict]) -> list[dict]:
    """
    Derives the edge list the Neural Brain Visualization animates a pulse
    along, from data hybrid_query() already computed -- no new scoring or
    graph traversal happens here. The `"query"` string used as a source
    below is a transient UI/visualization root only: it is never written to
    Postgres/Neo4j/Qdrant and never acquires a source_type/source_id, unlike
    every other endpoint in this ref -- those all resolve back to a real
    canonical row.

    Two kinds of edges:
      - "seed": query -> entity, one per entity in all_seed_entities (every
        entity graph_retrieve() actually walked from -- both the ones the
        query text named directly AND the ones pulled in from top semantic
        hits, see _resolve_entities()). This is the query-to-graph-seed step
        (spec stage 1/2), not itself a retrieval channel.
      - one edge per (discovery, matching source) pair: a graph-channel
        discovery gets one edge from EVERY seed entity in all_seed_ids that
        the discovery's own entity_ids intersects -- this is what produces
        real multi-source convergence (A->C, B->C, D->C) when several seeds
        independently reach the same memory/belief via INVOLVES/ABOUT.
        Every other channel (semantic/temporal/state/situation) has no
        graph path, so it is sourced directly from "query".
    """
    events: list[dict] = []
    for e in all_seed_entities:
        events.append({
            "source": "query", "target": f"entity:{e['id']}", "channel": "seed",
            "strength": 1.0, "hops": None, "reason": f"query resolves to entity '{e['canonical_name']}'",
        })

    seed_id_set = set(res.all_seed_ids)
    for mc, _score in res.scored:
        target = f"{mc.source_type}:{mc.source_id}"
        for d in mc.discoveries:
            if d.retrieval_channel == "graph":
                matching_seeds = seed_id_set.intersection(d.entity_ids)
                sources = [f"entity:{sid}" for sid in matching_seeds] or ["query"]
            else:
                sources = ["query"]
            for source in sources:
                events.append({
                    "source": source, "target": target, "channel": d.retrieval_channel,
                    "strength": d.normalized_score, "hops": d.hops, "reason": d.reason,
                })
    return events


@router.post("/query")
def run_query(body: RetrievalRequest, request: Request, conn=Depends(get_conn), embedder=Depends(get_embedder)) -> dict:
    owner_id = body.owner_id
    if not owner_id:
        cur = conn.cursor()
        cur.execute("SELECT id FROM owner LIMIT 1")
        row = cur.fetchone()
        if not row:
            raise HTTPException(400, "No owner seeded. POST /api/status/seed-owner first.")
        owner_id = str(row[0])

    res = hybrid_query(
        body.query, conn, embedder, request.app.state.qdrant, request.app.state.neo4j_driver,
        owner_id, record_trace=body.record_trace,
    )
    if body.record_trace:
        conn.commit()  # persist the activation_traces row
    else:
        conn.rollback()

    seed_by_id = {e["id"]: e for e in res.seed_entities}
    missing_ids = [eid for eid in res.all_seed_ids if eid not in seed_by_id]
    resolved_extra = _resolve_entities(conn.cursor(), missing_ids)
    all_seed_entities = list(seed_by_id.values()) + list(resolved_extra.values())

    kept_refs = {c.ref for c, _ in res.kept}
    merged = []
    for mc, score in res.scored:
        dc = mc.dynamics_candidate
        merged.append({
            "ref": mc.ref, "source_type": mc.source_type, "source_id": mc.source_id,
            "final_score": score, "included": mc.ref in kept_refs,
            "breakdown": {
                "semantic": dc.semantic, "association": dc.association,
                "situation": dc.situation, "goal_alignment": dc.goal_alignment,
                "salience": dc.salience, "evidence_quality": dc.evidence_quality,
                "staleness": dc.staleness, "is_contradiction": dc.is_contradiction,
            },
            "discoveries": [_serialize_candidate(d) for d in mc.discoveries],
        })

    return {
        "query": res.query,
        "seed_entities": res.seed_entities,
        "qdrant_results": [_serialize_candidate(rc) for rc in res.raw_candidates if rc.retrieval_channel == "semantic"],
        "neo4j_results": [_serialize_candidate(rc) for rc in res.raw_candidates if rc.retrieval_channel == "graph"],
        "postgres_results": {
            "temporal": [_serialize_candidate(rc) for rc in res.raw_candidates if rc.retrieval_channel == "temporal"],
            "state": [_serialize_candidate(rc) for rc in res.raw_candidates if rc.retrieval_channel == "state"],
            "situation": [_serialize_candidate(rc) for rc in res.raw_candidates if rc.retrieval_channel == "situation"],
        },
        "merged_candidates": merged,
        "trace_id": res.trace_id,
        "all_seed_entities": all_seed_entities,
        "activation_events": _build_activation_events(res, all_seed_entities),
    }
