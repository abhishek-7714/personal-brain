"""
core/extractor.py

THE LLM PROPOSES. THE APPLICATION VALIDATES.

The LLM receives the full classification rules from core/classification.md as
its system prompt and returns structured JSON. Every field is validated against
the ontology before an ExtractionResult is returned. Nothing from the LLM
reaches the database until this module says it is clean.

Public API
----------
extract(text, client) -> ExtractionResult
    Call the LLM, parse the response, validate, return.

validate_extraction_response(raw, source_text) -> ExtractionResult
    Pure validation only. Used directly in tests with pre-crafted inputs.

AnthropicLLMClient
    Thin wrapper around the Anthropic SDK. Requires ANTHROPIC_API_KEY env var.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional, Protocol

from core.schema import (
    FORBIDDEN_MEMORY_KINDS,
    VALID_BELIEF_KIND,
    VALID_COGNITIVE_OP,
    VALID_ENTITY_KIND,
    VALID_ENTITY_LINK_ROLE,
    VALID_EPISTEMIC_OWNER,
    VALID_EPISTEMIC_STATUS,
    VALID_EPISODE_KIND,
    VALID_MEMORY_SYSTEM_KIND,
    VALID_REVERSIBILITY,
    VALID_STATE_ITEM_KIND,
    BeliefDraft,
    CognitiveTraceDraft,
    DecisionDraft,
    DecisionOptionDraft,
    EntityDraft,
    EntityLinkDraft,
    EpisodeCandidateDraft,
    EpisodeStateItemDraft,
    ExtractionResult,
    MemoryDraft,
    ObservationDraft,
    SpanDraft,
)

_CLASSIFICATION_MD = Path(__file__).parent / "classification.md"

# ---------------------------------------------------------------------------
# LLM client protocol — injected so tests can pass a mock
# ---------------------------------------------------------------------------

class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str:
        ...


class AnthropicLLMClient:
    """
    Wraps the Anthropic SDK. model defaults to Haiku 4.5 (cheap, fast,
    sufficient for structured extraction). Override via the constructor.

    max_tokens must leave room for thinking: on a thinking-enabled model the
    reasoning is billed against the same budget as the response, so a budget
    sized only for the JSON is spent before any text block is emitted and the
    call returns nothing usable.
    """

    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 16000,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def complete(self, system: str, user: str) -> str:
        import anthropic  # imported here so the module loads without the package
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # Thinking-enabled models return a ThinkingBlock first, so the
        # extraction JSON is not necessarily content[0]. Selecting by type
        # keeps every model on the same path; non-thinking models are
        # unaffected because their text block is already first.
        return next(b.text for b in msg.content if b.type == "text")


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_JSON_SCHEMA = """
Return ONLY a JSON object with this exact structure (no markdown fences):

