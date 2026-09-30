-- 002_canonical.sql
-- The canonical model. PostgreSQL is the source of truth; Neo4j and the vector
-- store are rebuildable projections of what lives here.

BEGIN;

-- ===========================================================================
-- 0. Owner and provenance
-- ===========================================================================

CREATE TABLE owner (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    singleton       boolean NOT NULL DEFAULT true UNIQUE CHECK (singleton),
    display_name    text NOT NULL,
    timezone        text NOT NULL DEFAULT 'UTC',
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Every derived row records which model produced it, so extraction can be
-- re-run and back-filled when the models improve without losing history.
CREATE TABLE model_versions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    role            text NOT NULL,            -- extraction | embedding | reasoning | consolidation
    provider        text NOT NULL,
    model_name      text NOT NULL,
    model_revision  text,
    prompt_hash     text,                     -- hash of the prompt template in use
    params          jsonb NOT NULL DEFAULT '{}'::jsonb,
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (role, provider, model_name, model_revision, prompt_hash)
);

CREATE TABLE extraction_runs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    experience_id       uuid NOT NULL,
    model_version_id    uuid NOT NULL REFERENCES model_versions(id),
    stage               text NOT NULL,        -- observation | episode | trace | memory | entity | belief
    status              job_status NOT NULL DEFAULT 'pending',
    started_at          timestamptz,
    finished_at         timestamptz,
    error               text,
    superseded_by       uuid REFERENCES extraction_runs(id),
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- ===========================================================================
-- 1. Raw surface: conversations and messages
-- ===========================================================================

CREATE TABLE conversations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        uuid NOT NULL REFERENCES owner(id),
    channel         text NOT NULL,            -- chat | voice | import
    title           text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    ended_at        timestamptz,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE messages (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq             bigint NOT NULL,
    speaker         text NOT NULL,            -- owner | assistant | <person name/entity ref>
    speaker_entity_id uuid,                   -- resolved later; FK added in 003
    content         text NOT NULL,
    artifact_uri    text,                     -- object storage pointer for audio/attachments
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (conversation_id, seq)
);

-- ===========================================================================
-- 2. Experience: immutable history
-- ===========================================================================

CREATE TABLE experiences (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        uuid NOT NULL REFERENCES owner(id),
    kind            experience_kind NOT NULL,
    summary         text,                     -- may be null until extraction runs
    content_uri     text,                     -- canonical raw artifact in object storage
    content_hash    text NOT NULL,            -- dedupe + idempotency key for ingestion
    conversation_id uuid REFERENCES conversations(id),
    occurred_at     timestamptz NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    source          jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (owner_id, content_hash)
);

ALTER TABLE extraction_runs
    ADD CONSTRAINT extraction_runs_experience_fk
    FOREIGN KEY (experience_id) REFERENCES experiences(id) ON DELETE CASCADE;

-- ===========================================================================
-- 3. Observation: evidence, not truth
-- ===========================================================================

CREATE TABLE observations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    experience_id       uuid NOT NULL REFERENCES experiences(id) ON DELETE CASCADE,
    message_id          uuid REFERENCES messages(id),
    content             text NOT NULL,
    epistemic_status    epistemic_status NOT NULL,
    epistemic_owner     epistemic_owner NOT NULL,
    asserted_by_entity_id uuid,               -- who made the claim, if not the owner
    confidence          double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    reliability         double precision NOT NULL DEFAULT 1.0
                          CHECK (reliability >= 0 AND reliability <= 1),
    span                jsonb,                -- character offsets back into the raw artifact
    extraction_run_id   uuid REFERENCES extraction_runs(id),
    observed_at         timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- ===========================================================================
-- 4. Episode and cognitive trace
-- ===========================================================================

