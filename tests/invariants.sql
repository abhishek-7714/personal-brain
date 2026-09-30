-- tests/invariants.sql
--
-- Executable verification suite for the Personal Brain foundation.
-- Run after migrations 001-005:
--
--     psql "$PG_DSN" -v ON_ERROR_STOP=1 -f tests/invariants.sql
--
-- Everything runs inside one transaction and is rolled back at the end, so the
-- store is left exactly as empty as it started. No real history is ingested.
--
-- Convention: a check that should FAIL is wrapped in a sub-block that catches
-- check_violation or unique_violation. If the statement unexpectedly succeeds,
-- the block raises P0001 (raise_exception), which is not caught and aborts the run.

\set ON_ERROR_STOP on
\timing off

BEGIN;

DO $suite$
DECLARE
    v_owner        uuid;
    v_owner_ent    uuid;
    v_priya        uuid;
    v_phynx        uuid;
    v_exp          uuid;
    v_obs          uuid;
    v_obs2         uuid;
    v_epi          uuid;
    v_trace        uuid;
    v_mem_ep       uuid;
    v_mem_ep2      uuid;
    v_mem_sem      uuid;
    v_mem_proc     uuid;
    v_belief_self  uuid;
    v_belief_world uuid;
    v_assoc        uuid;
    v_trace_act    uuid;
    v_run          uuid;
    v_int          integer;
    v_bool         boolean;
    v_json         jsonb;
    v_txt          text;
    passed         integer := 0;
    v_reinf        bigint;
    v_expl         bigint;
    v_dec          bigint;
    v_epless       bigint;
    v_ctx          jsonb;
BEGIN

-- =========================================================================
RAISE NOTICE '--- SETUP ---';
-- =========================================================================

-- owner is a strict singleton (CHECK singleton = true, UNIQUE). If the
-- Brain Console (api/) or a prior manual seed already created the real
-- owner row, reuse it instead of inserting a second one -- safe here
-- specifically because this entire script is one transaction that is
-- ALWAYS rolled back at the end (see the ROLLBACK; at the bottom, and
-- Postgres's own implicit rollback on disconnect if an earlier check
-- aborts the script first), so nothing this block does to that row can
-- ever persist.
SELECT id INTO v_owner FROM owner LIMIT 1;
IF v_owner IS NULL THEN
    INSERT INTO owner (display_name, timezone) VALUES ('Abhishek', 'Asia/Kolkata')
        RETURNING id INTO v_owner;
END IF;

INSERT INTO entities (owner_id, kind, canonical_name, is_owner)
    VALUES (v_owner, 'person', 'Abhishek', true) RETURNING id INTO v_owner_ent;
UPDATE owner SET entity_id = v_owner_ent WHERE id = v_owner;

INSERT INTO entities (owner_id, kind, canonical_name)
    VALUES (v_owner, 'person', 'Priya') RETURNING id INTO v_priya;
INSERT INTO entities (owner_id, kind, canonical_name)
    VALUES (v_owner, 'project', 'Phynx Labs') RETURNING id INTO v_phynx;

INSERT INTO experiences (owner_id, kind, summary, content_hash, occurred_at)
    VALUES (v_owner, 'conversation', 'synthetic fixture', 'hash-fixture-001', now() - interval '10 days')
    RETURNING id INTO v_exp;

INSERT INTO observations (experience_id, content, epistemic_status, epistemic_owner,
                          extraction_confidence, source_reliability, span, observed_at)
    VALUES (v_exp, 'I think this startup will work', 'asserted', 'owner',
            0.95, 1.0, '{"start": 0, "end": 30}'::jsonb, now() - interval '10 days')
    RETURNING id INTO v_obs;

INSERT INTO observations (experience_id, content, epistemic_status, epistemic_owner,
                          extraction_confidence, source_reliability, span, observed_at)
    VALUES (v_exp, 'filing shows the market contracted 40%', 'reported', 'authoritative_source',
            0.9, 0.95, '{"start": 31, "end": 70}'::jsonb, now() - interval '3 days')
    RETURNING id INTO v_obs2;

INSERT INTO episodes (owner_id, kind, title, started_at)
    VALUES (v_owner, 'decision', 'Whether to keep building', now() - interval '10 days')
    RETURNING id INTO v_epi;

INSERT INTO episode_experiences (episode_id, experience_id) VALUES (v_epi, v_exp);

