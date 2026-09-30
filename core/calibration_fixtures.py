"""
core/calibration_fixtures.py

Shared calibration fixtures. Both tests/test_retrieval.py and the Brain
Console's Calibration page (api/routers/calibration.py) import from here,
so there is exactly one definition of each fixture's text and mock LLM
JSON -- the console's "click to ingest" and the automated calibration
suite exercise identical inputs.

Each fixture is a self-contained text -> mock-LLM-JSON pair, structured
exactly like the JSON core.extractor.validate_extraction_response() expects
(see core/extractor.py's _JSON_SCHEMA). Together they cover the ingredients
tests/test_retrieval.py's ten calibration scenarios need: an entity-linked
memory with subject/context role variants, an entity-free semantic memory,
an attributed belief, a low-evidence-quality belief, a contradiction
candidate, and an old episodic memory paired with a newer semantic
generalization for consolidation testing. (Current-state and
cognitive-pattern scenarios are seeded via direct SQL in the test file, not
text ingestion, so they aren't fixtures here.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CalibrationFixture:
    key: str
    label: str
    description: str
    text: str
    source_type: str
    mock_json: str
    occurred_at_offset_days: Optional[int] = None  # None = now


def _obs(text: str, epistemic_status: str = "asserted", epistemic_owner: str = "owner",
         confidence: float = 0.9, reliability: float = 1.0) -> dict:
    return {
        "content": text, "epistemic_status": epistemic_status, "epistemic_owner": epistemic_owner,
        "span": text[0:len(text)],
        "extraction_confidence": confidence, "source_reliability": reliability,
    }


_TEXT_A = (
    "Priya raised concerns about the Kubernetes rollout strategy. "
    "Kubernetes clusters need resource limits to run smoothly. "
    "Had lunch with Priya to discuss team logistics."
)
_FIXTURE_A = CalibrationFixture(
    key="priya_kubernetes_mixed",
    label="Priya + Kubernetes (mixed)",
    description=(
        "Semantic-only relevance (an entity-free fact), graph subject vs. "
        "context role weighting (two memories linked to the same entity, "
        "different roles), and an attributed belief."
    ),
    text=_TEXT_A, source_type="note",
    mock_json=json.dumps({
        "observations": [_obs(_TEXT_A, "reported", "other_person")],
        "entities": [{"canonical_name": "Priya", "kind": "person",
                       "mention_span": _TEXT_A[0:5]}],
        "memories": [
            {"memory_system": "episodic", "kind": "exchange",
             "content": "Priya raised concerns about the Kubernetes rollout strategy.",
             "salience": 0.6, "emotion_is_stated": False,
             "evidence_span": _TEXT_A[0:len(_TEXT_A)],
             "involved_entities": [{"canonical_name": "Priya", "role": "subject"}]},
            {"memory_system": "semantic", "kind": "fact",
             "content": "Kubernetes clusters need resource limits to run smoothly.",
             "salience": 0.5, "emotion_is_stated": False,
             "evidence_span": _TEXT_A[0:len(_TEXT_A)]},
            {"memory_system": "episodic", "kind": "encounter",
             "content": "Had lunch with Priya to discuss team logistics.",
             "salience": 0.4, "emotion_is_stated": False,
             "evidence_span": _TEXT_A[0:len(_TEXT_A)],
             "involved_entities": [{"canonical_name": "Priya", "role": "context"}]},
        ],
        "beliefs": [{
            "proposition": "Our Kubernetes rollout strategy needs review",
            "belief_kind": "attributed", "epistemic_owner": "other_person",
            "about_entity": "Priya", "domain": "deployment",
            "holding_confidence": 0.7, "truth_confidence": 0.8,
            "evidence_span": _TEXT_A[0:len(_TEXT_A)],
        }],
        "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
    }),
)

_TEXT_B = "Kubernetes autoscaling reliably prevents outages."
_FIXTURE_B = CalibrationFixture(
    key="weak_evidence_belief",
    label="Weak-evidence belief",
    description=(
        "Semantically on-topic but backed by a low-confidence, low-reliability "
        "observation -- shows evidence_quality pulling final score down "
        "despite a high truth_confidence on the belief itself."
    ),
    text=_TEXT_B, source_type="note",
    mock_json=json.dumps({
        "observations": [_obs(_TEXT_B, "hypothesis", "owner", confidence=0.3, reliability=0.3)],
        "entities": [], "memories": [],
        "beliefs": [{
            "proposition": "Kubernetes autoscaling reliably prevents outages",
            "belief_kind": "world_claim", "epistemic_owner": "owner", "domain": "infrastructure",
            "holding_confidence": 0.6, "truth_confidence": 0.9,
            "evidence_span": _TEXT_B[0:len(_TEXT_B)],
        }],
        "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
    }),
)

_TEXT_C = "Our deployment pipeline is fully automated end to end."
_FIXTURE_C = CalibrationFixture(
    key="contradiction_candidate",
    label="Contradiction candidate belief",
    description=(
        "Ingest this, then mark it contradicted directly in Postgres "
        "(UPDATE beliefs SET status='contradicted' WHERE ...) to see the "
        "contradiction-reserve behavior in the Retrieval Playground."
    ),
    text=_TEXT_C, source_type="note",
    mock_json=json.dumps({
        "observations": [_obs(_TEXT_C)],
        "entities": [], "memories": [],
        "beliefs": [{
            "proposition": "Our deployment pipeline is fully automated end to end",
            "belief_kind": "world_claim", "epistemic_owner": "owner", "domain": "infrastructure",
            "holding_confidence": 0.8, "truth_confidence": 0.7,
            "evidence_span": _TEXT_C[0:len(_TEXT_C)],
        }],
        "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
    }),
)

_TEXT_D = "Debugged a Kubernetes networking outage that took the whole afternoon."
_FIXTURE_D = CalibrationFixture(
    key="old_episodic",
    label="Old episodic memory (400 days ago)",
    description=(
        "Backdated 400 days. Proves staleness never penalizes an episodic "
        "memory (it is hard-coded 0.0 regardless of age -- see "
        "migrations/005_episodic_invariants.sql) while recency naturally "
        "decays its temporal-channel score."
    ),
    text=_TEXT_D, source_type="note",
    mock_json=json.dumps({
        "observations": [_obs(_TEXT_D)],
        "entities": [],
        "memories": [{
            "memory_system": "episodic", "kind": "event", "content": _TEXT_D,
            "salience": 0.5, "emotion_is_stated": False,
            "evidence_span": _TEXT_D[0:len(_TEXT_D)],
        }],
        "beliefs": [], "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
    }),
    occurred_at_offset_days=-400,
)

_TEXT_E = "Kubernetes networking outages are usually caused by misconfigured CNI plugins."
_FIXTURE_E = CalibrationFixture(
    key="newer_generalization",
    label="Newer semantic generalization",
    description=(
        "Ingest after 'Old episodic memory', then in Postgres set "
        "memories.consolidated_into_id on the old episodic memory to this "
        "one's id -- proves consolidation changes retrieval priority "
        "without ever implying the episodic memory became false."
    ),
    text=_TEXT_E, source_type="note",
    mock_json=json.dumps({
        "observations": [_obs(_TEXT_E)],
        "entities": [],
        "memories": [{
            "memory_system": "semantic", "kind": "fact", "content": _TEXT_E,
            "salience": 0.6, "emotion_is_stated": False,
            "evidence_span": _TEXT_E[0:len(_TEXT_E)],
        }],
        "beliefs": [], "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
    }),
)

# ---------------------------------------------------------------------------
# Episode calibration fixtures (migrations/007_episode_model.sql). Each one
# is built to exercise one primary combination of episode features; overlap
# with a second feature is fine and expected (real episodes aren't clean),
# but each fixture's `description` names the ONE thing it's there to prove.
# ---------------------------------------------------------------------------

def _episode_obs(text: str) -> dict:
    return _obs(text)


_TEXT_F = (
    "I was debating whether to raise a seed round. I did not know our real "
    "burn rate. I asked the CFO for the numbers. He said we had 8 months "
    "left. I decided to raise now rather than wait. I emailed our lead "
    "investor that afternoon."
)
_FIXTURE_F = CalibrationFixture(
    key="episode_decision_unknown_to_decision",
    label="Decision episode: unknown -> information seeking -> decision",
    description="Full cognitive_traces sequence (identify_unknown -> seek_information -> decide), "
                 "a linked decision, and an action memory -- the canonical episode shape.",
    text=_TEXT_F, source_type="note",
    mock_json=json.dumps({
        "observations": [_episode_obs(_TEXT_F)],
        "entities": [{"canonical_name": "CFO", "kind": "person", "mention_span": _TEXT_F[0:3]}],
        "memories": [{
            "memory_system": "episodic", "kind": "event",
            "content": "Emailed the lead investor to start the fundraising process.",
            "salience": 0.6, "emotion_is_stated": False,
            "evidence_span": _TEXT_F[0:len(_TEXT_F)],
            "is_episode_action": True,
        }],
        "beliefs": [],
        "episode_candidate": {
            "kind": "decision", "title": "Deciding whether to raise a seed round now",
            "trigger": "Uncertainty about runway", "narrative": "Weighed raising now vs. waiting.",
            "resolution": "Decided to raise now.", "salience": 0.7,
            "outcome": None, "outcome_valence": None,
            "evidence_span": _TEXT_F[0:len(_TEXT_F)],
        },
        "cognitive_traces": [
            {"operation": "identify_unknown", "target": "actual burn rate", "observation_index": 0, "confidence": 0.8},
            {"operation": "seek_information", "target": "asked the CFO for the numbers", "observation_index": 0, "confidence": 0.8},
            {"operation": "decide", "target": "raise now rather than wait", "observation_index": 0, "confidence": 0.7},
        ],
        "episode_state_items": [],
        "decision": {
            "question": "Should we raise a seed round now or wait?",
            "options": [{"label": "raise now", "rationale": "runway is short"},
                        {"label": "wait", "rationale": "could get better terms later"}],
            "chosen_option": "raise now", "rationale": "8 months of runway is too tight to wait",
            "confidence_at_time": 0.6, "reversibility": "costly",
            "outcome": None, "outcome_valence": None, "reflection": None,
            "evidence_span": _TEXT_F[0:len(_TEXT_F)],
        },
        "ambiguous": False, "ambiguity_note": "",
    }),
)

_TEXT_G = (
    "I spent the afternoon learning how Kubernetes operators work. I read "
    "through the docs and then built a small toy operator to see "
    "reconciliation loops in action. By the end I could explain the "
    "reconciliation loop to someone else."
)
_FIXTURE_G = CalibrationFixture(
    key="episode_learning",
    label="Learning episode",
    description="episode_kind='learning', no decision, a procedural memory plus an action memory "
                 "(the toy operator actually built).",
    text=_TEXT_G, source_type="note",
    mock_json=json.dumps({
        "observations": [_episode_obs(_TEXT_G)],
        "entities": [],
        "memories": [
            {"memory_system": "procedural", "kind": "skill",
             "content": "Comfortable explaining how Kubernetes operator reconciliation loops work.",
             "salience": 0.6, "emotion_is_stated": False,
             "evidence_span": _TEXT_G[0:len(_TEXT_G)]},
            {"memory_system": "episodic", "kind": "event",
             "content": "Built a toy Kubernetes operator to see reconciliation loops in action.",
             "salience": 0.5, "emotion_is_stated": False,
             "evidence_span": _TEXT_G[0:len(_TEXT_G)],
             "is_episode_action": True},
        ],
        "beliefs": [],
        "episode_candidate": {
            "kind": "learning", "title": "Learning how Kubernetes operators work",
            "trigger": None, "narrative": "Read the docs, then built a toy operator to see it work.",
            "resolution": "Could explain the reconciliation loop by the end.", "salience": 0.6,
            "outcome": None, "outcome_valence": None,
            "evidence_span": _TEXT_G[0:len(_TEXT_G)],
        },
        "cognitive_traces": [
            {"operation": "seek_information", "target": "read the operator docs", "observation_index": 0, "confidence": 0.8},
            {"operation": "revise", "target": "understanding of reconciliation loops", "observation_index": 0, "confidence": 0.7},
        ],
        "episode_state_items": [],
        "decision": None,
        "ambiguous": False, "ambiguity_note": "",
    }),
)

_TEXT_H = (
    "Looking back on the failed launch, I used to think speed mattered more "
    "than user research. Now I think the opposite: without validating "
    "demand first, speed just gets you to failure faster."
)
_FIXTURE_H = CalibrationFixture(
    key="episode_reflection_belief_revision",
    label="Reflection episode with belief revision",
    description="episode_kind='reflection' triggering the existing belief-revision procedure "
                 "(classification.md §9/§10) -- a retracted observation, a new belief, both cited "
                 "to the reflection episode.",
    text=_TEXT_H, source_type="note",
    mock_json=json.dumps({
        "observations": [
            {"content": "I used to think speed mattered more than user research.",
             "epistemic_status": "retracted", "epistemic_owner": "owner",
             "span": _TEXT_H[26:26 + len("I used to think speed mattered more than user research.")],
             "extraction_confidence": 0.9, "source_reliability": 1.0},
            {"content": "Without validating demand first, speed just gets you to failure faster.",
             "epistemic_status": "asserted", "epistemic_owner": "owner",
             "span": _TEXT_H[0:len(_TEXT_H)],
             "extraction_confidence": 0.9, "source_reliability": 1.0},
        ],
        "entities": [],
        "memories": [],
        "beliefs": [
            {"proposition": "Validating demand before building matters more than speed",
             "belief_kind": "normative", "epistemic_owner": "owner", "domain": "product strategy",
             "holding_confidence": 0.85, "truth_confidence": 0.7,
             "evidence_span": _TEXT_H[0:len(_TEXT_H)]},
        ],
        "episode_candidate": {
            "kind": "reflection", "title": "Reflecting on why the launch failed",
            "trigger": "The launch failed", "narrative": "Reconsidered the speed-vs-research tradeoff.",
            "resolution": "Concluded that validating demand matters more than speed.", "salience": 0.6,
            "outcome": None, "outcome_valence": None,
            "evidence_span": _TEXT_H[0:len(_TEXT_H)],
        },
        "cognitive_traces": [
            {"operation": "reflect", "target": "why the launch failed", "observation_index": 0, "confidence": 0.7},
            {"operation": "revise", "target": "belief about speed vs. user research", "observation_index": 1, "confidence": 0.7},
        ],
        "episode_state_items": [],
        "decision": None,
        "ambiguous": False, "ambiguity_note": "",
    }),
)

_TEXT_I = (
    "My co-founder and I disagreed sharply about whether to bring on a new "
    "engineering hire immediately. He wanted to move fast, I wanted more "
    "runway first. We argued for twenty minutes and neither of us backed "
    "down, so we agreed to revisit it next week with actual numbers."
)
_FIXTURE_I = CalibrationFixture(
    key="episode_social_conflict",
    label="Social/conflict episode",
    description="episode_kind='conflict', a deferred (not decided) decision, and an entity "
                 "(co-founder) linked via an episode-scoped state item.",
    text=_TEXT_I, source_type="note",
    mock_json=json.dumps({
        "observations": [_episode_obs(_TEXT_I)],
        "entities": [{"canonical_name": "Co-founder", "kind": "person", "mention_span": _TEXT_I[3:14]}],
        "memories": [],
        "beliefs": [],
        "episode_candidate": {
            "kind": "conflict", "title": "Disagreement with co-founder about hiring timing",
            "trigger": "Co-founder wanted to hire immediately", "narrative": "Argued for twenty minutes, neither backed down.",
            "resolution": "Agreed to revisit next week with actual numbers.", "salience": 0.7,
            "outcome": None, "outcome_valence": None,
            "evidence_span": _TEXT_I[0:len(_TEXT_I)],
        },
        "cognitive_traces": [
            {"operation": "evaluate_option", "target": "hire immediately vs. wait for more runway", "observation_index": 0, "confidence": 0.6},
            {"operation": "defer", "target": "the hiring decision, until next week", "observation_index": 0, "confidence": 0.7},
        ],
        "episode_state_items": [
            {"kind": "uncertainty", "content": "Whether to hire an engineer immediately or wait for more runway",
             "about_entity": "Co-founder", "priority": 0.7, "activation": 0.8,
             "evidence_span": _TEXT_I[0:len(_TEXT_I)]},
        ],
        "decision": {
            "question": "Should we hire a new engineer immediately?",
            "options": [{"label": "hire now", "rationale": "co-founder wants to move fast"},
                        {"label": "wait", "rationale": "preserve runway"}],
            "chosen_option": None, "rationale": "No agreement reached yet",
            "confidence_at_time": None, "reversibility": "costly",
            "outcome": None, "outcome_valence": None, "reflection": None,
            "evidence_span": _TEXT_I[0:len(_TEXT_I)],
        },
        "ambiguous": False, "ambiguity_note": "",
    }),
)

_TEXT_J = (
    "I was brainstorming names for the new product. I came up with three "
    "candidates: Northwind, Fieldglass, and Anchorpoint. I liked Anchorpoint "
    "best because it suggested stability without sounding boring."
)
_FIXTURE_J = CalibrationFixture(
    key="episode_creative_ideation",
    label="Creative/ideation episode",
    description="episode_kind='creative', generate_option/evaluate_option traces, no formal decision "
                 "-- a lightweight brainstorm resolved in the episode's own resolution field.",
    text=_TEXT_J, source_type="note",
    mock_json=json.dumps({
        "observations": [_episode_obs(_TEXT_J)],
        "entities": [], "memories": [], "beliefs": [],
        "episode_candidate": {
            "kind": "creative", "title": "Brainstorming names for the new product",
            "trigger": None, "narrative": "Generated three candidate names.",
            "resolution": "Anchorpoint stood out as the leading candidate.", "salience": 0.5,
            "outcome": None, "outcome_valence": None,
            "evidence_span": _TEXT_J[0:len(_TEXT_J)],
        },
        "cognitive_traces": [
            {"operation": "generate_option", "target": "Northwind, Fieldglass, Anchorpoint", "observation_index": 0, "confidence": 0.7},
            {"operation": "evaluate_option", "target": "Anchorpoint -- suggests stability without sounding boring", "observation_index": 0, "confidence": 0.6},
        ],
        "episode_state_items": [],
        "decision": None,
        "ambiguous": False, "ambiguity_note": "",
    }),
)

_TEXT_K = (
    "My goal this week was to get our onboarding flow down to under two "
    "minutes. I recorded five test sessions and found the biggest drop-off "
    "was at the payment step."
)
_FIXTURE_K = CalibrationFixture(
    key="episode_explicit_goal",
    label="Episode with explicit goal",
    description="An episode-scoped state_item of kind='goal' -- never episodes.goal, which does "
                 "not exist by design.",
    text=_TEXT_K, source_type="note",
    mock_json=json.dumps({
        "observations": [_episode_obs(_TEXT_K)],
        "entities": [], "memories": [], "beliefs": [],
        "episode_candidate": {
            "kind": "problem_solving", "title": "Reducing onboarding time",
            "trigger": None, "narrative": "Recorded test sessions to find the drop-off point.",
            "resolution": "Found the biggest drop-off is at the payment step.", "salience": 0.6,
            "outcome": None, "outcome_valence": None,
            "evidence_span": _TEXT_K[0:len(_TEXT_K)],
        },
        "cognitive_traces": [
            {"operation": "seek_information", "target": "recorded five test sessions", "observation_index": 0, "confidence": 0.8},
            {"operation": "identify_unknown", "target": "where the drop-off happens", "observation_index": 0, "confidence": 0.7},
        ],
        "episode_state_items": [
            {"kind": "goal", "content": "Get the onboarding flow down to under two minutes",
             "priority": 0.8, "activation": 0.7,
             "evidence_span": _TEXT_K[0:len(_TEXT_K)]},
        ],
        "decision": None,
        "ambiguous": False, "ambiguity_note": "",
    }),
)

_TEXT_L = (
    "I was focused on the pricing page redesign, but then a customer "
    "reported a checkout bug that lost them money, so I dropped the "
    "pricing work and focused entirely on the bug for the rest of the day."
)
_FIXTURE_L = CalibrationFixture(
    key="episode_attention_shift",
    label="Episode with attention shift",
    description="Two episode-scoped state_items of kind='attention' -- the abandoned focus and "
                 "the new one -- showing a shift, not just a single standing item.",
    text=_TEXT_L, source_type="note",
    mock_json=json.dumps({
        "observations": [_episode_obs(_TEXT_L)],
        "entities": [], "memories": [], "beliefs": [],
        "episode_candidate": {
            "kind": "problem_solving", "title": "Dropping pricing work for an urgent checkout bug",
            "trigger": "Customer reported a checkout bug that lost them money",
            "narrative": "Shifted focus from the pricing redesign to the bug.",
            "resolution": "Spent the rest of the day on the checkout bug.", "salience": 0.7,
            "outcome": None, "outcome_valence": None,
            "evidence_span": _TEXT_L[0:len(_TEXT_L)],
        },
        "cognitive_traces": [
            {"operation": "commit", "target": "dropped the pricing work to focus on the bug", "observation_index": 0, "confidence": 0.8},
        ],
        "episode_state_items": [
            {"kind": "attention", "content": "Pricing page redesign", "priority": 0.3, "activation": 0.1,
             "evidence_span": _TEXT_L[0:len(_TEXT_L)]},
            {"kind": "attention", "content": "Checkout bug losing customers money", "priority": 0.9, "activation": 0.9,
             "evidence_span": _TEXT_L[0:len(_TEXT_L)]},
        ],
        "decision": None,
        "ambiguous": False, "ambiguity_note": "",
    }),
)

_TEXT_M = (
    "Our API latency spiked overnight. Before digging in, I guessed it was "
    "probably the new caching layer we deployed yesterday. I checked the "
    "metrics and confirmed the cache hit rate had collapsed to zero."
)
_FIXTURE_M = CalibrationFixture(
    key="episode_hypothesis",
    label="Episode with hypothesis",
    description="Exercises the new 'hypothesize' cognitive_op specifically, distinct from "
                 "identify_unknown and generate_option.",
    text=_TEXT_M, source_type="note",
    mock_json=json.dumps({
        "observations": [_episode_obs(_TEXT_M)],
        "entities": [], "memories": [], "beliefs": [],
        "episode_candidate": {
            "kind": "problem_solving", "title": "Diagnosing an overnight API latency spike",
            "trigger": "API latency spiked overnight", "narrative": "Guessed the cause, then checked metrics to confirm.",
            "resolution": "Confirmed the cache hit rate had collapsed to zero.", "salience": 0.7,
            "outcome": None, "outcome_valence": None,
            "evidence_span": _TEXT_M[0:len(_TEXT_M)],
        },
        "cognitive_traces": [
            {"operation": "hypothesize", "target": "the new caching layer deployed yesterday", "observation_index": 0, "confidence": 0.5},
            {"operation": "seek_information", "target": "checked the cache hit rate metrics", "observation_index": 0, "confidence": 0.8},
        ],
        "episode_state_items": [],
        "decision": None,
        "ambiguous": False, "ambiguity_note": "",
    }),
)

_TEXT_N = (
    "Three months ago I decided to switch our database from MongoDB to "
    "Postgres. It took six weeks longer than planned, but query "
    "performance improved dramatically and we haven't had a single data "
    "consistency bug since."
)
_FIXTURE_N = CalibrationFixture(
    key="episode_decision_with_outcome",
    label="Decision episode with outcome (decision.outcome canonical)",
    description="A decision WITH an outcome -- proves decision.outcome/outcome_valence are used "
                 "and episode_candidate.outcome stays null (the outcome invariant).",
    text=_TEXT_N, source_type="note",
    mock_json=json.dumps({
        "observations": [_episode_obs(_TEXT_N)],
        "entities": [], "memories": [], "beliefs": [],
        "episode_candidate": {
            "kind": "decision", "title": "Migrating the database from MongoDB to Postgres",
            "trigger": None, "narrative": "Decided to migrate three months ago.",
            "resolution": "Migration completed, six weeks late but successful.", "salience": 0.6,
            "outcome": None, "outcome_valence": None,  # MUST stay null -- decision.outcome is canonical
            "evidence_span": _TEXT_N[0:len(_TEXT_N)],
        },
        "cognitive_traces": [
            {"operation": "decide", "target": "migrate from MongoDB to Postgres", "observation_index": 0, "confidence": 0.8},
        ],
        "episode_state_items": [],
        "decision": {
            "question": "Should we migrate from MongoDB to Postgres?",
            "options": [{"label": "migrate to Postgres", "rationale": "better consistency guarantees"},
                        {"label": "stay on MongoDB", "rationale": "avoid migration risk"}],
            "chosen_option": "migrate to Postgres", "rationale": "Wanted stronger consistency guarantees",
            "confidence_at_time": 0.6, "reversibility": "costly",
            "outcome": "Took six weeks longer than planned, but query performance improved "
                       "dramatically with no data consistency bugs since.",
            "outcome_valence": 0.7,
            "reflection": "Worth the delay in hindsight.",
            "evidence_span": _TEXT_N[0:len(_TEXT_N)],
        },
        "ambiguous": False, "ambiguity_note": "",
    }),
)

_TEXT_O = (
    "I spent the sprint pairing with a junior engineer on our billing "
    "module. By the end of the week she was shipping changes to it on her "
    "own without needing review on every PR."
)
_FIXTURE_O = CalibrationFixture(
    key="episode_outcome_without_decision",
    label="Episode with outcome but no decision",
    description="No decision object at all -- proves episode_candidate.outcome/outcome_valence "
                 "get populated directly when there is nothing for a decision row to be canonical over.",
    text=_TEXT_O, source_type="note",
    mock_json=json.dumps({
        "observations": [_episode_obs(_TEXT_O)],
        "entities": [], "memories": [], "beliefs": [],
        "episode_candidate": {
            "kind": "learning", "title": "Pairing with a junior engineer on the billing module",
            "trigger": None, "narrative": "Paired for the sprint to build her familiarity with the module.",
            "resolution": "She was shipping changes independently by the end of the week.", "salience": 0.6,
            "outcome": "By the end of the week she was shipping changes to the billing module "
                       "independently, without needing review on every PR.",
            "outcome_valence": 0.8,
            "evidence_span": _TEXT_O[0:len(_TEXT_O)],
        },
        "cognitive_traces": [
            {"operation": "delegate", "target": "billing module changes, to the junior engineer", "observation_index": 0, "confidence": 0.6},
        ],
        "episode_state_items": [],
        "decision": None,
        "ambiguous": False, "ambiguity_note": "",
    }),
)


FIXTURES: list[CalibrationFixture] = [
    _FIXTURE_A, _FIXTURE_B, _FIXTURE_C, _FIXTURE_D, _FIXTURE_E,
    _FIXTURE_F, _FIXTURE_G, _FIXTURE_H, _FIXTURE_I, _FIXTURE_J,
    _FIXTURE_K, _FIXTURE_L, _FIXTURE_M, _FIXTURE_N, _FIXTURE_O,
]


def get_fixture(key: str) -> Optional[CalibrationFixture]:
    return next((f for f in FIXTURES if f.key == key), None)
