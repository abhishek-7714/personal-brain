"""
core/ingest.py

Ingestion orchestrator.

Flow:
    text → MinIO (raw artifact) → experience (immutable) → observations
         → entities → episode (+ episode_experiences, cognitive_traces,
           episode-scoped state_items, decision) → memories (+ action
           memory linked to episode) → memory_entities (INVOLVES links)
         → beliefs
         → extraction_run (observation, succeeded)
         → extraction_runs (qdrant_embedding, neo4j_projection, pending)
         → experience.created event

Episodes are optional: most experiences contain none, and ingest() creates
nothing episode-shaped unless core.extractor already decided one exists
(core.schema.ExtractionResult.episode_candidate is not None). See
core/classification.md §4 and §10 for what belongs on the episode itself
vs. cognitive_traces vs. episode-scoped state_items vs. a linked decision --
this module does not re-derive that judgment, only persists what the
extractor already decided.

Idempotency: (owner_id, content_hash) is UNIQUE on experiences. A second call
with the same text skips all work and returns the existing experience_id.
The MinIO PUT is keyed on content_hash, so re-uploading identical content
is also a no-op.

Usage:
    import psycopg2, os
    from core.extractor import AnthropicLLMClient
    from core.storage import from_env as storage_from_env
    from core.ingest import ingest

    conn = psycopg2.connect(os.environ["PG_DSN"])
    result = ingest(
        "I went to Japan in 2024.", "note",
        conn=conn, owner_id=<uuid>,
        client=AnthropicLLMClient(),
        storage=storage_from_env(),
    )
    conn.commit()
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from core.extractor import (
    AnthropicLLMClient,
    LLMClient,
    extract,
    prompt_hash,
)
from core.schema import (
    BeliefDraft,
    CognitiveTraceDraft,
    DecisionDraft,
    EntityDraft,
    EpisodeCandidateDraft,
    EpisodeStateItemDraft,
    ExtractionResult,
    MemoryDraft,
    ObservationDraft,
)
from core.storage import StorageClient

UTC = timezone.utc

_EXTRACTOR_MODEL = "claude-haiku-4-5-20251001"
_EXTRACTOR_PROVIDER = "anthropic"

# Stages created as `pending` on every new experience so that the projection
# workers can query `WHERE stage IN PROJECTION_STAGES AND status = 'pending'`
# to find unprocessed experiences. Model versions for these stages are
# registered as placeholders and updated when the workers are wired up.
PROJECTION_STAGES = ("qdrant_embedding", "neo4j_projection")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class IngestResult:
    experience_id: UUID
    content_uri: str                  # minio:// URI where the raw text lives
    created: bool                     # False if deduplicated
    observation_count: int = 0
    entity_count: int = 0
    memory_count: int = 0
    belief_count: int = 0
    memory_entity_link_count: int = 0
    episode_created: bool = False
    cognitive_trace_count: int = 0
    episode_state_item_count: int = 0
    decision_created: bool = False
    ambiguous: bool = False
    ambiguity_note: str = ""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _fetch_one(cur, sql: str, params: tuple) -> Optional[tuple]:
    cur.execute(sql, params)
    return cur.fetchone()


def _get_or_create_model_version(
    cur,
    role: str,
    provider: str,
    model_name: str,
    ph: Optional[str] = None,
) -> UUID:
    row = _fetch_one(
        cur,
        """
        SELECT id FROM model_versions
        WHERE role = %s AND provider = %s AND model_name = %s
          AND COALESCE(prompt_hash, '') = %s
        """,
        (role, provider, model_name, ph or ""),
    )
    if row:
        return row[0]
    cur.execute(
        """
        INSERT INTO model_versions (role, provider, model_name, prompt_hash, active)
        VALUES (%s, %s, %s, %s, true)
        RETURNING id
        """,
        (role, provider, model_name, ph),
    )
    return cur.fetchone()[0]


def _resolve_or_create_entity(cur, owner_id: UUID, draft: EntityDraft) -> UUID:
    """
    Three-pass resolution per classification.md §7:
      1. Exact alias match
      2. Exact canonical_name match
      3. Trigram similarity > 0.6 (persons excluded — never merge on name alone)
      4. Create new
    """
    row = _fetch_one(
        cur,
        """
        SELECT e.id FROM entities e
        JOIN entity_aliases ea ON ea.entity_id = e.id
        WHERE e.owner_id = %s AND e.merged_into_id IS NULL
          AND lower(ea.alias) = lower(%s)
        LIMIT 1
        """,
        (owner_id, draft.canonical_name),
    )
    if row:
        cur.execute("UPDATE entities SET last_seen_at = now() WHERE id = %s", (row[0],))
        return row[0]

    row = _fetch_one(
        cur,
        """
        SELECT id FROM entities
        WHERE owner_id = %s AND merged_into_id IS NULL
          AND lower(canonical_name) = lower(%s)
        LIMIT 1
        """,
        (owner_id, draft.canonical_name),
    )
    if row:
        cur.execute("UPDATE entities SET last_seen_at = now() WHERE id = %s", (row[0],))
        return row[0]

    if draft.kind != "person":
        row = _fetch_one(
            cur,
            """
            SELECT id FROM entities
            WHERE owner_id = %s AND merged_into_id IS NULL AND kind = %s
              AND similarity(canonical_name, %s) > 0.6
            ORDER BY similarity(canonical_name, %s) DESC
            LIMIT 1
            """,
            (owner_id, draft.kind, draft.canonical_name, draft.canonical_name),
        )
        if row:
            cur.execute("UPDATE entities SET last_seen_at = now() WHERE id = %s", (row[0],))
            return row[0]

    cur.execute(
        "INSERT INTO entities (owner_id, kind, canonical_name) VALUES (%s, %s, %s) RETURNING id",
        (owner_id, draft.kind, draft.canonical_name),
    )
    ent_id = cur.fetchone()[0]
    cur.execute(
        """
        INSERT INTO entity_aliases (entity_id, alias, source, confidence)
        VALUES (%s, %s, 'observed', 1.0)
        ON CONFLICT (entity_id, alias) DO NOTHING
        """,
        (ent_id, draft.canonical_name),
    )
    return ent_id


def _insert_observation(
    cur, experience_id: UUID, draft: ObservationDraft, run_id: UUID, occurred_at: datetime
) -> UUID:
    cur.execute(
        """
        INSERT INTO observations
            (experience_id, content, epistemic_status, epistemic_owner,
             extraction_confidence, source_reliability, span, extraction_run_id, observed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            experience_id, draft.content, draft.epistemic_status, draft.epistemic_owner,
            draft.extraction_confidence, draft.source_reliability,
            json.dumps({"start": draft.span.start, "end": draft.span.end}),
            run_id, occurred_at,
        ),
    )
    return cur.fetchone()[0]