RAISE NOTICE 'setup complete';

-- =========================================================================
RAISE NOTICE '';
RAISE NOTICE '=== 1. OWNER MODEL ===';
-- =========================================================================

-- 1.1 exactly one owner
SELECT count(*) INTO v_int FROM owner;
IF v_int <> 1 THEN RAISE EXCEPTION 'FAIL 1.1: expected 1 owner row, found %', v_int; END IF;
RAISE NOTICE 'PASS 1.1  exactly one owner row'; passed := passed + 1;

-- 1.2 a second owner is rejected by the singleton constraint
BEGIN
    INSERT INTO owner (display_name) VALUES ('Someone Else');
    RAISE EXCEPTION 'FAIL 1.2: a second owner row was accepted';
EXCEPTION WHEN unique_violation OR check_violation THEN
    RAISE NOTICE 'PASS 1.2  second owner rejected'; passed := passed + 1;
END;

-- 1.3 owner is representable as an entity, and that entity is flagged
SELECT o.entity_id = e.id AND e.is_owner
  INTO v_bool
  FROM owner o JOIN entities e ON e.id = o.entity_id;
IF NOT COALESCE(v_bool, false) THEN RAISE EXCEPTION 'FAIL 1.3: owner is not linked to an owner entity'; END IF;
RAISE NOTICE 'PASS 1.3  owner linked to an entity with is_owner = true'; passed := passed + 1;

-- 1.4 only one entity may be the owner
BEGIN
    UPDATE entities SET is_owner = true WHERE id = v_priya;
    RAISE EXCEPTION 'FAIL 1.4: a second owner entity was accepted';
EXCEPTION WHEN unique_violation THEN
    RAISE NOTICE 'PASS 1.4  second owner entity rejected'; passed := passed + 1;
END;

-- 1.5 OWNER WORKS_WITH Priya is representable in the canonical relationship table
INSERT INTO relationships (source_entity_id, target_entity_id, rel_type, confidence, epistemic_owner)
    VALUES (v_owner_ent, v_priya, 'WORKS_WITH', 0.9, 'owner');
SELECT count(*) INTO v_int FROM relationships
  WHERE source_entity_id = v_owner_ent AND target_entity_id = v_priya AND rel_type = 'WORKS_WITH';
IF v_int <> 1 THEN RAISE EXCEPTION 'FAIL 1.5: OWNER WORKS_WITH Priya not representable'; END IF;
RAISE NOTICE 'PASS 1.5  OWNER WORKS_WITH Priya representable'; passed := passed + 1;

-- 1.7 owner.entity_id cannot reference an entity that does not carry is_owner = true
--     (006 trigger: owner_entity_must_be_owner)
BEGIN
    UPDATE owner SET entity_id = v_priya WHERE id = v_owner;
    RAISE EXCEPTION 'FAIL 1.7: owner.entity_id accepted a non-owner entity';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 1.7  owner.entity_id rejected when target is_owner = false'; passed := passed + 1;
END;

-- 1.8 clearing is_owner on the entity that owner.entity_id references is blocked
--     (006 trigger: entities_owner_flag_immutable)
BEGIN
    UPDATE entities SET is_owner = false WHERE id = v_owner_ent;
    RAISE EXCEPTION 'FAIL 1.8: is_owner was cleared on the entity owner references';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 1.8  is_owner cannot be cleared while owner.entity_id references the entity'; passed := passed + 1;
END;

-- 1.6 other people are not independent memory owners: memories.owner_id only
--     resolves to the singleton, so a memory owned by Priya is impossible
BEGIN
    INSERT INTO memories (owner_id, memory_system, kind, content)
        VALUES (v_priya, 'semantic', 'fact', 'Priya-owned memory');
    RAISE EXCEPTION 'FAIL 1.6: a non-owner entity was accepted as a memory owner';
EXCEPTION WHEN foreign_key_violation THEN
    RAISE NOTICE 'PASS 1.6  other people cannot own memories'; passed := passed + 1;
END;

-- =========================================================================
RAISE NOTICE '';
RAISE NOTICE '=== 2. MEMORY SYSTEMS ===';
-- =========================================================================

INSERT INTO memories (owner_id, memory_system, kind, content, episode_id, salience)
    VALUES (v_owner, 'episodic', 'event', 'Demo crashed during the Phynx pitch', v_epi, 0.8)
    RETURNING id INTO v_mem_ep;

