"""
core/indexer.py

Postgres -> Qdrant embedding/indexing worker. Mirrors core/projector.py:
read canonical rows from Postgres, transform into the text representations
defined in core/embedding_text.py, embed via an Embedder (core/embedder.py),
and upsert idempotently into the already-bootstrapped Qdrant collections
(vector/collections.py). Qdrant is a rebuildable projection, exactly like
Neo4j -- never the source of truth.

Idempotency and rebuild
------------------------
Every point's id is the canonical Postgres UUID of the object it represents
(memory id, belief id, episode id, mental_model id, cognitive_pattern id, or
experience id for a document). Re-indexing the same object overwrites the
same Qdrant point rather than duplicating it -- there is no separate
"upsert vs insert" branch to get wrong. Deleting every collection and
re-running index_pending() + index_mental_models_and_patterns() over
everything in Postgres reproduces the same logical index: same point ids,
same payloads, same text inputs to the embedder. The only thing that can
differ byte-for-byte on a rebuild is whatever floating-point non-determinism
the embedding provider itself introduces between calls -- not this module's
logic.

Projection bookkeeping
-----------------------
core/ingest.py already creates one `extraction_runs` row per experience with
stage='qdrant_embedding', status='pending'. This module drives that row
through running -> succeeded, or running -> failed with `error` set to a
"partial: N/M points written" note so a reader can tell a clean success from
a run that died partway through, without a new job_status enum value.
index_pending() re-picks-up both 'pending' and 'failed' rows, so reruns are
always safe -- and even reprocessing an already-'succeeded' experience is
harmless, because the Qdrant writes are idempotent regardless of what the
Postgres bookkeeping says.

mental_models and cognitive_patterns have no extraction_runs/experience
linkage in the current schema, because nothing in core/ingest.py produces
them yet -- they are outputs of consolidation, which is deferred by design
(see IMPLEMENTATION_STATUS.md). index_mental_models_and_patterns() is a
full-table sweep, not a pending-status queue, for exactly that reason: it is
still idempotent (same point ids), just not incremental. When consolidation
is implemented it should register model_versions/extraction_runs rows
(role='consolidation') the same way ingest.py does, and this function should
be replaced by a proper pending-status sweep at that point.

Epistemic rule
--------------
Embedding similarity is a RELEVANCE signal, never a TRUTH signal. This
module only ever writes to Qdrant. It never merges, updates, strengthens, or
weakens a belief, memory, or anything else in Postgres because two vectors
turned out to be close -- there is no code path here that writes back to
beliefs/belief_versions/memories at all.

is_stale is a retrieval-state marker, not a truth marker
-----------------------------------------------------------
The `is_stale` payload field (vector/collections.py:PAYLOAD_SCHEMA) means
"a Postgres-side signal exists that a future ranker should prefer a
different representation for this query." It never means "this fact might
not be true anymore." Reading it as a truth signal downstream (retrieval,
ranking, a UI badge) would be a bug in that code, not in what this field is
for.

Migration 005's authority on this (COMMENT ON CONSTRAINT
episodic_not_superseded, migrations/005_episodic_invariants.sql) is
explicit and absolute, and this module follows it to the letter:

    "The vector projection must never set is_stale = true for a point
    whose source memory is episodic."

So: is_stale is HARD-CODED FALSE for every episodic memory point, full
stop -- not derived from consolidated_into_id, not derived from anything.
An episodic memory that has been folded into a semantic generalization
(consolidated_into_id set) is still "fully valid and still retrievable on
its own" per migration 005's comment on that column; nothing about that
event should make it rank as if it were invalid. Consolidation-aware
retrieval preference (surfacing the generalization ahead of its episodic
sources) is a real future ranker concern, but it is NOT this field's job --
today it is simply not represented in Qdrant at all, which is the safe
default (an unconsolidated-looking episodic memory, never a falsely-stale
one). A prior version of this module violated this rule by deriving
episodic is_stale from consolidated_into_id; tests/test_indexer.py's
"IS_STALE INVARIANT" section exists specifically to catch a regression back
to that bug.

For semantic memories, beliefs, and cognitive_patterns, is_stale legitimately
tracks something truth-adjacent (semantic memories go stale and get
superseded by design; belief.status and cognitive_pattern.epistemic_status
directly encode contradiction/retraction) -- that is correct and intentional
for those object types, which migration 005's rule does not cover and which
were never claimed to be permanent.

Entry points
------------
    index_experience(experience_id, conn, embedder, qdrant_client, storage=None) -> IndexResult
    index_pending(conn, embedder, qdrant_client, storage=None, limit=None) -> list[IndexResult]
    index_mental_models_and_patterns(conn, embedder, qdrant_client) -> IndexResult
    semantic_search(query, embedder, qdrant_client, owner_id, ...) -> list[dict]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from qdrant_client import QdrantClient, models

from core.embedder import Embedder
from core.embedding_text import (
    belief_text,
    cognitive_pattern_text,
    document_text,
    episode_text,
    episodic_memory_text,
    mental_model_text,
    procedural_memory_text,
    semantic_memory_text,
)
from core.ingest import _get_or_create_model_version
from core.projector import _fetch as _fetch_projection_data
from core.storage import StorageClient

UTC = timezone.utc

# module-level cache: collection name -> configured Qdrant vector size.
# See _check_dimension. Cleared implicitly per process; each process re-reads
# the live collection config on first use, so a mid-run RECREATE is caught.
_KNOWN_DIMS: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class IndexResult:
    experience_id: Optional[str]
    points_written: int = 0
    collections_touched: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _ts(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return dt.isoformat(timespec="seconds")
    return str(dt)


def _check_dimension(qdrant_client: QdrantClient, collection: str, vector: list[float]) -> None:
    """
    Cross-check the embedder's actual output length against the live Qdrant
    collection's configured vector size, instead of trusting EMBEDDING_DIM
    and VOYAGE_MODEL to independently agree. Fails loudly and specifically
    rather than letting qdrant-client raise its own less actionable error.
    """
    dim = _KNOWN_DIMS.get(collection)
    if dim is None:
        info = qdrant_client.get_collection(collection)
        dim = info.config.params.vectors.size
        _KNOWN_DIMS[collection] = dim
    if len(vector) != dim:
        raise ValueError(
            f"Embedder produced a {len(vector)}-dim vector but Qdrant collection "
            f"{collection!r} was created with size={dim}. Fix EMBEDDING_DIM / "
            f"VOYAGE_MODEL in .env and recreate collections: "
            f"RECREATE=1 python -m vector.collections"
        )


def _get_or_create_embedding_model_version(cur, embedder: Embedder) -> str:
    return str(
        _get_or_create_model_version(cur, "embedding", embedder.provider, embedder.model_name)
    )


# ---------------------------------------------------------------------------
# Supplemental Postgres reads (columns core.projector._fetch does not select,
# because Neo4j doesn't need them, but staleness/episode text does)
# ---------------------------------------------------------------------------

def _fetch_memory_staleness_flags(cur, mem_ids: list[str]) -> dict[str, tuple]:
    if not mem_ids:
        return {}
    cur.execute(
        "SELECT id, valid_to, superseded_by, consolidated_into_id "
        "FROM memories WHERE id = ANY(%s::uuid[])",
        (mem_ids,),
    )
    return {str(r[0]): (r[1], r[2], r[3]) for r in cur.fetchall()}


def _fetch_episode_detail(cur, episode_ids: list[str]) -> dict[str, tuple]:
    if not episode_ids:
        return {}
    cur.execute(
        "SELECT id, kind, title, narrative, trigger, resolution "
        "FROM episodes WHERE id = ANY(%s::uuid[])",
        (episode_ids,),
    )
    return {str(r[0]): (r[1], r[2], r[3], r[4], r[5]) for r in cur.fetchall()}


def _empty_payload(source_type: str, source_id: str, owner_id: str, **overrides: Any) -> dict:
    payload = {
        "source_type": source_type,
        "source_id": source_id,
        "owner_id": owner_id,
        "kind": None,
        "epistemic_owner": None,
        "epistemic_status": None,
        "entity_ids": [],
        "domain": None,
        "salience": None,
        "confidence": None,
        "occurred_at": None,
        "valid_from": None,
        "valid_to": None,
        "is_stale": False,
    }
    payload.update(overrides)
    return payload


def _write_points(
    conn,
    qdrant_client: QdrantClient,
    embedder: Embedder,
    texts: list[str],
    meta: list[tuple[str, str, dict]],
    result: IndexResult,
) -> None:
    """Shared embed + upsert tail for both index_experience and the sweep."""
    if not texts:
        return
    cur = conn.cursor()
    mv_id = _get_or_create_embedding_model_version(cur, embedder)

    vectors = embedder.embed(texts, input_type="document")
    if len(vectors) != len(texts):
        raise ValueError(
            f"embedder returned {len(vectors)} vectors for {len(texts)} inputs"
        )

    by_collection: dict[str, list] = {}
    for (collection, point_id, payload), vector in zip(meta, vectors):
        _check_dimension(qdrant_client, collection, vector)
        payload = dict(payload)
        payload["model_version_id"] = mv_id
        by_collection.setdefault(collection, []).append(
            models.PointStruct(id=point_id, vector=vector, payload=payload)
        )

    written = 0
    for collection, points in by_collection.items():
        qdrant_client.upsert(collection_name=collection, points=points, wait=True)
        written += len(points)
        result.collections_touched[collection] = len(points)
        result.points_written = written


# ---------------------------------------------------------------------------
# extraction_runs bookkeeping (stage='qdrant_embedding')
# ---------------------------------------------------------------------------

def _start_run(conn, experience_id: str) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE extraction_runs
           SET status = 'running', started_at = now()
         WHERE experience_id = %s AND stage = 'qdrant_embedding'
           AND status IN ('pending', 'failed')
        """,
        (experience_id,),
    )


