"""
core/calibration_assertions.py

Expected semantic assertions for every calibration fixture in
core/calibration_fixtures.py.

Code prefixes: `U-` is reserved for UNIVERSAL_ASSERTIONS; each fixture uses
its own letter, so no fixture code can collide with a universal one.

These are deliberately written from core/classification.md's rules and each
fixture's SOURCE TEXT -- not from the fixture's mock JSON. That distinction
is the whole point: if an assertion were derived from the mock JSON it would
be guaranteed to pass on the mock path and would tell us nothing. Written
from the rules instead, a mock-path failure is a real finding about the
hand-written fixture, and a live-path failure is a real finding about the
extractor.

Assertions are semantic, not structural: they say "must produce an episodic
memory" or "must not claim stated emotion", never "field X must equal
literal Y". Nonessential detail (wording, salience values, confidence
numbers, how many observations a passage was split into) is free to vary.

`essential=True`  -- a violation of an ontology rule or a stated requirement.
                      Counts as a FAIL.
`essential=False` -- a judgment call where more than one defensible
                      extraction exists. Counts as an ADVISORY: reported with
                      its actual value, never as a failure, because we do not
                      yet know which way the extractor *should* go and the
                      point of this pass is to look at real behaviour first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from core.calibration_view import CanonicalView

# The twelve evaluation dimensions.
DIM_EXPERIENCE = "experience_interpretation"
DIM_OBSERVATION = "observation_extraction"
DIM_EPISTEMIC = "epistemic_ownership"
DIM_BELIEF_KIND = "belief_kind"
DIM_MEMORY = "memory_system_kind"
DIM_ENTITY = "entity_resolution"
DIM_EPISODE = "episode_boundary"
DIM_STATE_ITEM = "state_item_extraction"
DIM_TRACE = "cognitive_trace"
DIM_DECISION = "decision_extraction"
DIM_PROVENANCE = "provenance"
DIM_OVER_INFERENCE = "over_inference"

DIMENSIONS = [
    DIM_EXPERIENCE, DIM_OBSERVATION, DIM_EPISTEMIC, DIM_BELIEF_KIND,
    DIM_MEMORY, DIM_ENTITY, DIM_EPISODE, DIM_STATE_ITEM, DIM_TRACE,
    DIM_DECISION, DIM_PROVENANCE, DIM_OVER_INFERENCE,
]


@dataclass(frozen=True)
class Assertion:
    code: str
    dimension: str
    description: str
    essential: bool
    check: Callable[[CanonicalView], "tuple[bool, str]"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# classification.md §8's own examples plus the obvious neighbours. Used only
# to detect emotion claimed where the text names none -- never to *add*
# emotion.
_EMOTION_WORDS = {
    "frustrated", "frustrating", "frustration", "excited", "exciting", "excitement",
    "devastated", "glad", "relieved", "relief", "nervous", "anxious", "anxiety",
    "enjoyed", "enjoy", "annoyed", "annoying", "sad", "sadness", "angry", "anger",
    "joy", "joyful", "happy", "happiness", "satisfied", "satisfaction", "proud",
    "pride", "overwhelmed", "ecstatic", "elated", "uncomfortable", "afraid",
    "fear", "scared", "worried", "worry", "delighted", "disappointed", "ashamed",
    "embarrassed", "grateful", "thrilled", "miserable", "upset", "stressed",
}

_STOPWORDS = {
    "the", "and", "that", "this", "with", "from", "have", "were", "was", "for",
    "not", "but", "then", "than", "them", "they", "there", "their", "what",
    "when", "which", "would", "could", "should", "about", "into", "over",
    "just", "more", "most", "some", "such", "only", "also", "been", "being",
    "does", "did", "done", "will", "your", "yours", "ours", "here", "very",
}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 3 and w not in _STOPWORDS}


def trace_grounding(view: CanonicalView) -> list[tuple[str, str, float]]:
    """(operation, target, grounding_ratio) per trace step.

    grounding_ratio = fraction of the trace target's content words that
    actually appear in the source text. classification.md §5: "If the
    estimation is not visible in the text, it did not happen as far as this
    system is concerned." A ratio of 0.0 means the extractor described a
    cognitive step using vocabulary found nowhere in the source -- the
    signature of invented cognition. This is a heuristic and is reported,
    never enforced.
    """
    text_words = _words(view.source_text)
    out = []
    for t in view.traces:
        target_words = _words(t.target)
        ratio = (len(target_words & text_words) / len(target_words)) if target_words else 1.0
        out.append((t.operation, t.target, ratio))
    return out


# ---------------------------------------------------------------------------
# Universal assertions -- applied to every fixture
# ---------------------------------------------------------------------------

def _u_memory_evidence(view: CanonicalView):
    missing = [m.content[:40] for m in view.memories if m.evidence_count == 0]
    return (not missing, f"{len(view.memories)} memories, {len(missing)} without evidence: {missing}")


def _u_belief_evidence(view: CanonicalView):
    missing = [b.proposition[:40] for b in view.beliefs if b.evidence_count == 0]
    return (not missing, f"{len(view.beliefs)} beliefs, {len(missing)} without evidence: {missing}")


def _u_trace_citation(view: CanonicalView):
    uncited = [t.operation for t in view.traces if not t.cites_observation]
    return (not uncited, f"{len(view.traces)} traces, uncited: {uncited}")


def _u_no_unstated_emotion(view: CanonicalView):
    text_words = _words(view.source_text) | set(
        re.findall(r"[a-z]+", (view.source_text or "").lower())
    )
    claimed = [m.content[:40] for m in view.memories if m.emotion_is_stated]
    if not claimed:
        return (True, "no memory claims stated emotion")
    named = _EMOTION_WORDS & text_words
    return (bool(named),
            f"{len(claimed)} memories claim stated emotion; emotion words in text: {sorted(named) or 'NONE'}")


def _u_episode_not_empty(view: CanonicalView):
    if view.episode is None:
        return (True, "no episode")
    # The episode threshold is observable cognitive engagement, not merely
    # the presence of an associated state item or decision-shaped container.
    grounded = bool(view.traces) and all(t.cites_observation for t in view.traces)
    return (grounded,
            f"episode '{view.episode.title[:40]}' has {len(view.traces)} "
            f"trace(s); every trace must cite an observation")


def _no_episode_state_kind(*kinds: str):
    def check(view: CanonicalView):
        found = [s.kind for s in view.state_items if s.kind in kinds]
        return (not found, f"forbidden state item kinds present: {found or 'none'}")
    return check


def _no_memory_kind(kind: str):
    def check(view: CanonicalView):
        found = [m.content[:35] for m in view.memories if m.kind == kind]
        return (not found, f"memories of kind '{kind}': {found or 'none'}")
    return check


def _episode_has_resolution_without_outcome(view: CanonicalView):
    resolution = view.episode.resolution if view.episode else None
    outcome = view.episode.outcome if view.episode else None
    return (bool(resolution) and outcome is None,
            f"resolution={resolution!r}; outcome={outcome!r}")


# An outcome restates its resolution when it adds almost nothing new. The
# measure is one-directional on purpose: an outcome is SUPPOSED to extend the
# resolution ("the test passed" -> "the test passed and cut latency in half"),
# so shared words are expected and only the share of NEW words matters. A
# symmetric overlap flagged exactly those valid extensions.
_MIN_NEW_CONTENT = 0.2


def _new_content_share(resolution: str, outcome: str) -> float:
    """Fraction of the outcome's content words absent from the resolution."""
    wr, wo = _words(resolution), _words(outcome)
    if not wo:
        return 0.0 if outcome.strip().lower() == resolution.strip().lower() else 1.0
    return len(wo - wr) / len(wo)