INSERT INTO memories (owner_id, memory_system, kind, content, salience)
    VALUES (v_owner, 'semantic', 'person_attribute', 'Priya leads the platform team', 0.6)
    RETURNING id INTO v_mem_sem;

INSERT INTO memories (owner_id, memory_system, kind, content, proficiency, last_practiced_at)
    VALUES (v_owner, 'procedural', 'tool_use', 'Bootstrapping Temporal with auto-setup', 0.7, now())
    RETURNING id INTO v_mem_proc;

RAISE NOTICE 'PASS 2.0  all three memory systems accept rows'; passed := passed + 1;

-- 2.1 kind must belong to its system
BEGIN
    INSERT INTO memories (owner_id, memory_system, kind, content)
        VALUES (v_owner, 'episodic', 'person_attribute', 'wrong system for this kind');
    RAISE EXCEPTION 'FAIL 2.1: a semantic kind was accepted under episodic';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 2.1  kind/system mismatch rejected'; passed := passed + 1;
END;

-- 2.2 episodic cannot be superseded
BEGIN
    UPDATE memories SET superseded_by = v_mem_sem WHERE id = v_mem_ep;
    RAISE EXCEPTION 'FAIL 2.2: an episodic memory was superseded';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 2.2  episodic memory cannot be superseded'; passed := passed + 1;
END;

-- 2.3 episodic cannot be marked stale (valid_to)
BEGIN
    UPDATE memories SET valid_to = now() WHERE id = v_mem_ep;
    RAISE EXCEPTION 'FAIL 2.3: an episodic memory was expired';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 2.3  episodic memory cannot be expired'; passed := passed + 1;
END;

-- 2.4 the reclassification bypass is closed (005)
BEGIN
    UPDATE memories SET memory_system = 'semantic' WHERE id = v_mem_ep;
    RAISE EXCEPTION 'FAIL 2.4: episodic memory was reclassified to semantic';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 2.4  memory_system is immutable, reclassification bypass closed'; passed := passed + 1;
END;

-- 2.5 episodic cannot be deleted (005)
BEGIN
    DELETE FROM memories WHERE id = v_mem_ep;
    RAISE EXCEPTION 'FAIL 2.5: an episodic memory was deleted';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 2.5  episodic memory cannot be deleted'; passed := passed + 1;
END;

-- 2.6 episodic CAN be reinterpreted: a new memory points back at the original
INSERT INTO memories (owner_id, memory_system, kind, content, reinterprets_id)
    VALUES (v_owner, 'episodic', 'event',
            'The demo crash was a config error, not a model failure', v_mem_ep)
    RETURNING id INTO v_mem_ep2;
SELECT count(*) INTO v_int FROM memories WHERE reinterprets_id = v_mem_ep;
IF v_int <> 1 THEN RAISE EXCEPTION 'FAIL 2.6: reinterpretation not recorded'; END IF;
-- and the original is still fully valid
SELECT valid_to IS NULL AND superseded_by IS NULL INTO v_bool FROM memories WHERE id = v_mem_ep;
IF NOT v_bool THEN RAISE EXCEPTION 'FAIL 2.6: reinterpretation invalidated the original'; END IF;
RAISE NOTICE 'PASS 2.6  episodic reinterpretation works and preserves the original'; passed := passed + 1;

-- 2.7 semantic memories CAN go stale and be superseded
UPDATE memories SET valid_to = now(), superseded_by = v_mem_proc WHERE id = v_mem_sem;
SELECT valid_to IS NOT NULL INTO v_bool FROM memories WHERE id = v_mem_sem;
IF NOT v_bool THEN RAISE EXCEPTION 'FAIL 2.7: semantic memory could not be expired'; END IF;
RAISE NOTICE 'PASS 2.7  semantic memory can be expired and superseded'; passed := passed + 1;
UPDATE memories SET valid_to = NULL, superseded_by = NULL WHERE id = v_mem_sem;

-- 2.8 procedural is distinct: proficiency is exclusive to it
BEGIN
    INSERT INTO memories (owner_id, memory_system, kind, content, proficiency)
        VALUES (v_owner, 'semantic', 'fact', 'proficiency does not apply here', 0.5);
    RAISE EXCEPTION 'FAIL 2.8: proficiency accepted on a non-procedural memory';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 2.8  proficiency is exclusive to procedural memory'; passed := passed + 1;
END;

