"""
core/retrieval.py

Hybrid retrieval + deterministic activation. Combines Qdrant (semantic),
Neo4j (graph), and Postgres (temporal / current state / mental models &
cognitive patterns) into one candidate pool, then scores and bounds it with
the UNMODIFIED core/dynamics.py:activation_score()/rank_and_bound(). No new
ranking math is invented here -- this module only builds real Candidate
objects for functions that already existed.

Postgres remains canonical throughout: every RetrievalCandidate carries
source_type/source_id resolving back to a Postgres row, and nothing here
writes to memories/beliefs/entities. The only writes this module makes are
to activation_traces/activation_members (append-only, see
record_activation_trace()).

Five retrieval primitives, matching the `retrieval_channel` Postgres enum
('semantic', 'graph', 'temporal', 'state', 'situation') exactly -- that enum
already existed (migration 004) before this module did, which is a strong
signal this is the intended shape:

    semantic_retrieve()        -- Qdrant vector search              -> channel='semantic'
    graph_retrieve()           -- Neo4j structural traversal         -> channel='graph'
    temporal_retrieve()        -- Postgres recency ordering          -> channel='temporal'
    current_state_retrieve()   -- Postgres state_items (open goals)  -> channel='state'
    pattern_model_retrieve()   -- Postgres mental_models/patterns    -> channel='situation'

Why graph_retrieve is depth-1 only (INVOLVES/ABOUT), and does not walk
ASSOCIATED_WITH
-----------------------------------------------------------------------
graph/schema.cypher documents an "activation query" that spreads across
ASSOCIATED_WITH edges (the *learned* association graph, populated by
core/dynamics.py:reinforce() from association_events). Nothing writes
ASSOCIATED_WITH edges yet -- core/projector.py projects INVOLVES, ABOUT,
DERIVED_FROM, ASSERTED_BY, PART_OF, {SUPERSEDES,CONTRADICTS,SUPPORTS,
REFINES}, RELATED_TO, EXPERIENCED, PRODUCED, but the *learned* association
mechanism (the `associations` Postgres table, ASSOCIATED_WITH in Neo4j) is
explicitly deferred -- this session was told not to implement "learned
association discovery." So graph_retrieve here walks only the structural
edges that actually exist today: a seed Entity's directly connected
Memory/Belief nodes via INVOLVES/ABOUT (hops=1). RELATED_TO (entity-entity)
is also unpopulated in the current data (core/ingest.py never writes
`relationships` rows), so multi-hop entity expansion isn't possible on real
data yet either -- documented here rather than silently degraded.

Why "situation" retrieval doesn't do situation clustering
-----------------------------------------------------------
pattern_model_retrieve() uses the 'situation' channel because that's the
only retrieval_channel slot that fits mental_models/cognitive_patterns
(situation_strategies literally links situation_id -> {cognitive_pattern,
mental_model, belief}). It does NOT cluster or detect situations -- that
stays deferred, per CLAUDE.md and this session's instructions. It is a
plain keyword-overlap match between the query and each model's
trigger_condition/applies_when, labeled as exactly that, not a claim of
real situation-awareness.

Epistemic rule (same as core/indexer.py)
------------------------------------------
Retrieval and ranking never write to beliefs/memories. Nothing here merges,
strengthens, or weakens a belief because it scored high.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from core.dynamics import (
    Candidate,
    MemorySystem,
    activation_score,
    rank_and_bound,
    semantic_staleness,
)
from core.embedder import Embedder
from core.indexer import semantic_search
from vector.collections import COLLECTIONS

UTC = timezone.utc

# derived_object_type does not have a value for 'episode' or 'experience'
# (documents) -- migrations/001_foundation.sql. Candidates of these source
# types can be scored and returned, but cannot be persisted as an
# activation_members row. See record_activation_trace().
_ACTIVATION_MEMBER_TYPES = frozenset({
    "memory", "belief", "mental_model", "cognitive_pattern", "state_item",
})


# ---------------------------------------------------------------------------
# Candidate schema (Step 2)
# ---------------------------------------------------------------------------

@dataclass
class RetrievalCandidate:
    """
    One thing a retrieval primitive found, with enough metadata to explain
    itself and to be scored by core/dynamics.py. Never carries the object's
    full text as its primary payload -- callers resolve that from Postgres
    via source_type/source_id when they actually need to display it (see
    core/query_inspector.py). `content_preview` is a short label for human
    inspection only, not a substitute for the canonical row.
    """
    source_id: str
    source_type: str                    # memory | belief | mental_model | cognitive_pattern | state_item | episode | experience
    retrieval_channel: str              # semantic | graph | temporal | state | situation
    raw_score: float                    # the primitive's native score (cosine, hop-weight, recency-decay, ...)
    normalized_score: float             # raw_score rescaled to [0, 1] for cross-channel comparison
    reason: str                         # human-readable activation cause
    kind: Optional[str] = None          # sub-kind (memory.kind / belief.belief_kind / ...), used for staleness half-life
    content_preview: str = ""
    occurred_at: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    salience: Optional[float] = None
    confidence: Optional[float] = None
    epistemic_status: Optional[str] = None
    epistemic_owner: Optional[str] = None
    memory_system: Optional[str] = None
    is_contradiction: bool = False
    association_strength: Optional[float] = None   # graph channel only; hop-weight
    hops: Optional[int] = None                       # graph channel only
    entity_ids: list[str] = field(default_factory=list)
    evidence_refs: list[dict] = field(default_factory=list)   # [{source_type, source_id, stance, weight}]


# ---------------------------------------------------------------------------
# Shared Postgres row resolution -- used by every primitive so a candidate
# discovered anywhere carries the same shape of metadata.
# ---------------------------------------------------------------------------

def _evidence_refs(cur, derived_type: str, derived_id: str) -> list[dict]:
    cur.execute(
        "SELECT source_type, source_id, stance, weight FROM evidence "
        "WHERE derived_type = %s AND derived_id = %s",
        (derived_type, derived_id),
    )
    return [
        {"source_type": r[0], "source_id": str(r[1]), "stance": r[2], "weight": float(r[3])}
        for r in cur.fetchall()
    ]


def resolve_candidate_row(cur, source_type: str, source_id: str) -> Optional[dict]:
    """
    Fetch the canonical Postgres row for one candidate. Returns None if the
    row no longer exists (e.g. a stale Qdrant point after a Postgres
    rollback in tests) -- callers must skip candidates that resolve to None
    rather than fabricate metadata.
    """
    if source_type == "memory":
        cur.execute(
            "SELECT memory_system, kind, content, salience, confidence, "
            "epistemic_status, epistemic_owner, valid_from, valid_to "
            "FROM memories WHERE id = %s",
            (source_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("SELECT entity_id FROM memory_entities WHERE memory_id = %s", (source_id,))
        entity_ids = [str(r[0]) for r in cur.fetchall()]
        return {
            "content_preview": row[2], "kind": row[1], "memory_system": row[0], "salience": row[3],
            "confidence": row[4], "epistemic_status": row[5], "epistemic_owner": row[6],
            "occurred_at": row[7], "valid_to": row[8], "is_contradiction": False,
            "entity_ids": entity_ids,
            "evidence_refs": _evidence_refs(cur, "memory", source_id),
        }

    if source_type == "belief":
        cur.execute(
            "SELECT proposition, belief_kind, domain, status, confidence, "
            "epistemic_owner, held_since, about_entity_id "
            "FROM beliefs WHERE id = %s",
            (source_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        entity_ids = [str(row[7])] if row[7] else []
        return {
            "content_preview": row[0], "kind": row[1], "memory_system": None, "salience": None,
            "confidence": row[4], "epistemic_status": row[3], "epistemic_owner": row[5],
            "occurred_at": row[6], "valid_to": None,
            "is_contradiction": (row[3] == "contradicted"),
            "entity_ids": entity_ids,
            "evidence_refs": _evidence_refs(cur, "belief", source_id),
        }

    if source_type == "mental_model":
        cur.execute(
            "SELECT name, principle, domain, confidence FROM mental_models WHERE id = %s",
            (source_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "content_preview": f"{row[0]}: {row[1]}", "memory_system": None, "salience": None,
            "confidence": row[3], "epistemic_status": None, "epistemic_owner": None,
            "occurred_at": None, "valid_to": None, "is_contradiction": False,
            "entity_ids": [], "evidence_refs": [],
        }

    if source_type == "cognitive_pattern":
        cur.execute(
            "SELECT name, trigger_condition, epistemic_status, confidence "
            "FROM cognitive_patterns WHERE id = %s",
            (source_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "content_preview": f"{row[0]} (when {row[1]})", "memory_system": None,
            "salience": None, "confidence": row[3], "epistemic_status": row[2],
            "epistemic_owner": None, "occurred_at": None, "valid_to": None,
            "is_contradiction": (row[2] in ("contradicted", "retracted")),
            "entity_ids": [], "evidence_refs": [],
        }

    if source_type == "state_item":
        cur.execute(
            "SELECT kind, content, entity_id, priority, activation, closed_at "
            "FROM state_items WHERE id = %s",
            (source_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "content_preview": row[1], "kind": row[0], "memory_system": None,
            "salience": float(row[3]) * float(row[4]),   # priority * activation stands in for salience
            "confidence": None, "epistemic_status": ("closed" if row[5] else "open"),
            "epistemic_owner": None, "occurred_at": None, "valid_to": row[5],
            "is_contradiction": False,
            "entity_ids": [str(row[2])] if row[2] else [],
            "evidence_refs": [],
        }

    if source_type == "episode":
        cur.execute(
            "SELECT kind, title, salience, started_at FROM episodes WHERE id = %s",
            (source_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "content_preview": f"{row[0]}: {row[1]}", "kind": row[0], "memory_system": None,
            "salience": row[2], "confidence": None, "epistemic_status": None,
            "epistemic_owner": None, "occurred_at": row[3], "valid_to": None,
            "is_contradiction": False, "entity_ids": [], "evidence_refs": [],
        }

    if source_type == "experience":
        cur.execute("SELECT kind, occurred_at FROM experiences WHERE id = %s", (source_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "content_preview": f"[{row[0]} experience]", "kind": row[0], "memory_system": None,
            "salience": None, "confidence": None, "epistemic_status": None,
            "epistemic_owner": None, "occurred_at": row[1], "valid_to": None,
            "is_contradiction": False, "entity_ids": [], "evidence_refs": [],
        }

    return None


# ---------------------------------------------------------------------------
# 1. Semantic retrieval (Qdrant)
# ---------------------------------------------------------------------------

def semantic_retrieve(
    query: str,
    embedder: Embedder,
    qdrant_client,
    owner_id: str,
    conn,
    collections: Optional[list[str]] = None,
    limit: int = 20,
) -> list[RetrievalCandidate]:
    """Vector search across the requested Qdrant collections (default: all
    four). Wraps core.indexer.semantic_search() -- the same primitive the
    Qdrant validation pass used -- then resolves each hit's canonical
    Postgres row rather than returning the bare Qdrant hit.

    Passes include_stale=True: this module's downstream ranker
    (activation_score/rank_and_bound, called from merge_and_score() and
    hybrid_query()) already applies STALE_PENALTY and CONTRADICTION_BONUS,
    so contradicted/superseded candidates must reach it rather than being
    filtered out here first -- see core.indexer.semantic_search's own
    include_stale docstring."""
    hits = semantic_search(query, embedder, qdrant_client, owner_id,
                            collections=collections, limit=limit, include_stale=True)
    cur = conn.cursor()
    out: list[RetrievalCandidate] = []
    for h in hits:
        row = resolve_candidate_row(cur, h["source_type"], h["source_id"])
        if row is None:
            continue
        normalized = max(0.0, min(1.0, (h["score"] + 1.0) / 2.0))  # cosine in [-1,1] -> [0,1]
        rc = RetrievalCandidate(
            source_id=h["source_id"], source_type=h["source_type"],
            retrieval_channel="semantic", raw_score=h["score"],
            normalized_score=normalized,
            reason=f"semantic similarity to query (cosine={h['score']:.3f}, {h['collection']})",
        )
        for k, v in row.items():
            setattr(rc, k, v)
        out.append(rc)
    return out


# ---------------------------------------------------------------------------
# Entity seed resolution (Postgres) -- shared by graph_retrieve callers
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9']+")


def resolve_seed_entities(cur, owner_id: str, query: str, limit: int = 10) -> list[dict]:
    """
    Resolve entities mentioned in query text against entity_aliases /
    canonical_name, the same exact-match tiers core/ingest.py's entity
    resolution already uses (read-only here -- this never creates an
    entity). Trigram similarity is intentionally skipped for query-time
    seeding: a fuzzy match on a two-word query is more likely to pull in an
    unrelated entity than to help, unlike at ingest time where the
    surrounding sentence disambiguates it.
    """
    words = _WORD_RE.findall(query)
    candidates = set(words)
    # also try 2-word and 3-word windows, since canonical names are often multi-word
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            candidates.add(" ".join(words[i:i + n]))
    if not candidates:
        return []
    cur.execute(
        """
        SELECT DISTINCT e.id, e.canonical_name, e.kind, e.centrality
        FROM entities e
        JOIN entity_aliases ea ON ea.entity_id = e.id
        WHERE e.owner_id = %s AND e.merged_into_id IS NULL
          AND lower(ea.alias) = ANY(%s::text[])
        ORDER BY e.centrality DESC
        LIMIT %s
        """,
        (owner_id, [c.lower() for c in candidates], limit),
    )
    return [
        {"id": str(r[0]), "canonical_name": r[1], "kind": r[2], "centrality": r[3]}
        for r in cur.fetchall()
    ]


# ---------------------------------------------------------------------------
# 2. Graph retrieval (Neo4j)
# ---------------------------------------------------------------------------

_ROLE_WEIGHT = {"subject": 1.0, "object": 0.8, "context": 0.6}


def graph_retrieve(
    seed_entity_ids: list[str],
    neo4j_driver,
    conn,
    limit: int = 20,
) -> list[RetrievalCandidate]:
    """
    Depth-1 structural traversal from seed entities via INVOLVES (Memory)
    and ABOUT (Belief) -- the only entity-linking edges the projector
    currently populates. See the module docstring for why this does not
    walk ASSOCIATED_WITH or RELATED_TO.
    """
    if not seed_entity_ids:
        return []
    with neo4j_driver.session() as session:
        records = session.run(
            """
            MATCH (ent:Entity)<-[r:INVOLVES|ABOUT]-(n)
            WHERE ent.id IN $seed_ids
            RETURN n.id AS id, labels(n)[0] AS label, type(r) AS rel_type,
                   coalesce(r.role, '') AS role,
                   ent.id AS via_entity_id, ent.canonical_name AS via_entity_name
            LIMIT $limit
            """,
            seed_ids=seed_entity_ids, limit=limit,
        )
        rows = [dict(r) for r in records]

    cur = conn.cursor()
    label_to_source_type = {"Memory": "memory", "Belief": "belief"}
    out: list[RetrievalCandidate] = []
    seen: set[str] = set()
    for r in rows:
        source_type = label_to_source_type.get(r["label"])
        if source_type is None or r["id"] in seen:
            continue
        seen.add(r["id"])
        row = resolve_candidate_row(cur, source_type, r["id"])
        if row is None:
            continue
        weight = _ROLE_WEIGHT.get(r["role"], 0.9 if r["rel_type"] == "ABOUT" else 0.7)
        role_suffix = f" ({r['role']})" if r["role"] else ""
        reason = (
            f"connected to entity '{r['via_entity_name']}' via "
            f"{r['rel_type']}{role_suffix}"
        )
        rc = RetrievalCandidate(
            source_id=r["id"], source_type=source_type,
            retrieval_channel="graph", raw_score=weight, normalized_score=weight,
            reason=reason,
            association_strength=weight, hops=1,
        )
        for k, v in row.items():
            setattr(rc, k, v)
        out.append(rc)
    return out


# ---------------------------------------------------------------------------
# 3. Temporal retrieval (Postgres)
# ---------------------------------------------------------------------------

RECENCY_HALF_LIFE_DAYS = 90.0  # matches core/dynamics.py:RECENCY_HALF_LIFE_DAYS exactly


def temporal_retrieve(
    conn,
    owner_id: str,
    as_of: Optional[datetime] = None,
    limit: int = 20,
) -> list[RetrievalCandidate]:
    """
    Surfaces the most recently valid/occurred memories, ordered by the same
    recency-decay formula activation_score() applies internally -- so a
    candidate's temporal raw_score here is directly comparable to the
    recency term it will later receive during scoring. This is a discovery
    channel (broadens the pool via recency), not a duplicate of
    activation_score's recency weight, which applies to every candidate
    from every channel regardless of how it was found.
    """
    as_of = as_of or datetime.now(UTC)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, memory_system, kind, content, salience, confidence,
               epistemic_status, epistemic_owner, valid_from, valid_to
        FROM memories
        WHERE owner_id = %s
        ORDER BY valid_from DESC
        LIMIT %s
        """,
        (owner_id, limit),
    )
    rows = cur.fetchall()
    out: list[RetrievalCandidate] = []
    for row in rows:
        mem_id = str(row[0])
        occurred_at = row[8]
        days = max((as_of - occurred_at).total_seconds() / 86400.0, 0.0) if occurred_at else None
        raw = 0.5 ** (days / RECENCY_HALF_LIFE_DAYS) if days is not None else 0.0
        cur.execute("SELECT entity_id FROM memory_entities WHERE memory_id = %s", (mem_id,))
        entity_ids = [str(r[0]) for r in cur.fetchall()]
        rc = RetrievalCandidate(
            source_id=mem_id, source_type="memory", retrieval_channel="temporal",
            raw_score=raw, normalized_score=raw,
            reason=(f"occurred {days:.1f} day(s) ago" if days is not None else "no timestamp"),
            content_preview=row[3], memory_system=row[1], salience=row[4],
            confidence=row[5], epistemic_status=row[6], epistemic_owner=row[7],
            occurred_at=occurred_at, valid_to=row[9], entity_ids=entity_ids,
            evidence_refs=_evidence_refs(cur, "memory", mem_id),
        )
        out.append(rc)
    return out