def _finish_run_succeeded(conn, experience_id: str) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE extraction_runs
           SET status = 'succeeded', finished_at = now(), error = NULL
         WHERE experience_id = %s AND stage = 'qdrant_embedding'
        """,
        (experience_id,),
    )


def _finish_run_failed(conn, experience_id: str, error: str, written: int, total: int) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE extraction_runs
           SET status = 'failed', finished_at = now(), error = %s
         WHERE experience_id = %s AND stage = 'qdrant_embedding'
        """,
        (f"partial: {written}/{total} points written before error: {error}", experience_id),
    )


# ---------------------------------------------------------------------------
# Public entry point: one experience
# ---------------------------------------------------------------------------

def index_experience(
    experience_id: str,
    conn,
    embedder: Embedder,
    qdrant_client: QdrantClient,
    storage: Optional[StorageClient] = None,
) -> IndexResult:
    """
    Embed and index every canonical object derived from one experience:
    its episodic/semantic/procedural memories, its beliefs, the episode(s)
    it belongs to, and -- if it is itself a raw document -- its own text.
    Drives that experience's stage='qdrant_embedding' extraction_runs row
    through running -> succeeded/failed.
    """
    cur = conn.cursor()
    data = _fetch_projection_data(cur, experience_id)

    owner_id = str(data["owner"][0])
    owner_name = data["owner"][1]
    entities = data["entities"]  # id(str) -> (id, kind, canonical_name, description, centrality)

    mem_ids = [str(m[0]) for m in data["memories"]]
    episode_ids = [str(e[0]) for e in data["episodes"]]
    mem_flags = _fetch_memory_staleness_flags(cur, mem_ids)
    episode_detail = _fetch_episode_detail(cur, episode_ids)

    mem_entity_names: dict[str, list[str]] = {}
    mem_entity_ids: dict[str, list[str]] = {}
    for mem_id, ent_id, _role in data["mem_entity_links"]:
        mem_id, ent_id = str(mem_id), str(ent_id)
        mem_entity_ids.setdefault(mem_id, []).append(ent_id)
        if ent_id in entities:
            mem_entity_names.setdefault(mem_id, []).append(entities[ent_id][2])

    texts: list[str] = []
    meta: list[tuple[str, str, dict]] = []

    # --- Memories --------------------------------------------------------
    for mem in data["memories"]:
        mem_id = str(mem[0])
        system, kind, content, salience = mem[1], mem[2], mem[3], mem[4]
        names = mem_entity_names.get(mem_id, [])

        if system == "episodic":
            text, collection = episodic_memory_text(kind, content, names), "episodic_memory"
        elif system == "semantic":
            text, collection = semantic_memory_text(kind, content, names), "semantic_memory"
        else:
            text, collection = procedural_memory_text(kind, content), "cognitive_memory"

        valid_to, superseded_by, consolidated_into_id = mem_flags.get(
            mem_id, (None, None, None)
        )
        if system == "episodic":
            # migrations/005_episodic_invariants.sql, COMMENT ON CONSTRAINT
            # episodic_not_superseded: "The vector projection must never set
            # is_stale = true for a point whose source memory is episodic."
            # Hard-coded False -- NOT derived from consolidated_into_id.
            # Consolidation does not invalidate the historical fact, and a
            # future ranker's own preference for the generalization is not
            # this field's job. See the module docstring for the full rule
            # and tests/test_indexer.py's "IS_STALE INVARIANT" section for
            # the regression test.
            is_stale = False
        else:
            # For system="semantic": legitimately reflects epistemic
            # staleness (semantic memories go stale/get superseded by
            # design -- see core/dynamics.py:semantic_staleness).
            # For system="procedural": these three columns aren't part of
            # that memory system's lifecycle, so this is always False.
            is_stale = bool(valid_to or superseded_by or consolidated_into_id)

        payload = _empty_payload(
            "memory", mem_id, owner_id,
            kind=kind,
            epistemic_owner=mem[6],
            epistemic_status=mem[5],
            entity_ids=mem_entity_ids.get(mem_id, []),
            salience=salience,
            occurred_at=_ts(mem[11]),
            valid_from=_ts(mem[11]),
            valid_to=_ts(valid_to),
            is_stale=is_stale,
        )
        texts.append(text)
        meta.append((collection, mem_id, payload))

    # --- Beliefs -----------------------------------------------------------
    for bel in data["beliefs"]:
        bel_id = str(bel[0])
        proposition, belief_kind, domain, status = bel[1], bel[2], bel[3], bel[4]
        epistemic_owner, confidence, about_id = bel[5], bel[7], bel[10]
        about_id = str(about_id) if about_id else None
        about_name = entities[about_id][2] if about_id and about_id in entities else None

        text = belief_text(belief_kind, proposition, epistemic_owner, about_name, owner_name)
        payload = _empty_payload(
            "belief", bel_id, owner_id,
            kind=belief_kind,
            epistemic_owner=epistemic_owner,
            entity_ids=[about_id] if about_id else [],
            domain=domain,
            confidence=confidence,
            occurred_at=_ts(bel[11]),
            is_stale=(status != "active"),
        )
        texts.append(text)
        meta.append(("semantic_memory", bel_id, payload))

    # --- Episodes ------------------------------------------------------------
    for ep in data["episodes"]:
        ep_id = str(ep[0])
        kind, title, narrative, trigger, resolution = episode_detail.get(
            ep_id, (ep[1], ep[2], None, None, None)
        )
        text = episode_text(kind, title, narrative, trigger, resolution)
        payload = _empty_payload(
            "episode", ep_id, owner_id,
            kind=kind,
            salience=ep[3],
            occurred_at=_ts(ep[4]),
        )
        texts.append(text)
        meta.append(("episodic_memory", ep_id, payload))

    # --- Document (raw source text) ------------------------------------------
    exp = data["experience"]
    if exp[2] == "document" and storage is not None and exp[4]:
        raw = storage.get_text(exp[4])
        payload = _empty_payload(
            "experience", str(exp[0]), owner_id,
            kind="document",
            occurred_at=_ts(exp[6]),
        )
        texts.append(document_text(raw))
        meta.append(("documents", str(exp[0]), payload))

    result = IndexResult(experience_id=str(experience_id))
    if not texts:
        _finish_run_succeeded(conn, experience_id)
        return result

    _start_run(conn, experience_id)
    try:
        _write_points(conn, qdrant_client, embedder, texts, meta, result)
    except Exception as e:
        _finish_run_failed(conn, experience_id, str(e), result.points_written, len(texts))
        raise
    _finish_run_succeeded(conn, experience_id)
    return result