-- 2.9 there is no memory system meaning "something I learned", and no belief or
--     cognitive-pattern memory system
SELECT count(*) INTO v_int FROM unnest(enum_range(NULL::memory_system)) x
  WHERE x::text IN ('lesson', 'belief', 'cognitive', 'cognitive_pattern');
IF v_int <> 0 THEN RAISE EXCEPTION 'FAIL 2.9: forbidden value present in memory_system'; END IF;
SELECT count(*) INTO v_int FROM unnest(enum_range(NULL::memory_kind)) x
  WHERE x::text IN ('lesson', 'belief', 'mental_model', 'cognitive_pattern', 'pattern');
IF v_int <> 0 THEN RAISE EXCEPTION 'FAIL 2.9: forbidden value present in memory_kind'; END IF;
RAISE NOTICE 'PASS 2.9  no learned/belief/cognitive kinds; beliefs and patterns have one home only'; passed := passed + 1;

-- =========================================================================
RAISE NOTICE '';
RAISE NOTICE '=== 3. EPISTEMICS ===';
-- =========================================================================

-- 3.1 the two axes are independently representable
INSERT INTO beliefs (owner_id, proposition, belief_kind, domain,
                     log_odds, confidence, holding_log_odds, holding_confidence)
    VALUES (v_owner, 'This startup will work', 'world_claim', 'phynx',
            0.31, 0.578, 1.046, 0.740)
    RETURNING id INTO v_belief_world;

INSERT INTO beliefs (owner_id, proposition, belief_kind,
                     log_odds, confidence, holding_log_odds, holding_confidence)
    VALUES (v_owner, 'I prefer thesis-first writing', 'self_report',
            1.046, 0.740, 1.046, 0.740)
    RETURNING id INTO v_belief_self;

SELECT confidence <> holding_confidence INTO v_bool FROM beliefs WHERE id = v_belief_world;
IF NOT v_bool THEN RAISE EXCEPTION 'FAIL 3.1: truth and holding confidence are not independent'; END IF;
RAISE NOTICE 'PASS 3.1  holding confidence and truth confidence stored independently'; passed := passed + 1;

-- 3.2 owner says startup will work: high holding, moderate truth
SELECT holding_confidence > confidence INTO v_bool FROM beliefs WHERE id = v_belief_world;
IF NOT v_bool THEN RAISE EXCEPTION 'FAIL 3.2: owner assertion did not favour holding over truth'; END IF;
RAISE NOTICE 'PASS 3.2  owner world claim: holding > truth'; passed := passed + 1;

-- 3.3 owner reports own preference: the two axes coincide
SELECT abs(holding_confidence - confidence) < 0.001 INTO v_bool FROM beliefs WHERE id = v_belief_self;
IF NOT v_bool THEN RAISE EXCEPTION 'FAIL 3.3: self_report axes diverged'; END IF;
RAISE NOTICE 'PASS 3.3  self report: holding = truth'; passed := passed + 1;

-- 3.4 authoritative source makes a world claim: truth moves, holding barely does
INSERT INTO beliefs (owner_id, proposition, belief_kind,
                     log_odds, confidence, holding_log_odds, holding_confidence)
    VALUES (v_owner, 'The addressable market contracted 40% last year', 'world_claim',
            0.940, 0.719, 0.052, 0.513);
SELECT confidence > holding_confidence INTO v_bool FROM beliefs
  WHERE proposition = 'The addressable market contracted 40% last year';
IF NOT v_bool THEN RAISE EXCEPTION 'FAIL 3.4: authoritative source did not favour truth over holding'; END IF;
RAISE NOTICE 'PASS 3.4  authoritative world claim: truth > holding'; passed := passed + 1;

-- 3.5 THE KEY CASE: strongly held, contradicted by evidence, still active
UPDATE beliefs
   SET holding_log_odds = 0.895, holding_confidence = 0.710,
       log_odds = -2.360,        confidence = 0.086,
       status = 'active', last_evidence_at = now()
 WHERE id = v_belief_world;

SELECT status = 'active' AND holding_confidence > 0.6 AND confidence < 0.15
  INTO v_bool FROM beliefs WHERE id = v_belief_world;
IF NOT v_bool THEN
    RAISE EXCEPTION 'FAIL 3.5: a firmly held, evidentially contradicted belief could not be represented';