{
  "observations": [
    {
      "content": "verbatim or near-verbatim quote from the source text",
      "epistemic_status": "asserted|observed|reported|inferred|hypothesis|contradicted|retracted",
      "epistemic_owner": "owner|other_person|external_source|authoritative_source|system_inference",
      "span": "<EXACT quote copied character-for-character from the source text>",
      "extraction_confidence": <0.0-1.0>,
      "source_reliability": <0.0-1.0>
    }
  ],
  "entities": [
    {
      "canonical_name": "string",
      "kind": "person|project|organization|place|technology|concept|product|work|event|other",
      "mention_span": "<EXACT quote copied character-for-character from the source text>"
    }
  ],
  "memories": [
    {
      "memory_system": "episodic|semantic|procedural",
      "kind": "event|encounter|exchange|milestone|fact|concept|person_attribute|preference|goal|definition|skill|procedure|heuristic|tool_use",
      "content": "string",
      "salience": <0.0-1.0>,
      "emotion_is_stated": <true|false>,
      "emotional_valence": <-1.0 to 1.0 or null>,
      "emotional_intensity": <0.0-1.0 or null>,
      "proficiency": <0.0-1.0 or null — procedural only>,
      "evidence_span": "<EXACT quote copied character-for-character from the source text>",
      "involved_entities": [
        {"canonical_name": "<must match an entry in entities[]>",
         "role": "subject|object|context or null"}
      ],
      "is_episode_action": <true|false — true ONLY for the one episodic memory
        that records the concrete action taken as a result of episode_candidate,
        if any. Requires episode_candidate to be non-null. Most memories are false.>
    }
  ],
  "beliefs": [
    {
      "proposition": "string",
      "belief_kind": "self_report|world_claim|normative|attributed",
      "epistemic_owner": "owner|other_person|external_source|authoritative_source|system_inference",
      "about_entity": "<canonical_name or null>",
      "domain": "<string or null>",
      "holding_confidence": <0.0-1.0>,
      "truth_confidence": <0.0-1.0>,
      "evidence_span": "<EXACT quote copied character-for-character from the source text>"
    }
  ],
  "episode_candidate": {
    "kind": "decision|problem_solving|planning|learning|reflection|conflict|social|creative|routine",
    "title": "string",
    "trigger": "<string or null>",
    "narrative": "<string or null — a short prose account of the episode>",
    "resolution": "<string or null — how it concluded, if it did>",
    "salience": <0.0-1.0>,
    "outcome": "<string or null — ONLY if this episode has NO decision object below>",
    "outcome_valence": <-1.0 to 1.0 or null>,
    "evidence_span": "<EXACT quote copied character-for-character from the source text>"
  } or null,
  "cognitive_traces": [
    {
      "operation": "identify_unknown|seek_information|generate_option|evaluate_option|compare|estimate|decide|commit|defer|abandon|reflect|revise|abstract|analogize|delegate|hypothesize",
      "target": "string — what the operation was applied to",
      "detail": "<string or null>",
      "observation_index": <integer index into the top-level observations[] array
        that this step cites -- REQUIRED, must be a real index>,
      "confidence": <0.0-1.0>
    }
  ],
  "episode_state_items": [
    {
      "kind": "goal|project|question|uncertainty|attention|constraint",
      "content": "string",
      "about_entity": "<canonical_name or null — must match entities[] if set>",
      "priority": <0.0-1.0>,
      "activation": <0.0-1.0>,
      "evidence_span": "<EXACT quote copied character-for-character from the source text>"
    }
  ],
  "decision": {
    "question": "string",
    "context": "<string or null>",
    "options": [{"label": "string", "rationale": "<string or null>", "risks": "<string or null>"}],
    "chosen_option": "<string or null>",
    "rationale": "<string or null>",
    "confidence_at_time": <0.0-1.0 or null>,
    "reversibility": "reversible|costly|irreversible or null",
    "outcome": "<string or null>",
    "outcome_valence": <-1.0 to 1.0 or null>,
    "reflection": "<string or null>",
    "evidence_span": "<EXACT quote copied character-for-character from the source text>"
  } or null,
  "ambiguous": <true|false>,
  "ambiguity_note": "<string — empty if not ambiguous>"
}

Critical rules that override everything else:
1. "I think X about the world" is a world_claim with epistemic_owner=owner. NOT self_report.
   self_report is ONLY for claims about the owner's own mental/dispositional state.
2. "I learned X" MUST be split: procedural memory (skill) if X is a capability,
   normative belief if X is a policy/lesson. Never write a single "learned" memory.
3. emotion_is_stated must be TRUE only if the owner explicitly names an emotion.
   Never infer emotion from tone or brevity.
4. If classification is genuinely ambiguous, set ambiguous=true and emit an
   observation with epistemic_status=hypothesis. Do not guess.
5. Do not create a situation. Situations require multiple episodes to justify.
6. Every span / mention_span / evidence_span value is the EXACT substring of
   the source text -- copy it character-for-character, including
   capitalization and punctuation. Never paraphrase or shorten it to
   something close. Never report a numeric character offset; the
   application locates your quote in the source text itself.
7. Every canonical_name in a memory's involved_entities MUST also appear in the
   top-level entities[] array. Do not name an entity in involved_entities that
   was not separately extracted as an entity.
8. cognitive_traces, episode_state_items, and decision are parts of
   episode_candidate. If episode_candidate is null, all three MUST be empty/null.
   Do not emit an episode just because these arrays would otherwise be empty --
   see classification.md §4: if nothing cognitively meaningful happened, emit no
   episode at all, and then none of this applies.
9. decision.outcome and episode_candidate.outcome are mutually exclusive: if you
   emit a decision object, episode_candidate.outcome MUST be null (decision.outcome
   is canonical). Only set episode_candidate.outcome when there is no decision.
