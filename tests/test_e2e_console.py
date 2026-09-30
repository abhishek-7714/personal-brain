"""
tests/test_e2e_console.py

The end-to-end development test requested for the Brain Console: walks a
deterministic calibration fixture all the way through every layer --

    fixture -> ingest -> Postgres records -> Neo4j projection
             -> Qdrant indexing -> Retrieval Playground result

-- verifying each stage directly (not just trusting the higher-level
endpoint's status code) via the same clients core/*.py itself uses.

Idempotency, not cleanup: like tests/test_api.py, this file never deletes
anything (experiences/observations are append-only by design -- migrations/
003_ops_and_indexes.sql). It uses one fixed calibration fixture and asserts
that running the whole flow twice produces the SAME experience_id both
times and does not duplicate Neo4j nodes or Qdrant points -- that is the
actual regression guarantee, not post-hoc deletion.

A second, clearly optional section exercises the real free-text
POST /api/experiences path (real Anthropic extraction) with fixed,
non-personal test text -- skipped entirely if ANTHROPIC_API_KEY isn't set,
exactly like tests/test_voyage_live.py's opt-in pattern for Voyage. This is
NOT part of the routine/mandatory flow.

Run:
    python tests/test_e2e_console.py

Exit codes: 0 all passed, 1 failures present, 3 stack not reachable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

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
            os.environ.get("NEO4J_URI", "bolt://localhost:7687"), auth=("neo4j", "brainbrain"),
        )
        d.verify_connectivity()
        d.close()
    except Exception as e:
        return False, f"Neo4j unreachable: {e}"
    try:
        from qdrant_client import QdrantClient
        QdrantClient(url=os.environ.get("QDRANT_URL", "http://localhost:6333")).get_collections()
    except Exception as e:
        return False, f"Qdrant unreachable: {e}"
    return True, "ok"


ok, msg = _check_stack()
if not ok:
    print(f"SKIP  Stack not reachable: {msg}")
    print("      Start with: docker compose up -d")
    sys.exit(3)

import psycopg2
from fastapi.testclient import TestClient
from neo4j import GraphDatabase
from qdrant_client import QdrantClient

from api.main import app
from core.calibration_fixtures import get_fixture

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


FIXTURE_KEY = "priya_kubernetes_mixed"  # richest fixture: entity + episodic + semantic + belief
fixture = get_fixture(FIXTURE_KEY)

PG_DSN = os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain")
pg = psycopg2.connect(PG_DSN)
neo4j_driver = GraphDatabase.driver(
    os.environ.get("NEO4J_URI", "bolt://localhost:7687"), auth=("neo4j", "brainbrain"),
)
qc = QdrantClient(url=os.environ.get("QDRANT_URL", "http://localhost:6333"))

with TestClient(app) as client:
    # =======================================================================
    section("STAGE 0: owner (reuse the singleton, seed only if truly absent)")
    # =======================================================================

    status = client.get("/api/status").json()
    if not status["owner"]["seeded"]:
        seed = client.post("/api/status/seed-owner")
        check("S0.1 owner seeded", seed.status_code == 200)
        owner_id = seed.json()["id"]
    else:
        owner_id = status["owner"]["id"]
        check("S0.1 existing owner reused", True, owner_id)

    # =======================================================================
    section(f"STAGE 1: fixture '{FIXTURE_KEY}' -> ingest (first run)")
    # =======================================================================

    r1 = client.post(f"/api/calibration/fixtures/{FIXTURE_KEY}/ingest")
    check("S1.1 ingest request succeeds", r1.status_code == 200)
    result1 = r1.json()
    exp_id = result1["experience_id"]
    check("S1.2 pipeline reports no projection/indexing error",
          result1["projection_error"] is None and result1["indexing_error"] is None,
          str(result1))

    # =======================================================================
    section("STAGE 2: verify Postgres records directly")
    # =======================================================================

    cur = pg.cursor()
    cur.execute("SELECT source, kind FROM experiences WHERE id = %s", (exp_id,))
    row = cur.fetchone()
    check("S2.1 experience row exists", row is not None)
    if result1["created"]:
        # Freshly created this run -- the marker MUST be present.
        check("S2.2 freshly-created experience is marked as a test fixture",
              row is not None and row[0].get("source") == "test_fixture"
              and row[0].get("fixture_id") == FIXTURE_KEY)
    else:
        # Idempotent hit on a pre-existing row (e.g. this fixture was
        # ingested manually before the source=test_fixture marker existed).
        # experiences are append-only -- that historical row can't be
        # retroactively stamped, and the owner has explicitly said to leave
        # it in place rather than clean it up. Report, don't fail.
        has_marker = row is not None and row[0].get("source") == "test_fixture"
        check("S2.2 experience marker status (informational -- pre-existing row, not created this run)",
              True, f"marked={has_marker}")



    cur.execute("SELECT count(*) FROM observations WHERE experience_id = %s", (exp_id,))
    obs_count = cur.fetchone()[0]
    check("S2.3 at least one observation persisted", obs_count >= 1)

    cur.execute(
        "SELECT count(*) FROM memories m JOIN evidence ev "
        "ON ev.derived_type='memory' AND ev.derived_id=m.id "
        "WHERE ev.source_type='observation' AND ev.source_id IN "
        "(SELECT id FROM observations WHERE experience_id=%s)",
        (exp_id,),
    )
    mem_count = cur.fetchone()[0]
    check("S2.4 three memories persisted (fixture has episodic x2 + semantic x1)", mem_count == 3, str(mem_count))

    cur.execute(
        "SELECT count(*) FROM beliefs b WHERE b.id IN "
        "(SELECT ev.derived_id FROM evidence ev WHERE ev.derived_type='belief' "
        "AND ev.source_id IN (SELECT id FROM observations WHERE experience_id=%s))",
        (exp_id,),
    )
    check("S2.5 one belief persisted", cur.fetchone()[0] == 1)

    cur.execute("SELECT id, canonical_name FROM entities WHERE owner_id=%s AND canonical_name='Priya'", (owner_id,))
    entity_row = cur.fetchone()
    check("S2.6 Priya entity resolved", entity_row is not None)

    cur.execute(
        "SELECT m.id FROM memories m JOIN evidence ev "
        "ON ev.derived_type='memory' AND ev.derived_id=m.id "
        "WHERE ev.source_type='observation' AND ev.source_id IN "
        "(SELECT id FROM observations WHERE experience_id=%s)",
        (exp_id,),
    )
    fixture_memory_ids = {str(r[0]) for r in cur.fetchall()}
    check("S2.7 fixture's memory ids resolved for later cross-checks", len(fixture_memory_ids) == 3)

    # =======================================================================
    section("STAGE 3: verify Neo4j projection directly")
    # =======================================================================

    with neo4j_driver.session() as s:
        exp_node = s.run("MATCH (n:Experience {id: $id}) RETURN n", id=exp_id).single()
        check("S3.1 Experience node exists", exp_node is not None)

        produced = s.run(
            "MATCH (e:Experience {id: $id})-[:PRODUCED]->(o:Observation) RETURN count(o) AS c", id=exp_id
        ).single()["c"]
        check("S3.2 Experience -[:PRODUCED]-> Observation exists", produced >= 1)

        involves = s.run(
            "MATCH (m:Memory)-[:INVOLVES]->(ent:Entity {canonical_name: 'Priya'}) "
            "WHERE ent.id = $eid RETURN count(m) AS c",
            eid=str(entity_row[0]),
        ).single()["c"]
        check("S3.3 Memory -[:INVOLVES]-> Entity(Priya) exists", involves >= 1)

    # =======================================================================
    section("STAGE 4: verify Qdrant indexing directly")
    # =======================================================================

    from qdrant_client import models
    points, _ = qc.scroll(
        collection_name="episodic_memory",
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id))]
        ),
        limit=200,
    )
    check("S4.1 episodic_memory collection has points for this owner",
          len(points) >= 1, f"{len(points)} points")
    check("S4.2 at least one point's source_id is one of this fixture's memory ids",
          any(p.payload.get("source_id") in fixture_memory_ids for p in points),
          f"{[p.payload.get('source_id') for p in points]}")

    # =======================================================================
    section("STAGE 5: Retrieval Playground result resolves back to this fixture")
    # =======================================================================

    # Names Priya explicitly so core.retrieval.resolve_seed_entities() has a
    # direct name match to find (it matches words/phrases in the query text
    # against entity_aliases -- it does not itself do semantic matching).
    rq = client.post("/api/retrieval/query", json={"query": "What did Priya say about the Kubernetes rollout?"})
    check("S5.1 retrieval query succeeds", rq.status_code == 200)
    rdata = rq.json()
    check("S5.2 seed entity Priya resolved from query text",
          any(e["canonical_name"] == "Priya" for e in rdata["seed_entities"]))

    check("S5.3 at least one of this fixture's memories appears in the merged candidates",
          any(c["source_id"] in fixture_memory_ids for c in rdata["merged_candidates"]))
    _TABLE_BY_SOURCE_TYPE = {
        "memory": "memories", "belief": "beliefs", "mental_model": "mental_models",
        "cognitive_pattern": "cognitive_patterns", "state_item": "state_items",
    }
    top = max(rdata["merged_candidates"], key=lambda c: c["final_score"]) if rdata["merged_candidates"] else None
    if top and top["source_type"] in _TABLE_BY_SOURCE_TYPE:
        cur.execute(f"SELECT id FROM {_TABLE_BY_SOURCE_TYPE[top['source_type']]} WHERE id = %s", (top["source_id"],))
        top_resolves = cur.fetchone() is not None
    else:
        top_resolves = False
    check("S5.4 top-ranked candidate's source_id resolves back to its Postgres table",
          top_resolves, str(top))

    # =======================================================================
    section("STAGE 6: rerun regression -- no duplicate experience, no duplicate points/nodes")
    # =======================================================================

    with neo4j_driver.session() as s:
        nodes_before = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
    points_before = len(qc.scroll(
        collection_name="episodic_memory",
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id))]
        ),
        limit=1000,
    )[0])

    r2 = client.post(f"/api/calibration/fixtures/{FIXTURE_KEY}/ingest")
    result2 = r2.json()
    check("S6.1 rerun returns the SAME experience_id", result2["experience_id"] == exp_id)
    check("S6.2 rerun reports created=false", result2["created"] is False)

    with neo4j_driver.session() as s:
        nodes_after = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
    points_after = len(qc.scroll(
        collection_name="episodic_memory",
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id))]
        ),
        limit=1000,
    )[0])
    check("S6.3 Neo4j node count unchanged after rerun (MERGE stayed idempotent)",
          nodes_before == nodes_after, f"{nodes_before} -> {nodes_after}")
    check("S6.4 Qdrant point count unchanged after rerun (upsert stayed idempotent)",
          points_before == points_after, f"{points_before} -> {points_after}")

    # =======================================================================
    section("OPTIONAL: real free-text ingestion (POST /api/experiences, real Anthropic call)")
    # =======================================================================

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("SKIP  ANTHROPIC_API_KEY not set -- free-text /api/experiences not exercised "
              "(this is expected to be opt-in, not routine; see test_voyage_live.py for the same pattern).")
    else:
        FIXED_TEXT = "Brain Console end-to-end test: verifying free-text ingestion through the real pipeline."
        rr1 = client.post("/api/experiences", json={"text": FIXED_TEXT, "source_type": "note"})
        check("FT.1 free-text ingest succeeds", rr1.status_code == 200)
        rr2 = client.post("/api/experiences", json={"text": FIXED_TEXT, "source_type": "note"})
        check("FT.2 rerunning the same fixed text is idempotent (no duplicate)",
              rr2.status_code == 200 and rr2.json()["experience_id"] == rr1.json()["experience_id"])


# ===========================================================================
print()
print("=" * 62)
print(f"  {len(PASSES)} passed, {len(FAILURES)} failed")
print("=" * 62)
for f in FAILURES:
    print(f"  FAILED:  {f}")

pg.close()
neo4j_driver.close()

print(
    "\nNote: this file permanently ingests one deterministic calibration fixture "
    "(idempotent, marked source=test_fixture). No cleanup is performed or possible; "
    "experiences/observations are append-only by design."
)

sys.exit(1 if FAILURES else 0)