def _u_outcome_not_restated_resolution(view: CanonicalView):
    ep = view.episode
    if ep is None or not ep.outcome or not ep.resolution:
        return (True, "no outcome/resolution pair to compare")
    share = _new_content_share(ep.resolution, ep.outcome)
    return (share >= _MIN_NEW_CONTENT,
            f"outcome adds {share:.0%} new content words; resolution={ep.resolution!r}; outcome={ep.outcome!r}")


def _episode_valence_not_positive(view: CanonicalView):
    v = view.episode.outcome_valence if view.episode else None
    return (v is None or v <= 0, f"outcome_valence={v}")


def _u_outcome_invariant(view: CanonicalView):
    if view.episode is None:
        return (True, "no episode")
    if view.decisions and view.episode.outcome is not None:
        return (False, "decision present AND episode.outcome set -- violates the outcome invariant")
    return (True, f"decisions={len(view.decisions)} episode.outcome={'set' if view.episode.outcome else 'null'}")


def _u_belief_entity_resolves(view: CanonicalView):
    names = view.entity_names()
    dangling = [b.about_entity for b in view.beliefs
                if b.about_entity and b.about_entity.lower() not in names]
    return (not dangling, f"dangling about_entity refs: {dangling}" if dangling else "all about_entity refs resolve")