END IF;
RAISE NOTICE 'PASS 3.5  belief remains ACTIVE with holding 0.710 and world-truth 0.086'; passed := passed + 1;

-- 3.6 both axes leave an append-only trajectory row
INSERT INTO belief_versions (belief_id, version, status, confidence, log_odds,
                             holding_confidence, holding_log_odds, delta, axis, reason)
    VALUES (v_belief_world, 1, 'active', 0.086, -2.360, 0.710, 0.895, -2.05, 'both',
            'authoritative contradiction; belief still held');
BEGIN
    UPDATE belief_versions SET confidence = 0.5 WHERE belief_id = v_belief_world;
    RAISE EXCEPTION 'FAIL 3.6: belief_versions is not append-only';
EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE 'FAIL 3.6%' THEN RAISE; END IF;
    RAISE NOTICE 'PASS 3.6  belief_versions is append-only'; passed := passed + 1;
END;

-- 3.7 observations separate extraction confidence from source reliability
SELECT count(*) INTO v_int FROM information_schema.columns
 WHERE table_name = 'observations'
   AND column_name IN ('extraction_confidence', 'source_reliability');
IF v_int <> 2 THEN RAISE EXCEPTION 'FAIL 3.7: observation confidence axes not separated'; END IF;
SELECT count(*) INTO v_int FROM information_schema.columns
 WHERE table_name = 'observations' AND column_name IN ('confidence', 'reliability');
IF v_int <> 0 THEN RAISE EXCEPTION 'FAIL 3.7: legacy conflated columns still present'; END IF;
RAISE NOTICE 'PASS 3.7  extraction_confidence and source_reliability are separate columns'; passed := passed + 1;

-- =========================================================================
RAISE NOTICE '';
RAISE NOTICE '=== 4. PROVENANCE ===';
-- =========================================================================

-- 4.1 derived memories carry evidence
INSERT INTO evidence (derived_type, derived_id, source_type, source_id, stance, weight)
    VALUES ('memory', v_mem_ep, 'observation', v_obs, 'supports', 1.0);
INSERT INTO evidence (derived_type, derived_id, source_type, source_id, stance, weight)
    VALUES ('belief', v_belief_world, 'observation', v_obs, 'supports', 1.0);
INSERT INTO evidence (derived_type, derived_id, source_type, source_id, stance, weight)
    VALUES ('belief', v_belief_world, 'observation', v_obs2, 'contradicts', 1.0);

SELECT supporting, (contradicting > 0) INTO v_reinf, v_bool
  FROM evidence_summary WHERE derived_type = 'belief' AND derived_id = v_belief_world;
IF v_reinf < 1 OR NOT v_bool THEN RAISE EXCEPTION 'FAIL 4.1: evidence summary incomplete'; END IF;
RAISE NOTICE 'PASS 4.1  derived objects carry supporting and contradicting evidence'; passed := passed + 1;

-- 4.2 cognitive trace steps cite an observation with a source span
INSERT INTO cognitive_traces (episode_id, seq, operation, target, observation_id, occurred_at)
    VALUES (v_epi, 1, 'identify_unknown', 'whether demand is real', v_obs, now() - interval '10 days')
    RETURNING id INTO v_trace;

SELECT (o.span IS NOT NULL) INTO v_bool
  FROM cognitive_traces t JOIN observations o ON o.id = t.observation_id
 WHERE t.id = v_trace;
IF NOT COALESCE(v_bool, false) THEN RAISE EXCEPTION 'FAIL 4.2: trace step has no source span'; END IF;

SELECT count(*) INTO v_int FROM cognitive_traces t
  LEFT JOIN observations o ON o.id = t.observation_id
 WHERE o.span IS NULL;
IF v_int <> 0 THEN RAISE EXCEPTION 'FAIL 4.2: % trace step(s) lack a source span', v_int; END IF;
RAISE NOTICE 'PASS 4.2  every cognitive trace step cites an observation with a span'; passed := passed + 1;

-- 4.3 association events retain context and trigger kind
INSERT INTO associations (owner_id, source_type, source_id, target_type, target_id,
                          context, strength, confidence, observation_count)
    VALUES (v_owner, 'entity', v_phynx, 'entity', v_priya, 'decision', 0.18, 0.14, 1)
    RETURNING id INTO v_assoc;

