-- 004_memory_systems_and_epistemics.sql
--
-- Implements five changes:
--   1. Memory systems (episodic / semantic / procedural) with per-system validity rules
--   2. Belief kinds, separating "what Abhishek thinks" from "what is true"
--   3. Situations as first-class activation contexts
--   4. Owner as an entity, so the singleton participates in the relationship graph
--   5. Association provenance that can answer "why is A linked to B in my brain"
--
-- Plus activation_traces, which records what the retrieval engine actually surfaced.
--
-- NOTE: the ALTER TYPE ... ADD VALUE statements must run outside a transaction
-- block and cannot be used in the same transaction that creates them, so they
-- come first, unwrapped. Everything after runs inside BEGIN/COMMIT.

ALTER TYPE derived_object_type ADD VALUE IF NOT EXISTS 'situation';
ALTER TYPE source_object_type ADD VALUE IF NOT EXISTS 'situation';
ALTER TYPE epistemic_owner ADD VALUE IF NOT EXISTS 'authoritative_source';

-- ===========================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Memory systems
--
-- These are different cognitive functions, not different labels. They decay
-- differently, they go stale differently, and the re-ranker weights them
-- differently depending on whether the question is about what happened, what
-- is the case, or how to do something.
-- ---------------------------------------------------------------------------

CREATE TYPE memory_system AS ENUM (
    'episodic',     -- what happened to me; never becomes false, only reinterpreted
    'semantic',     -- what I know about the world; goes stale and gets superseded
    'procedural'    -- how I do things; degrades with disuse, restored by practice
);

-- memory_kind is replaced rather than extended. There is no kind that means
-- "something I learned": "I learned Kubernetes" is a procedural memory, and
-- "I learned not to trust that business model" is a normative belief with an
-- episodic memory as its evidence. The extractor decides which at write time.
-- See core/classification.md for the decision rule.
ALTER TYPE memory_kind RENAME TO memory_kind_v0;

CREATE TYPE memory_kind AS ENUM (
    -- episodic
    'event', 'encounter', 'exchange', 'milestone',
    -- semantic
    'fact', 'concept', 'person_attribute', 'preference', 'goal', 'definition',
    -- procedural
    'skill', 'procedure', 'heuristic', 'tool_use'
);

ALTER TABLE memories
    ADD COLUMN memory_system memory_system,
    ADD COLUMN reinterprets_id uuid REFERENCES memories(id),
    ADD COLUMN consolidated_into_id uuid REFERENCES memories(id),
    ADD COLUMN instance_count integer NOT NULL DEFAULT 1;

-- Backfill, then swap the column type. v0.1 has no production data; the mapping
-- exists so the migration is safe to run against an early test store.
UPDATE memories SET memory_system = CASE kind::text
    WHEN 'event'      THEN 'episodic'
    WHEN 'skill'      THEN 'procedural'
    WHEN 'procedure'  THEN 'procedural'
    ELSE 'semantic'
END::memory_system;

-- live_memories uses SELECT * FROM memories, which pins the column types.
-- Drop it here; recreate with the same definition immediately after the
-- ALTER COLUMN so the view is never absent for more than one statement.
DROP VIEW live_memories;

ALTER TABLE memories
    ALTER COLUMN kind TYPE memory_kind USING (
        CASE kind::text
            WHEN 'person' THEN 'person_attribute'
            ELSE kind::text
        END
    )::memory_kind;

-- Restore the view — identical definition to migration 003.
CREATE VIEW live_memories AS
SELECT * FROM memories WHERE valid_to IS NULL AND superseded_by IS NULL;

ALTER TABLE memories ALTER COLUMN memory_system SET NOT NULL;

DROP TYPE memory_kind_v0;

-- A kind belongs to exactly one system. This is what stops the table becoming a
-- dumping ground three years from now.
ALTER TABLE memories ADD CONSTRAINT memory_kind_matches_system CHECK (
    (memory_system = 'episodic'   AND kind IN ('event', 'encounter', 'exchange', 'milestone'))
 OR (memory_system = 'semantic'   AND kind IN ('fact', 'concept', 'person_attribute',
                                               'preference', 'goal', 'definition'))
 OR (memory_system = 'procedural' AND kind IN ('skill', 'procedure', 'heuristic', 'tool_use'))
);

-- Episodic memories are never superseded. The event happened. What can change is
-- how it is understood, which is a new memory pointing back via reinterprets_id.
ALTER TABLE memories ADD CONSTRAINT episodic_not_superseded CHECK (
    memory_system <> 'episodic' OR (superseded_by IS NULL AND valid_to IS NULL)
);

