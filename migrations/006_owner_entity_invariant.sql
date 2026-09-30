-- 006_owner_entity_invariant.sql
--
-- Closes a consistency gap identified in the audit: owner.entity_id was a plain
-- FK to entities(id) with no enforcement that the target carries is_owner = true.
-- tests/invariants.sql check 1.3 verified the relationship manually at setup time
-- but nothing prevented drifting out of sync afterward in two ways:
--
--   1. INSERT/UPDATE owner SET entity_id = <some non-owner entity>
--      The FK accepted it because the entity exists.
--   2. UPDATE entities SET is_owner = false WHERE id = <the entity owner points at>
--      Nothing blocked clearing the flag on the entity the owner references.
--
-- Both paths leave owner.entity_id pointing at an entity that does not carry the
-- is_owner flag, which breaks every downstream query that uses that flag to locate
-- the owner's node in the graph.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Forward direction: owner.entity_id must point at an is_owner entity.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION check_owner_entity_is_owner() RETURNS trigger AS $$
DECLARE
    v_is_owner boolean;
BEGIN
    IF NEW.entity_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT is_owner INTO v_is_owner FROM entities WHERE id = NEW.entity_id;
    IF NOT COALESCE(v_is_owner, false) THEN
        RAISE EXCEPTION
            'owner.entity_id must reference an entity with is_owner = true '
            '(entity % has is_owner = %)', NEW.entity_id, v_is_owner
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER owner_entity_must_be_owner
    BEFORE INSERT OR UPDATE OF entity_id ON owner
    FOR EACH ROW EXECUTE FUNCTION check_owner_entity_is_owner();

-- ---------------------------------------------------------------------------
-- 2. Reverse direction: clearing is_owner on the entity owner references is blocked.
--
-- Without this a two-step bypass survives: set entity_id legitimately, then clear
-- is_owner on the target. The FK stays intact; the invariant is silently violated.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION check_entity_owner_flag_not_cleared() RETURNS trigger AS $$
BEGIN
    IF OLD.is_owner = true AND NEW.is_owner = false THEN
        IF EXISTS (SELECT 1 FROM owner WHERE entity_id = OLD.id) THEN
            RAISE EXCEPTION
                'Cannot clear is_owner on entity % because owner.entity_id still '
                'references it. Reassign owner.entity_id first.', OLD.id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER entities_owner_flag_immutable
    BEFORE UPDATE OF is_owner ON entities
    FOR EACH ROW EXECUTE FUNCTION check_entity_owner_flag_not_cleared();

COMMIT;
