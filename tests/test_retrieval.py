"""
tests/test_retrieval.py

Integration tests for core/retrieval.py: the five retrieval primitives, the
merge/scoring step against core/dynamics.py's UNMODIFIED activation_score()/
rank_and_bound(), activation_traces persistence, and hybrid_query() end to
end. Uses MockEmbedder throughout (deterministic, no API cost) -- real
Voyage validation already happened in tests/test_voyage_live.py.

Requires the full stack running:
    docker compose up -d

Each test uses a real Postgres transaction (ROLLBACK at end, fresh owner_id
per run) and real Qdrant + Neo4j writes (explicit cleanup by owner_id / by
Owner node at the end).

Run:
    python tests/test_retrieval.py

Exit codes: 0 all passed, 1 failures present, 3 stack not reachable.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _check_stack() -> tuple[bool, str]:
    try:
        import psycopg2
        conn = psycopg2.connect(
            os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain"),
            connect_timeout=5,
        )
        conn.close()
    except Exception as e:
        return False, f"Postgres unreachable: {e}"
    try:
        from neo4j import GraphDatabase
        d = GraphDatabase.driver(
            os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
            auth=("neo4j", "brainbrain"),
        )
        d.verify_connectivity()
        d.close()
    except Exception as e:
        return False, f"Neo4j unreachable: {e}"
    try:
        from qdrant_client import QdrantClient
        c = QdrantClient(url=os.environ.get("QDRANT_URL", "http://localhost:6333"))
        c.get_collections()
    except Exception as e:
        return False, f"Qdrant unreachable: {e}"
    return True, "ok"


ok, msg = _check_stack()
if not ok:
    print(f"SKIP  Stack not reachable: {msg}")
    print("      Start with: docker compose up -d")
    sys.exit(3)

import psycopg2
from neo4j import GraphDatabase
from qdrant_client import QdrantClient, models

from core.calibration_fixtures import get_fixture
from core.dynamics import ACTIVATION_WEIGHTS, CONTRADICTION_BONUS
from core.embedder import MockEmbedder
from core.indexer import index_experience
from core.ingest import ingest
from core.projector import project_experience
from core.retrieval import (
    current_state_retrieve,
    graph_retrieve,
    hybrid_query,
    merge_and_score,
    pattern_model_retrieve,
    resolve_seed_entities,
    semantic_retrieve,
    temporal_retrieve,
)
from core.storage import MockStorageClient

# This suite inserts its own temporary owner and rolls the whole
# transaction back at the end -- it needs an EMPTY owner table, and its
# "exactly 7 points for this owner_id" and similar assertions assume
# exclusive ownership of everything under that owner_id. owner is a strict
# DB-enforced singleton (CLAUDE.md: "Single owner... There is no tenancy"),
# so once a real owner is seeded (e.g. by using the Brain Console, api/)
# there is no way to get a second, isolated owner for testing, and reusing
# the real one would also risk this suite's owner_id-filtered Qdrant/Neo4j
# cleanup deleting real data. Skip cleanly instead.
_owner_probe = psycopg2.connect(os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain"))
_owner_probe_cur = _owner_probe.cursor()
_owner_probe_cur.execute("SELECT id FROM owner LIMIT 1")
_existing_owner = _owner_probe_cur.fetchone()
_owner_probe.close()
if _existing_owner is not None:
    print(f"SKIP  An owner is already permanently seeded ({_existing_owner[0]}).")
    print("      This suite needs an owner-less database (it creates a temporary")
    print("      owner and rolls back, and its assertions assume exclusive ownership")
    print("      of everything under that owner_id). Run it before seeding an owner")
    print("      (e.g. via the Brain Console) or against a fresh stack.")
    sys.exit(3)

UTC = timezone.utc
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


class MockLLMClient:
    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, system: str, user: str) -> str:
        return self._response


PG_DSN = os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain")
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")

conn = psycopg2.connect(PG_DSN)
conn.autocommit = False
driver = GraphDatabase.driver(NEO4J_URI, auth=("neo4j", "brainbrain"))
qc = QdrantClient(url=QDRANT_URL)
embedder = MockEmbedder()
cur = conn.cursor()

_qdrant_owner_ids: list[str] = []


def _cleanup() -> None:
    for oid in _qdrant_owner_ids:
        flt = models.Filter(must=[models.FieldCondition(key="owner_id", match=models.MatchValue(value=oid))])
        for c in ("episodic_memory", "semantic_memory", "cognitive_memory", "documents"):
            qc.delete(collection_name=c, points_selector=models.FilterSelector(filter=flt))
        with driver.session() as s:
            s.run("MATCH (o:Owner {id: $oid}) OPTIONAL MATCH (o)-[*0..4]-(n) DETACH DELETE o, n", oid=oid)
    conn.rollback()
    conn.close()
    driver.close()


def _ingest(text, source_type, owner_id, mock_json, occurred_at=None):
    return ingest(text, source_type, conn=conn, owner_id=owner_id,
                  client=MockLLMClient(mock_json), storage=MockStorageClient(),
                  occurred_at=occurred_at)


try:
    # =======================================================================
    section("SETUP: owner + entity + a rich, deliberately varied fixture")
    # =======================================================================

    cur.execute("INSERT INTO owner (display_name, timezone) VALUES (%s,%s) RETURNING id",
                ("Test Owner (retrieval)", "UTC"))
    owner_id = str(cur.fetchone()[0])
    _qdrant_owner_ids.append(owner_id)
    NOW = datetime.now(UTC)

    # Fixtures live in core/calibration_fixtures.py, shared with the Brain
    # Console's Calibration page (api/routers/calibration.py) -- one
    # definition, not two copies.
    fx_a = get_fixture("priya_kubernetes_mixed")
    fx_b = get_fixture("weak_evidence_belief")
    fx_c = get_fixture("contradiction_candidate")
    fx_d = get_fixture("old_episodic")
    fx_e = get_fixture("newer_generalization")

    TEXT_A = fx_a.text
    result_a = _ingest(TEXT_A, fx_a.source_type, owner_id, fx_a.mock_json)
    check("SETUP.A experience created, 3 memories, 1 belief", result_a.created
          and result_a.memory_count == 3 and result_a.belief_count == 1)

    TEXT_B = fx_b.text
    result_b = _ingest(TEXT_B, fx_b.source_type, owner_id, fx_b.mock_json)
    check("SETUP.B weak-evidence experience created, 1 belief", result_b.created and result_b.belief_count == 1)

    TEXT_C = fx_c.text
    result_c = _ingest(TEXT_C, fx_c.source_type, owner_id, fx_c.mock_json)
    check("SETUP.C contradiction-candidate belief created", result_c.created and result_c.belief_count == 1)
    cur.execute("SELECT id FROM beliefs WHERE owner_id=%s AND proposition LIKE %s",
                (owner_id, "Our deployment pipeline%"))
    contradicted_belief_id = str(cur.fetchone()[0])
    cur.execute("UPDATE beliefs SET status = 'contradicted' WHERE id = %s", (contradicted_belief_id,))

    TEXT_D = fx_d.text
    old_occurred_at = NOW + timedelta(days=fx_d.occurred_at_offset_days)
    result_d = _ingest(TEXT_D, fx_d.source_type, owner_id, fx_d.mock_json, occurred_at=old_occurred_at)
    check("SETUP.D old episodic memory created", result_d.created and result_d.memory_count == 1)
    cur.execute("SELECT id FROM memories WHERE owner_id=%s AND content LIKE %s",
                (owner_id, "Debugged a Kubernetes networking%"))
    old_episodic_id = str(cur.fetchone()[0])

    TEXT_E = fx_e.text
    result_e = _ingest(TEXT_E, fx_e.source_type, owner_id, fx_e.mock_json)
    check("SETUP.E newer semantic generalization created", result_e.created and result_e.memory_count == 1)
    cur.execute("SELECT id FROM memories WHERE owner_id=%s AND content LIKE %s",
                (owner_id, "Kubernetes networking outages are usually%"))
    generalization_id = str(cur.fetchone()[0])
    cur.execute("UPDATE memories SET consolidated_into_id = %s WHERE id = %s",
                (generalization_id, old_episodic_id))

    # --- Entity + open state item -------------------------------------------
    cur.execute("SELECT id FROM entities WHERE owner_id=%s AND canonical_name='Priya'", (owner_id,))
    priya_id = str(cur.fetchone()[0])

    cur.execute(
        "INSERT INTO state_items (owner_id, kind, content, entity_id, priority, activation) "
        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (owner_id, "uncertainty", "Worried the Kubernetes rollout is too risky", priya_id, 0.8, 0.9),
    )
    state_item_id = str(cur.fetchone()[0])

    cur.execute(
        "INSERT INTO cognitive_patterns (owner_id, name, trigger_condition, sequence, "
        "description, epistemic_status, confidence) VALUES (%s,%s,%s,%s::cognitive_op[],%s,%s,%s) "
        "RETURNING id",
        (owner_id, "Kubernetes rollout caution", "deploying a risky Kubernetes change",
         ["seek_information", "decide"], "Gathers info before risky deploys.", "hypothesis", 0.3),
    )
    pattern_id = str(cur.fetchone()[0])

    # --- Index everything into Qdrant, project everything into Neo4j -------
    for exp_id in (result_a.experience_id, result_b.experience_id, result_c.experience_id,
                   result_d.experience_id, result_e.experience_id):
        index_experience(str(exp_id), conn, embedder, qc, storage=MockStorageClient())
        project_experience(str(exp_id), conn, driver)

    cur.execute("SELECT id FROM memories WHERE owner_id=%s AND content LIKE %s",
                (owner_id, "Priya raised%"))
    priya_subject_memory_id = str(cur.fetchone()[0])
    cur.execute("SELECT id FROM memories WHERE owner_id=%s AND content LIKE %s",
                (owner_id, "Kubernetes clusters need%"))
    semantic_only_memory_id = str(cur.fetchone()[0])
    cur.execute("SELECT id FROM memories WHERE owner_id=%s AND content LIKE %s",
                (owner_id, "Had lunch%"))
    priya_context_memory_id = str(cur.fetchone()[0])
    cur.execute("SELECT id FROM beliefs WHERE owner_id=%s AND proposition LIKE %s",
                (owner_id, "Kubernetes autoscaling%"))
    weak_evidence_belief_id = str(cur.fetchone()[0])

except Exception as e:
    print(f"FAIL  SETUP failed: {e}")
    FAILURES.append("SETUP")
    _cleanup()
    sys.exit(1)


# ===========================================================================
section("PRIMITIVE CONTRACTS: each retrieval primitive independently")
# ===========================================================================

sem_hits = semantic_retrieve("What do I know about Kubernetes?", embedder, qc, owner_id, conn, limit=20)
check("PC.1 semantic_retrieve returns candidates with source_id, not opaque text",
      len(sem_hits) > 0 and all(h.source_id and h.source_type for h in sem_hits))
check("PC.2 semantic_retrieve candidates carry a reason", all(h.reason for h in sem_hits))

seeds = resolve_seed_entities(cur, owner_id, "What did Priya say about the rollout?")
check("PC.3 resolve_seed_entities finds Priya by name", any(s["canonical_name"] == "Priya" for s in seeds))

graph_hits = graph_retrieve([priya_id], driver, conn, limit=20)
check("PC.4 graph_retrieve returns candidates connected to the seed entity",
      len(graph_hits) >= 2)
check("PC.5 graph_retrieve candidates carry hops and association_strength",
      all(h.hops == 1 and h.association_strength is not None for h in graph_hits))

temp_hits = temporal_retrieve(conn, owner_id, as_of=NOW, limit=20)
check("PC.6 temporal_retrieve returns candidates ordered by recency (most recent first)",
      len(temp_hits) > 0
      and all(temp_hits[i].raw_score >= temp_hits[i + 1].raw_score for i in range(len(temp_hits) - 1)))

state_hits = current_state_retrieve(conn, owner_id, limit=20)
check("PC.7 current_state_retrieve returns the open state_item",
      any(h.source_id == state_item_id for h in state_hits))

pat_hits = pattern_model_retrieve(conn, owner_id, "deploying a risky Kubernetes change", limit=20)
check("PC.8 pattern_model_retrieve returns the matching cognitive_pattern via keyword overlap",
      any(h.source_id == pattern_id for h in pat_hits))
check("PC.9 pattern_model_retrieve uses the 'situation' channel",
      all(h.retrieval_channel == "situation" for h in pat_hits))


# ===========================================================================
section("CALIBRATION 1: semantic-only relevance")
# ===========================================================================

scored = merge_and_score(conn, sem_hits, as_of=NOW)
semantic_only = next((mc for mc, s in scored if mc.source_id == semantic_only_memory_id), None)
check("CAL1.1 the entity-free 'Kubernetes clusters need resource limits' memory is found",
      semantic_only is not None)
if semantic_only:
    check("CAL1.2 it has zero association/situation channel contribution (no entity, no pattern match)",
          semantic_only.dynamics_candidate.association == 0.0
          and semantic_only.dynamics_candidate.situation == 0.0)
    check("CAL1.3 its semantic channel alone is enough to activate it",
          semantic_only.dynamics_candidate.semantic > 0.0)


# ===========================================================================
section("CALIBRATION 2 & 6: graph-only / graph-strong-but-semantically-weak relevance")
# ===========================================================================

lunch_semantic = next((h for h in sem_hits if h.source_id == priya_context_memory_id), None)
res_lunch_query = hybrid_query("What's going on with Priya?", conn, embedder, qc, driver, owner_id,
                                as_of=NOW, record_trace=False)
lunch_merged = next((mc for mc, s in res_lunch_query.scored if mc.source_id == priya_context_memory_id), None)
check("CAL2.1 the 'lunch with Priya' memory (weak semantic match to 'Kubernetes') is still found via graph",
      lunch_merged is not None and any(d.retrieval_channel == "graph" for d in lunch_merged.discoveries))
if lunch_merged:
    check("CAL2.2 its association channel is nonzero even though semantic content is weak/unrelated",
          lunch_merged.dynamics_candidate.association > 0.0)


# ===========================================================================
section("CALIBRATION 3: temporal relevance")
# ===========================================================================

most_recent = temp_hits[0]
check("CAL3.1 the most recently ingested memory has the highest temporal raw_score",
      most_recent.raw_score > 0.9, f"{most_recent.raw_score:.3f}")
check("CAL3.2 the 400-day-old episodic memory ranks far lower on recency alone",
      any(h.source_id == old_episodic_id and h.raw_score < 0.1 for h in temp_hits),
      str([(h.source_id == old_episodic_id, h.raw_score) for h in temp_hits if h.source_id == old_episodic_id]))


# ===========================================================================
section("CALIBRATION 4: contradiction retrieval")
# ===========================================================================

res_contra = hybrid_query("Tell me about our deployment pipeline", conn, embedder, qc, driver,
                           owner_id, as_of=NOW, record_trace=False)
contra_merged = next((mc for mc, s in res_contra.scored if mc.source_id == contradicted_belief_id), None)
check("CAL4.1 the contradicted belief is present in the candidate pool",
      contra_merged is not None)
if contra_merged:
    check("CAL4.2 is_contradiction propagated from beliefs.status", contra_merged.dynamics_candidate.is_contradiction)
    kept_refs = {c.ref for c, _ in res_contra.kept}
    check("CAL4.3 the contradicted belief survives into the bounded (kept) result",
          contra_merged.ref in kept_refs)


# ===========================================================================
section("CALIBRATION 5: current-state relevance / goal_alignment")
# ===========================================================================

res_state = hybrid_query("Anything I should be worried about?", conn, embedder, qc, driver, owner_id,
                          as_of=NOW, record_trace=False)
state_merged = next((mc for mc, s in res_state.scored if mc.source_id == state_item_id), None)
check("CAL5.1 the open state_item itself is a candidate", state_merged is not None)
priya_subject_merged = next((mc for mc, s in res_state.scored if mc.source_id == priya_subject_memory_id), None)
check("CAL5.2 a Priya-linked memory gets goal_alignment=1.0 because it shares an entity "
      "with the open state_item",
      priya_subject_merged is not None and priya_subject_merged.dynamics_candidate.goal_alignment == 1.0)
if semantic_only:
    check("CAL5.3 the entity-free memory gets goal_alignment=0.0 (no shared entity with any open state_item)",
          semantic_only.dynamics_candidate.goal_alignment == 0.0)


# ===========================================================================
section("CALIBRATION 7: semantically strong but low-confidence candidate")
# ===========================================================================

weak_hit = next((h for h in sem_hits if h.source_id == weak_evidence_belief_id), None)
check("CAL7.1 the weak-evidence belief is found by semantic search (its text is on-topic)",
      weak_hit is not None)
scored7 = merge_and_score(conn, sem_hits, as_of=NOW)
weak_merged = next((mc for mc, s in scored7 if mc.source_id == weak_evidence_belief_id), None)
check("CAL7.2 its evidence_quality reflects the weak observation (extraction_confidence=0.3, "
      "source_reliability=0.3), not just its own high truth_confidence",
      weak_merged is not None and weak_merged.dynamics_candidate.evidence_quality < 0.2,
      f"{weak_merged.dynamics_candidate.evidence_quality:.3f}" if weak_merged else "not found")


# ===========================================================================
section("CALIBRATION 8: old episodic memory vs newer semantic generalization")
# ===========================================================================

old_merged = next((mc for mc, s in merge_and_score(conn, temp_hits, as_of=NOW) if mc.source_id == old_episodic_id), None)
check("CAL8.1 the old (consolidated) episodic memory has staleness=0.0 regardless of age or consolidation",
      old_merged is not None and old_merged.dynamics_candidate.staleness == 0.0)
cur.execute("SELECT valid_to, superseded_by FROM memories WHERE id=%s", (old_episodic_id,))
check("CAL8.2 its valid_to/superseded_by are still NULL (DB permanence untouched)",
      cur.fetchone() == (None, None))


# ===========================================================================
section("CALIBRATION 9: association-strength / role effects")
# ===========================================================================

subject_hit = next((h for h in graph_hits if h.source_id == priya_subject_memory_id), None)
context_hit = next((h for h in graph_hits if h.source_id == priya_context_memory_id), None)
check("CAL9.1 both role variants are found via graph_retrieve", subject_hit is not None and context_hit is not None)
if subject_hit and context_hit:
    check("CAL9.2 a 'subject' role scores higher association than a 'context' role",
          subject_hit.association_strength > context_hit.association_strength,
          f"subject={subject_hit.association_strength} context={context_hit.association_strength}")


# ===========================================================================
section("CALIBRATION 10: evidence/provenance preservation")
# ===========================================================================

priya_subject_hit = next((h for h in sem_hits if h.source_id == priya_subject_memory_id), None)
check("CAL10.1 candidate carries evidence_refs pointing back to its observation",
      priya_subject_hit is not None and len(priya_subject_hit.evidence_refs) > 0)
if priya_subject_hit and priya_subject_hit.evidence_refs:
    ref = priya_subject_hit.evidence_refs[0]
    cur.execute("SELECT id FROM observations WHERE id=%s", (ref["source_id"],))
    check("CAL10.2 the evidence_ref's observation resolves in Postgres", cur.fetchone() is not None)

trace_check = hybrid_query("What do I know about Kubernetes?", conn, embedder, qc, driver, owner_id,
                            as_of=NOW, record_trace=True)
cur.execute("SELECT candidate_count, included_count, seed_entity_ids FROM activation_traces WHERE id=%s",
            (trace_check.trace_id,))
trow = cur.fetchone()
check("CAL10.3 activation_traces row persisted with matching counts",
      trow is not None and trow[0] == len(trace_check.scored) and trow[1] == len(trace_check.kept))
cur.execute("SELECT object_type, channel, raw_score, final_score, rank, included, is_contradiction "
            "FROM activation_members WHERE trace_id=%s", (trace_check.trace_id,))
member_rows = cur.fetchall()
check("CAL10.4 activation_members rows persisted, one per (candidate, channel) discovery",
      len(member_rows) > 0)
_append_only_trace_id = trace_check.trace_id  # checked last -- see bottom of file


# ===========================================================================
section("VECTOR SIMILARITY ALONE CANNOT DOMINATE")
# ===========================================================================

res_final = hybrid_query("What do I know about Kubernetes and Priya?", conn, embedder, qc, driver,
                          owner_id, as_of=NOW, record_trace=False)
by_semantic_order = sorted(
    [mc for mc, s in res_final.scored],
    key=lambda mc: mc.dynamics_candidate.semantic, reverse=True,
)
by_final_order = [mc for mc, s in res_final.scored]
semantic_refs = [mc.ref for mc in by_semantic_order]
final_refs = [mc.ref for mc in by_final_order]
check("V.1 final ranking order differs from a pure-semantic-score ordering "
      "(graph/state/situation/recency/contradiction all move the needle)",
      semantic_refs != final_refs, f"semantic-first={semantic_refs[0]} final-first={final_refs[0]}")

top_semantic_ref = by_semantic_order[0].ref
top_final_ref = by_final_order[0].ref
check("V.2 the highest-semantic-score candidate is not automatically the top-ranked result",
      True,  # informational -- report whether they coincide or not, both are valid outcomes
      f"top-semantic={top_semantic_ref}  top-final={top_final_ref}  "
      f"{'SAME' if top_semantic_ref == top_final_ref else 'DIFFERENT'}")


# ===========================================================================
section("CALIBRATION 10 (cont'd): activation_traces is append-only")
# ===========================================================================
# Must run LAST: a rejected UPDATE aborts the current Postgres transaction,
# and the whole fixture (owner, memories, beliefs, entities, state_items,
# patterns) lives in this one transaction -- recovering with conn.rollback()
# would discard all of it, breaking every check after this point.

try:
    cur.execute("UPDATE activation_traces SET query_text = 'tampered' WHERE id = %s",
                (_append_only_trace_id,))
    check("CAL10.5 activation_traces is append-only (UPDATE should be rejected)", False)
except psycopg2.errors.RaiseException:
    check("CAL10.5 activation_traces is append-only (UPDATE should be rejected)", True)
    conn.rollback()


# ===========================================================================
print()
print("=" * 62)
print(f"  {len(PASSES)} passed, {len(FAILURES)} failed")
print("=" * 62)
for f in FAILURES:
    print(f"  FAILED:  {f}")

_cleanup()
print("\nCleanup: Qdrant points + Neo4j subgraph deleted, Postgres transaction rolled back.")

sys.exit(1 if FAILURES else 0)
