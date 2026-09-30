"""
core/schema.py

Draft dataclasses for extraction output. A "draft" is a validated object that
has not yet been persisted. The extractor produces drafts; the ingest layer
writes them to Postgres.

Nothing in this module touches the database or the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Ontology constants — must stay in sync with the migrations.
# These are the application-level gate: the LLM cannot write a value that is
# not in these sets regardless of what it returns.
# ---------------------------------------------------------------------------

VALID_MEMORY_SYSTEM_KIND: dict[str, frozenset[str]] = {
    "episodic":   frozenset({"event", "encounter", "exchange", "milestone"}),
    "semantic":   frozenset({"fact", "concept", "person_attribute", "preference",
                             "goal", "definition"}),
    "procedural": frozenset({"skill", "procedure", "heuristic", "tool_use"}),
}

VALID_EPISTEMIC_STATUS = frozenset({
    "asserted", "observed", "reported", "inferred",
    "hypothesis", "contradicted", "retracted",
})

VALID_EPISTEMIC_OWNER = frozenset({
    "owner", "other_person", "external_source",
    "authoritative_source", "system_inference",
})

VALID_BELIEF_KIND = frozenset({
    "self_report", "world_claim", "normative", "attributed",
})

VALID_ENTITY_KIND = frozenset({
    "person", "project", "organization", "place", "technology",
    "concept", "product", "work", "event", "other",
})

VALID_EPISODE_KIND = frozenset({
    "decision", "problem_solving", "planning", "learning",
    "reflection", "conflict", "social", "creative", "routine",
})

# memory_entities.role — see migrations/002_canonical.sql. Nullable: a link
# can be asserted without a role if the extractor cannot tell which.
VALID_ENTITY_LINK_ROLE = frozenset({"subject", "object", "context"})

# cognitive_op — migrations/001_foundation.sql + 007_episode_model.sql
# ('hypothesize' added in 007). Deliberately behavioural: each value must
# correspond to something the owner did or said, never a hidden reasoning
# step -- see core/classification.md §5.
VALID_COGNITIVE_OP = frozenset({
    "identify_unknown", "seek_information", "generate_option", "evaluate_option",
    "compare", "estimate", "decide", "commit", "defer", "abandon",
    "reflect", "revise", "abstract", "analogize", "delegate", "hypothesize",
})

# state_item_kind — migrations/001_foundation.sql. Reused as the sole
# representation of an episode-scoped goal/attention/question/uncertainty
# (via state_items.episode_id, migrations/007) -- episodes deliberately do
# not carry a parallel `goal` column.
VALID_STATE_ITEM_KIND = frozenset({
    "goal", "project", "question", "uncertainty", "attention", "constraint",
})

# decisions.reversibility — migrations/002_canonical.sql CHECK constraint.
VALID_REVERSIBILITY = frozenset({"reversible", "costly", "irreversible"})

# These must never appear as memory_kind values. The LLM is prompted to avoid
# them, but the validator enforces the rejection.
FORBIDDEN_MEMORY_KINDS = frozenset({
    "lesson", "belief", "mental_model", "cognitive_pattern",
    "pattern", "learned", "learning",
})


# ---------------------------------------------------------------------------
# Draft types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpanDraft:
    start: int
    end: int

    def validate(self, source_len: int) -> None:
        if self.start < 0 or self.end < 0:
            raise ValueError(f"Span offsets must be non-negative: {self}")
        if self.start > self.end:
            raise ValueError(f"Span start must be <= end: {self}")
        if self.end > source_len:
            raise ValueError(
                f"Span end {self.end} exceeds source length {source_len}"
            )


@dataclass(frozen=True)
class ObservationDraft:
    content: str
    epistemic_status: str
    epistemic_owner: str
    span: SpanDraft
    extraction_confidence: float = 0.9
    source_reliability: float = 1.0


@dataclass(frozen=True)
class EntityDraft:
    canonical_name: str
    kind: str
    mention_span: Optional[SpanDraft] = None


@dataclass(frozen=True)
class EntityLinkDraft:
    """One memory_entities row, keyed to an entity by canonical_name until
    persist time resolves it to an entity_id."""
    canonical_name: str
    role: Optional[str] = None            # subject | object | context | None


@dataclass
class MemoryDraft:
    memory_system: str
    kind: str
    content: str
    salience: float = 0.5
    emotion_is_stated: bool = False
    emotional_valence: Optional[float] = None
    emotional_intensity: Optional[float] = None
    proficiency: Optional[float] = None   # procedural only
    evidence_span: Optional[SpanDraft] = None
    involved_entities: list[EntityLinkDraft] = field(default_factory=list)
    # True only for the episodic memory that records the action actually
    # taken as a result of the episode -- core/ingest.py sets this memory's
    # episode_id when true. Action is deliberately represented this way
    # (an episodic memory, memories.episode_id already existed) rather than
    # as a new schema field -- see core/classification.md.
    is_episode_action: bool = False


@dataclass
class BeliefDraft:
    proposition: str
    belief_kind: str
    epistemic_owner: str
    about_entity: Optional[str] = None    # canonical_name; resolved to entity_id at persist time
    domain: Optional[str] = None
    holding_confidence: float = 0.7
    truth_confidence: float = 0.5
    evidence_span: Optional[SpanDraft] = None


@dataclass
class EpisodeCandidateDraft:
    kind: str
    title: str
    trigger: Optional[str] = None
    narrative: Optional[str] = None
    resolution: Optional[str] = None
    salience: float = 0.5
    # Only set when this episode has NO decision (see DecisionDraft below).
    # If a decision exists, decisions.outcome/reflection are canonical and
    # these must stay None -- enforced in core/extractor.py's validator,
    # not just by convention.
    outcome: Optional[str] = None
    outcome_valence: Optional[float] = None
    evidence_span: Optional[SpanDraft] = None


@dataclass
class CognitiveTraceDraft:
    """One cognitive_traces row. `observation_index` cross-references
    ExtractionResult.observations by position, the same way MemoryDraft
    cross-references entities by canonical_name -- observations have no
    stable name to reference by. Every trace MUST resolve to a real
    observation; core/extractor.py rejects an out-of-range index rather
    than defaulting to "the first observation," because a trace step
    silently miscited would be exactly the "no span, no trace" failure
    core/classification.md §5 exists to prevent."""
    operation: str
    target: str
    observation_index: int
    detail: Optional[str] = None
    confidence: float = 0.5


@dataclass
class EpisodeStateItemDraft:
    """One state_items row scoped to this episode (state_items.episode_id).
    Represents an episode-local goal/attention/question/uncertainty --
    NOT a new episode column; see migrations/007_episode_model.sql."""
    kind: str
    content: str
    about_entity: Optional[str] = None    # canonical_name; resolved like BeliefDraft.about_entity
    priority: float = 0.5
    activation: float = 0.5
    evidence_span: Optional[SpanDraft] = None


@dataclass
class DecisionOptionDraft:
    label: str
    rationale: Optional[str] = None
    risks: Optional[str] = None


@dataclass
class DecisionDraft:
    """One decisions row linked to this episode. Decision is a distinct
    concept from the episode itself (the situation) and from a
    CognitiveTraceDraft(operation='decide') step (the observable moment of
    deciding) -- this carries the decision's own structure (options
    considered, what was chosen, why, and its outcome), never merged into
    either. See core/classification.md's episode/decision/trace distinction."""
    question: str
    context: Optional[str] = None
    options: list[DecisionOptionDraft] = field(default_factory=list)
    chosen_option: Optional[str] = None
    rationale: Optional[str] = None
    confidence_at_time: Optional[float] = None
    reversibility: Optional[str] = None
    outcome: Optional[str] = None
    outcome_valence: Optional[float] = None
    reflection: Optional[str] = None
    evidence_span: Optional[SpanDraft] = None


@dataclass
class ExtractionResult:
    observations: list[ObservationDraft] = field(default_factory=list)
    entities: list[EntityDraft] = field(default_factory=list)
    memories: list[MemoryDraft] = field(default_factory=list)
    beliefs: list[BeliefDraft] = field(default_factory=list)
    episode_candidate: Optional[EpisodeCandidateDraft] = None
    # The following three are only ever non-empty/non-None when
    # episode_candidate is set -- they describe parts of that episode, and
    # core/extractor.py rejects them appearing without one. There is no
    # such thing as a cognitive trace, episode-scoped state item, or
    # decision that doesn't belong to an episode.
    cognitive_traces: list[CognitiveTraceDraft] = field(default_factory=list)
    episode_state_items: list[EpisodeStateItemDraft] = field(default_factory=list)
    decision: Optional[DecisionDraft] = None
    ambiguous: bool = False
    ambiguity_note: str = ""