CREATE TABLE episodes (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            uuid NOT NULL REFERENCES owner(id),
    kind                episode_kind NOT NULL,
    title               text NOT NULL,
    narrative           text,
    trigger             text,                 -- what put the owner into this situation
    resolution          text,
    salience            double precision NOT NULL DEFAULT 0.5
                          CHECK (salience >= 0 AND salience <= 1),
    started_at          timestamptz NOT NULL,
    ended_at            timestamptz,
    extraction_run_id   uuid REFERENCES extraction_runs(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE episode_experiences (
    episode_id      uuid NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    experience_id   uuid NOT NULL REFERENCES experiences(id) ON DELETE CASCADE,
    PRIMARY KEY (episode_id, experience_id)
);

-- Structured, observable cognitive behaviour. Each trace step must cite an
-- observation, which is what keeps this from becoming stored chain-of-thought.
CREATE TABLE cognitive_traces (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id          uuid NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    seq                 integer NOT NULL,
    operation           cognitive_op NOT NULL,
    target              text NOT NULL,        -- what the operation was applied to
    detail              text,
    observation_id      uuid REFERENCES observations(id),
    confidence          double precision NOT NULL DEFAULT 0.5
                          CHECK (confidence >= 0 AND confidence <= 1),
    occurred_at         timestamptz NOT NULL,
    extraction_run_id   uuid REFERENCES extraction_runs(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (episode_id, seq)
);

-- ===========================================================================
-- 5. Entities and relationships
-- ===========================================================================

CREATE TABLE entities (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        uuid NOT NULL REFERENCES owner(id),
    kind            entity_kind NOT NULL,
    canonical_name  text NOT NULL,
    description     text,
    attributes      jsonb NOT NULL DEFAULT '{}'::jsonb,
    centrality      double precision NOT NULL DEFAULT 0.0,  -- recomputed by consolidation
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    merged_into_id  uuid REFERENCES entities(id),           -- null unless this row was merged away
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE entity_aliases (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias           text NOT NULL,
    source          association_source NOT NULL DEFAULT 'observed',
    confidence      double precision NOT NULL DEFAULT 1.0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (entity_id, alias)
);

-- Merges and splits are recorded so entity resolution stays reversible.
CREATE TABLE entity_resolutions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action          text NOT NULL CHECK (action IN ('merge', 'split')),
    from_entity_id  uuid NOT NULL REFERENCES entities(id),
    to_entity_id    uuid NOT NULL REFERENCES entities(id),
    reason          text,
    confidence      double precision NOT NULL DEFAULT 0.5,
    performed_by    text NOT NULL DEFAULT 'system',
    created_at      timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE messages
    ADD CONSTRAINT messages_speaker_entity_fk
    FOREIGN KEY (speaker_entity_id) REFERENCES entities(id);

ALTER TABLE observations
    ADD CONSTRAINT observations_asserted_by_fk
    FOREIGN KEY (asserted_by_entity_id) REFERENCES entities(id);

CREATE TABLE relationships (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    rel_type        text NOT NULL,            -- WORKS_ON, KNOWS, USES, CAUSED, ...
    attributes      jsonb NOT NULL DEFAULT '{}'::jsonb,
    confidence      double precision NOT NULL DEFAULT 0.5
                      CHECK (confidence >= 0 AND confidence <= 1),
    epistemic_owner epistemic_owner NOT NULL DEFAULT 'system_inference',
    valid_from      timestamptz NOT NULL DEFAULT now(),
    valid_to        timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_entity_id, target_entity_id, rel_type, valid_from)
);

-- ===========================================================================
-- 6. Memory
-- ===========================================================================

CREATE TABLE memories (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            uuid NOT NULL REFERENCES owner(id),
    kind                memory_kind NOT NULL,
    content             text NOT NULL,
    epistemic_status    epistemic_status NOT NULL DEFAULT 'observed',
    epistemic_owner     epistemic_owner NOT NULL DEFAULT 'owner',
    confidence          double precision NOT NULL DEFAULT 0.5
                          CHECK (confidence >= 0 AND confidence <= 1),
    salience            double precision NOT NULL DEFAULT 0.5
                          CHECK (salience >= 0 AND salience <= 1),
    emotional_valence   double precision CHECK (emotional_valence >= -1 AND emotional_valence <= 1),
    emotional_intensity double precision CHECK (emotional_intensity >= 0 AND emotional_intensity <= 1),
    emotion_is_stated   boolean NOT NULL DEFAULT false,  -- false means inferred; never treated as fact
    episode_id          uuid REFERENCES episodes(id),
    valid_from          timestamptz NOT NULL DEFAULT now(),
    valid_to            timestamptz,
    last_activated_at   timestamptz,
    activation_count    integer NOT NULL DEFAULT 0,
    superseded_by       uuid REFERENCES memories(id),
    extraction_run_id   uuid REFERENCES extraction_runs(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memory_entities (
    memory_id       uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    entity_id       uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role            text,                     -- subject | object | context
    PRIMARY KEY (memory_id, entity_id)
);

-- ===========================================================================
-- 7. Beliefs and their trajectories
-- ===========================================================================

CREATE TABLE beliefs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            uuid NOT NULL REFERENCES owner(id),
    proposition         text NOT NULL,
    domain              text,
    status              belief_status NOT NULL DEFAULT 'active',
    confidence          double precision NOT NULL DEFAULT 0.5
                          CHECK (confidence >= 0 AND confidence <= 1),
    log_odds            double precision NOT NULL DEFAULT 0.0,  -- the quantity actually updated
    epistemic_owner     epistemic_owner NOT NULL DEFAULT 'owner',
    held_since          timestamptz NOT NULL DEFAULT now(),
    last_evidence_at    timestamptz,
    superseded_by       uuid REFERENCES beliefs(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Append-only trajectory. Every confidence movement leaves a row here, which is
-- what lets the system answer "when did I change my mind, and what changed it".
CREATE TABLE belief_versions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    belief_id       uuid NOT NULL REFERENCES beliefs(id) ON DELETE CASCADE,
    version         integer NOT NULL,
    status          belief_status NOT NULL,
    confidence      double precision NOT NULL,
    log_odds        double precision NOT NULL,
    delta           double precision NOT NULL DEFAULT 0.0,
    trigger_evidence_id uuid,                 -- FK added after evidence table exists
    reason          text,
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (belief_id, version)
);

CREATE TABLE belief_relations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_belief_id uuid NOT NULL REFERENCES beliefs(id) ON DELETE CASCADE,
    target_belief_id uuid NOT NULL REFERENCES beliefs(id) ON DELETE CASCADE,
    relation        text NOT NULL CHECK (relation IN ('contradicts', 'supports', 'refines', 'supersedes')),
    strength        double precision NOT NULL DEFAULT 0.5,
    detected_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_belief_id, target_belief_id, relation)
);

-- ===========================================================================
-- 8. Mental models, cognitive patterns, decisions
-- ===========================================================================

CREATE TABLE mental_models (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        uuid NOT NULL REFERENCES owner(id),
    name            text NOT NULL,
    principle       text NOT NULL,
    applies_when    text,
    domain          text,
    confidence      double precision NOT NULL DEFAULT 0.4
                      CHECK (confidence >= 0 AND confidence <= 1),
    application_count integer NOT NULL DEFAULT 0,
    last_applied_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- A pattern stays a hypothesis until enough distinct episodes support it.
CREATE TABLE cognitive_patterns (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            uuid NOT NULL REFERENCES owner(id),
    name                text NOT NULL,
    trigger_condition   text NOT NULL,
    sequence            cognitive_op[] NOT NULL,
    description         text,
    epistemic_status    epistemic_status NOT NULL DEFAULT 'hypothesis',
    confidence          double precision NOT NULL DEFAULT 0.2
                          CHECK (confidence >= 0 AND confidence <= 1),
    episode_support_count integer NOT NULL DEFAULT 0,
    contradiction_count integer NOT NULL DEFAULT 0,
    first_observed_at   timestamptz,
    last_observed_at    timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE decisions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            uuid NOT NULL REFERENCES owner(id),
    episode_id          uuid REFERENCES episodes(id),
    question            text NOT NULL,
    context             text,
    options             jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{label, rationale, risks}]
    chosen_option       text,
    rationale           text,
    confidence_at_time  double precision CHECK (confidence_at_time >= 0 AND confidence_at_time <= 1),
    reversibility       text CHECK (reversibility IN ('reversible', 'costly', 'irreversible')),
    decided_at          timestamptz,
    outcome             text,
    outcome_valence     double precision CHECK (outcome_valence >= -1 AND outcome_valence <= 1),
    outcome_recorded_at timestamptz,
    reflection          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- ===========================================================================
-- 9. Current cognitive state
-- ===========================================================================

CREATE TABLE state_items (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        uuid NOT NULL REFERENCES owner(id),
    kind            state_item_kind NOT NULL,
    content         text NOT NULL,
    entity_id       uuid REFERENCES entities(id),
    priority        double precision NOT NULL DEFAULT 0.5
                      CHECK (priority >= 0 AND priority <= 1),
    activation      double precision NOT NULL DEFAULT 0.5
                      CHECK (activation >= 0 AND activation <= 1),
    opened_at       timestamptz NOT NULL DEFAULT now(),
    closed_at       timestamptz,
    last_touched_at timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Periodic snapshots so past state can be reconstructed for temporal reasoning.
CREATE TABLE state_snapshots (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        uuid NOT NULL REFERENCES owner(id),
    snapshot        jsonb NOT NULL,
    taken_at        timestamptz NOT NULL DEFAULT now()
);

-- ===========================================================================
-- 10. Associations: the learned activation topology
-- ===========================================================================

CREATE TABLE associations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            uuid NOT NULL REFERENCES owner(id),
    source_type         derived_object_type NOT NULL,
    source_id           uuid NOT NULL,
    target_type         derived_object_type NOT NULL,
    target_id           uuid NOT NULL,
    context             text NOT NULL DEFAULT 'global',  -- directional AND context-scoped
    strength            double precision NOT NULL DEFAULT 0.0
                          CHECK (strength >= 0 AND strength <= 1),
    confidence          double precision NOT NULL DEFAULT 0.0
                          CHECK (confidence >= 0 AND confidence <= 1),
    observation_count   integer NOT NULL DEFAULT 0,
    source_kind         association_source NOT NULL DEFAULT 'observed',
    first_observed_at   timestamptz NOT NULL DEFAULT now(),
    last_observed_at    timestamptz NOT NULL DEFAULT now(),
    last_decayed_at     timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (NOT (source_type = target_type AND source_id = target_id)),
    UNIQUE (source_type, source_id, target_type, target_id, context)
);

-- Append-only log of every co-activation, so strength can be recomputed from
-- scratch if the learning rule changes.
CREATE TABLE association_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    association_id  uuid NOT NULL REFERENCES associations(id) ON DELETE CASCADE,
    episode_id      uuid REFERENCES episodes(id),
    strength_before double precision NOT NULL,
    strength_after  double precision NOT NULL,
    occurred_at     timestamptz NOT NULL DEFAULT now()
);

-- ===========================================================================
-- 11. Evidence spine
-- ===========================================================================

CREATE TABLE evidence (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    derived_type    derived_object_type NOT NULL,
    derived_id      uuid NOT NULL,
    source_type     source_object_type NOT NULL,
    source_id       uuid NOT NULL,
    stance          evidence_stance NOT NULL DEFAULT 'supports',
    weight          double precision NOT NULL DEFAULT 1.0 CHECK (weight >= 0),
    note            text,
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (derived_type, derived_id, source_type, source_id, stance)
);

ALTER TABLE belief_versions
    ADD CONSTRAINT belief_versions_evidence_fk
    FOREIGN KEY (trigger_evidence_id) REFERENCES evidence(id);

COMMIT;