INSERT INTO association_events (association_id, episode_id, strength_before, strength_after,
                                context, episode_kind, source_kind, trigger_type, trigger_id, salience)
    VALUES (v_assoc, v_epi, 0.0, 0.18, 'decision', 'decision', 'observed', 'episode', v_epi, 0.8);

-- the case that matters: a reinforcement with NO episode at all
INSERT INTO association_events (association_id, episode_id, strength_before, strength_after,
                                context, episode_kind, source_kind, trigger_type, trigger_id)
    VALUES (v_assoc, NULL, 0.18, 0.33, 'conversation', NULL, 'explicit', 'observation', v_obs);

SELECT count(*) INTO v_int FROM association_events
 WHERE association_id = v_assoc AND context IS NOT NULL AND source_kind IS NOT NULL;
IF v_int <> 2 THEN RAISE EXCEPTION 'FAIL 4.3: association events lost context or source kind'; END IF;
RAISE NOTICE 'PASS 4.3  association events retain context, source kind and trigger'; passed := passed + 1;

-- 4.4 the breakdown query works with no episode present
SELECT reinforcement_count, explicit_statements, decision_episodes,
       episodeless_reinforcements, context_breakdown
  INTO v_reinf, v_expl, v_dec, v_epless, v_ctx
  FROM association_provenance
 WHERE association_id = v_assoc;

IF v_reinf <> 2 THEN RAISE EXCEPTION 'FAIL 4.4: expected 2 reinforcements, got %', v_reinf; END IF;
IF v_dec <> 1 THEN RAISE EXCEPTION 'FAIL 4.4: expected 1 decision episode, got %', v_dec; END IF;
IF v_expl <> 1 THEN RAISE EXCEPTION 'FAIL 4.4: expected 1 explicit statement, got %', v_expl; END IF;
IF v_epless <> 1 THEN RAISE EXCEPTION 'FAIL 4.4: episodeless reinforcement not counted (got %)', v_epless; END IF;
IF v_ctx IS NULL THEN RAISE EXCEPTION 'FAIL 4.4: context breakdown is null'; END IF;
IF (v_ctx->>'decision') <> '1' OR (v_ctx->>'conversation') <> '1' THEN
    RAISE EXCEPTION 'FAIL 4.4: context breakdown wrong: %', v_ctx;
END IF;
RAISE NOTICE 'PASS 4.4  provenance breakdown resolves without requiring an episode: %', v_ctx;
passed := passed + 1;

-- 4.5 raw history is immutable
BEGIN
    UPDATE experiences SET summary = 'rewritten' WHERE id = v_exp;
    RAISE EXCEPTION 'FAIL 4.5: experiences are mutable';
EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE 'FAIL 4.5%' THEN RAISE; END IF;
    RAISE NOTICE 'PASS 4.5  experiences are append-only'; passed := passed + 1;
END;

-- =========================================================================
RAISE NOTICE '';
RAISE NOTICE '=== 5. ACTIVATION ===';
-- =========================================================================

INSERT INTO activation_traces (owner_id, query_text, detected_intent, seed_entity_ids,
                               candidate_count, included_count, latency_ms)
    VALUES (v_owner, 'Should I keep building this?', 'decision_support',
            ARRAY[v_phynx]::uuid[], 3, 2, 412)
    RETURNING id INTO v_trace_act;

INSERT INTO activation_members (trace_id, object_type, object_id, channel, hops,
                                raw_score, final_score, rank, included, is_contradiction)
VALUES
    (v_trace_act, 'memory', v_mem_ep,       'semantic',  NULL, 0.72, 0.68, 1, true,  false),
    (v_trace_act, 'belief', v_belief_world, 'graph',     2,    0.41, 0.56, 2, true,  true),
    (v_trace_act, 'memory', v_mem_proc,     'situation', NULL, 0.19, 0.11, 3, false, false);

SELECT count(*) INTO v_int FROM activation_members WHERE trace_id = v_trace_act;
IF v_int <> 3 THEN RAISE EXCEPTION 'FAIL 5.1: activation members not recorded'; END IF;

SELECT count(DISTINCT channel) INTO v_int FROM activation_members WHERE trace_id = v_trace_act;
IF v_int <> 3 THEN RAISE EXCEPTION 'FAIL 5.1: channels not distinguished'; END IF;

SELECT count(*) INTO v_int FROM activation_members
 WHERE trace_id = v_trace_act AND included AND is_contradiction;
IF v_int <> 1 THEN RAISE EXCEPTION 'FAIL 5.1: contradiction inclusion not recorded'; END IF;

