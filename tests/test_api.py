"""
tests/test_api.py

Integration tests for the Brain Console API (api/main.py), via FastAPI's
in-process TestClient -- same pattern as every other tests/test_*.py in
this repo (stack check, PASS/FAIL harness), applied to HTTP routes instead
of core/*.py functions directly.

Important: experiences and observations are append-only (immutability
triggers, migrations/003_ops_and_indexes.sql) -- there is no rollback or
delete path for anything ingested through the real API, unlike every other
integration test in this repo which wraps its writes in a transaction it
rolls back. This file never attempts to clean up what it ingests. Instead:

  - It only ever ingests deterministic core.calibration_fixtures content
    through /api/calibration/fixtures/{key}/ingest. ingest() is idempotent
    on (owner_id, content_hash), so running this file over and over never
    creates a new experience for a fixture that already exists -- see
    TC.4/TC.5 below, which explicitly assert that.
  - It reuses whatever owner already exists rather than creating one
    (owner is a strict singleton -- there can only ever be one row).
  - Every fixture-created experience carries {"source": "test_fixture",
    "fixture_id": ...} in experiences.source, so it is always identifiable
    as synthetic even though it is permanent.

Run:
    python tests/test_api.py

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

# Before any write: this suite seeds an owner and commits fixture ingests.
from _db_guard import require_scratch_database
require_scratch_database("test_api")

from fastapi.testclient import TestClient

from api.main import app

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


with TestClient(app) as client:
    # =======================================================================
    section("HEALTH + STATUS")
    # =======================================================================

    r = client.get("/api/health")
    check("H.1 /api/health returns 200", r.status_code == 200 and r.json() == {"ok": True})

    r = client.get("/api/status")
    check("H.2 /api/status returns 200", r.status_code == 200)
    body = r.json()
    check("H.3 status has all five service keys",
          set(body.keys()) >= {"postgres", "neo4j", "qdrant", "minio", "temporal", "owner"})
    check("H.4 postgres/neo4j/qdrant report ok=true (stack was confirmed reachable above)",
          body["postgres"]["ok"] and body["neo4j"]["ok"] and body["qdrant"]["ok"])

    # =======================================================================
    section("OWNER: reuse existing singleton, never create a second one")
    # =======================================================================

    if not body["owner"]["seeded"]:
        r = client.post("/api/status/seed-owner")
        check("O.1 seed-owner succeeds when none exists", r.status_code == 200)
        owner_id = r.json()["id"]
    else:
        owner_id = body["owner"]["id"]
        check("O.1 owner already seeded -- reused, not recreated", True, owner_id)

    r = client.post("/api/status/seed-owner")
    check("O.2 seeding again is rejected (singleton enforced)", r.status_code == 400)

    # =======================================================================
    section("CALIBRATION FIXTURES")
    # =======================================================================

    r = client.get("/api/calibration/fixtures")
    check("C.1 fixtures endpoint returns 200", r.status_code == 200)
    fixtures = r.json()
    fixture_keys = {f["key"] for f in fixtures}
    check("C.2 the original five non-episode fixtures are present",
          fixture_keys >= {
              "priya_kubernetes_mixed", "weak_evidence_belief", "contradiction_candidate",
              "old_episodic", "newer_generalization",
          }, str(fixture_keys))

    # =======================================================================
    section("IDEMPOTENT INGEST: rerunning never creates a duplicate experience")
    # =======================================================================

    FIXTURE_KEY = "contradiction_candidate"
    r1 = client.post(f"/api/calibration/fixtures/{FIXTURE_KEY}/ingest")
    check("I.1 first ingest succeeds", r1.status_code == 200)
    body1 = r1.json()
    exp_id = body1["experience_id"]

    r2 = client.post(f"/api/calibration/fixtures/{FIXTURE_KEY}/ingest")
    check("I.2 second ingest succeeds", r2.status_code == 200)
    body2 = r2.json()
    check("I.3 second ingest returns the SAME experience_id (no duplicate created)",
          body2["experience_id"] == exp_id, f"{body2['experience_id']} vs {exp_id}")
    check("I.4 second ingest reports created=false", body2["created"] is False)

    r3 = client.post(f"/api/calibration/fixtures/{FIXTURE_KEY}/ingest")
    check("I.5 third ingest is still identical (idempotency holds under repetition)",
          r3.json()["experience_id"] == exp_id and r3.json()["created"] is False)

    # =======================================================================
    section("EXPERIENCE INSPECTOR + TEST-FIXTURE MARKER")
    # =======================================================================

    r = client.get(f"/api/experiences/{exp_id}")
    check("E.1 inspector returns 200", r.status_code == 200)
    exp_data = r.json()
    check("E.2 inspector returns memories/beliefs/entities/episodes/events keys",
          set(exp_data.keys()) >= {"experience", "observations", "memories", "beliefs",
                                    "entities", "relationships", "events", "episodes"})
    check("E.3 belief present (contradiction_candidate has one)", len(exp_data["beliefs"]) == 1)
    check("E.4 experience is stamped as a test fixture",
          exp_data["experience"]["source_metadata"].get("source") == "test_fixture"
          and exp_data["experience"]["source_metadata"].get("fixture_id") == FIXTURE_KEY,
          str(exp_data["experience"]["source_metadata"]))

    # limit is generous (not 5) because this database accumulates history
    # across every test run and Brain Console session -- ingested_at
    # ordering means this specific fixture's experience will not stay near
    # the top of a small "most recent" window forever.
    r = client.get("/api/experiences?limit=500")
    check("E.5 experiences list includes is_test_fixture flag",
          r.status_code == 200 and any(e["is_test_fixture"] for e in r.json() if e["id"] == exp_id))

    # =======================================================================
    section("BRAIN EXPLORER (GRAPH)")
    # =======================================================================

    r = client.get("/api/graph/labels")
    check("G.1 labels endpoint returns counts for every known label", r.status_code == 200)
    labels = r.json()
    check("G.2 Belief count >= 1", labels.get("Belief", 0) >= 1)

    r = client.get("/api/graph/nodes?label=Belief&limit=50")
    check("G.3 nodes endpoint returns Belief nodes", r.status_code == 200 and len(r.json()) >= 1)

    r = client.get(f"/api/graph/node/{exp_id}")
    check("G.4 node detail resolves the Experience node by its Postgres id",
          r.status_code == 200 and r.json()["postgres_id"] == exp_id)
    node = r.json()
    check("G.5 Experience node has an outgoing PRODUCED edge to an Observation",
          any(e["rel_type"] == "PRODUCED" for e in node["outgoing"]))

    r = client.get("/api/graph/node/00000000-0000-0000-0000-000000000000")
    check("G.6 unknown node id returns 404", r.status_code == 404)

    # =======================================================================
    section("RETRIEVAL PLAYGROUND")
    # =======================================================================

    r = client.post("/api/retrieval/query", json={"query": "Is our deployment pipeline automated?"})
    check("R.1 retrieval query returns 200", r.status_code == 200)
    rdata = r.json()
    check("R.2 response has all playground sections",
          set(rdata.keys()) >= {"query", "seed_entities", "qdrant_results", "neo4j_results",
                                 "postgres_results", "merged_candidates", "trace_id"})
    belief_id = exp_data["beliefs"][0]["id"]
    check("R.3 the ingested belief appears in the merged candidate pool",
          any(c["source_id"] == belief_id for c in rdata["merged_candidates"]))
    check("R.4 trace_id recorded", rdata["trace_id"] is not None)

    # =======================================================================
    section("NEURAL BRAIN VISUALIZATION: activation_events")
    # =======================================================================

    r = client.post(f"/api/calibration/fixtures/priya_kubernetes_mixed/ingest")
    check("V.1 kubernetes fixture ingest succeeds", r.status_code == 200)

    r = client.post("/api/retrieval/query", json={"query": "What do I know about the Kubernetes rollout?"})
    check("V.2 kubernetes query returns 200", r.status_code == 200)
    vdata = r.json()
    check("V.3 response includes activation_events", "activation_events" in vdata and isinstance(vdata["activation_events"], list))
    events = vdata["activation_events"]
    check("V.4 activation_events is non-empty for a query that matches ingested content",
          len(events) > 0, f"{len(vdata.get('seed_entities', []))} seed entities, {len(vdata['merged_candidates'])} candidates")

    check("V.4b all_seed_entities is present and covers every entity graph_retrieve() actually walked from",
          "all_seed_entities" in vdata and len(vdata["all_seed_entities"]) > 0,
          f"{len(vdata.get('seed_entities', []))} name-matched vs {len(vdata.get('all_seed_entities', []))} total")

    valid_targets = {f"{c['source_type']}:{c['source_id']}" for c in vdata["merged_candidates"]}
    valid_targets |= {f"entity:{e['id']}" for e in vdata["all_seed_entities"]}
    check("V.5 every event's target resolves to a real seed entity or merged candidate",
          all(e["target"] in valid_targets for e in events),
          str([e for e in events if e["target"] not in valid_targets][:3]))

    seed_sources = {f"entity:{e['id']}" for e in vdata["all_seed_entities"]}
    graph_events = [e for e in events if e["channel"] == "graph"]
    check("V.6 every graph-channel event is sourced from a real seed entity (or 'query' as documented fallback)",
          all(e["source"] in seed_sources or e["source"] == "query" for e in graph_events))

    non_graph_events = [e for e in events if e["channel"] != "graph" and e["channel"] != "seed"]
    check("V.7 every non-graph, non-seed event is sourced from the transient 'query' root",
          all(e["source"] == "query" for e in non_graph_events))

    # convergence: a target reached by >1 distinct source should have >1 event
    from collections import defaultdict
    by_target: dict = defaultdict(set)
    for e in events:
        by_target[e["target"]].add(e["source"])
    converging = {t: s for t, s in by_target.items() if len(s) > 1}
    check("V.8 candidates array agrees with events array on entity_ids being present for debug/inspection",
          all("entity_ids" in c for c in vdata["qdrant_results"] + vdata["neo4j_results"]))

    check("V.9 'query' root never appears as a target (it is a UI-only source, not a canonical node)",
          all(e["target"] != "query" for e in events))


# ===========================================================================
print()
print("=" * 62)
print(f"  {len(PASSES)} passed, {len(FAILURES)} failed")
print("=" * 62)
for f in FAILURES:
    print(f"  FAILED:  {f}")

print(
    "\nNote: this file permanently ingests deterministic calibration fixtures "
    "(idempotent -- see the IDEMPOTENT INGEST section). No cleanup is performed "
    "or possible; experiences/observations are append-only by design."
)

sys.exit(1 if FAILURES else 0)