-- Procedural memory tracks practice rather than validity.
ALTER TABLE memories
    ADD COLUMN proficiency double precision CHECK (proficiency >= 0 AND proficiency <= 1),
    ADD COLUMN last_practiced_at timestamptz,
    ADD CONSTRAINT proficiency_only_procedural CHECK (
        memory_system = 'procedural' OR proficiency IS NULL
    );

CREATE INDEX idx_memories_system ON memories (owner_id, memory_system, kind)
    WHERE valid_to IS NULL AND superseded_by IS NULL;
CREATE INDEX idx_memories_reinterprets ON memories (reinterprets_id)
    WHERE reinterprets_id IS NOT NULL;
CREATE INDEX idx_memories_consolidation ON memories (consolidated_into_id)
    WHERE consolidated_into_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. Belief kinds: separating the claim from the claimant
--
-- "I think this startup will work" is conclusive evidence that Abhishek holds
-- that view and weak evidence that the startup will work. One weight cannot
-- express both, so the belief declares which proposition it is making.
-- ---------------------------------------------------------------------------

CREATE TYPE belief_kind AS ENUM (
    'self_report',   -- about the owner's own state, preferences, intentions
    'world_claim',   -- about external reality, verifiable independently of the owner
    'normative',     -- about what should be done; a policy, not a fact
    'attributed'     -- about what someone else believes
);

ALTER TABLE beliefs
    ADD COLUMN belief_kind belief_kind NOT NULL DEFAULT 'world_claim',
    ADD COLUMN about_entity_id uuid REFERENCES entities(id),
    -- A world_claim the owner holds can be tracked on two axes at once: how sure
    -- we are they believe it, and how sure we are it is true.
    ADD COLUMN holding_log_odds double precision NOT NULL DEFAULT 0.0,
    ADD COLUMN holding_confidence double precision NOT NULL DEFAULT 0.5
        CHECK (holding_confidence >= 0 AND holding_confidence <= 1);

COMMENT ON COLUMN beliefs.log_odds IS
    'Confidence that the proposition is true. Meaningless for self_report, where the proposition IS the holding.';
COMMENT ON COLUMN beliefs.holding_log_odds IS
    'Confidence that the owner currently holds this belief. Always meaningful.';

ALTER TABLE belief_versions
    ADD COLUMN holding_confidence double precision,
    ADD COLUMN holding_log_odds double precision,
    ADD COLUMN axis text NOT NULL DEFAULT 'truth' CHECK (axis IN ('truth', 'holding', 'both'));

CREATE INDEX idx_beliefs_kind ON beliefs (owner_id, belief_kind, status);

-- Observations carry three independent quantities that were previously conflated.
ALTER TABLE observations RENAME COLUMN confidence TO extraction_confidence;
ALTER TABLE observations RENAME COLUMN reliability TO source_reliability;

COMMENT ON COLUMN observations.extraction_confidence IS
    'Confidence that the observation faithfully represents what was said or seen.';
COMMENT ON COLUMN observations.source_reliability IS
    'Standing trust in this source. Independent of who owns the claim epistemically.';

-- Standing reliability per source, learned over time rather than hardcoded.
CREATE TABLE source_reliability (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id           uuid REFERENCES entities(id),
    source_label        text,                 -- for non-entity sources: a domain, a publication
    domain              text,                 -- reliability is domain-specific
    reliability         double precision NOT NULL DEFAULT 0.5
                          CHECK (reliability >= 0 AND reliability <= 1),
    confirmed_count     integer NOT NULL DEFAULT 0,
    contradicted_count  integer NOT NULL DEFAULT 0,
    set_by              text NOT NULL DEFAULT 'system' CHECK (set_by IN ('system', 'owner')),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (entity_id IS NOT NULL OR source_label IS NOT NULL),
    UNIQUE (entity_id, source_label, domain)
);

-- ---------------------------------------------------------------------------
-- 3. Situations: the activation context layer
--
-- A situation is a recurring class of circumstance, not a single occurrence.
-- "Facing an irreversible commitment under time pressure" is a situation;
-- the Phynx incorporation decision is an episode that instantiates it.
-- Associations can hang off situations, which is what turns "A associates with B"
-- into "in this kind of moment, these parts of the model light up".
-- ---------------------------------------------------------------------------

CREATE TABLE situations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            uuid NOT NULL REFERENCES owner(id),
    name                text NOT NULL,
    description         text,
    signature           jsonb NOT NULL DEFAULT '{}'::jsonb,  -- features that identify an instance
    epistemic_status    epistemic_status NOT NULL DEFAULT 'hypothesis',
    confidence          double precision NOT NULL DEFAULT 0.2
                          CHECK (confidence >= 0 AND confidence <= 1),
    instance_count      integer NOT NULL DEFAULT 0,
    first_observed_at   timestamptz,
    last_observed_at    timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name)
);