# ---------------------------------------------------------------------------
# 4. Current cognitive-state retrieval (Postgres)
# ---------------------------------------------------------------------------

def current_state_retrieve(
    conn,
    owner_id: str,
    limit: int = 20,
) -> list[RetrievalCandidate]:
    """Open (closed_at IS NULL) state_items, ordered by priority*activation
    -- goals/projects/questions/uncertainties/attention/constraints the
    owner currently holds. These feed the goal_alignment channel for
    themselves directly (an open goal is maximally aligned with itself) and
    are also used to score OTHER candidates' goal_alignment via shared
    entity_id -- see build_dynamics_candidates()."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, kind, content, entity_id, priority, activation, opened_at
        FROM state_items
        WHERE owner_id = %s AND closed_at IS NULL
        ORDER BY (priority * activation) DESC
        LIMIT %s
        """,
        (owner_id, limit),
    )
    out: list[RetrievalCandidate] = []
    for row in cur.fetchall():
        score = float(row[4]) * float(row[5])
        out.append(RetrievalCandidate(
            source_id=str(row[0]), source_type="state_item", retrieval_channel="state",
            raw_score=score, normalized_score=score,
            reason=f"open {row[1]}: priority={row[4]:.2f}, activation={row[5]:.2f}",
            content_preview=row[2], salience=score, epistemic_status="open",
            occurred_at=row[6], entity_ids=[str(row[3])] if row[3] else [],
        ))
    return out


