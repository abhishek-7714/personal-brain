"""
core/query_inspector.py

Read-only inspection of one hybrid_query() run (core/retrieval.py). Prints
what was retrieved from each channel, why, and how the deterministic ranker
(core/dynamics.py:activation_score/rank_and_bound, unmodified) scored it.
Does NOT call an LLM and does NOT generate an answer -- the point is to be
able to look at activation behavior and judge whether it makes sense before
anything reasons over the result.

CLI usage:
    python -m core.query_inspector "What do I know about Kubernetes?"
    python -m core.query_inspector "..." --embedder mock   # no API cost (default)
    python -m core.query_inspector "..." --embedder voyage # real embeddings, costs money
    python -m core.query_inspector "..." --no-trace        # don't write activation_traces

Library usage:
    from core.query_inspector import inspect_query
    print(inspect_query("...", conn, embedder, qdrant_client, neo4j_driver, owner_id))
"""

from __future__ import annotations

import os
from typing import Optional

from core.retrieval import HybridQueryResult, hybrid_query


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_score(x: Optional[float]) -> str:
    return f"{x:.3f}" if x is not None else "  -  "


def format_inspection(res: HybridQueryResult) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("QUERY")
    lines.append("=" * 78)
    lines.append(res.query)

    lines.append("")
    lines.append("=" * 78)
    lines.append("ACTIVATION SEEDS")
    lines.append("=" * 78)
    if res.seed_entities:
        for e in res.seed_entities:
            lines.append(f"  {e['canonical_name']}  (kind={e['kind']}, "
                          f"centrality={e['centrality']:.2f}, id={e['id']})")
    else:
        lines.append("  (none matched by name/alias in the query text -- graph retrieval, "
                      "if any, was seeded entirely from top semantic hits' entities)")

    semantic = [rc for rc in res.raw_candidates if rc.retrieval_channel == "semantic"]
    graph = [rc for rc in res.raw_candidates if rc.retrieval_channel == "graph"]
    temporal = [rc for rc in res.raw_candidates if rc.retrieval_channel == "temporal"]
    state = [rc for rc in res.raw_candidates if rc.retrieval_channel == "state"]
    situation = [rc for rc in res.raw_candidates if rc.retrieval_channel == "situation"]

    lines.append("")
    lines.append("=" * 78)
    lines.append("QDRANT RESULTS (semantic channel)")
    lines.append("=" * 78)
    if not semantic:
        lines.append("  (no hits)")
    for rc in semantic:
        lines.append(f"  score={_fmt_score(rc.normalized_score)}  {rc.source_type}:{rc.source_id}")
        lines.append(f"      source : {rc.content_preview!r}")
        lines.append(f"      reason : {rc.reason}")

    lines.append("")
    lines.append("=" * 78)
    lines.append("NEO4J RESULTS (graph channel)")
    lines.append("=" * 78)
    if not graph:
        lines.append("  (no hits -- either no seed entities, or none have INVOLVES/ABOUT edges)")
    for rc in graph:
        lines.append(f"  {rc.source_type}:{rc.source_id}")
        lines.append(f"      relationship : {rc.reason}")
        lines.append(f"      strength     : {_fmt_score(rc.association_strength)}  (hops={rc.hops})")

    lines.append("")
    lines.append("=" * 78)
    lines.append("POSTGRES RESULTS (temporal / state / situation channels)")
    lines.append("=" * 78)
    lines.append(f"  -- temporal ({len(temporal)}) --")
    for rc in temporal[:10]:
        lines.append(f"    score={_fmt_score(rc.normalized_score)}  {rc.reason}  :: {rc.content_preview!r}")
    lines.append(f"  -- current state ({len(state)}) --")
    for rc in state:
        lines.append(f"    score={_fmt_score(rc.normalized_score)}  {rc.reason}  :: {rc.content_preview!r}")
    lines.append(f"  -- mental models / cognitive patterns ({len(situation)}) --")
    for rc in situation:
        lines.append(f"    score={_fmt_score(rc.normalized_score)}  {rc.reason}  :: {rc.content_preview!r}")

    lines.append("")
    lines.append("=" * 78)
    lines.append("FINAL RANKING (core.dynamics.activation_score / rank_and_bound, unmodified)")
    lines.append("=" * 78)
    kept_refs = {c.ref for c, _ in res.kept}
    for mc, score in res.scored:
        included = mc.ref in kept_refs
        dc = mc.dynamics_candidate
        mark = "[INCLUDED]" if included else "[excluded]"
        lines.append(f"  {mark}  final={score:.3f}  {mc.ref}")
        lines.append(
            "      breakdown: semantic={:.2f} association={:.2f} situation={:.2f} "
            "goal_alignment={:.2f} salience={:.2f} evidence_quality={:.2f} "
            "staleness={:.2f} is_contradiction={}".format(
                dc.semantic, dc.association, dc.situation, dc.goal_alignment,
                dc.salience, dc.evidence_quality, dc.staleness, dc.is_contradiction,
            )
        )
        for d in mc.discoveries:
            lines.append(f"      reason [{d.retrieval_channel}]: {d.reason}")

    lines.append("")
    lines.append(f"activation_traces id: {res.trace_id or '(not recorded)'}")
    return "\n".join(lines)


def inspect_query(
    query: str,
    conn,
    embedder,
    qdrant_client,
    neo4j_driver,
    owner_id: str,
    record_trace: bool = True,
) -> str:
    res = hybrid_query(query, conn, embedder, qdrant_client, neo4j_driver, owner_id,
                        record_trace=record_trace)
    return format_inspection(res)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import psycopg2
    from neo4j import GraphDatabase
    from qdrant_client import QdrantClient

    parser = argparse.ArgumentParser(
        description="Inspect hybrid retrieval + deterministic activation for one query. "
                    "Read-only; never calls an LLM."
    )
    parser.add_argument("query", help="Query text")
    parser.add_argument("--embedder", choices=["mock", "voyage"], default="mock",
                         help="mock = no cost, lexical-hash only (default). "
                              "voyage = real embeddings, costs money.")
    parser.add_argument("--owner-id", default=None,
                         help="Owner UUID (default: the singleton owner row)")
    parser.add_argument("--no-trace", action="store_true",
                         help="Do not write an activation_traces row")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain"),
    )
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "brainbrain"))
    parser.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL", "http://localhost:6333"))
    args = parser.parse_args()

    if args.embedder == "voyage":
        from core.embedder import VoyageEmbedder
        embedder = VoyageEmbedder()
    else:
        from core.embedder import MockEmbedder
        embedder = MockEmbedder()

    conn = psycopg2.connect(args.dsn)
    driver = GraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    qc = QdrantClient(url=args.qdrant_url)

    try:
        owner_id = args.owner_id
        if owner_id is None:
            cur = conn.cursor()
            cur.execute("SELECT id FROM owner LIMIT 1")
            row = cur.fetchone()
            if row is None:
                print("No owner row found. Pass --owner-id or seed the owner first.")
                return
            owner_id = str(row[0])

        print(inspect_query(args.query, conn, embedder, qc, driver, owner_id,
                             record_trace=not args.no_trace))
        conn.commit()  # persist the activation_traces row, if any
    finally:
        conn.close()
        driver.close()


if __name__ == "__main__":
    main()
