-- 001_foundation.sql
-- Extensions, shared enums, and the immutability guard used by the append-only tables.
-- Run order: 001 -> 002 -> 003.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- fuzzy entity/alias matching
CREATE EXTENSION IF NOT EXISTS btree_gin;

-- ---------------------------------------------------------------------------
-- Epistemics. These two types are the spine of the whole model: nothing enters
-- the brain without declaring where it came from and how true it is claimed to be.
-- ---------------------------------------------------------------------------

CREATE TYPE epistemic_status AS ENUM (
    'asserted',      -- the owner stated it as true
    'observed',      -- the system directly witnessed it (a message was sent, a file opened)
    'reported',      -- someone other than the owner claimed it
    'inferred',      -- a model derived it; NOT fact
    'hypothesis',    -- the system is entertaining it pending evidence
    'contradicted',  -- live evidence points against it
    'retracted'      -- the owner withdrew it
);

CREATE TYPE epistemic_owner AS ENUM (
    'owner',
    'other_person',
    'external_source',
    'system_inference'
);

CREATE TYPE experience_kind AS ENUM (
    'conversation', 'document', 'voice', 'event', 'note', 'import', 'external'
);

CREATE TYPE episode_kind AS ENUM (
    'decision', 'problem_solving', 'planning', 'learning',
    'reflection', 'conflict', 'social', 'creative', 'routine'
);

-- Observable cognitive operations. Deliberately behavioural: each one corresponds
-- to something the owner did or said, never to a hidden reasoning trace.
CREATE TYPE cognitive_op AS ENUM (
    'identify_unknown', 'seek_information', 'generate_option', 'evaluate_option',
    'compare', 'estimate', 'decide', 'commit', 'defer', 'abandon',
    'reflect', 'revise', 'abstract', 'analogize', 'delegate'
);

-- Superseded by the memory-system ontology in 004. No kind here or there means
-- "something I learned": that phrasing collapses a procedural memory and a
-- belief into one unrecoverable label. See core/classification.md.
CREATE TYPE memory_kind AS ENUM (
    'fact', 'event', 'preference', 'goal', 'skill', 'person', 'procedure'
);

CREATE TYPE belief_status AS ENUM (
    'active', 'weakened', 'contradicted', 'superseded', 'retracted'
);

CREATE TYPE entity_kind AS ENUM (
    'person', 'project', 'organization', 'place', 'technology',
    'concept', 'product', 'work', 'event', 'other'
);

CREATE TYPE association_source AS ENUM ('explicit', 'observed', 'inferred');

CREATE TYPE state_item_kind AS ENUM (
    'goal', 'project', 'question', 'uncertainty', 'attention', 'constraint'
);

CREATE TYPE job_status AS ENUM (
    'pending', 'running', 'succeeded', 'failed', 'cancelled'
);

-- Polymorphic evidence spine.
CREATE TYPE derived_object_type AS ENUM (
    'memory', 'belief', 'mental_model', 'cognitive_pattern',
    'association', 'decision', 'entity', 'relationship', 'state_item'
);

CREATE TYPE source_object_type AS ENUM (
    'experience', 'observation', 'episode', 'cognitive_trace', 'decision', 'memory'
);

CREATE TYPE evidence_stance AS ENUM ('supports', 'contradicts', 'qualifies');

-- ---------------------------------------------------------------------------
-- Immutability guard. Attached in 003 to experiences, observations,
-- cognitive_traces and events. Corrections are new rows, never edits.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'Table % is append-only. Record a correcting row instead of mutating %.',
        TG_TABLE_NAME, COALESCE(OLD.id::text, '(unknown)');
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMIT;