def _u_state_item_entity_resolves(view: CanonicalView):
    names = view.entity_names()
    dangling = [s.about_entity for s in view.state_items
                if s.about_entity and s.about_entity.lower() not in names]
    return (not dangling, f"dangling state_item about_entity: {dangling}" if dangling else "ok")


def _u_trace_grounding_advisory(view: CanonicalView):
    g = trace_grounding(view)
    if not g:
        return (True, "no traces")
    ungrounded = [(op, tgt) for op, tgt, r in g if r == 0.0]
    avg = sum(r for _, _, r in g) / len(g)
    return (not ungrounded,
            f"mean grounding={avg:.2f}; ungrounded targets={ungrounded}")


def _u_observation_present(view: CanonicalView):
    return (len(view.observations) >= 1, f"{len(view.observations)} observations")


def observation_coverage(view: CanonicalView) -> float:
    """Fraction of the source text covered by the union of observation spans.

    Low coverage means the extractor read only part of the experience and
    silently dropped the rest -- an experience-interpretation failure that
    no per-object assertion catches, because everything it *did* extract can
    be perfectly well-formed.
    """
    text_len = len(view.source_text or "")
    if not text_len:
        return 0.0
    covered = [False] * text_len
    for o in view.observations:
        if not o.span:
            continue
        start = max(0, int(o.span.get("start", 0)))
        end = min(text_len, int(o.span.get("end", 0)))
        for i in range(start, end):
            covered[i] = True
    return sum(covered) / text_len


def _u_experience_coverage(view: CanonicalView):
    cov = observation_coverage(view)
    return (cov >= 0.5, f"observation span coverage of source text = {cov:.0%}")


UNIVERSAL_ASSERTIONS: list[Assertion] = [
    Assertion("U-OBS-1", DIM_OBSERVATION, "At least one observation extracted", True, _u_observation_present),
    Assertion("U-EXP-1", DIM_EXPERIENCE, "Observations cover at least half the source text (the whole experience was read)", False, _u_experience_coverage),
    Assertion("U-PROV-1", DIM_PROVENANCE, "Every memory carries evidence", True, _u_memory_evidence),
    Assertion("U-PROV-2", DIM_PROVENANCE, "Every belief carries evidence", True, _u_belief_evidence),
    Assertion("U-PROV-3", DIM_PROVENANCE, "Every cognitive trace cites an observation", True, _u_trace_citation),
    Assertion("U-OVR-1", DIM_OVER_INFERENCE, "No stated emotion claimed unless the text names an emotion", True, _u_no_unstated_emotion),
    Assertion("U-OVR-2", DIM_EPISODE, "An episode, if emitted, is not an empty shell", True, _u_episode_not_empty),
    Assertion("U-EPI-1", DIM_DECISION, "Outcome invariant: decision present => episode.outcome NULL", True, _u_outcome_invariant),
    # Advisory: word overlap cannot decide classification.md §10's semantic
    # "would swapping lose anything?" test, and a live model's paraphrases
    # would make it flaky. The essential signal comes from paired fixtures
    # (G-OUT-1, V-OUT-1, X-OUT-1) whose correct split is known.
    Assertion("U-EPI-2", DIM_EPISODE, "An episode outcome adds content beyond its resolution (advisory, lexical)", False, _u_outcome_not_restated_resolution),
    Assertion("U-ENT-1", DIM_ENTITY, "Every belief.about_entity resolves to a declared entity", True, _u_belief_entity_resolves),
    Assertion("U-ENT-2", DIM_ENTITY, "Every state_item.about_entity resolves to a declared entity", True, _u_state_item_entity_resolves),
    Assertion("U-OVR-3", DIM_OVER_INFERENCE, "Trace targets are lexically grounded in the source text (advisory)", False, _u_trace_grounding_advisory),
]


# ---------------------------------------------------------------------------
# Per-fixture assertion builders
# ---------------------------------------------------------------------------

def _a(code, dim, desc, essential, fn) -> Assertion:
    return Assertion(code, dim, desc, essential, fn)


def _no_episode(view: CanonicalView):
    return (view.episode is None, f"episode={'present: ' + view.episode.title[:40] if view.episode else 'none'}")


def _has_episode(view: CanonicalView):
    return (view.episode is not None, f"episode={'present' if view.episode else 'NONE'}")


def _episode_kind_in(kinds: set[str]):
    def check(view: CanonicalView):
        if view.episode is None:
            return (False, "no episode emitted")
        return (view.episode.kind in kinds, f"episode.kind={view.episode.kind} (expected one of {sorted(kinds)})")
    return check