CREATE TRIGGER situations_touch BEFORE UPDATE ON situations
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE episode_situations (
    episode_id      uuid NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    situation_id    uuid NOT NULL REFERENCES situations(id) ON DELETE CASCADE,
    match_confidence double precision NOT NULL DEFAULT 0.5
                      CHECK (match_confidence >= 0 AND match_confidence <= 1),
    PRIMARY KEY (episode_id, situation_id)
);

-- Which strategy the owner tends to reach for in a given situation. This is the
-- edge that makes the graph a model of cognition rather than of knowledge.
CREATE TABLE situation_strategies (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    situation_id        uuid NOT NULL REFERENCES situations(id) ON DELETE CASCADE,
    target_type         derived_object_type NOT NULL,   -- cognitive_pattern | mental_model | belief
    target_id           uuid NOT NULL,
    strength            double precision NOT NULL DEFAULT 0.0
                          CHECK (strength >= 0 AND strength <= 1),
    observation_count   integer NOT NULL DEFAULT 0,
    outcome_valence_avg double precision,     -- did reaching for this tend to work out
    first_observed_at   timestamptz NOT NULL DEFAULT now(),
    last_observed_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (situation_id, target_type, target_id)
);

CREATE INDEX idx_situation_strategies ON situation_strategies (situation_id, strength DESC);

-- Associations become situation-scoped. The free-text context stays as a label
-- for the cases where no situation has been identified yet.
ALTER TABLE associations
    ADD COLUMN situation_id uuid REFERENCES situations(id);

-- Drop the constraint first (which also drops the backing index).
-- Then drop the index IF it somehow outlived the constraint (no-op if already gone).
ALTER TABLE associations
    DROP CONSTRAINT IF EXISTS associations_source_type_source_id_target_type_target_id_co_key;
DROP INDEX IF EXISTS associations_source_type_source_id_target_type_target_id_co_key;

CREATE UNIQUE INDEX idx_assoc_unique ON associations (
    source_type, source_id, target_type, target_id, context,
    COALESCE(situation_id, '00000000-0000-0000-0000-000000000000'::uuid)
);

CREATE INDEX idx_assoc_situation ON associations (situation_id, strength DESC)
    WHERE situation_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. Owner is an entity
--
-- The singleton constraint already guaranteed exactly one owner row. What was
-- missing is that the owner had no presence in the entity graph, so relationships
-- between the owner and other people had nowhere to live.
-- ---------------------------------------------------------------------------

ALTER TABLE owner
    ADD COLUMN entity_id uuid REFERENCES entities(id);

COMMENT ON TABLE owner IS
    'Exactly one row, enforced by the singleton unique check. Every other person '
    'in the system is an Entity of kind person, never a second owner.';

-- Other people are entities and can never acquire owner status.
ALTER TABLE entities
    ADD COLUMN is_owner boolean NOT NULL DEFAULT false;

CREATE UNIQUE INDEX idx_single_owner_entity ON entities (is_owner) WHERE is_owner;

-- ---------------------------------------------------------------------------
-- 5. Association provenance
--
-- Every reinforcement records enough context to reconstruct the answer to
-- "why is AI strongly associated with startups in my brain".
-- ---------------------------------------------------------------------------

ALTER TABLE association_events
    ADD COLUMN context text NOT NULL DEFAULT 'global',
    ADD COLUMN situation_id uuid REFERENCES situations(id),
    ADD COLUMN episode_kind episode_kind,
    ADD COLUMN source_kind association_source NOT NULL DEFAULT 'observed',
    ADD COLUMN trigger_type source_object_type,
    ADD COLUMN trigger_id uuid,
    ADD COLUMN salience double precision NOT NULL DEFAULT 1.0;

CREATE INDEX idx_assoc_events_assoc ON association_events (association_id, occurred_at DESC);
CREATE INDEX idx_assoc_events_context ON association_events (association_id, context);

-- The explanation query, as a view. Returns the co-activation count, the
-- breakdown by episode kind and context, and the observation window.
CREATE VIEW association_provenance AS
SELECT
    a.id                                   AS association_id,
    a.source_type, a.source_id,
    a.target_type, a.target_id,
    a.context,
    a.situation_id,
    a.strength,
    a.confidence,
    a.observation_count,
    a.first_observed_at,
    a.last_observed_at,
    count(ae.id)                                                        AS reinforcement_count,
    count(ae.id) FILTER (WHERE ae.source_kind = 'explicit')             AS explicit_statements,
    count(ae.id) FILTER (WHERE ae.effective_kind = 'decision')        AS decision_episodes,
    count(ae.id) FILTER (WHERE ae.effective_kind = 'planning')        AS planning_episodes,
    count(ae.id) FILTER (WHERE ae.effective_kind = 'reflection')      AS reflection_episodes,
    count(ae.id) FILTER (WHERE ae.effective_kind = 'problem_solving') AS problem_solving_episodes,
    count(ae.id) FILTER (WHERE ae.effective_kind IS NULL)             AS episodeless_reinforcements,
    (
        SELECT jsonb_object_agg(x.context, x.n)
        FROM (
            SELECT e2.context, count(*) AS n
            FROM association_events e2
            WHERE e2.association_id = a.id
            GROUP BY e2.context
        ) x
    ) AS context_breakdown
