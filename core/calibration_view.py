"""
core/calibration_view.py

One normalized view of "what was extracted", buildable from either end of
the pipeline:

    view_from_extraction(result, text)   -- an ExtractionResult draft, i.e.
                                            exactly what the extractor
                                            produced, before anything is
                                            persisted. Used for the LIVE
                                            path, where the real LLM output
                                            cannot be ingested (the mock
                                            fixture already occupies that
                                            text's content_hash, so
                                            ingest() would dedupe against
                                            it and never store the live
                                            extraction).

    view_from_inspection(inspection)     -- core.reader.inspect_experience()
                                            output, i.e. what actually
                                            round-tripped through
                                            ingest() -> Postgres -> reader.
                                            Used for the MOCK path.

Assertions in core/calibration_assertions.py are written against this view
only, so the same assertion scores both paths and any difference between
them is a real difference in behaviour rather than an artifact of two
separate assertion sets.

What the two paths can and cannot tell you:

  MOCK  path exercises extract -> validate -> ingest -> Postgres -> reader.
        Because the mock's JSON is hand-written, a passing assertion here
        says the *representation layer* is faithful; it says nothing about
        whether a real extractor would produce that JSON.

  LIVE  path exercises a real LLM -> validate only. A passing assertion
        here says the *extractor* got it right. It does not exercise
        persistence at all.

Neither path modifies or repairs the thing it is scoring. See
core/calibration_eval.py's module docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ViewObservation:
    content: str
    epistemic_status: str
    epistemic_owner: str
    span: Optional[dict]
    extraction_confidence: Optional[float]


@dataclass
class ViewMemory:
    content: str
    memory_system: str
    kind: str
    salience: Optional[float]
    emotion_is_stated: bool
    emotional_valence: Optional[float]
    emotional_intensity: Optional[float]
    linked_entity_names: list[str] = field(default_factory=list)
    is_episode_action: bool = False
    evidence_count: int = 0


@dataclass
class ViewBelief:
    proposition: str
    belief_kind: str
    epistemic_owner: str
    about_entity: Optional[str]
    domain: Optional[str]
    truth_confidence: Optional[float]
    holding_confidence: Optional[float]
    evidence_count: int = 0


@dataclass
class ViewEntity:
    canonical_name: str
    kind: str


@dataclass
class ViewTrace:
    seq: int
    operation: str
    target: str
    detail: Optional[str]
    cites_observation: bool


@dataclass
class ViewStateItem:
    kind: str
    content: str
    about_entity: Optional[str]
    priority: Optional[float]


@dataclass
class ViewDecision:
    question: str
    chosen_option: Optional[str]
    options_count: int
    reversibility: Optional[str]
    outcome: Optional[str]
    outcome_valence: Optional[float]
    reflection: Optional[str]


@dataclass
class ViewEpisode:
    kind: str
    title: str
    trigger: Optional[str]
    narrative: Optional[str]
    resolution: Optional[str]
    outcome: Optional[str]
    outcome_valence: Optional[float]
    evidence_count: int = 0
    traces: list[ViewTrace] = field(default_factory=list)
    state_items: list[ViewStateItem] = field(default_factory=list)
    decisions: list[ViewDecision] = field(default_factory=list)


@dataclass
class CanonicalView:
    source_text: str
    observations: list[ViewObservation] = field(default_factory=list)
    memories: list[ViewMemory] = field(default_factory=list)
    beliefs: list[ViewBelief] = field(default_factory=list)
    entities: list[ViewEntity] = field(default_factory=list)
    episode: Optional[ViewEpisode] = None
    ambiguous: bool = False
    # Set only on the mock path -- the live path never persists, so it has
    # no notion of "this memory row got an episode_id".
    persisted: bool = False

    # -- convenience accessors used heavily by assertions -----------------

    @property
    def traces(self) -> list[ViewTrace]:
        return self.episode.traces if self.episode else []

    @property
    def state_items(self) -> list[ViewStateItem]:
        return self.episode.state_items if self.episode else []

    @property
    def decisions(self) -> list[ViewDecision]:
        return self.episode.decisions if self.episode else []

    def trace_ops(self) -> list[str]:
        return [t.operation for t in self.traces]

    def memories_of_system(self, system: str) -> list[ViewMemory]:
        return [m for m in self.memories if m.memory_system == system]

    def beliefs_of_kind(self, kind: str) -> list[ViewBelief]:
        return [b for b in self.beliefs if b.belief_kind == kind]

    def entity_names(self) -> set[str]:
        return {e.canonical_name.lower() for e in self.entities}


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

def view_from_extraction(result, source_text: str) -> CanonicalView:
    """Build a view from a core.schema.ExtractionResult (LIVE path)."""
    episode = None
    if result.episode_candidate is not None:
        ep = result.episode_candidate
        episode = ViewEpisode(
            kind=ep.kind, title=ep.title, trigger=ep.trigger,
            narrative=ep.narrative, resolution=ep.resolution,
            outcome=ep.outcome, outcome_valence=ep.outcome_valence,
            # A draft's "evidence" is its evidence_span -- there is no
            # evidence row yet because nothing has been persisted.
            evidence_count=1 if ep.evidence_span is not None else 0,
            traces=[
                ViewTrace(
                    seq=i, operation=t.operation, target=t.target, detail=t.detail,
                    # The validator already guarantees observation_index is a
                    # real in-range index, so a draft trace always cites one.
                    cites_observation=True,
                )
                for i, t in enumerate(result.cognitive_traces, start=1)
            ],
            state_items=[
                ViewStateItem(kind=s.kind, content=s.content,
                               about_entity=s.about_entity, priority=s.priority)
                for s in result.episode_state_items
            ],
            decisions=(
                [ViewDecision(
                    question=result.decision.question,
                    chosen_option=result.decision.chosen_option,
                    options_count=len(result.decision.options),
                    reversibility=result.decision.reversibility,
                    outcome=result.decision.outcome,
                    outcome_valence=result.decision.outcome_valence,
                    reflection=result.decision.reflection,
                )]
                if result.decision is not None else []
            ),
        )

    return CanonicalView(
        source_text=source_text,
        observations=[
            ViewObservation(
                content=o.content, epistemic_status=o.epistemic_status,
                epistemic_owner=o.epistemic_owner,
                span={"start": o.span.start, "end": o.span.end} if o.span else None,
                extraction_confidence=o.extraction_confidence,
            )
            for o in result.observations
        ],
        memories=[
            ViewMemory(
                content=m.content, memory_system=m.memory_system, kind=m.kind,
                salience=m.salience, emotion_is_stated=m.emotion_is_stated,
                emotional_valence=m.emotional_valence,
                emotional_intensity=m.emotional_intensity,
                linked_entity_names=[l.canonical_name for l in m.involved_entities],
                is_episode_action=m.is_episode_action,
                evidence_count=1 if m.evidence_span is not None else 0,
            )
            for m in result.memories
        ],
        beliefs=[
            ViewBelief(
                proposition=b.proposition, belief_kind=b.belief_kind,
                epistemic_owner=b.epistemic_owner, about_entity=b.about_entity,
                domain=b.domain, truth_confidence=b.truth_confidence,
                holding_confidence=b.holding_confidence,
                evidence_count=1 if b.evidence_span is not None else 0,
            )
            for b in result.beliefs
        ],
        entities=[ViewEntity(canonical_name=e.canonical_name, kind=e.kind)
                   for e in result.entities],
        episode=episode,
        ambiguous=result.ambiguous,
        persisted=False,
    )


def view_from_inspection(inspection: dict[str, Any]) -> CanonicalView:
    """Build a view from core.reader.inspect_experience() output (MOCK path)."""
    exp = inspection["experience"]
    raw_episodes = inspection.get("episodes") or []
    episode = None
    if raw_episodes:
        ep = raw_episodes[0]
        episode = ViewEpisode(
            kind=ep["kind"], title=ep["title"], trigger=ep["trigger"],
            narrative=ep["narrative"], resolution=ep["resolution"],
            outcome=ep["outcome"], outcome_valence=ep["outcome_valence"],
            evidence_count=len(ep.get("evidence") or []),
            traces=[
                ViewTrace(
                    seq=t["seq"], operation=t["operation"], target=t["target"],
                    detail=t.get("detail"),
                    cites_observation=bool(t.get("observation_id")),
                )
                for t in ep.get("cognitive_traces") or []
            ],
            state_items=[
                ViewStateItem(kind=s["kind"], content=s["content"],
                               about_entity=s.get("about_entity"), priority=s.get("priority"))
                for s in ep.get("state_items") or []
            ],
            decisions=[
                ViewDecision(
                    question=d["question"], chosen_option=d.get("chosen_option"),
                    options_count=len(d.get("options") or []),
                    reversibility=d.get("reversibility"),
                    outcome=d.get("outcome"), outcome_valence=d.get("outcome_valence"),
                    reflection=d.get("reflection"),
                )
                for d in ep.get("decisions") or []
            ],
        )

    # memory -> linked entity names, resolved through the entities list
    entities_by_name = {e["canonical_name"] for e in inspection.get("entities") or []}

    return CanonicalView(
        source_text=exp.get("raw_text") or "",
        observations=[
            ViewObservation(
                content=o["content"], epistemic_status=o["epistemic_status"],
                epistemic_owner=o["epistemic_owner"], span=o.get("span"),
                extraction_confidence=o.get("extraction_confidence"),
            )
            for o in inspection.get("observations") or []
        ],
        memories=[
            ViewMemory(
                content=m["content"], memory_system=m["memory_system"], kind=m["kind"],
                salience=m.get("salience"), emotion_is_stated=bool(m.get("emotion_is_stated")),
                emotional_valence=m.get("emotional_valence"),
                emotional_intensity=m.get("emotional_intensity"),
                # reader.py doesn't return per-memory entity names; the
                # entity set for the experience is checked separately by the
                # entity-resolution assertions.
                linked_entity_names=[],
                is_episode_action=bool(m.get("episode_id")),
                evidence_count=len(m.get("evidence") or []),
            )
            for m in inspection.get("memories") or []
        ],
        beliefs=[
            ViewBelief(
                proposition=b["proposition"], belief_kind=b["belief_kind"],
                epistemic_owner=b["epistemic_owner"], about_entity=b.get("about_entity"),
                domain=b.get("domain"),
                truth_confidence=(b.get("truth") or {}).get("confidence"),
                holding_confidence=(b.get("holding") or {}).get("confidence"),
                evidence_count=len(b.get("evidence") or []),
            )
            for b in inspection.get("beliefs") or []
        ],
        entities=[ViewEntity(canonical_name=e["canonical_name"], kind=e["kind"])
                   for e in inspection.get("entities") or []],
        episode=episode,
        ambiguous=False,  # not persisted anywhere; only meaningful pre-persistence
        persisted=True,
    )