def _has_memory_system(system: str):
    def check(view: CanonicalView):
        found = view.memories_of_system(system)
        return (bool(found), f"{system} memories: {[m.kind for m in found] or 'NONE'}")
    return check


def _no_memory_system(system: str):
    def check(view: CanonicalView):
        found = view.memories_of_system(system)
        return (not found, f"{system} memories: {[m.content[:35] for m in found] or 'none'}")
    return check


def _has_belief_kind(kinds: set[str]):
    def check(view: CanonicalView):
        found = [b for b in view.beliefs if b.belief_kind in kinds]
        return (bool(found),
                f"beliefs: {[(b.belief_kind, b.epistemic_owner) for b in view.beliefs] or 'NONE'} "
                f"(expected one of {sorted(kinds)})")
    return check


def _no_beliefs(view: CanonicalView):
    return (not view.beliefs, f"beliefs: {[(b.belief_kind, b.proposition[:30]) for b in view.beliefs] or 'none'}")


def _has_entity_like(fragment: str):
    def check(view: CanonicalView):
        names = [e.canonical_name for e in view.entities]
        hit = any(fragment.lower() in n.lower() for n in names)
        return (hit, f"entities: {names or 'NONE'} (looking for ~'{fragment}')")
    return check


def _traces_include(*required_ops: str):
    def check(view: CanonicalView):
        ops = view.trace_ops()
        missing = [op for op in required_ops if op not in ops]
        return (not missing, f"trace ops={ops or 'NONE'}; missing={missing}")
    return check


def _has_decision(view: CanonicalView):
    return (bool(view.decisions), f"decisions={len(view.decisions)}")


def _no_decision(view: CanonicalView):
    return (not view.decisions,
            f"decisions={[(d.question[:35], d.chosen_option) for d in view.decisions] or 'none'}")


def _decision_undecided(view: CanonicalView):
    if not view.decisions:
        return (False, "no decision emitted")
    d = view.decisions[0]
    return (d.chosen_option is None, f"chosen_option={d.chosen_option!r} (expected None -- deferred)")


def _decision_decided(view: CanonicalView):
    if not view.decisions:
        return (False, "no decision emitted")
    d = view.decisions[0]
    return (d.chosen_option is not None, f"chosen_option={d.chosen_option!r}")


def _decision_has_outcome(positive: bool = True):
    def check(view: CanonicalView):
        if not view.decisions:
            return (False, "no decision emitted")
        d = view.decisions[0]
        if d.outcome is None:
            return (False, "decision.outcome is NULL")
        if positive and (d.outcome_valence is None or d.outcome_valence <= 0):
            return (False, f"decision.outcome set but valence={d.outcome_valence} (expected > 0)")
        return (True, f"outcome set, valence={d.outcome_valence}")
    return check


def _episode_has_outcome(positive: Optional[bool] = True):
    """positive=True requires valence > 0, False requires a set valence <= 0,
    None checks only that an outcome exists (valence not asserted)."""
    def check(view: CanonicalView):
        if view.episode is None:
            return (False, "no episode emitted")
        if view.episode.outcome is None:
            return (False, "episode.outcome is NULL")
        if positive is True and (view.episode.outcome_valence is None or view.episode.outcome_valence <= 0):
            return (False, f"episode.outcome set but valence={view.episode.outcome_valence} (expected > 0)")
        if positive is False and (view.episode.outcome_valence is None or view.episode.outcome_valence > 0):
            return (False, f"episode.outcome set but valence={view.episode.outcome_valence} (expected <= 0)")
        return (True, f"outcome set, valence={view.episode.outcome_valence}")
    return check


def _has_state_item_kind(kind: str, minimum: int = 1):
    def check(view: CanonicalView):
        found = [s for s in view.state_items if s.kind == kind]
        return (len(found) >= minimum,
                f"state_items={[(s.kind, s.content[:30]) for s in view.state_items] or 'NONE'} "
                f"(expected >={minimum} of kind '{kind}')")
    return check


def _has_action_memory(view: CanonicalView):
    found = [m for m in view.memories if m.is_episode_action]
    return (bool(found), f"action memories: {[m.content[:35] for m in found] or 'NONE'}")


def _observation_status_present(*statuses: str):
    def check(view: CanonicalView):
        present = {o.epistemic_status for o in view.observations}
        missing = [s for s in statuses if s not in present]
        return (not missing, f"observation statuses={sorted(present)}; missing={missing}")
    return check


