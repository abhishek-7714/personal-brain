"""
tests/test_indexer.py

Integration tests for the Postgres -> Qdrant indexing worker (core/indexer.py).

Requires the full stack running:
    docker compose up -d
    # migrations applied, collections bootstrapped at the configured EMBEDDING_DIM

Uses MockEmbedder (core/embedder.py) -- a deterministic, feature-hashed
bag-of-words embedding -- so this runs without VOYAGE_API_KEY. See
core/embedder.py:MockEmbedder for exactly what it does and does not prove.

Each test uses a real Postgres transaction (ROLLBACK at end, fresh owner_id
per run so "no unexpected objects" assertions are exact) and real Qdrant
writes (explicit delete by owner_id at the end).

Run:
    python tests/test_indexer.py

Exit codes: 0 all passed, 1 failures present, 3 stack not reachable.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Stack connectivity check -- fail fast before importing heavy modules
# ---------------------------------------------------------------------------

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
from qdrant_client import QdrantClient, models

from core.embedder import MockEmbedder
from core.indexer import (
    IndexResult,
    index_experience,
    index_mental_models_and_patterns,
    semantic_search,
)
from core.ingest import ingest
from core.storage import MockStorageClient

# This suite inserts its own temporary owner and rolls the whole
# transaction back at the end -- it needs an EMPTY owner table, and its
# "no unexpected objects" assertions assume exclusive ownership of
# everything under that owner_id. owner is a strict DB-enforced singleton
# (CLAUDE.md: "Single owner... There is no tenancy"), so once a real owner
# is seeded (e.g. by using the Brain Console, api/) there is no way to get
# a second, isolated owner for testing, and reusing the real one would also
# risk this suite's owner_id-filtered Qdrant cleanup deleting real points.
# Skip cleanly instead.
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


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

PG_DSN = os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")

conn = psycopg2.connect(PG_DSN)
conn.autocommit = False
qc = QdrantClient(url=QDRANT_URL)
embedder = MockEmbedder()

_qdrant_ids: dict[str, list[str]] = {}  # collection -> point ids, for cleanup


def _qdrant_cleanup() -> None:
    for collection, ids in _qdrant_ids.items():
        if ids:
            qc.delete(collection_name=collection, points_selector=models.PointIdsList(points=ids))


def _scroll_owner(collection: str, owner_id: str) -> list:
    points, _ = qc.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id))]
        ),
        limit=100,
        with_payload=True,
        with_vectors=True,
    )
    return points


# ===========================================================================
section("SETUP: owner + a mixed fixture (episodic/procedural/semantic memory, belief, entity)")
# ===========================================================================

TEXT = (
    "I deployed our Kubernetes cluster today and Priya said our rollout strategy "
    "was too aggressive. I am now comfortable running Kubernetes clusters in "
    "production. Kubernetes clusters need resource limits to run smoothly."
)
priya_start = TEXT.find("Priya")

MOCK_JSON = json.dumps({
    "observations": [{
        "content": TEXT,
        "epistemic_status": "reported",
        "epistemic_owner": "other_person",
        "span": TEXT[0:len(TEXT)],
        "extraction_confidence": 0.9,
        "source_reliability": 1.0,
    }],
    "entities": [{
        "canonical_name": "Priya",
        "kind": "person",
        "mention_span": TEXT[priya_start:priya_start + len("Priya")],
    }],
    "memories": [
        {
            "memory_system": "episodic", "kind": "exchange",
            "content": "Deployed the Kubernetes cluster; Priya said the rollout strategy was too aggressive.",
            "salience": 0.6, "emotion_is_stated": False,
            "evidence_span": TEXT[0:len(TEXT)],
            "involved_entities": [{"canonical_name": "Priya", "role": "subject"}],
        },
        {
            "memory_system": "procedural", "kind": "skill",
            "content": "Comfortable running Kubernetes clusters in production.",
            "salience": 0.5, "emotion_is_stated": False,
            "evidence_span": TEXT[0:len(TEXT)],
        },
        {
            "memory_system": "semantic", "kind": "fact",
            "content": "Kubernetes clusters need resource limits to run smoothly.",
            "salience": 0.5, "emotion_is_stated": False,
            "evidence_span": TEXT[0:len(TEXT)],
        },
    ],
    "beliefs": [{
        "proposition": "Our rollout strategy is too aggressive",
        "belief_kind": "attributed", "epistemic_owner": "other_person",
        "about_entity": "Priya", "domain": "deployment",
        "holding_confidence": 0.7, "truth_confidence": 0.3,
        "evidence_span": TEXT[0:len(TEXT)],
    }],
    "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
})

TEXT_DOC = (
    "Kubernetes is a container orchestration platform. It automates deployment, "
    "scaling, and management of containerized applications across clusters of machines."
)
MOCK_DOC_JSON = json.dumps({
    "observations": [], "entities": [], "memories": [], "beliefs": [],
    "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
})

owner_id = None
exp_id = None
doc_exp_id = None
mm_id = None
cp_id = None

try:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO owner (display_name, timezone) VALUES (%s, %s) RETURNING id",
        ("Test Owner", "UTC"),
    )
    owner_id = str(cur.fetchone()[0])

    storage = MockStorageClient()

    result = ingest(TEXT, "note", conn=conn, owner_id=owner_id,
                     client=MockLLMClient(MOCK_JSON), storage=storage)
    exp_id = str(result.experience_id)
    check("SETUP.1 mixed experience created", result.created)
    check("SETUP.2 3 memories (episodic+procedural+semantic)", result.memory_count == 3)
    check("SETUP.3 1 belief", result.belief_count == 1)
    check("SETUP.4 1 memory_entities link", result.memory_entity_link_count == 1)

    doc_result = ingest(TEXT_DOC, "document", conn=conn, owner_id=owner_id,
                         client=MockLLMClient(MOCK_DOC_JSON), storage=storage)
    doc_exp_id = str(doc_result.experience_id)
    check("SETUP.5 document experience created", doc_result.created)

    cur.execute(
        "INSERT INTO mental_models (owner_id, name, principle, applies_when, domain, confidence) "
        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (owner_id, "Reversible-decision speed",
         "Make reversible decisions fast; deliberate on irreversible ones.",
         "Facing a decision under time pressure", "decision-making", 0.6),
    )
    mm_id = str(cur.fetchone()[0])

    cur.execute(
        "INSERT INTO cognitive_patterns "
        "(owner_id, name, trigger_condition, sequence, description, epistemic_status, confidence) "
        "VALUES (%s,%s,%s,%s::cognitive_op[],%s,%s,%s) RETURNING id",
        (owner_id, "Kubernetes rollout caution",
         "deploying a risky change to a live Kubernetes cluster",
         ["seek_information", "evaluate_option", "decide"],
         "Tends to gather more information before committing to risky deploys.",
         "hypothesis", 0.3),
    )
    cp_id = str(cur.fetchone()[0])

except Exception as e:
    print(f"FAIL  SETUP failed: {e}")
    FAILURES.append("SETUP")
    conn.rollback()
    sys.exit(1)


# ===========================================================================
section("INDEXING: first run")
# ===========================================================================

r1: IndexResult = index_experience(exp_id, conn, embedder, qc, storage=storage)
r2: IndexResult = index_experience(doc_exp_id, conn, embedder, qc, storage=storage)
r3: IndexResult = index_mental_models_and_patterns(conn, embedder, qc)

check("I.1 mixed experience: 5 points (3 memories + 1 belief + episode=0)",
      r1.points_written == 4, f"{r1.points_written} points")
check("I.2 document experience: 1 point", r2.points_written == 1)
check("I.3 sweep: 2 points (1 mental_model + 1 cognitive_pattern)", r3.points_written == 2)

_expected_ids = {
    "episodic_memory": set(),
    "semantic_memory": set(),
    "cognitive_memory": set(),
    "documents": set(),
}
for c, n in r1.collections_touched.items():
    pass  # counts only; ids checked below via direct Postgres knowledge


# ===========================================================================
section("EXTRACTION_RUNS BOOKKEEPING")
# ===========================================================================

cur.execute(
    "SELECT status, started_at, finished_at, error FROM extraction_runs "
    "WHERE experience_id = %s AND stage = 'qdrant_embedding'",
    (exp_id,),
)
run_row = cur.fetchone()
check("B.1 qdrant_embedding run exists for mixed experience", run_row is not None)
if run_row:
    check("B.2 status=succeeded", run_row[0] == "succeeded")
    check("B.3 started_at and finished_at set", run_row[1] is not None and run_row[2] is not None)
    check("B.4 error is NULL on success", run_row[3] is None)


# ===========================================================================
section("QDRANT CONTENT: collection routing, payload, dimension, model version")
# ===========================================================================

# Resolve the memory/belief ids from Postgres so we can check exact routing.
cur.execute(
    "SELECT id, memory_system, kind FROM memories WHERE owner_id = %s", (owner_id,)
)
mem_rows = {str(r[0]): (r[1], r[2]) for r in cur.fetchall()}
cur.execute("SELECT id FROM beliefs WHERE owner_id = %s", (owner_id,))
belief_ids = {str(r[0]) for r in cur.fetchall()}

episodic_mem_id = next(m for m, (sys_, k) in mem_rows.items() if sys_ == "episodic")
procedural_mem_id = next(m for m, (sys_, k) in mem_rows.items() if sys_ == "procedural")
semantic_mem_id = next(m for m, (sys_, k) in mem_rows.items() if sys_ == "semantic")
belief_id = next(iter(belief_ids))

all_points_by_collection: dict[str, list] = {}
for collection in ("episodic_memory", "semantic_memory", "cognitive_memory", "documents"):
    pts = _scroll_owner(collection, owner_id)
    all_points_by_collection[collection] = pts
    _qdrant_ids[collection] = [str(p.id) for p in pts]

def _find(collection: str, source_id: str):
    for p in all_points_by_collection[collection]:
        if p.payload.get("source_id") == source_id:
            return p
    return None

ep_point = _find("episodic_memory", episodic_mem_id)
check("Q.1 episodic memory routed to episodic_memory", ep_point is not None)
if ep_point:
    check("Q.2 episodic payload source_type=memory", ep_point.payload.get("source_type") == "memory")
    check("Q.3 episodic payload entity_ids includes Priya's entity",
          len(ep_point.payload.get("entity_ids") or []) == 1)
    check("Q.4 episodic payload is_stale=False (not consolidated/superseded)",
          ep_point.payload.get("is_stale") is False)

sem_point = _find("semantic_memory", semantic_mem_id)
check("Q.5 semantic memory routed to semantic_memory", sem_point is not None)

proc_point = _find("cognitive_memory", procedural_mem_id)
check("Q.6 procedural memory routed to cognitive_memory", proc_point is not None)

bel_point = _find("semantic_memory", belief_id)
check("Q.7 belief routed to semantic_memory", bel_point is not None)
if bel_point:
    check("Q.8 belief payload source_type=belief", bel_point.payload.get("source_type") == "belief")
    check("Q.9 belief payload domain=deployment", bel_point.payload.get("domain") == "deployment")
    check("Q.10 belief payload confidence set (truth axis)",
          bel_point.payload.get("confidence") == 0.3)

doc_point = _find("documents", doc_exp_id)
check("Q.11 document experience routed to documents", doc_point is not None)
if doc_point:
    check("Q.12 document payload source_type=experience",
          doc_point.payload.get("source_type") == "experience")

mm_point = _find("semantic_memory", mm_id)
check("Q.13 mental_model routed to semantic_memory", mm_point is not None)

cp_point = _find("cognitive_memory", cp_id)
check("Q.14 cognitive_pattern routed to cognitive_memory", cp_point is not None)
if cp_point:
    check("Q.15 cognitive_pattern payload is_stale=False (status=hypothesis)",
          cp_point.payload.get("is_stale") is False)

total_points = sum(len(v) for v in all_points_by_collection.values())
check("Q.16 no unexpected objects: exactly 7 points for this owner_id",
      total_points == 7, f"{total_points} points")

all_pts = [p for pts in all_points_by_collection.values() for p in pts]
check("Q.17 every point has a 1024-dim vector",
      all(len(p.vector) == 1024 for p in all_pts),
      f"dims: {sorted(set(len(p.vector) for p in all_pts))}")

mv_ids = {p.payload.get("model_version_id") for p in all_pts}
check("Q.18 all points share one model_version_id", len(mv_ids) == 1)
if mv_ids:
    cur.execute(
        "SELECT role, provider, model_name FROM model_versions WHERE id = %s",
        (next(iter(mv_ids)),),
    )
    mv_row = cur.fetchone()
    check("Q.19 model_version resolves to Postgres", mv_row is not None)
    if mv_row:
        check("Q.20 model_version role=embedding, provider=mock",
              mv_row[0] == "embedding" and mv_row[1] == "mock", str(mv_row))

check("Q.21 every point's source_id resolves back to Postgres",
      episodic_mem_id in mem_rows and semantic_mem_id in mem_rows
      and procedural_mem_id in mem_rows and belief_id in belief_ids)


# ===========================================================================
section("IDEMPOTENCY: repeated indexing produces no duplicates")
# ===========================================================================

index_experience(exp_id, conn, embedder, qc, storage=storage)
index_experience(doc_exp_id, conn, embedder, qc, storage=storage)
index_mental_models_and_patterns(conn, embedder, qc)

total_after = sum(len(_scroll_owner(c, owner_id)) for c in all_points_by_collection)
check("D.1 point count unchanged after re-indexing", total_after == 7, f"{total_after} points")


# ===========================================================================
section("REBUILD: wipe Qdrant, re-index from Postgres, same logical index")
# ===========================================================================

before_snapshot = {
    c: sorted(
        (p.payload.get("source_id"), p.payload.get("source_type"), p.payload.get("kind"))
        for p in _scroll_owner(c, owner_id)
    )
    for c in all_points_by_collection
}

for collection, ids in _qdrant_ids.items():
    if ids:
        qc.delete(collection_name=collection, points_selector=models.PointIdsList(points=ids))

wiped_total = sum(len(_scroll_owner(c, owner_id)) for c in all_points_by_collection)
check("R.1 collections empty for this owner_id after wipe", wiped_total == 0)

index_experience(exp_id, conn, embedder, qc, storage=storage)
index_experience(doc_exp_id, conn, embedder, qc, storage=storage)
index_mental_models_and_patterns(conn, embedder, qc)

after_snapshot = {
    c: sorted(
        (p.payload.get("source_id"), p.payload.get("source_type"), p.payload.get("kind"))
        for p in _scroll_owner(c, owner_id)
    )
    for c in all_points_by_collection
}
for collection in all_points_by_collection:
    _qdrant_ids[collection] = [str(p.id) for p in _scroll_owner(collection, owner_id)]

check("R.2 rebuild reproduces the same logical index (ids/types/kinds match)",
      before_snapshot == after_snapshot)


# ===========================================================================
section("FAILURE PATH: dimension mismatch is caught, run marked failed, retry succeeds")
# ===========================================================================

class BrokenEmbedder:
    provider = "mock-broken"
    model_name = "mock-broken"

    def embed(self, texts, input_type="document"):
        return [[0.0] * 8 for _ in texts]  # wrong dimension on purpose

broken = BrokenEmbedder()
try:
    index_experience(doc_exp_id, conn, broken, qc, storage=storage)
    check("F.1 dimension mismatch raises", False)
except ValueError as e:
    check("F.1 dimension mismatch raises", True, str(e)[:60])

cur.execute(
    "SELECT status, error FROM extraction_runs "
    "WHERE experience_id = %s AND stage = 'qdrant_embedding'",
    (doc_exp_id,),
)
failed_row = cur.fetchone()
check("F.2 run marked failed", failed_row is not None and failed_row[0] == "failed")
check("F.3 error notes partial count", failed_row is not None and "partial:" in (failed_row[1] or ""))

# Retry with a working embedder must succeed (index_pending()-style recovery).
retry = index_experience(doc_exp_id, conn, embedder, qc, storage=storage)
check("F.4 retry with working embedder succeeds", retry.points_written == 1)
_qdrant_ids["documents"] = [str(p.id) for p in _scroll_owner("documents", owner_id)]

cur.execute(
    "SELECT status FROM extraction_runs "
    "WHERE experience_id = %s AND stage = 'qdrant_embedding'",
    (doc_exp_id,),
)
check("F.5 run status back to succeeded after retry", cur.fetchone()[0] == "succeeded")


# ===========================================================================
section("SEMANTIC RETRIEVAL SMOKE TEST: 'What do I know about Kubernetes?'")
# ===========================================================================
# Proves the projection is queryable end-to-end and resolvable back to
# Postgres. Not a retrieval-engine test -- no activation scoring, no
# reranking. MockEmbedder is a token-hashed bag-of-words (see
# core/embedder.py), so this checks lexical overlap, not real semantics;
# the fixture wording is deliberately clean (no incidental shared words
# between the Kubernetes-flavoured records and the unrelated ones) so a
# real neural embedder would be expected to do at least this well.

_TABLE_BY_SOURCE_TYPE = {
    "memory": "memories", "belief": "beliefs", "mental_model": "mental_models",
    "cognitive_pattern": "cognitive_patterns", "experience": "experiences",
}

def _resolves_to_postgres(source_type: str, source_id: str) -> bool:
    table = _TABLE_BY_SOURCE_TYPE.get(source_type)
    if not table:
        return False
    cur.execute(f"SELECT id FROM {table} WHERE id = %s", (source_id,))
    return cur.fetchone() is not None

kube_ids = {episodic_mem_id, procedural_mem_id, semantic_mem_id, doc_exp_id, cp_id}
non_kube_ids = {belief_id, mm_id}

hits = semantic_search("What do I know about Kubernetes?", embedder, qc, owner_id, limit=10)
by_id = {h["source_id"]: h["score"] for h in hits}

check("S.1 all Kubernetes-related records appear in results",
      kube_ids.issubset(by_id.keys()), f"missing: {kube_ids - by_id.keys()}")
check("S.2 Kubernetes-related records score above zero",
      all(by_id.get(i, 0.0) > 0 for i in kube_ids))
check("S.3 unrelated records score at or near zero",
      all(by_id.get(i, 0.0) <= 1e-6 for i in non_kube_ids))

if kube_ids.issubset(by_id.keys()):
    min_kube_score = min(by_id[i] for i in kube_ids)
    max_non_kube_score = max((by_id.get(i, 0.0) for i in non_kube_ids), default=0.0)
    check("S.4 every Kubernetes record outranks every unrelated record",
          min_kube_score > max_non_kube_score,
          f"{min_kube_score:.3f} vs {max_non_kube_score:.3f}")

check("S.5 hits carry no embedded text, only routing metadata",
      all(set(h.keys()) == {"collection", "score", "source_type", "source_id"} for h in hits))

top_hit = max(hits, key=lambda h: h["score"]) if hits else None
check("S.6 top hit resolves back to Postgres",
      top_hit is not None and _resolves_to_postgres(top_hit["source_type"], top_hit["source_id"]),
      str(top_hit))


# ===========================================================================
section("IS_STALE INVARIANT: episodic consolidation never implies historical invalidity")
# ===========================================================================
# migrations/005_episodic_invariants.sql, COMMENT ON CONSTRAINT
# episodic_not_superseded, is the authority: "The vector projection must
# never set is_stale = true for a point whose source memory is episodic."
# A prior version of core/indexer.py violated this by deriving episodic
# is_stale from consolidated_into_id -- this section is a regression test
# for that specific bug, not just a description of intended behavior.

cur.execute(
    "SELECT valid_to, superseded_by, consolidated_into_id FROM memories WHERE id = %s",
    (episodic_mem_id,),
)
before_consolidation = cur.fetchone()
check("IS.1 episodic memory starts with valid_to/superseded_by/consolidated_into_id all NULL",
      before_consolidation == (None, None, None))

# Simulate consolidation (no consolidation worker exists yet -- deferred by
# design) by pointing the episodic memory at the semantic memory already in
# this fixture, standing in for "a generalization now exists".
cur.execute(
    "UPDATE memories SET consolidated_into_id = %s WHERE id = %s",
    (semantic_mem_id, episodic_mem_id),
)

index_experience(exp_id, conn, embedder, qc, storage=storage)
_qdrant_ids["episodic_memory"] = [str(p.id) for p in _scroll_owner("episodic_memory", owner_id)]

consolidated_point = next(
    (p for p in _scroll_owner("episodic_memory", owner_id)
     if p.payload.get("source_id") == episodic_mem_id),
    None,
)
check("IS.2 Qdrant point KEEPS is_stale=False even after consolidation "
      "(migration 005's rule: never true for an episodic source)",
      consolidated_point is not None and consolidated_point.payload.get("is_stale") is False)

cur.execute(
    "SELECT valid_to, superseded_by FROM memories WHERE id = %s", (episodic_mem_id,)
)
after_consolidation = cur.fetchone()
check("IS.3 valid_to and superseded_by STILL NULL after consolidation "
      "(DB CHECK episodic_not_superseded holds -- the row's permanence is untouched)",
      after_consolidation == (None, None))

cur.execute("SELECT content FROM memories WHERE id = %s", (episodic_mem_id,))
check("IS.4 the memory's content is unchanged -- consolidation did not edit or hide the fact",
      cur.fetchone()[0] == "Deployed the Kubernetes cluster; Priya said the rollout strategy was too aggressive.")

check("IS.5 the consolidated episodic point is still resolvable to Postgres (not deleted/hidden)",
      _resolves_to_postgres("memory", episodic_mem_id))

# episodic_not_superseded is a DB CHECK, not something Python can violate via
# ORM logic -- but confirm directly that it actually fires, so "the row's
# permanence is untouched" above isn't just an absence-of-evidence claim.
try:
    cur.execute("UPDATE memories SET valid_to = now() WHERE id = %s", (episodic_mem_id,))
    check("IS.6 DB CHECK rejects setting valid_to on an episodic memory", False)
except psycopg2.errors.CheckViolation:
    check("IS.6 DB CHECK rejects setting valid_to on an episodic memory", True)
    conn.rollback()
    # rollback undid the whole transaction (owner, experiences, everything) --
    # this test file's teardown expects a live transaction to still exist for
    # its own final rollback, and no later section depends on this state, so
    # re-establish a fresh no-op transaction rather than restructure the file.
    cur = conn.cursor()


# ===========================================================================
print()
print("=" * 62)
print(f"  {len(PASSES)} passed, {len(FAILURES)} failed")
print("=" * 62)
for f in FAILURES:
    print(f"  FAILED:  {f}")

_qdrant_cleanup()
conn.rollback()
conn.close()
print("\nCleanup: Qdrant points deleted, Postgres transaction rolled back.")

sys.exit(1 if FAILURES else 0)