SELECT query_text INTO v_txt FROM activation_traces WHERE id = v_trace_act;
IF v_txt IS NULL THEN RAISE EXCEPTION 'FAIL 5.1: query text not recorded'; END IF;
RAISE NOTICE 'PASS 5.1  activation trace records query, candidates, channel, scores, inclusion'; passed := passed + 1;

-- 5.2 activation traces are append-only
BEGIN
    UPDATE activation_traces SET query_text = 'rewritten' WHERE id = v_trace_act;
    RAISE EXCEPTION 'FAIL 5.2: activation traces are mutable';
EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE 'FAIL 5.2%' THEN RAISE; END IF;
    RAISE NOTICE 'PASS 5.2  activation traces are append-only'; passed := passed + 1;
END;

-- =========================================================================
RAISE NOTICE '';
RAISE NOTICE '=== 6. CONSOLIDATION ===';
-- =========================================================================

INSERT INTO consolidation_runs (owner_id, kind, window_start, window_end, status)
    VALUES (v_owner, 'episodic_to_semantic', now() - interval '90 days', now(), 'running')
    RETURNING id INTO v_run;

INSERT INTO consolidation_candidates (run_id, proposed_content, proposed_kind, proposed_system,
                                      member_memory_ids, support_count, confidence)
    VALUES (v_run, 'Live demos on unvalidated configs tend to fail', 'heuristic', 'procedural',
            ARRAY[v_mem_ep, v_mem_ep2]::uuid[], 2, 0.61);

-- accept it: a new derived memory, with the episodic members pointing at it
INSERT INTO memories (owner_id, memory_system, kind, content, instance_count)
    VALUES (v_owner, 'procedural', 'heuristic',
            'Live demos on unvalidated configs tend to fail', 2)
    RETURNING id INTO v_mem_proc;

UPDATE memories SET consolidated_into_id = v_mem_proc WHERE id IN (v_mem_ep, v_mem_ep2);

-- 6.1 the episodic evidence survives consolidation intact
SELECT count(*) INTO v_int FROM memories
 WHERE id IN (v_mem_ep, v_mem_ep2)
   AND memory_system = 'episodic' AND valid_to IS NULL AND superseded_by IS NULL
   AND consolidated_into_id = v_mem_proc;
IF v_int <> 2 THEN RAISE EXCEPTION 'FAIL 6.1: consolidation damaged the episodic members'; END IF;
RAISE NOTICE 'PASS 6.1  episodic members survive consolidation, still valid, pointing at the generalization'; passed := passed + 1;

-- 6.2 consolidation cannot delete its members
BEGIN
    DELETE FROM memories WHERE id = v_mem_ep;
    RAISE EXCEPTION 'FAIL 6.2: a consolidated episodic member was deleted';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 6.2  consolidated episodic members cannot be deleted'; passed := passed + 1;
END;

-- 6.3 a consolidation target may not itself be episodic
BEGIN
    UPDATE memories SET consolidated_into_id = v_mem_ep2 WHERE id = v_mem_sem;
    RAISE EXCEPTION 'FAIL 6.3: an episodic memory was accepted as a consolidation target';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 6.3  consolidation target cannot be episodic'; passed := passed + 1;
END;

-- 6.4 situation_discovery remains available but deferred: the run kind exists,
--     and no situations have been created by any automated process
SELECT count(*) INTO v_int FROM pg_constraint
 WHERE conname LIKE '%consolidation_runs_kind%'
   AND pg_get_constraintdef(oid) LIKE '%situation_discovery%';
IF v_int < 1 THEN RAISE EXCEPTION 'FAIL 6.4: situation_discovery run kind is missing'; END IF;
SELECT count(*) INTO v_int FROM situations;
IF v_int <> 0 THEN RAISE EXCEPTION 'FAIL 6.4: situations exist; clustering should still be deferred'; END IF;
RAISE NOTICE 'PASS 6.4  situation_discovery reserved and deferred, zero situations created'; passed := passed + 1;

-- =========================================================================
RAISE NOTICE '';
RAISE NOTICE '==========================================';
RAISE NOTICE '  ALL % INVARIANT CHECKS PASSED (expect 36)', passed;
RAISE NOTICE '==========================================';
-- =========================================================================

END
$suite$;

-- Leave the store exactly as it was found. No real history has been ingested.
ROLLBACK;