def _insert_memory_entities(
    cur, memory_id: UUID, draft: MemoryDraft, entity_map: dict[str, UUID]
) -> int:
    """
    Link a memory to the entities it involves. entity_map is keyed by the
    exact canonical_name casing the extractor returned in entities[]; draft
    links are validated case-insensitively, so look up case-insensitively too.
    """
    count = 0
    for link in draft.involved_entities:
        entity_id = _resolve_entity_ref(entity_map, link.canonical_name)
        if entity_id is None:
            # Should not happen: validate_extraction_response() already
            # rejects involved_entities not present in entities[].
            continue
        cur.execute(
            """
            INSERT INTO memory_entities (memory_id, entity_id, role)
            VALUES (%s, %s, %s)
            ON CONFLICT (memory_id, entity_id) DO NOTHING
            """,
            (memory_id, entity_id, link.role),
        )
        count += 1
    return count


def _insert_evidence(
    cur, derived_type: str, derived_id: UUID,
    source_type: str, source_id: UUID,
    stance: str = "supports", weight: float = 1.0,
) -> None:
    cur.execute(
        """
        INSERT INTO evidence
            (derived_type, derived_id, source_type, source_id, stance, weight)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (derived_type, derived_id, source_type, source_id, stance) DO NOTHING
        """,
        (derived_type, derived_id, source_type, source_id, stance, weight),
    )