# ---------------------------------------------------------------------------
# 5. Cognitive-pattern / mental-model retrieval (Postgres, keyword match)
# ---------------------------------------------------------------------------

def pattern_model_retrieve(
    conn,
    owner_id: str,
    query: str,
    limit: int = 20,
) -> list[RetrievalCandidate]:
    """
    Plain keyword-overlap match between the query and each mental_model's
    principle/applies_when or cognitive_pattern's trigger_condition. This is
    NOT situation clustering (deferred) -- it is a structured lookup over
    two small tables, labeled with the 'situation' retrieval_channel
    because that is the enum slot that fits what these objects represent
    (situation_strategies links situation_id -> {cognitive_pattern,
    mental_model, belief}), not because real situation matching happens here.
    """
    query_terms = {w.lower() for w in _WORD_RE.findall(query) if len(w) > 2}
    if not query_terms:
        return []
    cur = conn.cursor()
    out: list[RetrievalCandidate] = []

    cur.execute(
        "SELECT id, name, principle, applies_when, domain, confidence "
        "FROM mental_models WHERE owner_id = %s",
        (owner_id,),
    )
    for row in cur.fetchall():
        haystack = f"{row[1]} {row[2]} {row[3] or ''} {row[4] or ''}".lower()
        matched = {t for t in query_terms if t in haystack}
        if not matched:
            continue
        score = len(matched) / len(query_terms)
        out.append(RetrievalCandidate(
            source_id=str(row[0]), source_type="mental_model", retrieval_channel="situation",
            raw_score=score, normalized_score=score,
            reason=f"matches terms {sorted(matched)} in principle/applies_when",
            content_preview=f"{row[1]}: {row[2]}", confidence=row[5],
        ))

    cur.execute(
        "SELECT id, name, trigger_condition, description, epistemic_status, confidence "
        "FROM cognitive_patterns WHERE owner_id = %s",
        (owner_id,),
    )
    for row in cur.fetchall():
        haystack = f"{row[1]} {row[2]} {row[3] or ''}".lower()
        matched = {t for t in query_terms if t in haystack}
        if not matched:
            continue
        score = len(matched) / len(query_terms)
        out.append(RetrievalCandidate(
            source_id=str(row[0]), source_type="cognitive_pattern", retrieval_channel="situation",
            raw_score=score, normalized_score=score,
            reason=f"matches terms {sorted(matched)} in trigger_condition",
            content_preview=f"{row[1]} (when {row[2]})", confidence=row[5],
            epistemic_status=row[4], is_contradiction=(row[4] in ("contradicted", "retracted")),
        ))

    out.sort(key=lambda rc: rc.normalized_score, reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Merge + score (Step 4: existing dynamics primitives against real candidates)
# ---------------------------------------------------------------------------

_MEMORY_SYSTEM_MAP = {
    "episodic": MemorySystem.EPISODIC,
    "semantic": MemorySystem.SEMANTIC,
    "procedural": MemorySystem.PROCEDURAL,
}


@dataclass
class MergedCandidate:
    """One (source_type, source_id) folded across every channel that found
    it. `discoveries` keeps full per-channel provenance for the activation
    trace and the inspection command; `dynamics_candidate` is what actually
    gets scored, built by core/dynamics.py's own math, unmodified."""
    ref: str
    source_type: str
    source_id: str
    discoveries: list[RetrievalCandidate]
    dynamics_candidate: Candidate


def _evidence_quality(cur, evidence_refs: list[dict], fallback: Optional[float]) -> float:
    if not evidence_refs:
        return fallback if fallback is not None else 0.5
    weights = []
    for ref in evidence_refs:
        if ref["source_type"] != "observation":
            continue
        cur.execute(
            "SELECT extraction_confidence, source_reliability FROM observations WHERE id = %s",
            (ref["source_id"],),
        )
        row = cur.fetchone()
        if row:
            weights.append(float(row[0]) * float(row[1]) * min(1.0, ref["weight"]))
    if not weights:
        return fallback if fallback is not None else 0.5
    return max(0.0, min(1.0, sum(weights) / len(weights)))


def _staleness(memory_system: Optional[str], kind: Optional[str],
               valid_from: Optional[datetime], as_of: datetime) -> float:
    # Same invariant as core/indexer.py's is_stale fix: episodic never gets
    # a staleness value derived from anything -- it stays 0.0, full stop.
    # Procedural degrades via proficiency (a mechanism Candidate does not
    # model at all), not via this staleness channel, so it is 0.0 too.
    if memory_system != "semantic" or valid_from is None:
        return 0.0
    return semantic_staleness(kind or "", valid_from, as_of)


def merge_and_score(
    conn,
    all_candidates: list[RetrievalCandidate],
    as_of: Optional[datetime] = None,
) -> list[tuple[MergedCandidate, float]]:
    """
    Group discoveries by (source_type, source_id), build one
    core.dynamics.Candidate per group, score it with the unmodified
    activation_score(), and return every (merged candidate, score) pair
    sorted by score descending -- the FULL pool, not bounded. Callers apply
    core.dynamics.rank_and_bound() separately (on the dynamics_candidate
    list) so both the complete scored pool and the bounded result are
    available for the activation trace.
    """
    as_of = as_of or datetime.now(UTC)
    cur = conn.cursor()

    groups: dict[str, list[RetrievalCandidate]] = {}
    for rc in all_candidates:
        ref = f"{rc.source_type}:{rc.source_id}"
        groups.setdefault(ref, []).append(rc)

    # Open state_items' entity_ids drive goal_alignment for every OTHER
    # candidate that shares one -- computed once up front.
    open_state_entities: set[str] = set()
    for rc in all_candidates:
        if rc.source_type == "state_item":
            open_state_entities.update(rc.entity_ids)

    merged: list[MergedCandidate] = []
    for ref, discoveries in groups.items():
        primary = max(discoveries, key=lambda d: d.normalized_score)
        source_type, source_id = primary.source_type, primary.source_id

        semantic_v = max((d.normalized_score for d in discoveries if d.retrieval_channel == "semantic"), default=0.0)
        association_v = max((d.normalized_score for d in discoveries if d.retrieval_channel == "graph"), default=0.0)
        situation_v = max((d.normalized_score for d in discoveries if d.retrieval_channel == "situation"), default=0.0)
        salience_v = max((d.salience for d in discoveries if d.salience is not None), default=0.0)

        entity_ids = {eid for d in discoveries for eid in d.entity_ids}
        if source_type == "state_item":
            goal_alignment_v = 1.0
        else:
            goal_alignment_v = 1.0 if open_state_entities.intersection(entity_ids) else 0.0

        evidence_refs = next((d.evidence_refs for d in discoveries if d.evidence_refs), [])
        confidence_fallback = next((d.confidence for d in discoveries if d.confidence is not None), None)
        evidence_quality_v = _evidence_quality(cur, evidence_refs, confidence_fallback)

        occurred_at = next((d.occurred_at for d in discoveries if d.occurred_at is not None), None)
        memory_system = next((d.memory_system for d in discoveries if d.memory_system is not None), None)
        kind = next((d.kind for d in discoveries if d.kind is not None), None)
        is_contradiction = any(d.is_contradiction for d in discoveries)
        staleness_v = _staleness(memory_system, kind, occurred_at, as_of)

        dyn = Candidate(
            ref=ref,
            channel=",".join(sorted({d.retrieval_channel for d in discoveries})),
            semantic=semantic_v, association=association_v, situation=situation_v,
            goal_alignment=goal_alignment_v, salience=salience_v,
            evidence_quality=evidence_quality_v, occurred_at=occurred_at,
            memory_system=_MEMORY_SYSTEM_MAP.get(memory_system),
            is_contradiction=is_contradiction, staleness=staleness_v,
        )
        merged.append(MergedCandidate(
            ref=ref, source_type=source_type, source_id=source_id,
            discoveries=discoveries, dynamics_candidate=dyn,
        ))

    scored = [(mc, activation_score(mc.dynamics_candidate, as_of)) for mc in merged]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Activation trace persistence (Step 3) -- append-only
# ---------------------------------------------------------------------------

def record_activation_trace(
    conn,
    owner_id: str,
    query_text: str,
    seed_entity_ids: list[str],
    scored: list[tuple[MergedCandidate, float]],
    kept_refs: set[str],
    latency_ms: Optional[int] = None,
    model_version_id: Optional[str] = None,
) -> str:
    """
    Writes one activation_traces row plus one activation_members row per
    (merged candidate, channel discovery) pair -- so "why was this
    activated" is answerable by reading every discovery that contributed to
    a candidate's final score, not just its winning channel. `scored` is
    the FULL pool from merge_and_score() (excluded candidates included);
    `kept_refs` is the set of refs core.dynamics.rank_and_bound() kept.

    Both tables are populated exactly as their own immutability trigger
    (activation_traces_immutable, migration 004) expects: insert-only,
    never updated afterward. This function itself never issues an UPDATE
    or DELETE against either table.

    Candidates whose source_type has no matching derived_object_type enum
    value (episode, experience/document -- see _ACTIVATION_MEMBER_TYPES)
    still count toward candidate_count/included_count and still appear in
    the returned ranking, but cannot be written as an activation_members
    row -- migrations/001_foundation.sql's derived_object_type enum simply
    has no slot for them. This is a real, pre-existing schema gap, not
    something silently patched around here.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO activation_traces
            (owner_id, query_text, seed_entity_ids, candidate_count,
             included_count, latency_ms, model_version_id)
        VALUES (%s, %s, %s::uuid[], %s, %s, %s, %s)
        RETURNING id
        """,
        (owner_id, query_text, seed_entity_ids, len(scored), len(kept_refs),
         latency_ms, model_version_id),
    )
    trace_id = str(cur.fetchone()[0])

    for rank, (mc, final_score) in enumerate(scored, start=1):
        if mc.source_type not in _ACTIVATION_MEMBER_TYPES:
            continue
        included = mc.ref in kept_refs
        for d in mc.discoveries:
            cur.execute(
                """
                INSERT INTO activation_members
                    (trace_id, object_type, object_id, channel, hops,
                     raw_score, final_score, rank, included, is_contradiction)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (trace_id, mc.source_type, mc.source_id, d.retrieval_channel,
                 d.hops, d.raw_score, final_score, rank, included,
                 mc.dynamics_candidate.is_contradiction),
            )
    return trace_id


# ---------------------------------------------------------------------------
# Step 5: hybrid query
# ---------------------------------------------------------------------------

@dataclass
class HybridQueryResult:
    query: str
    seed_entities: list[dict]
    raw_candidates: list[RetrievalCandidate]           # everything every primitive found, unmerged
    scored: list[tuple[MergedCandidate, float]]         # merged + scored, full pool, descending
    kept: list[tuple[Candidate, float]]                 # core.dynamics.rank_and_bound()'s own output
    trace_id: Optional[str]
    all_seed_ids: list[str]                             # seed_ids + semantic-expanded extras, exactly what graph_retrieve() walked from


def hybrid_query(
    query: str,
    conn,
    embedder: Embedder,
    qdrant_client,
    neo4j_driver,
    owner_id: str,
    as_of: Optional[datetime] = None,
    record_trace: bool = True,
    semantic_limit: int = 20,
    graph_limit: int = 20,
    temporal_limit: int = 10,
    state_limit: int = 10,
    pattern_limit: int = 10,
    max_items: int = 40,
    min_score: float = 0.12,
) -> HybridQueryResult:
    """
    The smallest function that actually combines Qdrant + Neo4j + Postgres
    into one candidate pool and ranks it with core.dynamics.rank_and_bound()
    -- unmodified. No LLM call anywhere in this path.

    Flow: semantic_retrieve() seeds entity ids (from its hits' entity_ids)
    in addition to resolve_seed_entities()'s direct name/alias match, both
    feed graph_retrieve(); temporal/state/pattern primitives run
    independently of the query's semantic content. Every RetrievalCandidate
    from every primitive is merged (merge_and_score()) and scored with the
    real activation_score()/rank_and_bound() from core/dynamics.py.
    """
    as_of = as_of or datetime.now(UTC)
    cur = conn.cursor()

    seed_entities = resolve_seed_entities(cur, owner_id, query)
    seed_ids = [e["id"] for e in seed_entities]

    semantic_hits = semantic_retrieve(query, embedder, qdrant_client, owner_id, conn,
                                       limit=semantic_limit)

    # Fold in entities mentioned by the strongest semantic hits as extra
    # graph seeds, so the graph channel can expand even when the query text
    # itself doesn't name an entity verbatim.
    extra_seed_ids = {eid for rc in semantic_hits[:5] for eid in rc.entity_ids}
    all_seed_ids = list(dict.fromkeys(seed_ids + list(extra_seed_ids)))

    graph_hits = graph_retrieve(all_seed_ids, neo4j_driver, conn, limit=graph_limit)
    temporal_hits = temporal_retrieve(conn, owner_id, as_of=as_of, limit=temporal_limit)
    state_hits = current_state_retrieve(conn, owner_id, limit=state_limit)
    pattern_hits = pattern_model_retrieve(conn, owner_id, query, limit=pattern_limit)

    raw_candidates = semantic_hits + graph_hits + temporal_hits + state_hits + pattern_hits

    scored = merge_and_score(conn, raw_candidates, as_of=as_of)
    kept = rank_and_bound(
        [mc.dynamics_candidate for mc, _ in scored],
        max_items=max_items, min_score=min_score, as_of=as_of,
    )
    kept_refs = {c.ref for c, _ in kept}

    trace_id = None
    if record_trace:
        trace_id = record_activation_trace(
            conn, owner_id, query, all_seed_ids, scored, kept_refs,
        )

    return HybridQueryResult(
        query=query, seed_entities=seed_entities, raw_candidates=raw_candidates,
        scored=scored, kept=kept, trace_id=trace_id, all_seed_ids=all_seed_ids,
    )