FROM associations a
LEFT JOIN LATERAL (
    -- The denormalized episode_kind on the event wins. An association can be
    -- reinforced with no episode at all (an explicit statement, a document
    -- co-occurrence), and the breakdown must still be answerable.
    SELECT e.id, e.source_kind, e.context, e.occurred_at,
           COALESCE(e.episode_kind, ep.kind) AS effective_kind
    FROM association_events e
    LEFT JOIN episodes ep ON ep.id = e.episode_id
    WHERE e.association_id = a.id
) ae ON true
GROUP BY a.id;

-- ---------------------------------------------------------------------------
-- 6. Activation traces
--
-- What the retrieval engine actually surfaced, per query. Without this the
-- activation layer is a black box in exactly the way the association graph
-- would have been without association_events. It is also the only record that
-- could ever be used to learn better activation.
-- ---------------------------------------------------------------------------

CREATE TYPE retrieval_channel AS ENUM ('semantic', 'graph', 'temporal', 'state', 'situation');

CREATE TABLE activation_traces (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            uuid NOT NULL REFERENCES owner(id),
    query_text          text NOT NULL,
    detected_intent     text,
    situation_id        uuid REFERENCES situations(id),
    seed_entity_ids     uuid[] NOT NULL DEFAULT '{}',
    state_snapshot_id   uuid REFERENCES state_snapshots(id),
    candidate_count     integer NOT NULL DEFAULT 0,
    included_count      integer NOT NULL DEFAULT 0,
    experience_id       uuid REFERENCES experiences(id),  -- the resulting exchange
    latency_ms          integer,
    model_version_id    uuid REFERENCES model_versions(id),
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE activation_members (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id            uuid NOT NULL REFERENCES activation_traces(id) ON DELETE CASCADE,
    object_type         derived_object_type NOT NULL,
    object_id           uuid NOT NULL,
    channel             retrieval_channel NOT NULL,
    hops                integer,              -- graph distance from a seed, null for other channels
    raw_score           double precision NOT NULL,
    final_score         double precision NOT NULL,
    rank                integer,
    included            boolean NOT NULL DEFAULT false,
    is_contradiction    boolean NOT NULL DEFAULT false
);

CREATE INDEX idx_activation_members_trace ON activation_members (trace_id, rank);
CREATE INDEX idx_activation_members_object ON activation_members (object_type, object_id);
CREATE INDEX idx_activation_traces_time ON activation_traces (owner_id, created_at DESC);

CREATE TRIGGER activation_traces_immutable
    BEFORE UPDATE OR DELETE ON activation_traces
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------------------
-- 7. Consolidation
--
-- Nothing in v0.1 collapsed repeated episodic memories into semantic ones, which
-- is the main thing biological memory actually does. Without it the store only
-- grows and every recall competes with fifty near-identical instances.
-- ---------------------------------------------------------------------------

CREATE TABLE consolidation_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        uuid NOT NULL REFERENCES owner(id),
    kind            text NOT NULL CHECK (kind IN (
                        'episodic_to_semantic', 'association_decay',
                        'pattern_promotion', 'contradiction_scan',
                        'entity_centrality', 'situation_discovery')),
    window_start    timestamptz,
    window_end      timestamptz,
    objects_scanned integer NOT NULL DEFAULT 0,
    objects_changed integer NOT NULL DEFAULT 0,
    status          job_status NOT NULL DEFAULT 'pending',
    detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at      timestamptz,
    finished_at     timestamptz
);

CREATE INDEX idx_consolidation_kind ON consolidation_runs (owner_id, kind, finished_at DESC);

-- Candidate clusters awaiting promotion into a semantic memory. The source
-- episodic memories are never deleted; they gain a consolidated_into_id pointer.
CREATE TABLE consolidation_candidates (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              uuid NOT NULL REFERENCES consolidation_runs(id) ON DELETE CASCADE,
    proposed_content    text NOT NULL,
    proposed_kind       memory_kind NOT NULL,
    proposed_system     memory_system NOT NULL,
    member_memory_ids   uuid[] NOT NULL,
    support_count       integer NOT NULL,
    confidence          double precision NOT NULL,
    accepted            boolean,
    accepted_memory_id  uuid REFERENCES memories(id),
    reviewed_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now()
);

COMMIT;