def _insert_episode(
    cur, owner_id: UUID, draft: EpisodeCandidateDraft, run_id: UUID, occurred_at: datetime
) -> UUID:
    """
    outcome/outcome_valence/outcome_recorded_at are only ever non-null here
    when the extractor found no decision for this episode -- core.extractor
    already enforces the mutual-exclusivity invariant (decision.outcome is
    canonical when a decision exists), so this function trusts draft.outcome
    as given rather than re-checking it.
    """
    outcome_recorded_at = occurred_at if draft.outcome is not None else None
    cur.execute(
        """
        INSERT INTO episodes
            (owner_id, kind, title, narrative, trigger, resolution, salience,
             outcome, outcome_valence, outcome_recorded_at,
             started_at, extraction_run_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            owner_id, draft.kind, draft.title, draft.narrative, draft.trigger,
            draft.resolution, draft.salience, draft.outcome, draft.outcome_valence,
            outcome_recorded_at, occurred_at, run_id,
        ),
    )
    return cur.fetchone()[0]


def _link_episode_experience(cur, episode_id: UUID, experience_id: UUID) -> None:
    cur.execute(
        """
        INSERT INTO episode_experiences (episode_id, experience_id)
        VALUES (%s, %s)
        ON CONFLICT DO NOTHING
        """,
        (episode_id, experience_id),
    )


def _insert_cognitive_trace(
    cur, episode_id: UUID, seq: int, draft: CognitiveTraceDraft,
    observation_id: UUID, run_id: UUID, occurred_at: datetime,
) -> UUID:
    """observation_id is the REAL observation row resolved from
    draft.observation_index by the caller -- see the "every trace step must
    cite an observation with a span" rule (classification.md §5). There is
    no path through this function that inserts a trace with no observation."""
    cur.execute(
        """
        INSERT INTO cognitive_traces
            (episode_id, seq, operation, target, detail, observation_id,
             confidence, occurred_at, extraction_run_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            episode_id, seq, draft.operation, draft.target, draft.detail,
            observation_id, draft.confidence, occurred_at, run_id,
        ),
    )
    return cur.fetchone()[0]


def _resolve_entity_ref(entity_map: dict[str, UUID], canonical_name: Optional[str]) -> Optional[UUID]:
    """
    Case-insensitive canonical_name lookup. entity_map is keyed by the exact
    casing the extractor returned in entities[]; every cross-reference in the
    extraction payload is validated case-insensitively (see
    core/extractor.py:_validate_entity_link and _validate_episode_state_item),
    so resolution has to be case-insensitive too or a validated reference can
    still silently resolve to NULL here.
    """
    if not canonical_name:
        return None
    lower_map = {name.lower(): eid for name, eid in entity_map.items()}
    return lower_map.get(canonical_name.lower())


def _insert_episode_state_item(
    cur, owner_id: UUID, episode_id: UUID, draft: EpisodeStateItemDraft,
    entity_map: dict[str, UUID], occurred_at: datetime,
) -> UUID:
    about_id = _resolve_entity_ref(entity_map, draft.about_entity)
    cur.execute(
        """
        INSERT INTO state_items
            (owner_id, kind, content, entity_id, priority, activation,
             episode_id, opened_at, last_touched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            owner_id, draft.kind, draft.content, about_id, draft.priority,
            draft.activation, episode_id, occurred_at, occurred_at,
        ),
    )
    return cur.fetchone()[0]


def _insert_decision(
    cur, owner_id: UUID, episode_id: UUID, draft: DecisionDraft, occurred_at: datetime
) -> UUID:
    options_json = json.dumps([
        {"label": o.label, "rationale": o.rationale, "risks": o.risks}
        for o in draft.options
    ])
    decided_at = occurred_at if draft.chosen_option else None
    outcome_recorded_at = occurred_at if draft.outcome is not None else None
    cur.execute(
        """
        INSERT INTO decisions
            (owner_id, episode_id, question, context, options, chosen_option,
             rationale, confidence_at_time, reversibility, decided_at,
             outcome, outcome_valence, outcome_recorded_at, reflection)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            owner_id, episode_id, draft.question, draft.context, options_json,
            draft.chosen_option, draft.rationale, draft.confidence_at_time,
            draft.reversibility, decided_at, draft.outcome, draft.outcome_valence,
            outcome_recorded_at, draft.reflection,
        ),
    )
    return cur.fetchone()[0]


