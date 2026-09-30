"""
core/embedding_text.py

Canonical embedding-text representations. Pure functions, no DB, no network --
same shape as core/dynamics.py: the rule lives here, the plumbing lives in
core/indexer.py.

Every function below turns a canonical Postgres row into a natural-language
string meant for a semantic embedding, NOT a stringified row. The extractor
(core/extractor.py) already produces natural-language `content` /
`proposition` fields for exactly this reason -- classification.md requires
every memory and belief to read as a real sentence, not a label. These
functions add only the context an embedding needs that the bare field
doesn't carry: which sub-kind this is (a "preference" and a "fact" can have
near-identical sentence shape but should retrieve differently), who holds a
belief (critical -- see the epistemic rule below), and entity names the
content itself elides.

Epistemic rule -- read this before wiring retrieval on top of these
representations: embedding similarity is a RELEVANCE signal, never a TRUTH
signal. A belief's truth_confidence/holding_confidence are deliberately never
folded into the embedded text. Nothing here merges, strengthens, or weakens
a belief because two vectors are close; see core/indexer.py's module
docstring for the same rule applied to the write path.
"""

from __future__ import annotations

from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# Episodic memory
# ---------------------------------------------------------------------------

_EPISODIC_KIND_LABELS = {
    "event": "Event",
    "encounter": "Encounter",
    "exchange": "Exchange",
    "milestone": "Milestone",
}


def episodic_memory_text(kind: str, content: str, entity_names: Sequence[str]) -> str:
    """
    `content` is already the extractor's natural-language account of what
    happened (classification.md requires this). We prefix a readable label
    for the sub-kind so a query like "meetings with Priya" can match on
    "Encounter" semantically even when the word "meeting" never appears, and
    append any linked entity names the content doesn't already mention, so
    entity-scoped recall doesn't depend on the sentence happening to name
    them ("She raised a concern about our direction" still needs to surface
    for "what happened with Priya").
    """
    label = _EPISODIC_KIND_LABELS.get(kind, kind.replace("_", " ").title())
    text = f"{label}: {content}"
    extra = [n for n in entity_names if n and n.lower() not in content.lower()]
    if extra:
        text += f" (involving {', '.join(extra)})"
    return text


# ---------------------------------------------------------------------------
# Semantic memory
# ---------------------------------------------------------------------------

_SEMANTIC_KIND_LABELS = {
    "fact": "Fact",
    "concept": "Concept",
    "person_attribute": "About a person",
    "preference": "Preference",
    "goal": "Goal",
    "definition": "Definition",
}


def semantic_memory_text(kind: str, content: str, entity_names: Sequence[str]) -> str:
    """
    `content` is a declarative statement of what is known ("Priya leads the
    platform team"). The kind label matters here specifically because
    "preference" and "fact" can share near-identical sentence shape ("I
    prefer X" reads structurally like "X is true") but should not retrieve
    against each other for a query like "what do I prefer" vs "what is
    true" -- folding the label into the embedded text (not just the payload)
    lets the embedding itself carry that distinction, not only post-hoc
    filtering.
    """
    label = _SEMANTIC_KIND_LABELS.get(kind, kind.replace("_", " ").title())
    text = f"{label}: {content}"
    extra = [n for n in entity_names if n and n.lower() not in content.lower()]
    if extra:
        text += f" (about {', '.join(extra)})"
    return text


# ---------------------------------------------------------------------------
# Procedural memory
# ---------------------------------------------------------------------------

_PROCEDURAL_KIND_LABELS = {
    "skill": "Skill",
    "procedure": "Procedure",
    "heuristic": "Heuristic",
    "tool_use": "Tool use",
}


def procedural_memory_text(kind: str, content: str) -> str:
    """
    `content` already describes a capability or method ("Comfortable
    deploying a Kubernetes cluster from scratch"). Deliberately excludes
    `proficiency`: it's a decaying number (core/dynamics.py:
    procedural_proficiency), not semantic content -- folding "proficiency
    0.72" into the text would teach the embedding that numerically similar
    but topically unrelated skills are alike. Proficiency belongs in the
    payload for a ranker to weight, never in the vector itself.
    """
    label = _PROCEDURAL_KIND_LABELS.get(kind, kind.replace("_", " ").title())
    return f"{label}: {content}"


# ---------------------------------------------------------------------------
# Belief
# ---------------------------------------------------------------------------

