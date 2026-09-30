"""
tests/test_voyage_live.py

Live-provider validation for core/embedder.py:VoyageEmbedder. Makes real
Voyage AI API calls (billed, network-dependent) -- this is deliberately
separate from tests/test_indexer.py, which stays on MockEmbedder for
deterministic, no-key, no-cost unit testing. This file exists to answer one
question tests/test_indexer.py structurally cannot: does the real embedding
model produce genuinely semantic (not just lexical) retrieval?

Requires:
    docker compose up -d          # Postgres + Qdrant
    VOYAGE_API_KEY set             # real key, never logged or asserted-on
    voyageai installed             # pip install -r requirements.txt

Run:
    python tests/test_voyage_live.py

Exit codes: 0 all passed, 1 failures present, 3 stack/key not reachable.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _check_stack() -> tuple[bool, str]:
    if not os.environ.get("VOYAGE_API_KEY"):
        return False, "VOYAGE_API_KEY not set"
    try:
        import voyageai  # noqa: F401
    except ImportError:
        return False, "voyageai package not installed"
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
    print(f"SKIP  Stack/key not reachable: {msg}")
    print("      Set VOYAGE_API_KEY and run: docker compose up -d")
    sys.exit(3)

import psycopg2
from qdrant_client import QdrantClient, models

from core.embedder import VoyageEmbedder
from core.indexer import index_experience, semantic_search
from core.ingest import ingest
from core.storage import MockStorageClient
from vector.collections import COLLECTIONS, EMBEDDING_DIM

# This suite inserts its own temporary owner and rolls the whole
# transaction back at the end -- it needs an EMPTY owner table. owner is a
# strict DB-enforced singleton (CLAUDE.md: "Single owner... There is no
# tenancy"), so once a real owner is seeded (e.g. by using the Brain
# Console, api/) there is no way to get a second, isolated owner for
# testing. Skip cleanly rather than reusing the real owner.
_owner_probe = psycopg2.connect(os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain"))
_owner_probe_cur = _owner_probe.cursor()
_owner_probe_cur.execute("SELECT id FROM owner LIMIT 1")
_existing_owner = _owner_probe_cur.fetchone()
_owner_probe.close()
if _existing_owner is not None:
    print(f"SKIP  An owner is already permanently seeded ({_existing_owner[0]}).")
    print("      This suite needs an owner-less database (it creates a temporary")
    print("      owner and rolls back). Run it before seeding an owner (e.g. via")
    print("      the Brain Console) or against a fresh stack.")
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


PG_DSN = os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")

conn = psycopg2.connect(PG_DSN)
conn.autocommit = False
qc = QdrantClient(url=QDRANT_URL)
embedder = VoyageEmbedder()  # real provider -- reads VOYAGE_API_KEY itself, never logged here

_qdrant_ids: dict[str, list[str]] = {}


def _qdrant_cleanup() -> None:
    for collection, ids in _qdrant_ids.items():
        if ids:
            qc.delete(collection_name=collection, points_selector=models.PointIdsList(points=ids))


# ===========================================================================
section("1. LIVE VOYAGE CALL: model, dimension, connectivity")
# ===========================================================================

check("V.1 embedder configured for voyage-4 (or VOYAGE_MODEL override)",
      embedder.provider == "voyage", embedder.model_name)

live_vectors = embedder.embed(["What do I know about Kubernetes?"], input_type="query")
check("V.2 live API call returns exactly one vector", len(live_vectors) == 1)
check("V.3 returned dimension matches requested output_dimension",
      len(live_vectors[0]) == embedder.output_dimension,
      f"got {len(live_vectors[0])}, requested {embedder.output_dimension}")
check("V.4 EMBEDDING_DIM env matches embedder output_dimension",
      EMBEDDING_DIM == embedder.output_dimension,
      f"EMBEDDING_DIM={EMBEDDING_DIM}")


# ===========================================================================
section("2. QDRANT COLLECTION CONFIG: vector size and distance metric")
# ===========================================================================

for collection in COLLECTIONS:
    info = qc.get_collection(collection)
    size = info.config.params.vectors.size
    distance = info.config.params.vectors.distance
    check(f"C.{collection} vector size == embedder output_dimension",
          size == embedder.output_dimension, f"{size} vs {embedder.output_dimension}")
    check(f"C.{collection} distance == Cosine",
          str(distance).lower().endswith("cosine"), str(distance))


# ===========================================================================
section("3. SEMANTIC PARAPHRASE TEST (real embeddings, lexically disjoint fixture)")
# ===========================================================================
# mem2 is the load-bearing case: "container orchestration" shares zero
# tokens with "Kubernetes" or any other fixture text. MockEmbedder (pure
# lexical hashing) would score this at exactly 0 similarity to the query --
# see core/embedder.py:MockEmbedder's own docstring, which names this
# limitation explicitly. A real semantic model should not.

TEXT = (
    "I learned Helm and Kubernetes infrastructure. I worked extensively on "
    "container orchestration. I built cloud infrastructure around Kubernetes. "
    "I debugged a race condition in our Python asyncio event loop."
)

MOCK_JSON = json.dumps({
    "observations": [{
        "content": TEXT, "epistemic_status": "asserted", "epistemic_owner": "owner",
        "span": TEXT[0:len(TEXT)],
        "extraction_confidence": 0.9, "source_reliability": 1.0,
    }],
    "entities": [],
    "memories": [
        {
            "memory_system": "procedural", "kind": "skill",
            "content": "Comfortable working with Helm and Kubernetes infrastructure.",
            "salience": 0.5, "emotion_is_stated": False,
            "evidence_span": TEXT[0:len(TEXT)],
        },
        {
            "memory_system": "procedural", "kind": "skill",
            "content": "Experienced working on container orchestration across a fleet of services.",
            "salience": 0.5, "emotion_is_stated": False,
            "evidence_span": TEXT[0:len(TEXT)],
        },
        {
            "memory_system": "episodic", "kind": "milestone",
            "content": "Built out the company's cloud infrastructure around Kubernetes.",
            "salience": 0.6, "emotion_is_stated": False,
            "evidence_span": TEXT[0:len(TEXT)],
        },
        {
            "memory_system": "procedural", "kind": "skill",
            "content": "Comfortable debugging race conditions in a Python asyncio event loop.",
            "salience": 0.5, "emotion_is_stated": False,
            "evidence_span": TEXT[0:len(TEXT)],
        },
    ],
    "beliefs": [], "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
})

cur = conn.cursor()
cur.execute(
    "INSERT INTO owner (display_name, timezone) VALUES (%s, %s) RETURNING id",
    ("Test Owner (voyage live)", "UTC"),
)
owner_id = str(cur.fetchone()[0])
storage = MockStorageClient()

try:
    result = ingest(TEXT, "note", conn=conn, owner_id=owner_id,
                     client=MockLLMClient(MOCK_JSON), storage=storage)
    exp_id = str(result.experience_id)
    check("P.1 fixture experience created with 4 memories",
          result.created and result.memory_count == 4)

    ir = index_experience(exp_id, conn, embedder, qc, storage=storage)
    check("P.2 real embedding + indexing succeeded", ir.points_written == 4)

    cur.execute(
        "SELECT id, content FROM memories WHERE owner_id = %s ORDER BY created_at", (owner_id,)
    )
    mem_rows = cur.fetchall()

    helm_id = next(str(r[0]) for r in mem_rows if "Helm" in r[1])
    orchestration_id = next(str(r[0]) for r in mem_rows if "orchestration" in r[1])
    milestone_id = next(str(r[0]) for r in mem_rows if "Built out" in r[1])
    unrelated_id = next(str(r[0]) for r in mem_rows if "asyncio" in r[1])

    for coll in ("episodic_memory", "cognitive_memory"):
        pts, _ = qc.scroll(
            collection_name=coll,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id))]
            ),
            limit=10,
        )
        _qdrant_ids[coll] = [str(p.id) for p in pts]

    hits = semantic_search("What do I know about Kubernetes?", embedder, qc, owner_id, limit=10)
    by_id = {h["source_id"]: h["score"] for h in hits}

    check("P.3 all 4 memories appear in results", {helm_id, orchestration_id, milestone_id, unrelated_id}.issubset(by_id.keys()))

    kube_ids = {helm_id, orchestration_id, milestone_id}
    if kube_ids.issubset(by_id.keys()) and unrelated_id in by_id:
        min_kube = min(by_id[i] for i in kube_ids)
        unrelated_score = by_id[unrelated_id]
        check("P.4 every Kubernetes-related memory outscores the unrelated one",
              min_kube > unrelated_score,
              f"min kube={min_kube:.4f} vs unrelated={unrelated_score:.4f}")

        check("P.5 'container orchestration' paraphrase (zero lexical overlap "
              "with 'Kubernetes') scores meaningfully above the unrelated memory "
              "-- proves this is semantic, not lexical, matching",
              by_id[orchestration_id] > unrelated_score + 0.05,
              f"orchestration={by_id[orchestration_id]:.4f} vs unrelated={unrelated_score:.4f}")

    top_hit = max(hits, key=lambda h: h["score"]) if hits else None
    if top_hit:
        cur.execute("SELECT id FROM memories WHERE id = %s", (top_hit["source_id"],))
        resolved = cur.fetchone() is not None
    else:
        resolved = False
    check("P.6 top hit's source_id resolves back to Postgres", resolved, str(top_hit))

except Exception as e:
    print(f"FAIL  fixture/indexing failed: {e}")
    FAILURES.append("FIXTURE")


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