# ---------------------------------------------------------------------------
# Public entry point: drive the pending queue
# ---------------------------------------------------------------------------

def index_pending(
    conn,
    embedder: Embedder,
    qdrant_client: QdrantClient,
    storage: Optional[StorageClient] = None,
    limit: Optional[int] = None,
) -> list[IndexResult]:
    """
    Process every experience whose qdrant_embedding extraction_run is
    'pending' or 'failed' (a retry). Stands in for the Temporal worker that
    does not exist yet (see IMPLEMENTATION_STATUS.md) -- invoked manually,
    same as core/projector.py's project_experience() is today.
    """
    cur = conn.cursor()
    q = """
        SELECT experience_id FROM extraction_runs
         WHERE stage = 'qdrant_embedding' AND status IN ('pending', 'failed')
         ORDER BY created_at
    """
    if limit is not None:
        cur.execute(q + " LIMIT %s", (limit,))
    else:
        cur.execute(q)
    ids = [r[0] for r in cur.fetchall()]
    return [
        index_experience(str(exp_id), conn, embedder, qdrant_client, storage)
        for exp_id in ids
    ]


# ---------------------------------------------------------------------------
# Public entry point: mental_models + cognitive_patterns (full sweep)
# ---------------------------------------------------------------------------

def index_mental_models_and_patterns(
    conn, embedder: Embedder, qdrant_client: QdrantClient
) -> IndexResult:
    """See the module docstring's "Projection bookkeeping" section for why
    this is a full sweep rather than a pending-status queue."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM owner LIMIT 1")
    owner_row = cur.fetchone()
    if owner_row is None:
        raise ValueError("No owner row found -- seed the owner before indexing.")
    owner_id = str(owner_row[0])

    cur.execute(
        "SELECT id, name, principle, applies_when, domain, confidence FROM mental_models"
    )
    mental_models = cur.fetchall()

    cur.execute(
        "SELECT id, name, trigger_condition, sequence, description, "
        "epistemic_status, confidence FROM cognitive_patterns"
    )
    patterns = cur.fetchall()

    texts: list[str] = []
    meta: list[tuple[str, str, dict]] = []

    for mm_id, name, principle, applies_when, domain, confidence in mental_models:
        texts.append(mental_model_text(name, principle, applies_when))
        meta.append((
            "semantic_memory", str(mm_id),
            _empty_payload("mental_model", str(mm_id), owner_id,
                           domain=domain, confidence=confidence),
        ))

    for cp_id, name, trigger_condition, sequence, description, epistemic_status, confidence in patterns:
        texts.append(cognitive_pattern_text(name, trigger_condition, sequence, description))
        meta.append((
            "cognitive_memory", str(cp_id),
            _empty_payload(
                "cognitive_pattern", str(cp_id), owner_id,
                epistemic_status=epistemic_status,
                confidence=confidence,
                is_stale=(epistemic_status in ("contradicted", "retracted")),
            ),
        ))

    result = IndexResult(experience_id=None)
    _write_points(conn, qdrant_client, embedder, texts, meta, result)
    return result


# ---------------------------------------------------------------------------
# Retrieval smoke test primitive -- proves the projection is queryable.
# NOT the retrieval engine: no activation scoring, no reranking, no graph
# traversal, no cross-collection fusion beyond a plain score sort. Only
# exists to verify index_experience()/index_mental_models_and_patterns()
# actually produced a searchable, resolvable index.
# ---------------------------------------------------------------------------

def semantic_search(
    query: str,
    embedder: Embedder,
    qdrant_client: QdrantClient,
    owner_id: str,
    collections: Optional[list[str]] = None,
    limit: int = 5,
    include_stale: bool = False,
) -> list[dict]:
    """
    Embed the query (input_type="query" -- asymmetric from indexing), search
    each requested collection (default: all four), merge, sort by score.
    Returns hits carrying source_type/source_id so the caller can resolve
    back to Postgres -- Qdrant never stores the embedded text itself, only
    the vector and this routing metadata, so there is nothing here to
    mistake for a second source of truth.

    include_stale: vector/collections.py:default_filter's own docstring is
    the authority here -- "Temporal questions pass include_stale=True and
    let the re-ranker apply the staleness penalty instead." A caller that
    itself re-ranks with core.dynamics.activation_score() (which already
    applies STALE_PENALTY and CONTRADICTION_BONUS -- see core/retrieval.py)
    MUST pass include_stale=True, or contradicted/superseded candidates are
    silently filtered out before the ranker -- built to be permissive --
    ever sees them, defeating rank_and_bound()'s contradiction reserve
    entirely upstream of where it's supposed to matter. This smoke-test
    primitive defaults to the exclusive filter since it has no downstream
    re-ranker of its own.
    """
    from vector.collections import COLLECTIONS, default_filter

    cols = collections or list(COLLECTIONS)
    [vector] = embedder.embed([query], input_type="query")

    hits: list[dict] = []
    for c in cols:
        response = qdrant_client.query_points(
            collection_name=c,
            query=vector,
            query_filter=default_filter(owner_id, include_stale=include_stale),
            limit=limit,
        )
        for point in response.points:
            hits.append({
                "collection": c,
                "score": point.score,
                "source_type": point.payload.get("source_type"),
                "source_id": point.payload.get("source_id"),
            })
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:limit]
