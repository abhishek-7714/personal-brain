-- 007_episode_model.sql
--
-- Completes the episode model so extraction can actually persist episodes,
-- not just validate a discarded candidate. Adds:
--
--   1. 'episode' to derived_object_type -- episodes can now receive evidence
--      rows (they could already be cited as a source; they could not
--      receive one). Prerequisite for provenance on episode-level fields.
--   2. 'hypothesize' to cognitive_op -- "proposed a candidate explanation,"
--      distinct from generate_option (decision options) and
--      identify_unknown (naming a gap, not filling it).
--   3. episodes.outcome / outcome_valence / outcome_recorded_at -- mirrors
--      decisions' own columns exactly (same names, same CHECK range).
--      Populated ONLY for episodes with no linked decision; see the
--      invariant documented on the CHECK constraint below and in
--      core/classification.md. decisions.outcome/reflection stay canonical
--      whenever a decision exists -- this migration does not touch decisions.
--   4. state_items.episode_id -- nullable FK. Lets a state_item (goal,
--      attention, question, uncertainty, constraint -- kinds that already
--      exist) optionally scope to one episode instead of always being
--      standing/owner-level state. NOT adding episodes.goal: goal is
--      represented exclusively as a state_item, never duplicated as a
--      second free-text field on episodes.
--
-- No new table. No existing column altered or dropped. No immutability
-- trigger touched -- episodes/state_items/decisions were already mutable
-- (episodes_touch/state_items_touch/decisions_touch just maintain
-- updated_at; there is no forbid_mutation trigger on any of them), so this
-- migration doesn't change what could already be edited, only what can be
-- recorded. experiences/observations/cognitive_traces keep their existing
-- immutability triggers untouched.
--
-- Inspected before writing this: migrations/002 (episodes, decisions,
-- state_items, cognitive_traces, derived_object_type/cognitive_op enums),
-- migrations/003 (all triggers -- confirmed episodes_touch/decisions_touch/
-- state_items_touch are UPDATE-only touch triggers, cognitive_traces_immutable
-- is the only immutability trigger among these four tables; confirmed
-- active_state view lists columns explicitly, not SELECT *, so it is
-- unaffected by the new state_items column; confirmed evidence_summary
-- groups by derived_type generically, unaffected by the new enum value).
--
-- NOTE: the ALTER TYPE ... ADD VALUE statements must run outside a
-- transaction block and cannot be used in the same transaction that
-- creates them, so they come first, unwrapped -- same constraint
-- migrations/004 already documented and worked around.

ALTER TYPE derived_object_type ADD VALUE IF NOT EXISTS 'episode';
ALTER TYPE cognitive_op ADD VALUE IF NOT EXISTS 'hypothesize';

-- ===========================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Episode-level outcome, for episodes with no linked decision
--
-- Invariant (enforced by convention in core/ingest.py + core/classification.md,
-- not by a DB constraint, to keep this migration minimal): if an episode has
-- a decisions row, decisions.outcome/decisions.reflection are canonical and
-- episodes.outcome stays NULL. Only an episode with no decision may have
-- episodes.outcome populated. A CHECK constraint could enforce "outcome
-- implies no decision," but that requires a cross-table lookup a CHECK
-- can't express without a trigger, and this migration deliberately adds no
-- new trigger; the invariant is enforced at write time by the one function
-- that populates it (core/ingest.py), the same way instance_count and other
-- application-level invariants already work in this codebase.
-- ---------------------------------------------------------------------------

ALTER TABLE episodes
    ADD COLUMN outcome             text,
    ADD COLUMN outcome_valence     double precision
                                      CHECK (outcome_valence >= -1 AND outcome_valence <= 1),
    ADD COLUMN outcome_recorded_at timestamptz;

COMMENT ON COLUMN episodes.outcome IS
    'Populated ONLY when this episode has no linked decisions row. If a '
    'decision exists, decisions.outcome is canonical and this stays NULL -- '
    'see core/classification.md.';

-- ---------------------------------------------------------------------------
-- 2. Episode-scoped state items (goals, attention, questions, uncertainty)
--
-- Nullable: existing (and most future) state_items remain owner-level
-- standing state, exactly as today. Setting episode_id scopes one to the
-- specific episode it arose in, reusing the existing state_item_kind enum
-- ('goal', 'attention', 'question', 'uncertainty', 'constraint', 'project')
-- rather than adding a parallel goal/attention representation on episodes.
-- ---------------------------------------------------------------------------

ALTER TABLE state_items
    ADD COLUMN episode_id uuid REFERENCES episodes(id);

CREATE INDEX idx_state_items_episode ON state_items (episode_id)
    WHERE episode_id IS NOT NULL;

COMMENT ON COLUMN state_items.episode_id IS
    'Optional: scopes this state_item to one episode (its goal, attention, '
    'question, or uncertainty during that specific cognitive situation) '
    'instead of standing owner-level state. NULL means standing state, '
    'exactly as before this column existed.';

COMMIT;
