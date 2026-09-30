-- 005_episodic_invariants.sql
--
-- Consistency fix, not an architectural change. 004 already asserted that an
-- episodic memory cannot carry superseded_by or valid_to:
--
--   CONSTRAINT episodic_not_superseded CHECK (
--       memory_system <> 'episodic' OR (superseded_by IS NULL AND valid_to IS NULL))
--
-- That constraint is evaluated per row against the row's CURRENT memory_system,
-- which leaves two ways to invalidate a historical event anyway:
--
--   1. UPDATE memories SET memory_system = 'semantic' WHERE id = ...;
--      then set valid_to. The CHECK passes at every step.
--   2. DELETE FROM memories WHERE id = ...;
--      no constraint applies to deletion at all.
--
-- Both contradict the decision already encoded in 004 and in the README: an
-- episodic memory records something that happened, and nothing that happens
-- later makes it not have happened. It can be reinterpreted, never invalidated.
-- This migration closes both paths.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. memory_system is immutable once written
--
-- Reclassification is a re-extraction concern, not an update. If the extractor
-- got the system wrong, the correct repair is a new memory with reinterprets_id
-- pointing at the original, which preserves what the system used to think.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION forbid_memory_system_change() RETURNS trigger AS $$
BEGIN
    IF NEW.memory_system IS DISTINCT FROM OLD.memory_system THEN
        RAISE EXCEPTION
            'memory_system is immutable (memory %: % -> %). Write a new memory with '
            'reinterprets_id set instead of reclassifying in place.',
            OLD.id, OLD.memory_system, NEW.memory_system
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER memories_system_immutable
    BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION forbid_memory_system_change();

-- ---------------------------------------------------------------------------
-- 2. Episodic memories cannot be deleted
--
-- Semantic and procedural memories may be deleted; they are derived claims and
-- can be re-derived from the archive. An episodic memory is the system's record
-- that something occurred, so deletion is data loss rather than correction.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION forbid_episodic_delete() RETURNS trigger AS $$
BEGIN
    IF OLD.memory_system = 'episodic' THEN
        RAISE EXCEPTION
            'Episodic memory % cannot be deleted. The event happened; record a '
            'reinterpretation instead.', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER memories_episodic_no_delete
    BEFORE DELETE ON memories
    FOR EACH ROW EXECUTE FUNCTION forbid_episodic_delete();

-- ---------------------------------------------------------------------------
-- 3. Consolidation preserves its members
--
-- consolidated_into_id is a pointer, not a tombstone. An episodic memory that
-- has been folded into a semantic generalization is still fully valid and still
-- retrievable on its own. The 004 CHECK already forces valid_to IS NULL for
-- episodic rows, so this makes the intent explicit and enforces the other half:
-- a memory cannot be consolidated into itself, and the target must not be episodic.
-- ---------------------------------------------------------------------------

ALTER TABLE memories ADD CONSTRAINT consolidation_target_not_self CHECK (
    consolidated_into_id IS NULL OR consolidated_into_id <> id
);

CREATE OR REPLACE FUNCTION check_consolidation_target() RETURNS trigger AS $$
DECLARE
    target_system memory_system;
BEGIN
    IF NEW.consolidated_into_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT memory_system INTO target_system
      FROM memories WHERE id = NEW.consolidated_into_id;
    IF target_system = 'episodic' THEN
        RAISE EXCEPTION
            'Consolidation target % is episodic. Consolidation produces a semantic '
            'or procedural generalization, never another episode.', NEW.consolidated_into_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER memories_consolidation_target
    BEFORE INSERT OR UPDATE OF consolidated_into_id ON memories
    FOR EACH ROW EXECUTE FUNCTION check_consolidation_target();

-- ---------------------------------------------------------------------------
-- 4. Staleness is a semantic and procedural concept only
--
-- The vector projection carries an is_stale payload flag used for filtering.
-- Nothing in the database could stop the projector writing is_stale = true on an
-- episodic point, so the rule is recorded here as the authority the projector
-- reads, and asserted in tests/invariants.sql and tests/test_dynamics.py.
-- ---------------------------------------------------------------------------

COMMENT ON CONSTRAINT episodic_not_superseded ON memories IS
    'Episodic memories are never superseded and never expire. valid_to and '
    'superseded_by must both be NULL. Reinterpretation goes through '
    'reinterprets_id on a new row. The vector projection must never set '
    'is_stale = true for a point whose source memory is episodic.';

COMMENT ON COLUMN memories.consolidated_into_id IS
    'Pointer to a semantic or procedural generalization derived from this memory. '
    'Does not invalidate this row and does not permit its deletion.';

COMMIT;
