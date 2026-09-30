"""
dynamics.py  (v0.2)

The rules that make the schema move. Pure functions over plain data so the same
code runs inside a Temporal activity, inside a backfill, or in a notebook while
you tune constants.

Changed in v0.2:
  - Evidence weighting is two-dimensional. A piece of evidence moves two separate
    quantities: how sure we are the owner HOLDS a belief, and how sure we are the
    belief is TRUE. Owner assertion is near-conclusive for the first and weak for
    the second; an authoritative external source is the reverse.
  - Memory dynamics are per-system. Episodic memories do not go stale, semantic
    memories do, procedural memories lose proficiency with disuse.
  - Situation-conditioned activation: what lights up given this kind of moment,
    rather than what associates with what in general.
  - A consolidation rule for collapsing repeated episodic memories into semantic ones.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Sequence


UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _days_between(later: datetime, earlier: datetime) -> float:
    return max((later - earlier).total_seconds() / 86400.0, 0.0)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ===========================================================================
# 1. Beliefs, on two axes
# ===========================================================================

LOG_ODDS_CLAMP = 6.0
EVIDENCE_HALF_LIFE_DAYS = 540.0
CONTRADICTION_SUPERSEDE_THRESHOLD = 2.5
WEAKENED_BELOW = 0.4
CONTRADICTED_BELOW = 0.25

BASE_EVIDENCE_WEIGHT = 1.1


class Stance(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    QUALIFIES = "qualifies"


class EpistemicOwner(str, Enum):
    OWNER = "owner"
    AUTHORITATIVE_SOURCE = "authoritative_source"
    EXTERNAL_SOURCE = "external_source"
    OTHER_PERSON = "other_person"
    SYSTEM_INFERENCE = "system_inference"


class BeliefKind(str, Enum):
    SELF_REPORT = "self_report"
    WORLD_CLAIM = "world_claim"
    NORMATIVE = "normative"
    ATTRIBUTED = "attributed"


class MemorySystem(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


# The core correction in v0.2. Each cell is (holding_weight, truth_weight):
# how much this source moves our confidence that the owner holds the belief,
# and how much it moves our confidence that the belief is true.
#
# Read the first row: the owner saying "I think this startup will work" is
# conclusive about what he thinks and says almost nothing about whether it will.
# Read the second: a regulator's filing says nothing about what he believes and
# a great deal about the world.
EVIDENCE_WEIGHTS: dict[tuple[BeliefKind, EpistemicOwner], tuple[float, float]] = {
    # (belief kind, who produced the evidence): (holding, truth)
    (BeliefKind.SELF_REPORT, EpistemicOwner.OWNER):                (1.00, 1.00),
    (BeliefKind.SELF_REPORT, EpistemicOwner.OTHER_PERSON):         (0.25, 0.25),
    (BeliefKind.SELF_REPORT, EpistemicOwner.AUTHORITATIVE_SOURCE): (0.10, 0.10),
    (BeliefKind.SELF_REPORT, EpistemicOwner.EXTERNAL_SOURCE):      (0.08, 0.08),
    (BeliefKind.SELF_REPORT, EpistemicOwner.SYSTEM_INFERENCE):     (0.15, 0.15),

    (BeliefKind.WORLD_CLAIM, EpistemicOwner.OWNER):                (1.00, 0.30),
    (BeliefKind.WORLD_CLAIM, EpistemicOwner.AUTHORITATIVE_SOURCE): (0.05, 0.90),
    (BeliefKind.WORLD_CLAIM, EpistemicOwner.EXTERNAL_SOURCE):      (0.05, 0.55),
    (BeliefKind.WORLD_CLAIM, EpistemicOwner.OTHER_PERSON):         (0.05, 0.30),
    (BeliefKind.WORLD_CLAIM, EpistemicOwner.SYSTEM_INFERENCE):     (0.12, 0.15),

    # Normative claims have no external truth-maker. Only the owner's own
    # commitment and the observed outcomes of acting on it move the truth axis.
    (BeliefKind.NORMATIVE, EpistemicOwner.OWNER):                  (1.00, 0.70),
    (BeliefKind.NORMATIVE, EpistemicOwner.AUTHORITATIVE_SOURCE):   (0.05, 0.25),
    (BeliefKind.NORMATIVE, EpistemicOwner.EXTERNAL_SOURCE):        (0.05, 0.15),
    (BeliefKind.NORMATIVE, EpistemicOwner.OTHER_PERSON):           (0.08, 0.15),
    (BeliefKind.NORMATIVE, EpistemicOwner.SYSTEM_INFERENCE):       (0.10, 0.10),

    # "Priya believes X". The holding axis is about Priya, not the owner.
    (BeliefKind.ATTRIBUTED, EpistemicOwner.OWNER):                 (0.45, 0.20),
    (BeliefKind.ATTRIBUTED, EpistemicOwner.OTHER_PERSON):          (0.90, 0.20),
    (BeliefKind.ATTRIBUTED, EpistemicOwner.AUTHORITATIVE_SOURCE):  (0.55, 0.20),
    (BeliefKind.ATTRIBUTED, EpistemicOwner.EXTERNAL_SOURCE):       (0.40, 0.15),
    (BeliefKind.ATTRIBUTED, EpistemicOwner.SYSTEM_INFERENCE):      (0.15, 0.10),
}

DEFAULT_WEIGHTS = (0.15, 0.15)


@dataclass(frozen=True)
class EvidenceItem:
    """One evidence row joined to its source observation."""
    stance: Stance
    epistemic_owner: EpistemicOwner
    extraction_confidence: float = 1.0   # did we parse it correctly
    source_reliability: float = 1.0      # standing trust in this source
    weight: float = 1.0                  # manual multiplier on the evidence row
    observed_at: datetime = field(default_factory=_now)

    def contributions(self, belief_kind: BeliefKind, as_of: datetime) -> tuple[float, float]:
        """Returns (holding_delta, truth_delta) in log-odds."""
        if self.stance is Stance.QUALIFIES:
            return (0.0, 0.0)

        holding_w, truth_w = EVIDENCE_WEIGHTS.get(
            (belief_kind, self.epistemic_owner), DEFAULT_WEIGHTS
        )
        recency = 0.5 ** (_days_between(as_of, self.observed_at) / EVIDENCE_HALF_LIFE_DAYS)
        common = (
            BASE_EVIDENCE_WEIGHT
            * self.extraction_confidence
            * self.source_reliability
            * self.weight
            * recency
        )
        sign = 1.0 if self.stance is Stance.SUPPORTS else -1.0
        return (sign * common * holding_w, sign * common * truth_w)


@dataclass(frozen=True)
class BeliefUpdate:
    holding_log_odds: float
    holding_confidence: float
    truth_log_odds: float
    truth_confidence: float
    holding_delta: float
    truth_delta: float
    status: str
    should_supersede: bool
    reason: str


def confidence_from_log_odds(log_odds: float) -> float:
    clamped = max(-LOG_ODDS_CLAMP, min(LOG_ODDS_CLAMP, log_odds))
    return 1.0 / (1.0 + math.exp(-clamped))


def log_odds_from_confidence(confidence: float) -> float:
    eps = 1e-6
    c = min(max(confidence, eps), 1.0 - eps)
    return max(-LOG_ODDS_CLAMP, min(LOG_ODDS_CLAMP, math.log(c / (1.0 - c))))


def apply_evidence(
    belief_kind: BeliefKind,
    current_holding: float,
    current_truth: float,
    new_evidence: Sequence[EvidenceItem],
    as_of: datetime | None = None,
) -> BeliefUpdate:
    as_of = as_of or _now()
    pairs = [e.contributions(belief_kind, as_of) for e in new_evidence]
    h_delta = sum(p[0] for p in pairs)
    t_delta = sum(p[1] for p in pairs)
    holding = max(-LOG_ODDS_CLAMP, min(LOG_ODDS_CLAMP, current_holding + h_delta))
    truth = max(-LOG_ODDS_CLAMP, min(LOG_ODDS_CLAMP, current_truth + t_delta))
    return _classify(belief_kind, holding, truth, h_delta, t_delta)


def recompute_belief(
    belief_kind: BeliefKind,
    all_evidence: Iterable[EvidenceItem],
    prior_holding: float = 0.0,
    prior_truth: float = 0.0,
    as_of: datetime | None = None,
) -> BeliefUpdate:
    """
    Full recomputation from the evidence spine. Run after changing any constant
    above, or after re-extracting observations with a better model. Log-odds are
    additive, so this is order-independent and cheap.
    """
    as_of = as_of or _now()
    pairs = [e.contributions(belief_kind, as_of) for e in all_evidence]
    holding = max(-LOG_ODDS_CLAMP, min(LOG_ODDS_CLAMP, prior_holding + sum(p[0] for p in pairs)))
    truth = max(-LOG_ODDS_CLAMP, min(LOG_ODDS_CLAMP, prior_truth + sum(p[1] for p in pairs)))
    return _classify(belief_kind, holding, truth,
                     holding - prior_holding, truth - prior_truth)


def _classify(
    belief_kind: BeliefKind,
    holding: float,
    truth: float,
    h_delta: float,
    t_delta: float,
) -> BeliefUpdate:
    holding_c = confidence_from_log_odds(holding)
    truth_c = confidence_from_log_odds(truth)

    # Status tracks whether the owner still holds the belief. A world claim can be
    # false while still firmly held, and the system must be able to say both.
    if holding <= -CONTRADICTION_SUPERSEDE_THRESHOLD:
        status, supersede = "contradicted", True
        reason = "sustained evidence the owner no longer holds this; propose a replacement"
    elif holding_c < CONTRADICTED_BELOW:
        status, supersede = "contradicted", False
        reason = "confidence the owner holds this fell below the floor"
    elif holding_c < WEAKENED_BELOW:
        status, supersede = "weakened", False
        reason = "belief eroding but still held"
    elif belief_kind is BeliefKind.WORLD_CLAIM and truth_c < CONTRADICTED_BELOW and holding_c > 0.6:
        status, supersede = "active", False
        reason = "held firmly but the world evidence runs against it; surface on retrieval"
    else:
        status, supersede = "active", False
        reason = "belief reinforced" if h_delta > 0 else "belief held"

    return BeliefUpdate(
        holding_log_odds=holding,
        holding_confidence=holding_c,
        truth_log_odds=truth,
        truth_confidence=truth_c,
        holding_delta=h_delta,
        truth_delta=t_delta,
        status=status,
        should_supersede=supersede,
        reason=reason,
    )


# ===========================================================================
# 2. Memory dynamics, per system
# ===========================================================================
#
# Three different cognitive functions need three different rules. Collapsing them
# is what turns memory.kind into a dumping ground.

# Episodic: retrievability fades but the memory never becomes false, so it decays
# in accessibility only and is never superseded.
EPISODIC_ACCESS_HALF_LIFE_DAYS = 400.0
EPISODIC_REHEARSAL_BOOST = 0.25

# Semantic: goes stale. Staleness is a prior on needing re-verification, not proof
# of falsehood, so it lowers rank rather than invalidating the row.
SEMANTIC_STALENESS_HALF_LIFE_DAYS: dict[str, float] = {
    "person_attribute": 270.0,   # jobs, locations, relationships change
    "fact": 730.0,
    "preference": 540.0,
    "goal": 180.0,
    "concept": 1825.0,           # concepts barely age
    "definition": 1825.0,
}

# Procedural: proficiency decays with disuse and is restored by practice.
PROCEDURAL_DECAY_HALF_LIFE_DAYS = 240.0
PRACTICE_GAIN = 0.20


def episodic_retrievability(
    last_activated_at: datetime | None,
    created_at: datetime,
    activation_count: int,
    as_of: datetime | None = None,
) -> float:
    """
    How easily this memory comes to mind. Repeated retrieval flattens the curve,
    which is why the things you tell people about are the things you remember.
    """
    as_of = as_of or _now()
    anchor = last_activated_at or created_at
    half_life = EPISODIC_ACCESS_HALF_LIFE_DAYS * (
        1.0 + EPISODIC_REHEARSAL_BOOST * math.log1p(max(0, activation_count))
    )
    return _clamp(0.5 ** (_days_between(as_of, anchor) / half_life))


def semantic_staleness(
    kind: str,
    last_confirmed_at: datetime,
    as_of: datetime | None = None,
) -> float:
    """0.0 means freshly confirmed, 1.0 means overdue for re-verification."""
    as_of = as_of or _now()
    half_life = SEMANTIC_STALENESS_HALF_LIFE_DAYS.get(kind, 730.0)
    return _clamp(1.0 - 0.5 ** (_days_between(as_of, last_confirmed_at) / half_life))


def procedural_proficiency(
    current: float,
    last_practiced_at: datetime,
    practiced_now: bool = False,
    as_of: datetime | None = None,
) -> float:
    as_of = as_of or _now()
    decayed = current * (0.5 ** (_days_between(as_of, last_practiced_at)
                                 / PROCEDURAL_DECAY_HALF_LIFE_DAYS))
    if practiced_now:
        decayed = decayed + PRACTICE_GAIN * (1.0 - decayed)
    return _clamp(decayed)


# ===========================================================================
# 3. Associations
# ===========================================================================

LEARNING_RATE = 0.18
ASSOCIATION_HALF_LIFE_DAYS = 120.0
STRENGTH_FLOOR = 0.02
CONFIDENCE_SATURATION_K = 6.0

CONTEXT_MULTIPLIER: dict[str, float] = {
    "decision": 1.4,
    "problem_solving": 1.3,
    "reflection": 1.2,
    "planning": 1.1,
    "conversation": 1.0,
    "document": 0.7,
    "global": 1.0,
}


@dataclass(frozen=True)
class AssociationState:
    strength: float
    confidence: float
    observation_count: int
    last_observed_at: datetime
    last_decayed_at: datetime


def reinforce(
    state: AssociationState,
    context: str = "global",
    at: datetime | None = None,
    salience: float = 1.0,
) -> AssociationState:
    at = at or _now()
    decayed = decay(state, at=at)
    eta = LEARNING_RATE * CONTEXT_MULTIPLIER.get(context, 1.0) * _clamp(salience)
    strength = decayed.strength + eta * (1.0 - decayed.strength)
    count = decayed.observation_count + 1
    return AssociationState(
        strength=min(1.0, strength),
        confidence=count / (count + CONFIDENCE_SATURATION_K),
        observation_count=count,
        last_observed_at=at,
        last_decayed_at=at,
    )


def decay(state: AssociationState, at: datetime | None = None) -> AssociationState:
    at = at or _now()
    elapsed = _days_between(at, state.last_decayed_at)
    if elapsed <= 0:
        return state
    strength = max(
        STRENGTH_FLOOR,
        state.strength * (0.5 ** (elapsed / ASSOCIATION_HALF_LIFE_DAYS)),
    )
    return AssociationState(
        strength=strength,
        confidence=state.confidence,
        observation_count=state.observation_count,
        last_observed_at=state.last_observed_at,
        last_decayed_at=at,
    )


def new_association(context: str = "global", at: datetime | None = None) -> AssociationState:
    at = at or _now()
    return reinforce(AssociationState(0.0, 0.0, 0, at, at), context=context, at=at)


# ===========================================================================
# 4. Situations: conditioned activation
# ===========================================================================
#
# The layer that makes this a model of cognition rather than of knowledge. An
# association says A relates to B. A situation strategy says: in this kind of
# moment, Abhishek tends to reach for this pattern, and here is how that went.

SITUATION_MATCH_THRESHOLD = 0.55
STRATEGY_LEARNING_RATE = 0.22
OUTCOME_INFLUENCE = 0.30   # how much past outcomes reweight a strategy


@dataclass(frozen=True)
class SituationMatch:
    situation_id: str
    confidence: float
    matched_features: tuple[str, ...]


def match_situation(
    query_features: dict[str, float],
    situation_signature: dict[str, float],
) -> float:
    """
    Cosine-style overlap between the features of the current moment and a stored
    situation signature. Deliberately simple: the extractor produces the feature
    dict, and this only scores it.
    """
    keys = set(query_features) & set(situation_signature)
    if not keys:
        return 0.0
    dot = sum(query_features[k] * situation_signature[k] for k in keys)
    qn = math.sqrt(sum(v * v for v in query_features.values()))
    sn = math.sqrt(sum(v * v for v in situation_signature.values()))
    return 0.0 if qn == 0 or sn == 0 else _clamp(dot / (qn * sn))


def strategy_activation(
    strength: float,
    match_confidence: float,
    outcome_valence_avg: float | None,
) -> float:
    """
    How strongly to surface a pattern or mental model given the detected situation.
    Strategies that historically went badly are still surfaced, at reduced weight,
    because "you usually do X here and it usually goes wrong" is the single most
    useful thing this system can say.
    """
    base = strength * match_confidence
    if outcome_valence_avg is None:
        return _clamp(base)
    return _clamp(base * (1.0 + OUTCOME_INFLUENCE * outcome_valence_avg))


def reinforce_strategy(
    strength: float,
    outcome_valence: float | None = None,
) -> float:
    eta = STRATEGY_LEARNING_RATE
    if outcome_valence is not None and outcome_valence < 0:
        eta *= 0.5   # a strategy that went badly still gets recorded, more slowly
    return _clamp(strength + eta * (1.0 - strength))


# ===========================================================================
# 5. Salience and memory promotion
# ===========================================================================

SALIENCE_WEIGHTS: dict[str, float] = {
    "novelty": 0.22,
    "goal_relevance": 0.24,
    "emotional": 0.16,
    "entity_centrality": 0.14,
    "repetition": 0.12,
    "decision_linked": 0.12,
}

# Episodic memory is cheap to keep and impossible to reconstruct, so it is admitted
# more freely than semantic memory, which competes with everything already known.
PROMOTION_THRESHOLD: dict[MemorySystem, float] = {
    MemorySystem.EPISODIC: 0.25,
    MemorySystem.SEMANTIC: 0.40,
    MemorySystem.PROCEDURAL: 0.35,
}


@dataclass(frozen=True)
class SalienceSignals:
    novelty: float = 0.5
    goal_relevance: float = 0.0
    emotional: float = 0.0
    entity_centrality: float = 0.0
    repetition: float = 0.0
    decision_linked: float = 0.0
    emotion_is_stated: bool = False

    def as_dict(self) -> dict[str, float]:
        return {
            "novelty": self.novelty,
            "goal_relevance": self.goal_relevance,
            # Inferred emotion never counts. Only what the owner actually said.
            "emotional": self.emotional if self.emotion_is_stated else 0.0,
            "entity_centrality": self.entity_centrality,
            "repetition": self.repetition,
            "decision_linked": self.decision_linked,
        }


def salience(signals: SalienceSignals) -> float:
    return _clamp(sum(SALIENCE_WEIGHTS[k] * _clamp(v) for k, v in signals.as_dict().items()))


def should_promote_to_memory(
    signals: SalienceSignals,
    system: MemorySystem,
) -> tuple[bool, float]:
    s = salience(signals)
    return s >= PROMOTION_THRESHOLD[system], s


# ===========================================================================
# 6. Consolidation
# ===========================================================================
#
# Repeated episodic memories collapse into a semantic one. This is the main thing
# biological memory does and it was missing entirely: without it, every recall
# competes with fifty near-identical instances and the store only grows.
# The episodic members are never deleted, they gain a consolidated_into_id pointer.

MIN_INSTANCES_FOR_CONSOLIDATION = 4
CONSOLIDATION_SIMILARITY_THRESHOLD = 0.78
CONSOLIDATION_CONFIDENCE_K = 3.0


def consolidation_confidence(instance_count: int, mean_similarity: float) -> float:
    if instance_count < MIN_INSTANCES_FOR_CONSOLIDATION:
        return 0.0
    if mean_similarity < CONSOLIDATION_SIMILARITY_THRESHOLD:
        return 0.0
    count_term = instance_count / (instance_count + CONSOLIDATION_CONFIDENCE_K)
    return _clamp(count_term * mean_similarity)


def should_consolidate(instance_count: int, mean_similarity: float) -> tuple[bool, float]:
    c = consolidation_confidence(instance_count, mean_similarity)
    return c >= 0.5, c


# ===========================================================================
# 7. Activation and re-ranking
# ===========================================================================

ACTIVATION_WEIGHTS: dict[str, float] = {
    "semantic": 0.26,
    "association": 0.18,
    "situation": 0.16,   # new channel: strategies conditioned on the detected situation
    "goal_alignment": 0.16,
    "recency": 0.10,
    "salience": 0.08,
    "evidence_quality": 0.06,
}

RECENCY_HALF_LIFE_DAYS = 90.0
CONTRADICTION_BONUS = 0.15
STALE_PENALTY = 0.35


@dataclass(frozen=True)
class Candidate:
    ref: str                       # "memory:<uuid>", "belief:<uuid>", ...
    channel: str = "semantic"
    semantic: float = 0.0
    association: float = 0.0
    situation: float = 0.0
    goal_alignment: float = 0.0
    salience: float = 0.0
    evidence_quality: float = 0.0
    occurred_at: datetime | None = None
    memory_system: MemorySystem | None = None
    is_contradiction: bool = False
    staleness: float = 0.0         # from semantic_staleness; episodic is always 0.0


def activation_score(c: Candidate, as_of: datetime | None = None) -> float:
    as_of = as_of or _now()
    recency = (
        0.3 if c.occurred_at is None
        else 0.5 ** (_days_between(as_of, c.occurred_at) / RECENCY_HALF_LIFE_DAYS)
    )

    score = (
        ACTIVATION_WEIGHTS["semantic"] * c.semantic
        + ACTIVATION_WEIGHTS["association"] * c.association
        + ACTIVATION_WEIGHTS["situation"] * c.situation
        + ACTIVATION_WEIGHTS["goal_alignment"] * c.goal_alignment
        + ACTIVATION_WEIGHTS["recency"] * recency
        + ACTIVATION_WEIGHTS["salience"] * c.salience
        + ACTIVATION_WEIGHTS["evidence_quality"] * c.evidence_quality
    )
    if c.is_contradiction:
        score += CONTRADICTION_BONUS
    # Episodic memories are exempt: an old event is not a stale fact.
    if c.memory_system is not MemorySystem.EPISODIC and c.staleness > 0:
        score *= (1.0 - STALE_PENALTY * c.staleness)
    return max(0.0, min(1.5, score))


def rank_and_bound(
    candidates: Iterable[Candidate],
    max_items: int = 40,
    min_score: float = 0.12,
    always_keep_contradictions: bool = True,
    as_of: datetime | None = None,
) -> list[tuple[Candidate, float]]:
    """
    Produces the bounded context set. Contradictions get a reserved slice so a
    well-supported narrative cannot crowd out the evidence against it, which is
    the failure mode that turns a personal brain into a flattery machine.
    """
    as_of = as_of or _now()
    scored = [(c, activation_score(c, as_of)) for c in candidates]
    scored.sort(key=lambda pair: pair[1], reverse=True)

    kept = [(c, s) for c, s in scored if s >= min_score][:max_items]

    if always_keep_contradictions:
        kept_refs = {c.ref for c, _ in kept}
        reserve = max(1, max_items // 8)
        extra = [(c, s) for c, s in scored
                 if c.is_contradiction and c.ref not in kept_refs][:reserve]
        if extra:
            kept = kept[: max_items - len(extra)] + extra
            kept.sort(key=lambda pair: pair[1], reverse=True)

    return kept


# ===========================================================================
# 8. Pattern and situation promotion
# ===========================================================================

MIN_EPISODES_FOR_PATTERN = 4
MIN_EPISODES_FOR_SITUATION = 3
PATTERN_CONFIDENCE_K = 5.0


def pattern_confidence(support_count: int, contradiction_count: int) -> float:
    net = support_count - 0.75 * contradiction_count
    return 0.0 if net <= 0 else net / (net + PATTERN_CONFIDENCE_K)


def pattern_epistemic_status(support_count: int, contradiction_count: int) -> str:
    if support_count < MIN_EPISODES_FOR_PATTERN:
        return "hypothesis"
    if contradiction_count > support_count:
        return "contradicted"
    # Never 'asserted'. The owner did not say this about himself; the system inferred it.
    return "inferred"


def situation_epistemic_status(instance_count: int) -> str:
    return "hypothesis" if instance_count < MIN_EPISODES_FOR_SITUATION else "inferred"