def _insert_memory(
    cur, owner_id: UUID, draft: MemoryDraft, run_id: UUID, occurred_at: datetime
) -> UUID:
    cur.execute(
        """
        INSERT INTO memories
            (owner_id, memory_system, kind, content, epistemic_status, epistemic_owner,
             salience, emotion_is_stated, emotional_valence, emotional_intensity,
             proficiency, valid_from, extraction_run_id)
        VALUES (%s, %s, %s, %s, 'asserted', 'owner',
                %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            owner_id, draft.memory_system, draft.kind, draft.content,
            draft.salience, draft.emotion_is_stated,
            draft.emotional_valence, draft.emotional_intensity,
            draft.proficiency, occurred_at, run_id,
        ),
    )
    return cur.fetchone()[0]


def _insert_belief(
    cur, owner_id: UUID, draft: BeliefDraft, entity_map: dict[str, UUID]
) -> UUID:
    from core.dynamics import (
        BeliefKind, EpistemicOwner, EvidenceItem, Stance,
        log_odds_from_confidence, recompute_belief,
    )
    bk = BeliefKind(draft.belief_kind)
    eo = EpistemicOwner(draft.epistemic_owner)
    now = datetime.now(UTC)

    hold_lo = log_odds_from_confidence(draft.holding_confidence)
    truth_lo = log_odds_from_confidence(draft.truth_confidence)
    about_id = _resolve_entity_ref(entity_map, draft.about_entity)

    cur.execute(
        """
        INSERT INTO beliefs
            (owner_id, proposition, belief_kind, domain, epistemic_owner,
             log_odds, confidence, holding_log_odds, holding_confidence,
             about_entity_id, held_since, last_evidence_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
        RETURNING id
        """,
        (
            owner_id, draft.proposition, draft.belief_kind, draft.domain,
            draft.epistemic_owner,
            truth_lo, draft.truth_confidence,
            hold_lo, draft.holding_confidence,
            about_id,
        ),
    )
    belief_id = cur.fetchone()[0]

    cur.execute(
        """
        INSERT INTO belief_versions
            (belief_id, version, status, confidence, log_odds,
             holding_confidence, holding_log_odds, delta, axis, reason)
        VALUES (%s, 1, 'active', %s, %s, %s, %s, %s, 'both', 'initial extraction')
        """,
        (belief_id, draft.truth_confidence, truth_lo,
         draft.holding_confidence, hold_lo, truth_lo),
    )
    return belief_id


def _emit_event(
    cur, event_type: str, aggregate_type: str, aggregate_id: UUID,
    payload: dict[str, Any], idempotency_key: str,
) -> None:
    cur.execute(
        """
        INSERT INTO events
            (event_type, aggregate_type, aggregate_id, payload, idempotency_key)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (event_type, aggregate_type, aggregate_id,
         json.dumps(payload), idempotency_key),
    )


def _create_projection_runs(
    cur, experience_id: UUID, owner_id: UUID
) -> None:
    """
    Create pending extraction_run rows for each projection stage.

    Projection workers query:
        SELECT er.experience_id FROM extraction_runs er
        WHERE er.stage = 'qdrant_embedding' AND er.status = 'pending'

    Each stage gets a placeholder model_version row that the worker will
    replace with its real model details when it registers itself.
    """
    for stage in PROJECTION_STAGES:
        mv_id = _get_or_create_model_version(
            cur, stage, "unassigned", "unassigned"
        )
        cur.execute(
            """
            INSERT INTO extraction_runs
                (experience_id, model_version_id, stage, status)
            VALUES (%s, %s, %s, 'pending')
            """,
            (experience_id, mv_id, stage),
        )


# ---------------------------------------------------------------------------
# Public ingest function
# ---------------------------------------------------------------------------

def ingest(
    text: str,
    source_type: str,
    conn,
    owner_id: UUID,
    client: Optional[LLMClient] = None,
    storage: Optional[StorageClient] = None,
    occurred_at: Optional[datetime] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> IngestResult:
    """
    Ingest one text input into the personal brain.

    Parameters
    ----------
    text        : raw input, preserved verbatim in MinIO
    source_type : experience_kind enum value (note, document, conversation, …)
    conn        : open psycopg2 connection (caller commits/rolls back)
    owner_id    : must match the singleton owner row
    client      : LLMClient; defaults to AnthropicLLMClient()
    storage     : StorageClient for MinIO; defaults to from_env()
    occurred_at : timestamp of the event; defaults to now()
    metadata    : optional source metadata stored in experiences.source

    Returns IngestResult. Does NOT commit — caller controls the transaction.
    """
    if client is None:
        client = AnthropicLLMClient()
    if storage is None:
        from core.storage import from_env
        storage = from_env()
    if occurred_at is None:
        occurred_at = datetime.now(UTC)

    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cur = conn.cursor()

    # --- Idempotency ---------------------------------------------------------
    row = _fetch_one(
        cur,
        "SELECT id, content_uri FROM experiences WHERE owner_id = %s AND content_hash = %s",
        (owner_id, content_hash),
    )
    if row:
        return IngestResult(
            experience_id=row[0],
            content_uri=row[1] or "",
            created=False,
        )

    # --- 1. Store raw artifact in MinIO first --------------------------------
    # Content is written before the DB row exists. If the DB insert fails
    # and the transaction is rolled back, the MinIO object is an orphan —
    # acceptable because it is keyed on content_hash and can be GC'd later.
    content_uri = storage.put_experience(str(owner_id), content_hash, text)

    # --- 2. Experience -------------------------------------------------------
    source_blob: dict[str, Any] = dict(metadata or {})
    source_blob["source_type"] = source_type

    cur.execute(
        """
        INSERT INTO experiences
            (owner_id, kind, content_hash, content_uri, occurred_at, source)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (owner_id, source_type, content_hash, content_uri, occurred_at,
         json.dumps(source_blob)),
    )
    experience_id: UUID = cur.fetchone()[0]

    # --- 3. Model version + observation extraction run -----------------------
    mv_id = _get_or_create_model_version(
        cur, "extraction", _EXTRACTOR_PROVIDER, _EXTRACTOR_MODEL, prompt_hash()
    )
    cur.execute(
        """
        INSERT INTO extraction_runs
            (experience_id, model_version_id, stage, status, started_at)
        VALUES (%s, %s, 'observation', 'running', now())
        RETURNING id
        """,
        (experience_id, mv_id),
    )
    run_id: UUID = cur.fetchone()[0]

    # --- 4. Extract ----------------------------------------------------------
    result: ExtractionResult = extract(text, client)

    # --- 5. Entities ---------------------------------------------------------
    entity_map: dict[str, UUID] = {}
    for ent_draft in result.entities:
        entity_map[ent_draft.canonical_name] = _resolve_or_create_entity(
            cur, owner_id, ent_draft
        )

    # --- 6. Observations -----------------------------------------------------
    obs_ids: list[UUID] = []
    for obs_draft in result.observations:
        obs_ids.append(
            _insert_observation(cur, experience_id, obs_draft, run_id, occurred_at)
        )
    primary_obs_id = obs_ids[0] if obs_ids else None

    # --- 7. Episode (optional -- most experiences have none) -----------------
    # core.extractor already decided whether this experience contains a
    # cognitive episode worth representing (classification.md §4); this
    # module persists that decision, it does not re-derive it. Nothing here
    # ever creates an episode/trace/state_item/decision when
    # result.episode_candidate is None -- validate_extraction_response()
    # already guarantees traces/state_items/decision are all empty in that
    # case, so this block simply has nothing to do.
    episode_id: Optional[UUID] = None
    if result.episode_candidate is not None:
        episode_id = _insert_episode(cur, owner_id, result.episode_candidate, run_id, occurred_at)
        _link_episode_experience(cur, episode_id, experience_id)
        if result.episode_candidate.evidence_span is not None and primary_obs_id:
            _insert_evidence(cur, "episode", episode_id, "observation", primary_obs_id)

        for seq, trace_draft in enumerate(result.cognitive_traces, start=1):
            observation_id = obs_ids[trace_draft.observation_index]
            _insert_cognitive_trace(
                cur, episode_id, seq, trace_draft, observation_id, run_id, occurred_at
            )

        for state_draft in result.episode_state_items:
            state_id = _insert_episode_state_item(
                cur, owner_id, episode_id, state_draft, entity_map, occurred_at
            )
            if state_draft.evidence_span is not None and primary_obs_id:
                _insert_evidence(cur, "state_item", state_id, "observation", primary_obs_id)

        if result.decision is not None:
            decision_id = _insert_decision(cur, owner_id, episode_id, result.decision, occurred_at)
            if result.decision.evidence_span is not None and primary_obs_id:
                _insert_evidence(cur, "decision", decision_id, "observation", primary_obs_id)

    # --- 8. Memories -----------------------------------------------------------
    memory_entity_link_count = 0
    for mem_draft in result.memories:
        mem_id = _insert_memory(cur, owner_id, mem_draft, run_id, occurred_at)
        if primary_obs_id:
            _insert_evidence(cur, "memory", mem_id, "observation", primary_obs_id)
        memory_entity_link_count += _insert_memory_entities(
            cur, mem_id, mem_draft, entity_map
        )
        # Action: the concrete thing done as a result of the episode is an
        # ordinary episodic memory, linked via the existing memories.episode_id
        # column -- never a new field. core.extractor already guarantees
        # is_episode_action can only be true when episode_id is set here.
        if mem_draft.is_episode_action and episode_id is not None:
            cur.execute("UPDATE memories SET episode_id = %s WHERE id = %s", (episode_id, mem_id))

    # --- 9. Beliefs ----------------------------------------------------------
    for bel_draft in result.beliefs:
        bel_id = _insert_belief(cur, owner_id, bel_draft, entity_map)
        if primary_obs_id:
            _insert_evidence(cur, "belief", bel_id, "observation", primary_obs_id)
        _insert_evidence(cur, "belief", bel_id, "experience", experience_id)

    # --- 10. Mark observation run succeeded -----------------------------------
    cur.execute(
        "UPDATE extraction_runs SET status = 'succeeded', finished_at = now() WHERE id = %s",
        (run_id,),
    )

    # --- 11. Create pending projection runs ---------------------------------
    _create_projection_runs(cur, experience_id, owner_id)

    # --- 12. Emit experience.created event -----------------------------------
    _emit_event(
        cur,
        event_type="experience.created",
        aggregate_type="experience",
        aggregate_id=experience_id,
        payload={
            "source_type": source_type,
            "content_hash": content_hash,
            "content_uri": content_uri,
            "observation_count": len(result.observations),
            "memory_count": len(result.memories),
            "belief_count": len(result.beliefs),
            "entity_count": len(result.entities),
            "episode_created": episode_id is not None,
        },
        idempotency_key=f"experience.created:{experience_id}",
    )

    return IngestResult(
        experience_id=experience_id,
        content_uri=content_uri,
        created=True,
        observation_count=len(result.observations),
        entity_count=len(result.entities),
        memory_count=len(result.memories),
        belief_count=len(result.beliefs),
        memory_entity_link_count=memory_entity_link_count,
        episode_created=episode_id is not None,
        cognitive_trace_count=len(result.cognitive_traces) if episode_id is not None else 0,
        episode_state_item_count=len(result.episode_state_items) if episode_id is not None else 0,
        decision_created=result.decision is not None and episode_id is not None,
        ambiguous=result.ambiguous,
        ambiguity_note=result.ambiguity_note,
    )