10. At most one memory may have is_episode_action=true, and only when
    episode_candidate is set.
"""


def _build_system_prompt() -> str:
    rules = _CLASSIFICATION_MD.read_text(encoding="utf-8")
    return (
        "You are a structured information extractor for a personal knowledge system. "
        "Your job is to read a text input and extract structured data following the "
        "classification rules below exactly.\n\n"
        f"{rules}\n\n"
        "Output schema:\n"
        f"{_JSON_SCHEMA}"
    )


def prompt_hash() -> str:
    """Stable hash of (classification.md + schema). Changes when the prompt changes."""
    rules = _CLASSIFICATION_MD.read_text(encoding="utf-8")
    return hashlib.sha256((rules + _JSON_SCHEMA).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# JSON parsing — extract the first JSON object from LLM output
# ---------------------------------------------------------------------------

def _parse_json_response(raw: str) -> dict[str, Any]:
    # Strip markdown fences if the model added them despite instructions
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find the outermost {...}
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            raise ValueError(f"LLM response contained no JSON object:\n{raw[:400]}")
        return json.loads(m.group(0))


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

class ExtractionValidationError(ValueError):
    pass


def _require(field: str, value: Any, valid: frozenset[str]) -> str:
    if value not in valid:
        raise ExtractionValidationError(
            f"Invalid {field}={value!r}. Must be one of {sorted(valid)}"
        )
    return value


def _resolve_span(quote: Any, source_text: str, field: str = "span") -> Optional[SpanDraft]:
    """
    Resolve an LLM-provided exact quote to a SpanDraft by deterministically
    locating it in the immutable source text -- replaces the older
    {start, end} numeric-offset contract.

    Why: the live calibration run (2026-09-01) found every fixture whose
    final span reached end-of-text had an overrun -- a constant, per-fixture
    amount past the real source length -- on BOTH Haiku 4.5 and Sonnet 5.
    A model asked to count characters reliably miscounts near the end of a
    text; a model asked to reproduce a short verbatim quote does not. Since
    the application (not the model) holds the real string, it computes the
    offsets itself via an exact substring search, so a resolved span cannot
    exceed the source length by construction.

    Selection rule for a quote occurring more than once: the FIRST
    (leftmost) occurrence is used, deterministically. This is a documented
    simplification, not a disambiguation heuristic -- a future pass could
    prefer the occurrence nearest other already-resolved spans; that is out
    of scope here.

    Fails closed rather than approximate: a quote absent from the source
    text, or altered from it in ANY way (paraphrased, reworded, whitespace
    collapsed), is rejected outright. There is no fuzzy or partial match --
    "do not silently approximate spans" is the whole point of this function.
    """
    if quote is None:
        return None
    if not isinstance(quote, str) or not quote:
        raise ExtractionValidationError(
            f"{field} must be a non-empty exact quote string, got {quote!r}"
        )
    start = source_text.find(quote)
    if start == -1:
        raise ExtractionValidationError(
            f"{field}: quote {quote!r} does not appear verbatim in the source "
            "text -- quotes must be copied character-for-character, never "
            "paraphrased, reworded, or approximated"
        )
    span = SpanDraft(start, start + len(quote))
    span.validate(len(source_text))  # defensive; a find()-derived span cannot fail this
    return span


def _parse_float_01(value: Any, field: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ExtractionValidationError(f"{field} must be a float, got {value!r}")
    if not (0.0 <= v <= 1.0):
        raise ExtractionValidationError(f"{field} must be in [0, 1], got {v}")
    return v


# ---------------------------------------------------------------------------
# Observation validation
# ---------------------------------------------------------------------------

def _validate_observation(raw: dict, source_text: str) -> ObservationDraft:
    content = str(raw.get("content", "")).strip()
    if not content:
        raise ExtractionValidationError("observation.content must not be empty")
    status = _require("epistemic_status", raw.get("epistemic_status"), VALID_EPISTEMIC_STATUS)
    owner = _require("epistemic_owner", raw.get("epistemic_owner"), VALID_EPISTEMIC_OWNER)
    span = _resolve_span(raw.get("span"), source_text, "span")
    if span is None:
        raise ExtractionValidationError("observation.span is required")
    exc = _parse_float_01(raw.get("extraction_confidence", 0.9), "extraction_confidence")
    rel = _parse_float_01(raw.get("source_reliability", 1.0), "source_reliability")
    return ObservationDraft(
        content=content,
        epistemic_status=status,
        epistemic_owner=owner,
        span=span,
        extraction_confidence=exc,
        source_reliability=rel,
    )


# ---------------------------------------------------------------------------
# Entity validation
# ---------------------------------------------------------------------------

def _validate_entity(raw: dict, source_text: str) -> EntityDraft:
    name = str(raw.get("canonical_name", "")).strip()
    if not name:
        raise ExtractionValidationError("entity.canonical_name must not be empty")
    kind = _require("entity.kind", raw.get("kind"), VALID_ENTITY_KIND)
    mention = _resolve_span(raw.get("mention_span"), source_text, "mention_span")
    return EntityDraft(canonical_name=name, kind=kind, mention_span=mention)


# ---------------------------------------------------------------------------
# Memory validation
# ---------------------------------------------------------------------------

def _validate_entity_link(raw: dict, known_entity_names: frozenset[str]) -> EntityLinkDraft:
    name = str(raw.get("canonical_name", "")).strip()
    if not name:
        raise ExtractionValidationError("involved_entities[].canonical_name must not be empty")
    if name.lower() not in known_entity_names:
        raise ExtractionValidationError(
            f"involved_entities[].canonical_name={name!r} does not match any "
            "entity in the top-level entities[] array"
        )
    role = raw.get("role") or None
    if role is not None:
        role = _require("involved_entities[].role", role, VALID_ENTITY_LINK_ROLE)
    return EntityLinkDraft(canonical_name=name, role=role)


def _validate_memory(
    raw: dict, source_text: str, known_entity_names: frozenset[str] = frozenset()
) -> MemoryDraft:
    system = _require("memory_system", raw.get("memory_system"), frozenset(VALID_MEMORY_SYSTEM_KIND))
    kind_val = raw.get("kind", "")

    if kind_val in FORBIDDEN_MEMORY_KINDS:
        raise ExtractionValidationError(
            f"memory.kind={kind_val!r} is forbidden. "
            "Split 'I learned X' into a procedural memory or a belief."
        )
    if kind_val not in VALID_MEMORY_SYSTEM_KIND[system]:
        raise ExtractionValidationError(
            f"memory.kind={kind_val!r} does not belong to system={system!r}. "
            f"Valid kinds for {system}: {sorted(VALID_MEMORY_SYSTEM_KIND[system])}"
        )

    content = str(raw.get("content", "")).strip()
    if not content:
        raise ExtractionValidationError("memory.content must not be empty")

    salience = _parse_float_01(raw.get("salience", 0.5), "salience")
    emotion_is_stated = bool(raw.get("emotion_is_stated", False))

    ev_val = raw.get("emotional_valence")
    ei_val = raw.get("emotional_intensity")
    emotional_valence: Optional[float] = None
    emotional_intensity: Optional[float] = None

    if ev_val is not None:
        v = float(ev_val)
        if not (-1.0 <= v <= 1.0):
            raise ExtractionValidationError(f"emotional_valence must be in [-1,1], got {v}")
        emotional_valence = v
    if ei_val is not None:
        emotional_intensity = _parse_float_01(ei_val, "emotional_intensity")

    # Emotion fields set without emotion_is_stated is a classification error.
    if (emotional_valence is not None or emotional_intensity is not None) and not emotion_is_stated:
        raise ExtractionValidationError(
            "emotion_is_stated must be true when emotional_valence or "
            "emotional_intensity is set. Per classification rule 8: only "
            "explicitly stated emotion counts."
        )

    proficiency_val = raw.get("proficiency")
    proficiency: Optional[float] = None
    if proficiency_val is not None:
        if system != "procedural":
            raise ExtractionValidationError(
                f"proficiency may only be set on procedural memories (got system={system!r})"
            )
        proficiency = _parse_float_01(proficiency_val, "proficiency")

    evidence_span = _resolve_span(raw.get("evidence_span"), source_text, "evidence_span")

    involved_entities: list[EntityLinkDraft] = []
    for i, link in enumerate(raw.get("involved_entities") or []):
        try:
            involved_entities.append(_validate_entity_link(link, known_entity_names))
        except ExtractionValidationError as exc:
            raise ExtractionValidationError(f"involved_entities[{i}]: {exc}") from exc

    return MemoryDraft(
        memory_system=system,
        kind=kind_val,
        content=content,
        salience=salience,
        emotion_is_stated=emotion_is_stated,
        emotional_valence=emotional_valence,
        emotional_intensity=emotional_intensity,
        proficiency=proficiency,
        evidence_span=evidence_span,
        involved_entities=involved_entities,
        is_episode_action=bool(raw.get("is_episode_action", False)),
    )


# ---------------------------------------------------------------------------
# Belief validation
# ---------------------------------------------------------------------------

def _validate_belief(raw: dict, source_text: str) -> BeliefDraft:
    proposition = str(raw.get("proposition", "")).strip()
    if not proposition:
        raise ExtractionValidationError("belief.proposition must not be empty")

    bk = _require("belief_kind", raw.get("belief_kind"), VALID_BELIEF_KIND)
    eo = _require("belief.epistemic_owner", raw.get("epistemic_owner"), VALID_EPISTEMIC_OWNER)

    # attributed beliefs must name who holds them
    about = raw.get("about_entity") or None
    if bk == "attributed" and not about:
        raise ExtractionValidationError(
            "attributed beliefs must name the entity via about_entity"
        )

    hc = _parse_float_01(raw.get("holding_confidence", 0.7), "holding_confidence")
    tc = _parse_float_01(raw.get("truth_confidence", 0.5), "truth_confidence")
    evidence_span = _resolve_span(raw.get("evidence_span"), source_text, "evidence_span")

    return BeliefDraft(
        proposition=proposition,
        belief_kind=bk,
        epistemic_owner=eo,
        about_entity=about,
        domain=raw.get("domain") or None,
        holding_confidence=hc,
        truth_confidence=tc,
        evidence_span=evidence_span,
    )


# ---------------------------------------------------------------------------
# Episode candidate validation
# ---------------------------------------------------------------------------

def _validate_episode_candidate(
    raw: Optional[dict], source_text: str
) -> Optional[EpisodeCandidateDraft]:
    if raw is None:
        return None
    kind = _require("episode_candidate.kind", raw.get("kind"), VALID_EPISODE_KIND)
    title = str(raw.get("title", "")).strip()
    if not title:
        raise ExtractionValidationError("episode_candidate.title must not be empty")

    salience = _parse_float_01(raw.get("salience", 0.5), "episode_candidate.salience")

    ov_val = raw.get("outcome_valence")
    outcome_valence: Optional[float] = None
    if ov_val is not None:
        v = float(ov_val)
        if not (-1.0 <= v <= 1.0):
            raise ExtractionValidationError(
                f"episode_candidate.outcome_valence must be in [-1,1], got {v}"
            )
        outcome_valence = v

    evidence_span = _resolve_span(raw.get("evidence_span"), source_text, "episode_candidate.evidence_span")

    return EpisodeCandidateDraft(
        kind=kind,
        title=title,
        trigger=raw.get("trigger") or None,
        narrative=raw.get("narrative") or None,
        resolution=raw.get("resolution") or None,
        salience=salience,
        outcome=raw.get("outcome") or None,
        outcome_valence=outcome_valence,
        evidence_span=evidence_span,
    )


# ---------------------------------------------------------------------------
# Cognitive trace validation
# ---------------------------------------------------------------------------

def _validate_cognitive_trace(raw: dict, num_observations: int) -> CognitiveTraceDraft:
    operation = _require("cognitive_traces[].operation", raw.get("operation"), VALID_COGNITIVE_OP)
    target = str(raw.get("target", "")).strip()
    if not target:
        raise ExtractionValidationError("cognitive_traces[].target must not be empty")

    idx = raw.get("observation_index")
    if not isinstance(idx, int) or isinstance(idx, bool):
        raise ExtractionValidationError(
            f"cognitive_traces[].observation_index must be an integer, got {idx!r}"
        )
    if not (0 <= idx < num_observations):
        raise ExtractionValidationError(
            f"cognitive_traces[].observation_index={idx} is out of range "
            f"(0..{num_observations - 1}). Every trace step must cite a real "
            "observation -- classification.md §5: no span, no trace."
        )

    confidence = _parse_float_01(raw.get("confidence", 0.5), "cognitive_traces[].confidence")

    return CognitiveTraceDraft(
        operation=operation,
        target=target,
        observation_index=idx,
        detail=raw.get("detail") or None,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Episode-scoped state item validation
# ---------------------------------------------------------------------------

def _validate_episode_state_item(
    raw: dict, known_entity_names: frozenset[str], source_text: str
) -> EpisodeStateItemDraft:
    kind = _require("episode_state_items[].kind", raw.get("kind"), VALID_STATE_ITEM_KIND)
    content = str(raw.get("content", "")).strip()
    if not content:
        raise ExtractionValidationError("episode_state_items[].content must not be empty")

    about = raw.get("about_entity") or None
    if about and about.lower() not in known_entity_names:
        raise ExtractionValidationError(
            f"episode_state_items[].about_entity={about!r} does not match any "
            "entity in the top-level entities[] array"
        )

    priority = _parse_float_01(raw.get("priority", 0.5), "episode_state_items[].priority")
    activation = _parse_float_01(raw.get("activation", 0.5), "episode_state_items[].activation")
    evidence_span = _resolve_span(raw.get("evidence_span"), source_text, "episode_state_items[].evidence_span")

    return EpisodeStateItemDraft(
        kind=kind, content=content, about_entity=about,
        priority=priority, activation=activation, evidence_span=evidence_span,
    )


# ---------------------------------------------------------------------------
# Decision validation
# ---------------------------------------------------------------------------

def _validate_decision_option(raw: dict) -> DecisionOptionDraft:
    label = str(raw.get("label", "")).strip()
    if not label:
        raise ExtractionValidationError("decision.options[].label must not be empty")
    return DecisionOptionDraft(
        label=label, rationale=raw.get("rationale") or None, risks=raw.get("risks") or None,
    )


def _validate_decision(raw: Optional[dict], source_text: str) -> Optional[DecisionDraft]:
    if raw is None:
        return None
    question = str(raw.get("question", "")).strip()
    if not question:
        raise ExtractionValidationError("decision.question must not be empty")

    options: list[DecisionOptionDraft] = []
    for i, opt in enumerate(raw.get("options") or []):
        try:
            options.append(_validate_decision_option(opt))
        except ExtractionValidationError as exc:
            raise ExtractionValidationError(f"decision.options[{i}]: {exc}") from exc

    reversibility = raw.get("reversibility") or None
    if reversibility is not None:
        _require("decision.reversibility", reversibility, VALID_REVERSIBILITY)

    cat_val = raw.get("confidence_at_time")
    confidence_at_time = _parse_float_01(cat_val, "decision.confidence_at_time") if cat_val is not None else None

    ov_val = raw.get("outcome_valence")
    outcome_valence: Optional[float] = None
    if ov_val is not None:
        v = float(ov_val)
        if not (-1.0 <= v <= 1.0):
            raise ExtractionValidationError(f"decision.outcome_valence must be in [-1,1], got {v}")
        outcome_valence = v

    evidence_span = _resolve_span(raw.get("evidence_span"), source_text, "decision.evidence_span")

    return DecisionDraft(
        question=question,
        context=raw.get("context") or None,
        options=options,
        chosen_option=raw.get("chosen_option") or None,
        rationale=raw.get("rationale") or None,
        confidence_at_time=confidence_at_time,
        reversibility=reversibility,
        outcome=raw.get("outcome") or None,
        outcome_valence=outcome_valence,
        reflection=raw.get("reflection") or None,
        evidence_span=evidence_span,
    )


# ---------------------------------------------------------------------------
# Public validation entry point
# ---------------------------------------------------------------------------

def validate_extraction_response(
    raw: dict[str, Any],
    source_text: str,
) -> ExtractionResult:
    """
    Validate a raw LLM JSON response against the ontology. Raises
    ExtractionValidationError if any field violates the schema.

    This function is pure — no LLM call, no database. Tests call it directly.
    """
    errors: list[str] = []

    observations: list[ObservationDraft] = []
    for i, o in enumerate(raw.get("observations") or []):
        try:
            observations.append(_validate_observation(o, source_text))
        except ExtractionValidationError as e:
            errors.append(f"observations[{i}]: {e}")

    entities: list[EntityDraft] = []
    for i, e in enumerate(raw.get("entities") or []):
        try:
            entities.append(_validate_entity(e, source_text))
        except ExtractionValidationError as exc:
            errors.append(f"entities[{i}]: {exc}")

    known_entity_names = frozenset(e.canonical_name.lower() for e in entities)
    memories: list[MemoryDraft] = []
    for i, m in enumerate(raw.get("memories") or []):
        try:
            memories.append(_validate_memory(m, source_text, known_entity_names))
        except ExtractionValidationError as exc:
            errors.append(f"memories[{i}]: {exc}")

    beliefs: list[BeliefDraft] = []
    for i, b in enumerate(raw.get("beliefs") or []):
        try:
            beliefs.append(_validate_belief(b, source_text))
        except ExtractionValidationError as exc:
            errors.append(f"beliefs[{i}]: {exc}")

    try:
        episode = _validate_episode_candidate(raw.get("episode_candidate"), source_text)
    except ExtractionValidationError as exc:
        errors.append(f"episode_candidate: {exc}")
        episode = None

    raw_traces = raw.get("cognitive_traces") or []
    raw_state_items = raw.get("episode_state_items") or []
    raw_decision = raw.get("decision")

    # cognitive_traces, episode_state_items, and decision are parts of the
    # episode -- none of them may appear without one (classification.md §4:
    # do not emit episode-shaped data without an actual episode).
    if episode is None:
        if raw_traces:
            errors.append("cognitive_traces: present but episode_candidate is null")
        if raw_state_items:
            errors.append("episode_state_items: present but episode_candidate is null")
        if raw_decision is not None:
            errors.append("decision: present but episode_candidate is null")

    traces: list[CognitiveTraceDraft] = []
    for i, t in enumerate(raw_traces):
        try:
            traces.append(_validate_cognitive_trace(t, len(observations)))
        except ExtractionValidationError as exc:
            errors.append(f"cognitive_traces[{i}]: {exc}")

    episode_state_items: list[EpisodeStateItemDraft] = []
    for i, s in enumerate(raw_state_items):
        try:
            episode_state_items.append(_validate_episode_state_item(s, known_entity_names, source_text))
        except ExtractionValidationError as exc:
            errors.append(f"episode_state_items[{i}]: {exc}")

    try:
        decision = _validate_decision(raw_decision, source_text)
    except ExtractionValidationError as exc:
        errors.append(f"decision: {exc}")
        decision = None

    # decision.outcome is canonical over episode_candidate.outcome -- they
    # must never both be set (classification.md's outcome invariant).
    if decision is not None and episode is not None and episode.outcome is not None:
        errors.append(
            "episode_candidate.outcome must be null when a decision is present "
            "-- decision.outcome is canonical (classification.md outcome invariant)"
        )

    action_count = sum(1 for m in memories if m.is_episode_action)
    if action_count > 1:
        errors.append(f"at most one memory may have is_episode_action=true, got {action_count}")
    if action_count > 0 and episode is None:
        errors.append("a memory has is_episode_action=true but episode_candidate is null")

    if errors:
        raise ExtractionValidationError(
            f"{len(errors)} validation error(s):\n" + "\n".join(f"  • {e}" for e in errors)
        )

    return ExtractionResult(
        observations=observations,
        entities=entities,
        memories=memories,
        beliefs=beliefs,
        episode_candidate=episode,
        cognitive_traces=traces,
        episode_state_items=episode_state_items,
        decision=decision,
        ambiguous=bool(raw.get("ambiguous", False)),
        ambiguity_note=str(raw.get("ambiguity_note") or ""),
    )


# ---------------------------------------------------------------------------
# Main extraction entry point
# ---------------------------------------------------------------------------

def extract(text: str, client: LLMClient) -> ExtractionResult:
    """
    Extract structured knowledge from text.

    Calls the LLM once, parses the JSON response, validates every field,
    and returns a clean ExtractionResult. Raises ExtractionValidationError
    if the LLM response fails validation after parsing.
    """
    system_prompt = _build_system_prompt()
    user_msg = (
        "Extract from this text. Return only the JSON object, no explanation.\n\n"
        f"TEXT:\n{text}"
    )
    raw_str = client.complete(system_prompt, user_msg)
    raw = _parse_json_response(raw_str)
    return validate_extraction_response(raw, text)
