"""
tests/test_projector.py

Integration tests for the Neo4j projection worker.

Requires the full stack running:
    docker compose up -d
    # migrations applied

Each test uses a real Postgres transaction (ROLLBACK at end) and real Neo4j
writes (explicit DETACH DELETE at end), so the stack is left clean.

Run:
    python tests/test_projector.py

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
# Stack connectivity check — fail fast before importing heavy modules
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
        from neo4j import GraphDatabase
        d = GraphDatabase.driver(
            os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
            auth=("neo4j", "brainbrain"),
        )
        d.verify_connectivity()
        d.close()
    except Exception as e:
        return False, f"Neo4j unreachable: {e}"
    return True, "ok"


ok, msg = _check_stack()
if not ok:
    print(f"SKIP  Stack not reachable: {msg}")
    print("      Start with: docker compose up -d")
    sys.exit(3)

import psycopg2
from neo4j import GraphDatabase

from core.extractor import ExtractionValidationError, validate_extraction_response
from core.projector import project_experience, ProjectionResult
from core.storage import MockStorageClient

# This suite inserts its own temporary owner and rolls the whole
# transaction back at the end -- it needs an EMPTY owner table. owner is a
# strict DB-enforced singleton (CLAUDE.md: "Single owner... There is no
# tenancy"), so once a real owner is seeded (e.g. by using the Brain
# Console, api/) there is no way to get a second, isolated owner for
# testing. Skip cleanly rather than reusing the real owner, which would
# make this suite's exclusive-ownership assumptions wrong.
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
PASSES:   list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSES.append(label)
        print(f"PASS  {label}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILURES.append(label)
        print(f"FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

PG_DSN  = os.environ.get("PG_DSN",  "postgresql://brain:brain@localhost:5432/brain")
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")

conn   = psycopg2.connect(PG_DSN)
conn.autocommit = False
driver = GraphDatabase.driver(NEO4J_URI, auth=("neo4j", "brainbrain"))

# ---------------------------------------------------------------------------
# Mock LLM: returns fixture 6 JSON
# ("Priya thinks our strategy is too broad.")
# Chosen because it has: Observation + attributed Belief + Entity(Priya)
# giving a complete node-relationship path to verify.
# ---------------------------------------------------------------------------

TEXT6 = "Priya thinks our strategy is too broad."
MOCK6_JSON = json.dumps({
    "observations": [{
        "content": TEXT6,
        "epistemic_status": "reported",
        "epistemic_owner": "other_person",
        "span": TEXT6[0:len(TEXT6)],
        "extraction_confidence": 0.9,
        "source_reliability": 1.0,
    }],
    "entities": [{
        "canonical_name": "Priya",
        "kind": "person",
        "mention_span": TEXT6[0:5],
    }],
    "memories": [{
        "memory_system": "episodic",
        "kind": "exchange",
        "content": "Priya raised a concern about our strategy.",
        "salience": 0.6,
        "emotion_is_stated": False,
        "emotional_valence": None,
        "emotional_intensity": None,
        "proficiency": None,
        "evidence_span": TEXT6[0:len(TEXT6)],
        "involved_entities": [{"canonical_name": "Priya", "role": "subject"}],
    }],
    "beliefs": [{
        "proposition": "Our strategy is too broad",
        "belief_kind": "attributed",
        "epistemic_owner": "other_person",
        "about_entity": "Priya",
        "domain": "strategy",
        "holding_confidence": 0.72,
        "truth_confidence": 0.25,
        "evidence_span": TEXT6[0:len(TEXT6)],
    }],
    "episode_candidate": None,
    "ambiguous": False,
    "ambiguity_note": "",
})


class MockLLMClient:
    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, system: str, user: str) -> str:
        return self._response


# ---------------------------------------------------------------------------
# Test state — populated during the run, used for cleanup
# ---------------------------------------------------------------------------

_experience_id: str = ""
_neo4j_ids: list[str] = []   # all Postgres UUIDs we projected, for cleanup


def _neo4j_cleanup(neo4j_ids: list[str]) -> None:
    """Delete projected nodes from Neo4j by their Postgres id property."""
    if not neo4j_ids:
        return
    with driver.session() as s:
        s.run(
            "MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
            ids=neo4j_ids,
        )


def _query_neo4j(cypher: str, **params) -> list[dict]:
    with driver.session() as s:
        result = s.run(cypher, **params)
        return [dict(r) for r in result]


# ===========================================================================
section("SETUP: ingest fixture 6 to Postgres (transaction, not committed)")
# ===========================================================================

try:
    from core.ingest import ingest

    cur = conn.cursor()

    # Insert test owner (singleton — must not exist yet)
    cur.execute(
        "INSERT INTO owner (display_name, timezone) VALUES (%s, %s) RETURNING id",
        ("Test Owner", "UTC"),
    )
    owner_id = cur.fetchone()[0]

    mock_llm     = MockLLMClient(MOCK6_JSON)
    mock_storage = MockStorageClient()

    result = ingest(
        TEXT6,
        "note",
        conn=conn,
        owner_id=owner_id,
        client=mock_llm,
        storage=mock_storage,
    )
    _experience_id = str(result.experience_id)

    check("SETUP.1 experience created", result.created)
    check("SETUP.2 1 observation", result.observation_count == 1)
    check("SETUP.3 1 entity (Priya)", result.entity_count == 1)
    check("SETUP.4 1 belief (attributed)", result.belief_count == 1)
    check("SETUP.5 content_uri is set", result.content_uri.startswith("minio://"))
    check("SETUP.6 1 memory (episodic/exchange)", result.memory_count == 1)
    check("SETUP.7 1 memory_entities link written",
          result.memory_entity_link_count == 1)

except Exception as e:
    print(f"FAIL  SETUP failed: {e}")
    FAILURES.append("SETUP")
    conn.rollback()
    driver.close()
    sys.exit(1)


# ===========================================================================
section("CANONICAL CHAIN (Postgres only, before any projection): "
        "Experience -> Observation -> Memory -> Entity")
# ===========================================================================

# This section proves the chain exists in Postgres — the source of truth —
# independent of Neo4j. It runs before project_experience() is ever called.

cur.execute("SELECT id FROM experiences WHERE id = %s", (_experience_id,))
exp_row = cur.fetchone()
check("PG.1 Experience row exists", exp_row is not None)

cur.execute(
    "SELECT id FROM observations WHERE experience_id = %s", (_experience_id,)
)
obs_rows = cur.fetchall()
check("PG.2 Observation row exists, linked to Experience", len(obs_rows) == 1)
_obs_id = obs_rows[0][0] if obs_rows else None

cur.execute(
    """
    SELECT m.id, m.content FROM memories m
    JOIN evidence ev ON ev.derived_type = 'memory' AND ev.derived_id = m.id
    WHERE ev.source_type = 'observation' AND ev.source_id = %s
    """,
    (_obs_id,),
)
mem_rows = cur.fetchall()
check("PG.3 Memory row exists, linked to Observation via evidence",
      len(mem_rows) == 1)
_memory_id = mem_rows[0][0] if mem_rows else None

cur.execute(
    "SELECT id FROM entities WHERE owner_id = %s AND canonical_name = 'Priya'",
    (owner_id,),
)
ent_rows = cur.fetchall()
check("PG.4 Entity(Priya) row exists", len(ent_rows) == 1)
_priya_entity_id = ent_rows[0][0] if ent_rows else None

cur.execute(
    "SELECT entity_id, role FROM memory_entities WHERE memory_id = %s",
    (_memory_id,),
)
me_rows = cur.fetchall()
check("PG.5 memory_entities row exists, linked to Memory", len(me_rows) == 1)
check("PG.6 memory_entities links to Entity(Priya)",
      bool(me_rows) and bool(ent_rows) and me_rows[0][0] == _priya_entity_id)
check("PG.7 memory_entities.role == 'subject'",
      bool(me_rows) and me_rows[0][1] == "subject")

check("PG.8 full canonical chain resolves end-to-end "
      "(Experience -> Observation -> Memory -> Entity)",
      exp_row is not None and len(obs_rows) == 1 and len(mem_rows) == 1
      and len(me_rows) == 1 and len(ent_rows) == 1)


# ===========================================================================
section("PROJECTION: project_experience first run")
# ===========================================================================

pr: ProjectionResult
try:
    pr = project_experience(_experience_id, conn, driver)
    check("P.1 projection returned a result", pr is not None)
    check("P.2 experience_id matches", pr.experience_id == _experience_id)
    check("P.3 nodes merged > 0", pr.nodes_merged > 0,
          f"{pr.nodes_merged} nodes")
    check("P.4 relationships merged > 0", pr.relationships_merged > 0,
          f"{pr.relationships_merged} rels")
except Exception as e:
    check("P.1 projection raised an exception", False, str(e))


# ===========================================================================
section("NEO4J CONTENT: verify nodes and relationships")
# ===========================================================================

exp_nodes = _query_neo4j("MATCH (n:Experience {id: $id}) RETURN n", id=_experience_id)
check("N.1 Experience node exists", len(exp_nodes) == 1)
if exp_nodes:
    n = exp_nodes[0]["n"]
    check("N.2 Experience.kind set", n.get("kind") == "note")
    check("N.3 Experience.content_uri set", bool(n.get("content_uri")))
    _neo4j_ids.append(n["id"])

obs_nodes = _query_neo4j(
    "MATCH (e:Experience {id: $exp_id})-[:PRODUCED]->(o:Observation) RETURN o",
    exp_id=_experience_id,
)
check("N.4 Observation node exists (via PRODUCED)", len(obs_nodes) == 1)
if obs_nodes:
    obs = obs_nodes[0]["o"]
    check("N.5 Observation.epistemic_owner set",
          obs.get("epistemic_owner") == "other_person")
    check("N.6 Observation.span_start is integer",
          isinstance(obs.get("span_start"), int))
    _neo4j_ids.append(obs["id"])

belief_nodes = _query_neo4j(
    """
    MATCH (e:Experience {id: $exp_id})-[:PRODUCED]->(o:Observation)
          <-[:DERIVED_FROM]-(b:Belief)
    RETURN b
    """,
    exp_id=_experience_id,
)
check("N.7 Belief node exists (via DERIVED_FROM chain)", len(belief_nodes) == 1)
if belief_nodes:
    bel = belief_nodes[0]["b"]
    check("N.8 Belief.belief_kind=attributed",
          bel.get("belief_kind") == "attributed")
    check("N.9 Belief.holding_confidence set",
          bel.get("holding_confidence") is not None)
    check("N.10 Belief.truth_confidence < Belief.holding_confidence",
          bel.get("truth_confidence", 1) < bel.get("holding_confidence", 0))
    _neo4j_ids.append(bel["id"])

entity_nodes = _query_neo4j(
    """
    MATCH (b:Belief {id: $bel_id})-[:ABOUT]->(ent:Entity)
    RETURN ent
    """,
    bel_id=(belief_nodes[0]["b"]["id"] if belief_nodes else ""),
)
check("N.11 Entity(Priya) node exists (via ABOUT)", len(entity_nodes) == 1)
if entity_nodes:
    ent = entity_nodes[0]["ent"]
    check("N.12 Entity.canonical_name=Priya",
          ent.get("canonical_name") == "Priya")
    check("N.13 Entity.kind=person", ent.get("kind") == "person")
    _neo4j_ids.append(ent["id"])

memory_nodes = _query_neo4j(
    """
    MATCH (e:Experience {id: $exp_id})-[:PRODUCED]->(o:Observation)
          <-[:DERIVED_FROM]-(m:Memory)
    RETURN m
    """,
    exp_id=_experience_id,
)
check("N.11a Memory node exists (via DERIVED_FROM chain)", len(memory_nodes) == 1)
if memory_nodes:
    mem = memory_nodes[0]["m"]
    check("N.11b Memory.memory_system=episodic",
          mem.get("memory_system") == "episodic")
    _neo4j_ids.append(mem["id"])

involves = _query_neo4j(
    """
    MATCH (m:Memory {id: $mem_id})-[r:INVOLVES]->(ent:Entity)
    RETURN r, ent
    """,
    mem_id=(memory_nodes[0]["m"]["id"] if memory_nodes else ""),
)
check("N.11c Memory -[:INVOLVES]-> Entity edge exists", len(involves) == 1)
if involves:
    check("N.11d INVOLVES target is Entity(Priya)",
          involves[0]["ent"].get("canonical_name") == "Priya")
    check("N.11e INVOLVES.role == 'subject'",
          involves[0]["r"].get("role") == "subject")

owner_nodes = _query_neo4j(
    "MATCH (o:Owner)-[:EXPERIENCED]->(e:Experience {id: $id}) RETURN o",
    id=_experience_id,
)
check("N.14 Owner node linked via EXPERIENCED", len(owner_nodes) == 1)
if owner_nodes:
    _neo4j_ids.append(owner_nodes[0]["o"]["id"])

# Full path: Owner -> Experience -> Observation -> Belief -> Entity
full_path = _query_neo4j(
    """
    MATCH path = (ow:Owner)-[:EXPERIENCED]->(ex:Experience {id: $exp_id})
                 -[:PRODUCED]->(ob:Observation)
                 <-[:DERIVED_FROM]-(bl:Belief)
                 -[:ABOUT]->(en:Entity)
    RETURN length(path) AS hops, en.canonical_name AS entity_name
    """,
    exp_id=_experience_id,
)
check("N.15 complete 4-hop path Owner->Experience->Observation->Belief->Entity exists",
      len(full_path) == 1)
if full_path:
    check("N.16 path length = 4 hops", full_path[0]["hops"] == 4)
    check("N.17 terminal entity = Priya",
          full_path[0]["entity_name"] == "Priya")

# Provenance: every projected node has an id that traces back to Postgres
prov_query = _query_neo4j(
    "MATCH (n) WHERE n.id IN $ids RETURN n.id AS pg_id, labels(n) AS lbls",
    ids=_neo4j_ids,
)
check("N.18 all projected node IDs are non-null Postgres UUIDs",
      all(r["pg_id"] for r in prov_query))
check("N.19 projected node count matches expected",
      len(prov_query) == len(set(_neo4j_ids)),
      f"{len(prov_query)} found, {len(set(_neo4j_ids))} expected")


# ===========================================================================
section("IDEMPOTENCY: project_experience second run")
# ===========================================================================

# Collect counts before second run
counts_before = {
    "exp":  len(_query_neo4j("MATCH (n:Experience {id: $id}) RETURN n", id=_experience_id)),
    "obs":  len(_query_neo4j("MATCH ()-[:PRODUCED]->(o:Observation) RETURN o")),
    "bel":  len(_query_neo4j("MATCH (n:Belief) WHERE n.id IN $ids RETURN n",
                              ids=[b["b"]["id"] for b in belief_nodes] if belief_nodes else [])),
    "ent":  len(_query_neo4j("MATCH (n:Entity) WHERE n.id IN $ids RETURN n",
                              ids=[e["ent"]["id"] for e in entity_nodes] if entity_nodes else [])),
    "mem":  len(_query_neo4j("MATCH (n:Memory) WHERE n.id IN $ids RETURN n",
                              ids=[m["m"]["id"] for m in memory_nodes] if memory_nodes else [])),
    "involves": _query_neo4j(
        "MATCH (:Memory)-[r:INVOLVES]->(:Entity) RETURN count(r) AS n")[0]["n"],
}

pr2 = project_experience(_experience_id, conn, driver)

counts_after = {
    "exp":  len(_query_neo4j("MATCH (n:Experience {id: $id}) RETURN n", id=_experience_id)),
    "obs":  len(_query_neo4j("MATCH ()-[:PRODUCED]->(o:Observation) RETURN o")),
    "bel":  len(_query_neo4j("MATCH (n:Belief) WHERE n.id IN $ids RETURN n",
                              ids=[b["b"]["id"] for b in belief_nodes] if belief_nodes else [])),
    "ent":  len(_query_neo4j("MATCH (n:Entity) WHERE n.id IN $ids RETURN n",
                              ids=[e["ent"]["id"] for e in entity_nodes] if entity_nodes else [])),
    "mem":  len(_query_neo4j("MATCH (n:Memory) WHERE n.id IN $ids RETURN n",
                              ids=[m["m"]["id"] for m in memory_nodes] if memory_nodes else [])),
    "involves": _query_neo4j(
        "MATCH (:Memory)-[r:INVOLVES]->(:Entity) RETURN count(r) AS n")[0]["n"],
}

check("I.1 Experience count unchanged after re-projection",
      counts_before["exp"] == counts_after["exp"],
      f"{counts_before['exp']} -> {counts_after['exp']}")
check("I.2 Observation count unchanged",
      counts_before["obs"] == counts_after["obs"])
check("I.3 Belief count unchanged",
      counts_before["bel"] == counts_after["bel"])
check("I.4 Entity count unchanged",
      counts_before["ent"] == counts_after["ent"])
check("I.5 second projection ops_count matches first",
      pr.nodes_merged == pr2.nodes_merged,
      f"{pr.nodes_merged} == {pr2.nodes_merged}")
check("I.7 Memory count unchanged after re-projection",
      counts_before["mem"] == counts_after["mem"])
check("I.8 INVOLVES relationship count unchanged after re-projection",
      counts_before["involves"] == counts_after["involves"],
      f"{counts_before['involves']} -> {counts_after['involves']}")

# Relationship idempotency: count all rels on the experience subgraph
rels_before = _query_neo4j(
    "MATCH ()-[r]->() WHERE r.source_id IS NOT NULL OR true RETURN count(r) AS n"
)

pr3 = project_experience(_experience_id, conn, driver)
rels_after = _query_neo4j(
    "MATCH ()-[r]->() WHERE r.source_id IS NOT NULL OR true RETURN count(r) AS n"
)
check("I.6 global relationship count unchanged after third projection",
      rels_before[0]["n"] == rels_after[0]["n"],
      f"{rels_before[0]['n']} -> {rels_after[0]['n']}")


# ===========================================================================
section("CLEANUP")
# ===========================================================================

_neo4j_cleanup(_neo4j_ids)

# Verify cleanup before closing the driver
remaining = len(_query_neo4j(
    "MATCH (n) WHERE n.id IN $ids RETURN n", ids=_neo4j_ids
)) if _neo4j_ids else 0
check("C.1 Neo4j nodes removed after cleanup", remaining == 0,
      f"{remaining} nodes still present")

conn.rollback()   # removes owner + experience + all derived data from Postgres
conn.close()
driver.close()

print("\nCleanup: Neo4j nodes deleted, Postgres transaction rolled back.")


# ===========================================================================
print()
print("=" * 62)
print(f"  {len(PASSES)} passed, {len(FAILURES)} failed")
print("=" * 62)
for f in FAILURES:
    print(f"  FAILED:  {f}")
sys.exit(1 if FAILURES else 0)
