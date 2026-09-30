-- 003_ops_and_indexes.sql
-- Event log, job state, indexes, and the append-only enforcement.

BEGIN;

-- ===========================================================================
-- Event log. Every canonical mutation emits one row; workflows subscribe to it.
-- This is what makes the pipeline replayable without Kafka on day one.
-- ===========================================================================

CREATE TABLE events (
    id              bigserial PRIMARY KEY,
    event_id        uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    event_type      text NOT NULL,            -- experience.created, observation.extracted, ...
    aggregate_type  text NOT NULL,
    aggregate_id    uuid NOT NULL,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key text UNIQUE,
    occurred_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE processing_jobs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow            text NOT NULL,
    aggregate_id        uuid,
    idempotency_key     text NOT NULL UNIQUE,
    status              job_status NOT NULL DEFAULT 'pending',
    attempts            integer NOT NULL DEFAULT 0,
    last_error          text,
    temporal_workflow_id text,
    scheduled_for       timestamptz NOT NULL DEFAULT now(),
    started_at          timestamptz,
    finished_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Tracks what has been pushed to the two projections, so a rebuild knows where
-- it is and a drifted projection can be detected and repaired.
CREATE TABLE projection_offsets (
    projection      text PRIMARY KEY,         -- 'neo4j' | 'qdrant'
    last_event_id   bigint NOT NULL DEFAULT 0,
    rebuilt_at      timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

INSERT INTO projection_offsets (projection) VALUES ('neo4j'), ('qdrant');

-- ===========================================================================
-- Append-only enforcement
-- ===========================================================================

CREATE TRIGGER experiences_immutable
    BEFORE UPDATE OR DELETE ON experiences
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TRIGGER observations_immutable
    BEFORE UPDATE OR DELETE ON observations
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TRIGGER cognitive_traces_immutable
    BEFORE UPDATE OR DELETE ON cognitive_traces
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TRIGGER belief_versions_immutable
    BEFORE UPDATE OR DELETE ON belief_versions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TRIGGER events_immutable
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TRIGGER association_events_immutable
    BEFORE UPDATE OR DELETE ON association_events
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ===========================================================================
-- updated_at maintenance
-- ===========================================================================

CREATE TRIGGER episodes_touch BEFORE UPDATE ON episodes
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER entities_touch BEFORE UPDATE ON entities
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER relationships_touch BEFORE UPDATE ON relationships
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER memories_touch BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER beliefs_touch BEFORE UPDATE ON beliefs
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER mental_models_touch BEFORE UPDATE ON mental_models
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER cognitive_patterns_touch BEFORE UPDATE ON cognitive_patterns
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER decisions_touch BEFORE UPDATE ON decisions
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER state_items_touch BEFORE UPDATE ON state_items
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER associations_touch BEFORE UPDATE ON associations
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER processing_jobs_touch BEFORE UPDATE ON processing_jobs
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ===========================================================================
-- Indexes
-- ===========================================================================

CREATE INDEX idx_experiences_occurred ON experiences (owner_id, occurred_at DESC);
CREATE INDEX idx_experiences_kind ON experiences (kind, occurred_at DESC);

CREATE INDEX idx_observations_experience ON observations (experience_id);
CREATE INDEX idx_observations_status ON observations (epistemic_status, epistemic_owner);
CREATE INDEX idx_observations_observed ON observations (observed_at DESC);

CREATE INDEX idx_episodes_time ON episodes (owner_id, started_at DESC);
CREATE INDEX idx_episodes_kind ON episodes (kind);
CREATE INDEX idx_traces_episode ON cognitive_traces (episode_id, seq);
CREATE INDEX idx_traces_operation ON cognitive_traces (operation, occurred_at DESC);

CREATE INDEX idx_entities_name_trgm ON entities USING gin (canonical_name gin_trgm_ops);
CREATE INDEX idx_aliases_trgm ON entity_aliases USING gin (alias gin_trgm_ops);
CREATE INDEX idx_entities_kind ON entities (kind) WHERE merged_into_id IS NULL;
CREATE INDEX idx_entities_attributes ON entities USING gin (attributes);

CREATE INDEX idx_relationships_source ON relationships (source_entity_id, rel_type);
CREATE INDEX idx_relationships_target ON relationships (target_entity_id, rel_type);
CREATE INDEX idx_relationships_live ON relationships (rel_type) WHERE valid_to IS NULL;

CREATE INDEX idx_memories_live ON memories (owner_id, kind)
    WHERE valid_to IS NULL AND superseded_by IS NULL;
CREATE INDEX idx_memories_salience ON memories (salience DESC) WHERE valid_to IS NULL;
CREATE INDEX idx_memories_activation ON memories (last_activated_at DESC NULLS LAST);
CREATE INDEX idx_memory_entities_entity ON memory_entities (entity_id);

CREATE INDEX idx_beliefs_active ON beliefs (owner_id, status) WHERE status = 'active';
CREATE INDEX idx_beliefs_domain ON beliefs (domain);
CREATE INDEX idx_belief_versions_belief ON belief_versions (belief_id, version DESC);

CREATE INDEX idx_patterns_confidence ON cognitive_patterns (confidence DESC);
CREATE INDEX idx_decisions_time ON decisions (owner_id, decided_at DESC NULLS LAST);
CREATE INDEX idx_decisions_pending_outcome ON decisions (decided_at)
    WHERE outcome IS NULL AND decided_at IS NOT NULL;

CREATE INDEX idx_state_open ON state_items (owner_id, kind, priority DESC)
    WHERE closed_at IS NULL;

CREATE INDEX idx_assoc_source ON associations (source_type, source_id, context, strength DESC);
CREATE INDEX idx_assoc_target ON associations (target_type, target_id, context, strength DESC);
CREATE INDEX idx_assoc_stale ON associations (last_decayed_at);

CREATE INDEX idx_evidence_derived ON evidence (derived_type, derived_id);
CREATE INDEX idx_evidence_source ON evidence (source_type, source_id);

CREATE INDEX idx_events_unprocessed ON events (id) WHERE true;
CREATE INDEX idx_events_type ON events (event_type, occurred_at DESC);
CREATE INDEX idx_jobs_pending ON processing_jobs (scheduled_for)
    WHERE status IN ('pending', 'failed');

-- ===========================================================================
-- Convenience views used by the activation engine
-- ===========================================================================

CREATE VIEW active_state AS
SELECT id, owner_id, kind, content, entity_id, priority, activation, opened_at
FROM state_items
WHERE closed_at IS NULL
ORDER BY priority DESC, last_touched_at DESC;

CREATE VIEW live_memories AS
SELECT *
FROM memories
WHERE valid_to IS NULL AND superseded_by IS NULL;

-- Every derived claim with its supporting and contradicting evidence counts.
-- This is what backs "why do you think that about me".
CREATE VIEW evidence_summary AS
SELECT derived_type,
       derived_id,
       count(*) FILTER (WHERE stance = 'supports')    AS supporting,
       count(*) FILTER (WHERE stance = 'contradicts') AS contradicting,
       sum(weight) FILTER (WHERE stance = 'supports')    AS support_weight,
       sum(weight) FILTER (WHERE stance = 'contradicts') AS contradiction_weight,
       max(recorded_at) AS last_evidence_at
FROM evidence
GROUP BY derived_type, derived_id;

COMMIT;