def _observation_owner_present(owner: str):
    def check(view: CanonicalView):
        present = {o.epistemic_owner for o in view.observations}
        return (owner in present, f"observation owners={sorted(present)} (expected '{owner}')")
    return check


# ---------------------------------------------------------------------------
# Fixture assertion table
# ---------------------------------------------------------------------------

FIXTURE_ASSERTIONS: dict[str, list[Assertion]] = {

    # --- Non-episode fixtures (the original five) -------------------------

    "priya_kubernetes_mixed": [
        _a("A-EXP-1", DIM_EXPERIENCE,
            "Three distinct topics (Priya's concern, a Kubernetes fact, a lunch) must not collapse into one derived object",
            True,
            lambda v: (len(v.memories) + len(v.beliefs) >= 2,
                        f"derived objects: {len(v.memories)} memories + {len(v.beliefs)} beliefs")),
        _a("A-ENT-1", DIM_ENTITY, "Resolves Priya as an entity", True, _has_entity_like("Priya")),
        _a("A-MEM-1", DIM_MEMORY, "Produces an episodic memory (things that happened with Priya)", True, _has_memory_system("episodic")),
        _a("A-MEM-2", DIM_MEMORY, "Produces a semantic memory (the Kubernetes resource-limits fact)", True, _has_memory_system("semantic")),
        _a("A-BEL-1", DIM_BELIEF_KIND, "Priya's concern is attributed to her, not asserted as the owner's", False,
            _has_belief_kind({"attributed"})),
        _a("A-EPI-1", DIM_EPISODE, "Three disconnected statements -- an episode is over-segmentation (advisory)", False, _no_episode),
    ],

    "weak_evidence_belief": [
        _a("B-BEL-1", DIM_BELIEF_KIND, "A claim about the world is a world_claim", True, _has_belief_kind({"world_claim"})),
        _a("B-EPS-1", DIM_EPISTEMIC, "Owner is the epistemic owner (nobody else is named)", True, _observation_owner_present("owner")),
        _a("B-EPI-1", DIM_EPISODE, "A single bare claim is not a cognitive episode", True, _no_episode),
        _a("B-OVR-1", DIM_OVER_INFERENCE, "No episodic memory invented from a general claim", True, _no_memory_system("episodic")),
    ],

    "contradiction_candidate": [
        _a("C-BEL-1", DIM_BELIEF_KIND, "A claim about the world is a world_claim", True, _has_belief_kind({"world_claim"})),
        _a("C-EPI-1", DIM_EPISODE, "A single bare claim is not a cognitive episode", True, _no_episode),
        _a("C-OVR-1", DIM_OVER_INFERENCE, "No episodic memory invented from a general claim", True, _no_memory_system("episodic")),
    ],

    "old_episodic": [
        _a("D-MEM-1", DIM_MEMORY, "Debugging an outage is an episodic memory (it happened)", True, _has_memory_system("episodic")),
        _a("D-OVR-1", DIM_OVER_INFERENCE, "A single recounted activity is not a world_claim belief", False, _no_beliefs),
        _a("D-EPI-1", DIM_EPISODE, "Bare activity report has no visible cognitive engagement", True, _no_episode),
    ],

    "newer_generalization": [
        _a("E-KNW-1", DIM_MEMORY, "A general causal fact lands as a semantic memory or a world_claim belief", True,
            lambda v: (bool(v.memories_of_system("semantic")) or bool(v.beliefs_of_kind("world_claim")),
                        f"semantic memories={len(v.memories_of_system('semantic'))}, "
                        f"world_claim beliefs={len(v.beliefs_of_kind('world_claim'))}")),
        _a("E-OVR-1", DIM_OVER_INFERENCE, "No episodic memory -- nothing happened, this is a generalization", True, _no_memory_system("episodic")),
        _a("E-EPI-1", DIM_EPISODE, "A standalone generalization is not a cognitive episode", True, _no_episode),
    ],

    # --- Episode fixtures --------------------------------------------------

    "episode_decision_unknown_to_decision": [
        _a("F-EPI-1", DIM_EPISODE, "Emits an episode", True, _has_episode),
        _a("F-EPI-2", DIM_EPISODE, "Episode kind is decision (or problem_solving)", True, _episode_kind_in({"decision", "problem_solving"})),
        _a("F-TRC-1", DIM_TRACE, "Trace contains identify_unknown (didn't know the burn rate)", True, _traces_include("identify_unknown")),
        _a("F-TRC-2", DIM_TRACE, "Trace contains seek_information (asked the CFO)", True, _traces_include("seek_information")),
        _a("F-TRC-3", DIM_TRACE, "Trace contains decide (decided to raise now)", True, _traces_include("decide")),
        _a("F-DEC-1", DIM_DECISION, "Creates a decision linked to the episode", True, _has_decision),
        _a("F-DEC-2", DIM_DECISION, "The decision was actually made (chosen_option set)", True, _decision_decided),
        _a("F-ENT-1", DIM_ENTITY, "Resolves the CFO as an entity", False, _has_entity_like("CFO")),
        _a("F-ACT-1", DIM_MEMORY, "The email to the investor is an action memory linked to the episode", False, _has_action_memory),
    ],

    "episode_learning": [
        _a("G-EPI-1", DIM_EPISODE, "Emits an episode", True, _has_episode),
        _a("G-EPI-2", DIM_EPISODE, "Episode kind is learning", True, _episode_kind_in({"learning"})),
        _a("G-MEM-1", DIM_MEMORY, "'I could explain it' is a capability -> procedural memory", True, _has_memory_system("procedural")),
        _a("G-TRC-1", DIM_TRACE, "Trace contains seek_information (read the docs)", True, _traces_include("seek_information")),
        _a("G-TRC-2", DIM_TRACE, "No revise step -- nothing was believed and then changed", True,
            lambda v: ("revise" not in v.trace_ops(), f"trace ops={v.trace_ops()}")),
        _a("G-DEC-1", DIM_DECISION, "No decision was made", True, _no_decision),
        _a("G-OUT-1", DIM_DECISION, "'Could explain it by the end' is a capability gained -> the outcome, not the resolution",
            True, _episode_has_outcome(positive=None)),
    ],

    "episode_reflection_belief_revision": [
        _a("H-EPI-1", DIM_EPISODE, "Emits an episode", True, _has_episode),
        _a("H-EPI-2", DIM_EPISODE, "Episode kind is reflection", True, _episode_kind_in({"reflection"})),
        _a("H-OBS-1", DIM_OBSERVATION, "The old view is recorded as a retracted observation", True, _observation_status_present("retracted")),
        _a("H-OBS-2", DIM_OBSERVATION, "The new view is recorded as an asserted observation", True, _observation_status_present("asserted")),
        _a("H-BEL-1", DIM_BELIEF_KIND, "The new view is a belief (normative or world_claim)", True, _has_belief_kind({"normative", "world_claim"})),
        _a("H-BEL-2", DIM_BELIEF_KIND, "Preferably normative -- it is a policy about what to do", False, _has_belief_kind({"normative"})),
        _a("H-TRC-1", DIM_TRACE, "Trace contains reflect and/or revise", True,
            lambda v: (("reflect" in v.trace_ops() or "revise" in v.trace_ops()), f"trace ops={v.trace_ops()}")),
        _a("H-DEC-1", DIM_DECISION, "No decision was made", True, _no_decision),
    ],

    "episode_social_conflict": [
        _a("I-EPI-1", DIM_EPISODE, "Emits an episode", True, _has_episode),
        _a("I-EPI-2", DIM_EPISODE, "Episode kind is conflict (or social)", True, _episode_kind_in({"conflict", "social"})),
        _a("I-TRC-1", DIM_TRACE, "Trace contains defer (agreed to revisit next week)", True, _traces_include("defer")),
        _a("I-OUT-1", DIM_DECISION, "Agreement to revisit is the resolution, not an episode outcome", True,
            _episode_has_resolution_without_outcome),
        _a("I-DEC-1", DIM_DECISION, "If a decision is emitted it must be undecided (deferred)", True,
            lambda v: (True, "no decision emitted") if not v.decisions else _decision_undecided(v)),
        _a("I-STA-1", DIM_STATE_ITEM, "The unresolved hiring question is an open state item", False,
            lambda v: (bool([s for s in v.state_items if s.kind in {"uncertainty", "question"}]),
                        f"state_items={[(s.kind, s.content[:30]) for s in v.state_items] or 'NONE'}")),
        _a("I-OVR-1", DIM_OVER_INFERENCE, "'disagreed sharply'/'argued' is conflict, not a named emotion", True,
            lambda v: (not any(m.emotion_is_stated for m in v.memories),
                        f"memories claiming stated emotion: {[m.content[:35] for m in v.memories if m.emotion_is_stated] or 'none'}")),
    ],

    "episode_creative_ideation": [
        _a("J-EPI-1", DIM_EPISODE, "Emits an episode", True, _has_episode),
        _a("J-EPI-2", DIM_EPISODE, "Episode kind is creative", True, _episode_kind_in({"creative"})),
        _a("J-TRC-1", DIM_TRACE, "Trace contains generate_option (three names)", True, _traces_include("generate_option")),
        _a("J-TRC-2", DIM_TRACE, "Trace contains evaluate_option (liked one best)", True, _traces_include("evaluate_option")),
        _a("J-DEC-1", DIM_DECISION, "A preference for a name need not be a formal decision (advisory)", False, _no_decision),
    ],

    "episode_explicit_goal": [
        _a("K-EPI-1", DIM_EPISODE, "Emits an episode", True, _has_episode),
        _a("K-STA-1", DIM_STATE_ITEM, "'My goal this week was...' becomes a state item of kind goal", True, _has_state_item_kind("goal")),
        _a("K-STA-2", DIM_STATE_ITEM, "The goal is episode-scoped, not a bare episode field", True,
            lambda v: (bool(v.state_items), f"episode-scoped state_items={len(v.state_items)}")),
        _a("K-TRC-1", DIM_TRACE, "Trace contains seek_information (recorded test sessions)", True, _traces_include("seek_information")),
        _a("K-DEC-1", DIM_DECISION, "No decision was made", True, _no_decision),
    ],

    "episode_attention_shift": [
        _a("L-EXP-1", DIM_EXPERIENCE,
            "The passage describes an interruption, not two unrelated activities -- one episode, not two",
            False,
            lambda v: (v.episode is not None, f"episode={'present' if v.episode else 'NONE'}")),
        _a("L-EPI-1", DIM_EPISODE, "Emits an episode", True, _has_episode),
        _a("L-STA-1", DIM_STATE_ITEM, "At least one attention state item is captured", True, _has_state_item_kind("attention")),
        _a("L-STA-2", DIM_STATE_ITEM, "Both sides of the shift are captured (advisory)", False, _has_state_item_kind("attention", minimum=2)),
        _a("L-STA-3", DIM_STATE_ITEM, "Focus shift alone does not establish goal/project state", True,
            _no_episode_state_kind("goal", "project")),
        _a("L-DEC-1", DIM_DECISION, "Abandoning a task does not itself establish an explicit choice between alternatives", True, _no_decision),
    ],

    "episode_hypothesis": [
        _a("M-EPI-1", DIM_EPISODE, "Emits an episode", True, _has_episode),
        _a("M-EPI-2", DIM_EPISODE, "Episode kind is problem_solving", False, _episode_kind_in({"problem_solving"})),
        _a("M-TRC-1", DIM_TRACE, "'I guessed it was probably X' is hypothesize", True, _traces_include("hypothesize")),
        _a("M-TRC-2", DIM_TRACE, "Trace contains seek_information (checked the metrics)", True, _traces_include("seek_information")),
        _a("M-OVR-1", DIM_OVER_INFERENCE, "The guess must not be recorded as identify_unknown or generate_option instead", False,
            lambda v: ("hypothesize" in v.trace_ops(), f"trace ops={v.trace_ops()}")),
    ],

    "episode_decision_with_outcome": [
        _a("N-EPI-1", DIM_EPISODE, "Emits an episode", True, _has_episode),
        _a("N-DEC-1", DIM_DECISION, "Creates a decision", True, _has_decision),
        _a("N-DEC-2", DIM_DECISION, "The decision carries the outcome, with positive valence", True, _decision_has_outcome(positive=True)),
        _a("N-DEC-3", DIM_DECISION, "episode.outcome stays NULL (decision is canonical)", True,
            lambda v: (v.episode is None or v.episode.outcome is None,
                        f"episode.outcome={v.episode.outcome if v.episode else 'n/a'}")),
        _a("N-TRC-1", DIM_TRACE, "Trace contains decide", True, _traces_include("decide")),
    ],

    "episode_outcome_without_decision": [
        _a("O-EPI-1", DIM_EPISODE, "Emits an episode", True, _has_episode),
        _a("O-DEC-1", DIM_DECISION, "No decision -- no choice between alternatives is described", True, _no_decision),
        _a("O-OUT-1", DIM_DECISION, "The episode carries the supported positive result", True, _episode_has_outcome(positive=True)),
    ],

    "episode_bare_activity": [
        _a("P-MEM-1", DIM_MEMORY, "Bare activity is retained as episodic memory", True, _has_memory_system("episodic")),
        _a("P-EPI-1", DIM_EPISODE, "Bare activity does not establish cognitive engagement", True, _no_episode),
    ],
    "episode_visible_investigation": [
        _a("Q-EPI-1", DIM_EPISODE, "Visible investigation supports a problem-solving episode", True, _has_episode),
        _a("Q-TRC-1", DIM_TRACE, "DNS check is recorded as information seeking", True, _traces_include("seek_information")),
        _a("Q-TRC-2", DIM_TRACE, "Comparing the configuration to a known-good cluster is recorded", True, _traces_include("compare")),
        _a("Q-ACT-1", DIM_MEMORY, "Rollback is the action memory", True, _has_action_memory),
    ],
    "episode_attention_shift_with_goal": [
        _a("R-EPI-1", DIM_EPISODE, "Observable abandonment supports the episode", True, _has_episode),
        _a("R-STA-1", DIM_STATE_ITEM, "Explicit desired result is a goal state", True, _has_state_item_kind("goal")),
        _a("R-STA-2", DIM_STATE_ITEM, "Current focus is represented as attention", True, _has_state_item_kind("attention")),
        _a("R-DEC-1", DIM_DECISION, "Abandonment does not imply an explicit decision", True, _no_decision),
    ],
    # Episode-scoped goal/project state cannot exist without an episode, so
    # the no-episode assertion already covers it. The remaining leak is a
    # semantic memory of kind 'goal' (classification.md §1 allows one for a
    # referenced goal), which is what these check.
    "episode_topic_not_goal": [
        _a("S-EPI-1", DIM_EPISODE, "A topic being on the owner's mind is not an episode", True, _no_episode),
        _a("S-MEM-1", DIM_OVER_INFERENCE, "A topic mention alone is not recorded as a goal memory", True, _no_memory_kind("goal")),
    ],
    "episode_project_mention_only": [
        _a("T-EPI-1", DIM_EPISODE, "A one-time project mention is not an episode", True, _no_episode),
        _a("T-MEM-1", DIM_MEMORY, "The work is retained as an episodic memory", True, _has_memory_system("episodic")),
        _a("T-MEM-2", DIM_OVER_INFERENCE, "A project mention alone is not recorded as a goal memory", True, _no_memory_kind("goal")),
        _a("T-ENT-1", DIM_ENTITY, "Phynx is resolved as an entity (advisory; kind project expected)", False, _has_entity_like("Phynx")),
    ],
    "episode_activity_without_outcome": [
        _a("W-EPI-1", DIM_EPISODE, "Activity without visible engagement is not an episode", True, _no_episode),
        _a("W-MEM-1", DIM_MEMORY, "The activity is retained as an episodic memory", True, _has_memory_system("episodic")),
        _a("W-OVR-1", DIM_OVER_INFERENCE, "No result is invented: nothing beyond the one episodic memory", False,
            lambda v: (len(v.memories) <= 1 and not v.beliefs,
                       f"memories={[m.content[:30] for m in v.memories]}, beliefs={len(v.beliefs)}")),
    ],
    "episode_negative_outcome": [
        _a("V-EPI-1", DIM_EPISODE, "Visible investigation supports an episode", True, _has_episode),
        _a("V-OUT-1", DIM_DECISION, "'The release slipped a week' is a stated, clearly negative consequence", True,
            _episode_has_outcome(positive=False)),
        _a("V-RES-1", DIM_EPISODE, "'Still fails' is where the situation stands -> the resolution", False,
            lambda v: (bool(v.episode and v.episode.resolution),
                       f"resolution={v.episode.resolution if v.episode else None!r}")),
    ],
    # Owner's rule: an unresolved result may be negative, but "still fails"
    # alone states no consequence and no direction, so none may be inferred.
    "episode_unresolved_without_outcome": [
        _a("X-EPI-1", DIM_EPISODE, "Visible investigation supports an episode", True, _has_episode),
        _a("X-OUT-1", DIM_EPISODE, "'Still fails' is recorded as the resolution with no outcome", True,
            _episode_has_resolution_without_outcome),
        _a("X-OUT-2", DIM_OVER_INFERENCE, "No positive valence is inferred from an unresolved result", True,
            _episode_valence_not_positive),
        _a("X-OUT-3", DIM_OVER_INFERENCE, "No valence at all is inferred from ambiguous wording (advisory)", False,
            lambda v: (v.episode is None or v.episode.outcome_valence is None,
                       f"outcome_valence={v.episode.outcome_valence if v.episode else None}")),
    ],
}


def assertions_for(fixture_key: str) -> list[Assertion]:
    """Universal assertions plus this fixture's own."""
    return UNIVERSAL_ASSERTIONS + FIXTURE_ASSERTIONS.get(fixture_key, [])