def belief_text(
    belief_kind: str,
    proposition: str,
    epistemic_owner: str,
    about_entity_name: Optional[str],
    owner_display_name: str,
) -> str:
    """
    A bare `proposition` ("Our strategy is too broad") means something
    different depending on who holds it. Without framing, "I think our
    strategy is too broad" (world_claim, owner) and "Priya thinks our
    strategy is too broad" (attributed) would embed as near-duplicates
    because the proposition text is identical -- exactly the conflation
    core/classification.md §2 exists to prevent at the data-modeling level.
    Framing the holder into the text keeps that distinction alive in the
    vector, not just in the payload's epistemic_owner/belief_kind fields:

        attributed                  -> "{about_entity} believes: {proposition}"
        normative                   -> "Policy {owner} holds: {proposition}"
        self_report / world_claim,
          epistemic_owner=owner     -> "{owner} believes: {proposition}"
        otherwise                   -> "Reported by {source}: {proposition}"

    truth_confidence / holding_confidence are never embedded -- see the
    module epistemic rule.
    """
    if belief_kind == "attributed" and about_entity_name:
        return f"{about_entity_name} believes: {proposition}"
    if belief_kind == "normative":
        return f"Policy {owner_display_name} holds: {proposition}"
    if epistemic_owner == "owner":
        return f"{owner_display_name} believes: {proposition}"
    return f"Reported by {epistemic_owner.replace('_', ' ')}: {proposition}"


# ---------------------------------------------------------------------------
# Episode
# ---------------------------------------------------------------------------

_EPISODE_KIND_LABELS = {
    "decision": "Decision",
    "problem_solving": "Problem-solving",
    "planning": "Planning",
    "learning": "Learning",
    "reflection": "Reflection",
    "conflict": "Conflict",
    "social": "Social",
    "creative": "Creative",
    "routine": "Routine",
}


def episode_text(
    kind: str,
    title: str,
    narrative: Optional[str],
    trigger: Optional[str],
    resolution: Optional[str],
) -> str:
    """
    Prefer `narrative` (a composed prose account of the episode) when
    present -- it's the richest single field. Otherwise compose title +
    trigger + resolution, since together they answer what kind of episode
    this was, what set it off, and how it ended: the three things a query
    like "how did I handle disagreements with cofounders" needs to match
    against when no narrative was ever written.
    """
    label = _EPISODE_KIND_LABELS.get(kind, kind.replace("_", " ").title())
    if narrative:
        return f"{label} -- {title}: {narrative}"
    parts = [f"{label} -- {title}"]
    if trigger:
        parts.append(f"Triggered by: {trigger}")
    if resolution:
        parts.append(f"Resolved: {resolution}")
    return ". ".join(parts)


# ---------------------------------------------------------------------------
# Mental model
# ---------------------------------------------------------------------------

def mental_model_text(name: str, principle: str, applies_when: Optional[str]) -> str:
    """
    `principle` is the model's substance ("Reversible decisions should be
    made fast; irreversible ones deserve deliberation"). `name` is a label a
    query might use directly ("first principles thinking"). `applies_when`
    narrows retrieval to the triggering context, which is what lets a query
    describing a *situation* match a model by when it applies, not only by
    restating the principle back.
    """
    text = f"{name}: {principle}"
    if applies_when:
        text += f" Applies when: {applies_when}"
    return text


# ---------------------------------------------------------------------------
# Cognitive pattern
# ---------------------------------------------------------------------------

_COGNITIVE_OP_LABELS = {
    "identify_unknown": "identify what's unknown",
    "seek_information": "seek information",
    "generate_option": "generate options",
    "evaluate_option": "evaluate options",
    "compare": "compare",
    "estimate": "estimate",
    "decide": "decide",
    "commit": "commit",
    "defer": "defer",
    "abandon": "abandon",
    "reflect": "reflect",
    "revise": "revise",
    "abstract": "abstract",
    "analogize": "draw an analogy",
    "delegate": "delegate",
}


def cognitive_pattern_text(
    name: str,
    trigger_condition: str,
    sequence: Sequence[str],
    description: Optional[str],
) -> str:
    """
    `sequence` is a list of cognitive_op enum values (e.g.
    ['seek_information', 'compare', 'decide']) -- symbolic and meaningless as
    raw text. The pattern's actual content IS that sequence of behaviours,
    so we translate each op to a short verb phrase and chain them, rather
    than embedding the enum tokens or dropping the sequence entirely.
    """
    steps = ", then ".join(_COGNITIVE_OP_LABELS.get(op, op.replace("_", " ")) for op in sequence)
    text = f"{name}: when {trigger_condition}, tends to {steps}."
    if description:
        text += f" {description}"
    return text


# ---------------------------------------------------------------------------
# Document (raw source text)
# ---------------------------------------------------------------------------

def document_text(raw_text: str) -> str:
    """
    The raw source text itself, whitespace-normalized -- intentionally NOT a
    distillation. This is what vector/collections.py's documents collection
    description means by "Chunks of ingested external material. Source
    text, not conclusions.": retrieval here must be able to surface a source
    passage even when nothing was ever extracted from it into a memory or
    belief. v1 embeds the whole experience as one point (no chunker yet --
    see the indexer report for why chunking is flagged as a follow-up
    rather than implemented here).
    """
    return " ".join(raw_text.split())
