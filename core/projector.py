"""
core/projector.py

Postgres -> Neo4j projection worker.

Takes an already-persisted experience and projects it with all canonical
derived objects into the graph. Neo4j is a rebuildable projection of
Postgres; it is never the source of truth.

Idempotency guarantee
---------------------
Every node write uses MERGE keyed on the Postgres UUID (`id` property).
Every relationship write uses MATCH on both endpoints + MERGE on the
relationship type. Re-running for the same experience_id produces
identical graph state regardless of how many times it is called.

Node types projected
--------------------
Owner, Experience, Observation, Memory, Belief, Entity, Episode

Relationship types projected
----------------------------
EXPERIENCED     (Owner)       -> (Experience)
PRODUCED        (Experience)  -> (Observation)
ASSERTED_BY     (Observation) -> (Entity)        asserted_by_entity_id
DERIVED_FROM    (Memory)      -> (Observation)   via evidence
DERIVED_FROM    (Belief)      -> (Observation)   via evidence
INVOLVES        (Memory)      -> (Entity)         via memory_entities
ABOUT           (Belief)      -> (Entity)         belief.about_entity_id
PART_OF         (Experience)  -> (Episode)        via episode_experiences
SUPERSEDES      (Belief)      -> (Belief)         belief_relations
CONTRADICTS     (Belief)      -> (Belief)         belief_relations
SUPPORTS        (Belief)      -> (Belief)         belief_relations
REFINES         (Belief)      -> (Belief)         belief_relations
RELATED_TO      (Entity)      -> (Entity)         relationships table

Entry point
-----------
    project_experience(experience_id, conn, driver) -> ProjectionResult
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


UTC = timezone.utc


@dataclass
class ProjectionResult:
    experience_id: str
    nodes_merged: int = 0
    relationships_merged: int = 0


# ---------------------------------------------------------------------------
# Postgres fetch
# ---------------------------------------------------------------------------

def _fetch(cur, experience_id: str) -> dict[str, Any]:
    """Read all canonical data for one experience from Postgres."""

    # Owner (singleton)
    cur.execute(
        "SELECT id, display_name, timezone, created_at FROM owner LIMIT 1"
    )
    owner = cur.fetchone()
    if owner is None:
        raise ValueError("No owner row found — seed the owner before projecting.")

    # Experience
    cur.execute(
        """
        SELECT id, owner_id, kind, summary, content_uri, content_hash,
               occurred_at, ingested_at
        FROM experiences WHERE id = %s
        """,
        (experience_id,),
    )
    exp = cur.fetchone()
    if exp is None:
        raise ValueError(f"Experience {experience_id} not found in Postgres.")

    # Observations
    cur.execute(
        """
        SELECT id, content, epistemic_status, epistemic_owner,
               extraction_confidence, source_reliability, span, observed_at,
               asserted_by_entity_id
        FROM observations WHERE experience_id = %s
        """,
        (experience_id,),
    )
    observations = cur.fetchall()
    obs_ids = [str(r[0]) for r in observations]

    # Memories derived from this experience (via evidence spine)
    cur.execute(
        """
        SELECT DISTINCT m.id, m.memory_system, m.kind, m.content, m.salience,
               m.epistemic_status, m.epistemic_owner,
               m.emotion_is_stated, m.emotional_valence, m.emotional_intensity,
               m.proficiency, m.valid_from, m.reinterprets_id
        FROM memories m
        JOIN evidence ev
          ON ev.derived_type = 'memory' AND ev.derived_id = m.id
        WHERE (ev.source_type = 'observation' AND ev.source_id = ANY(%s::uuid[]))
           OR (ev.source_type = 'experience'  AND ev.source_id = %s)
        """,
        (obs_ids or ['00000000-0000-0000-0000-000000000000'],
         experience_id),
    )
    memories = cur.fetchall()
    mem_ids = [str(r[0]) for r in memories]

    # Beliefs derived from this experience (via evidence spine)
    cur.execute(
        """
        SELECT DISTINCT b.id, b.proposition, b.belief_kind, b.domain, b.status,
               b.epistemic_owner, b.log_odds, b.confidence,
               b.holding_log_odds, b.holding_confidence,
               b.about_entity_id, b.held_since
        FROM beliefs b
        JOIN evidence ev
          ON ev.derived_type = 'belief' AND ev.derived_id = b.id
        WHERE (ev.source_type = 'observation' AND ev.source_id = ANY(%s::uuid[]))
           OR (ev.source_type = 'experience'  AND ev.source_id = %s)
        """,
        (obs_ids or ['00000000-0000-0000-0000-000000000000'],
         experience_id),
    )
    beliefs = cur.fetchall()
    belief_ids = [str(r[0]) for r in beliefs]

    # Entities referenced by beliefs (about_entity_id) and observations (asserted_by)
    entity_id_set: set[str] = set()
    for b in beliefs:
        if b[10]:
            entity_id_set.add(str(b[10]))
    for obs in observations:
        if obs[8]:
            entity_id_set.add(str(obs[8]))

    # Entities referenced by memory_entities for these memories
    if mem_ids:
        cur.execute(
            """
            SELECT DISTINCT e.id, e.kind, e.canonical_name, e.description, e.centrality
            FROM entities e
            JOIN memory_entities me ON me.entity_id = e.id
            WHERE me.memory_id = ANY(%s::uuid[])
            """,
            (mem_ids,),
        )
        for row in cur.fetchall():
            entity_id_set.add(str(row[0]))

    entities: dict[str, tuple] = {}
    if entity_id_set:
        cur.execute(
            """
            SELECT id, kind, canonical_name, description, centrality
            FROM entities WHERE id = ANY(%s::uuid[])
            """,
            (list(entity_id_set),),
        )
        for row in cur.fetchall():
            entities[str(row[0])] = row

    # Evidence for each memory and belief
    evidence: dict[tuple, list] = {}
    all_derived = [(mid, 'memory') for mid in mem_ids] + \
                  [(bid, 'belief') for bid in belief_ids]
    for derived_id, derived_type in all_derived:
        cur.execute(
            """
            SELECT source_type, source_id, stance, weight
            FROM evidence
            WHERE derived_type = %s AND derived_id = %s
            """,
            (derived_type, derived_id),
        )
        evidence[(derived_id, derived_type)] = cur.fetchall()

    # memory_entities links
    mem_entity_links: list[tuple] = []
    if mem_ids:
        cur.execute(
            """
            SELECT memory_id, entity_id, role
            FROM memory_entities WHERE memory_id = ANY(%s::uuid[])
            """,
            (mem_ids,),
        )
        mem_entity_links = cur.fetchall()

    # belief_relations (where at least one belief is from this experience)
    belief_relations: list[tuple] = []
    if belief_ids:
        cur.execute(
            """
            SELECT source_belief_id, target_belief_id, relation, strength
            FROM belief_relations
            WHERE source_belief_id = ANY(%s::uuid[])
               OR target_belief_id = ANY(%s::uuid[])
            """,
            (belief_ids, belief_ids),
        )
        belief_relations = cur.fetchall()

    # Episodes linked to this experience
    cur.execute(
        """
        SELECT ep.id, ep.kind, ep.title, ep.salience, ep.started_at, ep.ended_at
        FROM episodes ep
        JOIN episode_experiences ee ON ee.episode_id = ep.id
        WHERE ee.experience_id = %s
        """,
        (experience_id,),
    )
    episodes = cur.fetchall()

    # Entity-entity relationships where both endpoints are in our entity set
    entity_relationships: list[tuple] = []
    if len(entity_id_set) >= 2:
        cur.execute(
            """
            SELECT r.id, r.source_entity_id, r.target_entity_id,
                   r.rel_type, r.confidence, r.epistemic_owner
            FROM relationships r
            WHERE r.source_entity_id = ANY(%s::uuid[])
              AND r.target_entity_id = ANY(%s::uuid[])
              AND r.valid_to IS NULL
            """,
            (list(entity_id_set), list(entity_id_set)),
        )
        entity_relationships = cur.fetchall()

    return {
        "owner": owner,
        "experience": exp,
        "observations": observations,
        "memories": memories,
        "beliefs": beliefs,
        "entities": entities,
        "evidence": evidence,
        "mem_entity_links": mem_entity_links,
        "belief_relations": belief_relations,
        "episodes": episodes,
        "entity_relationships": entity_relationships,
    }


# ---------------------------------------------------------------------------
# Cypher helpers
# ---------------------------------------------------------------------------

def _ts(dt: Optional[datetime]) -> Optional[str]:
    """Convert a datetime to an ISO string for storage as a Neo4j string property."""
    if dt is None:
        return None
    if hasattr(dt, 'isoformat'):
        return dt.isoformat(timespec="seconds")
    return str(dt)


def _merge_node(tx, label: str, props: dict[str, Any]) -> None:
    """MERGE a node by its `id` property, then set all other properties."""
    tx.run(
        f"MERGE (n:{label} {{id: $id}}) SET n += $props",
        id=props["id"],
        props=props,
    )


def _merge_rel(
    tx,
    from_label: str, from_id: str,
    to_label: str, to_id: str,
    rel_type: str,
    props: Optional[dict[str, Any]] = None,
) -> None:
    """
    MATCH both endpoints by id, MERGE the relationship.
    Uses a relationship key that includes the rel_type, so two nodes can have
    multiple differently-typed relationships between them.
    """
    tx.run(
        f"""
        MATCH (a:{from_label} {{id: $from_id}})
        MATCH (b:{to_label} {{id: $to_id}})
        MERGE (a)-[r:{rel_type}]->(b)
        SET r += $props
        """,
        from_id=from_id,
        to_id=to_id,
        props=props or {},
    )


# ---------------------------------------------------------------------------
# Graph write
# ---------------------------------------------------------------------------

def _write(tx, data: dict[str, Any]) -> int:
    """
    Execute all MERGE statements for one experience. Returns the number of
    node+relationship merges issued (useful for test assertions).
    """
    ops = 0
    owner = data["owner"]
    exp   = data["experience"]

    owner_id = str(owner[0])
    exp_id   = str(exp[0])

    # --- Owner ---------------------------------------------------------------
    _merge_node(tx, "Owner", {
        "id": owner_id,
        "display_name": owner[1],
        "timezone": owner[2],
        "created_at": _ts(owner[3]),
    })
    ops += 1

    # --- Experience ----------------------------------------------------------
    span = exp[6]  # occurred_at index
    _merge_node(tx, "Experience", {
        "id": exp_id,
        "kind": exp[2],
        "summary": exp[3],
        "content_uri": exp[4],
        "content_hash": exp[5],
        "occurred_at": _ts(exp[6]),
        "ingested_at": _ts(exp[7]),
    })
    ops += 1

    _merge_rel(tx, "Owner", owner_id, "Experience", exp_id, "EXPERIENCED")
    ops += 1

    # --- Observations --------------------------------------------------------
    obs_id_set: set[str] = set()
    for obs in data["observations"]:
        obs_id = str(obs[0])
        obs_id_set.add(obs_id)
        span_raw = obs[6]
        span_start = span_raw.get("start") if isinstance(span_raw, dict) else None
        span_end   = span_raw.get("end")   if isinstance(span_raw, dict) else None
        _merge_node(tx, "Observation", {
            "id": obs_id,
            "content": obs[1],
            "epistemic_status": obs[2],
            "epistemic_owner": obs[3],
            "extraction_confidence": obs[4],
            "source_reliability": obs[5],
            "span_start": span_start,
            "span_end": span_end,
            "observed_at": _ts(obs[7]),
        })
        ops += 1
        _merge_rel(tx, "Experience", exp_id, "Observation", obs_id, "PRODUCED")
        ops += 1

        if obs[8]:  # asserted_by_entity_id
            asserted_by = str(obs[8])
            if asserted_by in data["entities"]:
                _merge_rel(tx, "Observation", obs_id,
                           "Entity", asserted_by, "ASSERTED_BY",
                           {"epistemic_owner": obs[3]})
                ops += 1

    # --- Entities ------------------------------------------------------------
    for ent_id, ent in data["entities"].items():
        _merge_node(tx, "Entity", {
            "id": ent_id,
            "kind": ent[1],
            "canonical_name": ent[2],
            "description": ent[3],
            "centrality": ent[4],
        })
        ops += 1

    # --- Memories ------------------------------------------------------------
    for mem in data["memories"]:
        mem_id = str(mem[0])
        _merge_node(tx, "Memory", {
            "id": mem_id,
            "memory_system": mem[1],
            "kind": mem[2],
            "content": mem[3],
            "salience": mem[4],
            "epistemic_status": mem[5],
            "epistemic_owner": mem[6],
            "emotion_is_stated": mem[7],
            "emotional_valence": mem[8],
            "emotional_intensity": mem[9],
            "proficiency": mem[10],
            "valid_from": _ts(mem[11]),
        })
        ops += 1

        # DERIVED_FROM evidence edges
        for ev in data["evidence"].get((mem_id, "memory"), []):
            src_type, src_id, stance, weight = ev
            if src_type == "observation" and str(src_id) in obs_id_set:
                _merge_rel(
                    tx, "Memory", mem_id,
                    "Observation", str(src_id),
                    "DERIVED_FROM",
                    {"stance": stance, "weight": float(weight),
                     "epistemic_owner": mem[6]},
                )
                ops += 1

        # REINTERPRETS
        if mem[12]:
            _merge_rel(tx, "Memory", mem_id,
                       "Memory", str(mem[12]), "REINTERPRETS")
            ops += 1

    # memory_entities -> INVOLVES
    for link in data["mem_entity_links"]:
        mem_eid, ent_id, role = str(link[0]), str(link[1]), link[2]
        _merge_rel(tx, "Memory", mem_eid,
                   "Entity", ent_id, "INVOLVES",
                   {"role": role or ""})
        ops += 1

    # --- Beliefs -------------------------------------------------------------
    for bel in data["beliefs"]:
        bel_id = str(bel[0])
        _merge_node(tx, "Belief", {
            "id": bel_id,
            "proposition": bel[1],
            "belief_kind": bel[2],
            "domain": bel[3],
            "status": bel[4],
            "epistemic_owner": bel[5],
            "truth_log_odds": bel[6],
            "truth_confidence": bel[7],
            "holding_log_odds": bel[8],
            "holding_confidence": bel[9],
            "held_since": _ts(bel[11]),
        })
        ops += 1

        # DERIVED_FROM
        for ev in data["evidence"].get((bel_id, "belief"), []):
            src_type, src_id, stance, weight = ev
            if src_type == "observation" and str(src_id) in obs_id_set:
                _merge_rel(
                    tx, "Belief", bel_id,
                    "Observation", str(src_id),
                    "DERIVED_FROM",
                    {"stance": stance, "weight": float(weight),
                     "epistemic_owner": bel[5]},
                )
                ops += 1

        # ABOUT entity
        if bel[10] and str(bel[10]) in data["entities"]:
            _merge_rel(tx, "Belief", bel_id,
                       "Entity", str(bel[10]), "ABOUT",
                       {"belief_kind": bel[2]})
            ops += 1

    # belief_relations
    _BELIEF_REL_MAP = {
        "supersedes":  "SUPERSEDES",
        "contradicts": "CONTRADICTS",
        "supports":    "SUPPORTS",
        "refines":     "REFINES",
    }
    for br in data["belief_relations"]:
        src, tgt, relation, strength = (
            str(br[0]), str(br[1]), br[2], br[3]
        )
        neo_rel = _BELIEF_REL_MAP.get(relation)
        if neo_rel:
            _merge_rel(tx, "Belief", src,
                       "Belief", tgt, neo_rel,
                       {"strength": float(strength) if strength else 0.5})
            ops += 1

    # --- Episodes ------------------------------------------------------------
    for ep in data["episodes"]:
        ep_id = str(ep[0])
        _merge_node(tx, "Episode", {
            "id": ep_id,
            "kind": ep[1],
            "title": ep[2],
            "salience": ep[3],
            "started_at": _ts(ep[4]),
            "ended_at": _ts(ep[5]),
        })
        ops += 1
        _merge_rel(tx, "Experience", exp_id,
                   "Episode", ep_id, "PART_OF")
        ops += 1

    # --- Entity-to-entity relationships -------------------------------------
    for er in data["entity_relationships"]:
        _merge_rel(
            tx,
            "Entity", str(er[1]),
            "Entity", str(er[2]),
            "RELATED_TO",
            {
                "rel_type": er[3],
                "confidence": float(er[4]) if er[4] else 0.5,
                "epistemic_owner": er[5],
                "source_id": str(er[0]),
            },
        )
        ops += 1

    return ops


# ---------------------------------------------------------------------------
# Mark the neo4j_projection extraction run as succeeded
# ---------------------------------------------------------------------------

def _mark_projection_succeeded(conn, experience_id: str) -> None:
    """
    Find the pending neo4j_projection extraction_run for this experience
    and mark it succeeded. No-op if the run doesn't exist (e.g. test data
    inserted without going through ingest.py).
    """
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE extraction_runs
           SET status = 'succeeded', finished_at = now()
         WHERE experience_id = %s
           AND stage = 'neo4j_projection'
           AND status = 'pending'
        """,
        (experience_id,),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def project_experience(
    experience_id: str,
    conn,    # psycopg2 connection (read queries + mark run succeeded)
    driver,  # neo4j.GraphDatabase.driver
) -> ProjectionResult:
    """
    Project one experience and all derived canonical records to Neo4j.

    Parameters
    ----------
    experience_id : Postgres UUID of the experience
    conn          : open psycopg2 connection (caller manages commit)
    driver        : Neo4j driver from neo4j.GraphDatabase.driver(...)

    Returns a ProjectionResult with merge counts. Idempotent.
    """
    cur = conn.cursor()
    data = _fetch(cur, experience_id)

    with driver.session() as session:
        total_ops = session.execute_write(_write, data)

    _mark_projection_succeeded(conn, experience_id)

    return ProjectionResult(
        experience_id=experience_id,
        nodes_merged=_count_nodes(data),
        relationships_merged=total_ops - _count_nodes(data),
    )


def _count_nodes(data: dict) -> int:
    """Count distinct node merges in one projection."""
    return (
        1  # owner
        + 1  # experience
        + len(data["observations"])
        + len(data["entities"])
        + len(data["memories"])
        + len(data["beliefs"])
        + len(data["episodes"])
    )
