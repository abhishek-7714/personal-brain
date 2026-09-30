"""
tests/test_ingestion.py

Fixture suite for the extraction vertical slice. Runs without a database or
LLM API key -- every test drives validate_extraction_response() directly with
hand-crafted JSON that represents what a correct LLM response should look like.

Also tests:
  - MockStorageClient (MinIO protocol)
  - Belief revision (retracted obs + new belief)
  - Emotion intensity calibration rubric
  - Projection stage metadata (PROJECTION_STAGES constant)
  - core/ package import (no sys.path manipulation in library code)

Run:
    python tests/test_ingestion.py

Exit codes: 0 all passed, 1 failures present.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Only the test runner itself needs the path tweak -- library code does not.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.extractor import ExtractionValidationError, validate_extraction_response
from core.schema import ExtractionResult
from core.storage import MockStorageClient, _build_uri, _experience_key

FAILURES: list[str] = []
PASSES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSES.append(label)
        print(f"PASS  {label}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILURES.append(label)
        print(f"FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def accept(label: str, raw: dict[str, Any], text: str) -> ExtractionResult:
    try:
        result = validate_extraction_response(raw, text)
        check(label, True)
        return result
    except ExtractionValidationError as e:
        check(label, False, str(e)[:120])
        return ExtractionResult()


def reject(label: str, raw: dict[str, Any], text: str, fragment: str = "") -> None:
    try:
        validate_extraction_response(raw, text)
        check(label, False, "validator accepted but should have rejected")
    except ExtractionValidationError as e:
        if fragment and fragment.lower() not in str(e).lower():
            check(label, False, f"rejected but wrong reason: {str(e)[:100]}")
        else:
            check(label, True)


# ===========================================================================
section("STORAGE PROTOCOL -- MockStorageClient")
# ===========================================================================

_mock_storage = MockStorageClient()
_owner = "00000000-0000-0000-0000-000000000001"
_hash = "abc123"
_text = "I went to Japan in 2024."

uri1 = _mock_storage.put_experience(_owner, _hash, _text)
expected_uri = _build_uri("raw", _experience_key(_owner, _hash))

check("S.1 put_experience returns correct URI", uri1 == expected_uri, uri1)
check("S.2 get_text returns original text", _mock_storage.get_text(uri1) == _text)

# Idempotency: second put must return same URI without error
uri2 = _mock_storage.put_experience(_owner, _hash, _text)
check("S.3 put_experience is idempotent", uri1 == uri2, "same URI on second call")
check("S.4 store has exactly one entry for same key",
      sum(1 for k in _mock_storage._store if _hash in k) == 1)

# Different content hash -> different URI
uri3 = _mock_storage.put_experience(_owner, "def456", "Different text")
check("S.5 different content_hash -> different URI", uri3 != uri1)

try:
    _mock_storage.get_text("minio://raw/does/not/exist")
    check("S.6 get_text raises for missing key", False)
except KeyError:
    check("S.6 get_text raises for missing key", True)


# ===========================================================================
section("PROJECTION STAGE METADATA")
# ===========================================================================

from core.ingest import PROJECTION_STAGES

check("P.1 PROJECTION_STAGES contains qdrant_embedding",
      "qdrant_embedding" in PROJECTION_STAGES)
check("P.2 PROJECTION_STAGES contains neo4j_projection",
      "neo4j_projection" in PROJECTION_STAGES)
check("P.3 both stages are strings",
      all(isinstance(s, str) for s in PROJECTION_STAGES))
check("P.4 exactly two projection stages", len(PROJECTION_STAGES) == 2)


# ===========================================================================
section("PACKAGE IMPORT -- core is a proper package")
# ===========================================================================

import importlib, core, core.schema, core.extractor, core.ingest, core.storage, core.reader
check("I.1 core.__init__ exists and is importable", hasattr(core, "__file__"))
check("I.2 core.schema importable", hasattr(core.schema, "ExtractionResult"))
check("I.3 core.extractor importable", hasattr(core.extractor, "validate_extraction_response"))
check("I.4 core.ingest importable", hasattr(core.ingest, "ingest"))
check("I.5 core.storage importable", hasattr(core.storage, "MockStorageClient"))
check("I.6 core.reader importable (inspection command)", hasattr(core.reader, "inspect_experience"))


# ===========================================================================
section("FIXTURE 1: 'I went to Japan in 2024.'")
# ===========================================================================

TEXT1 = "I went to Japan in 2024."
MOCK1: dict[str, Any] = {
    "observations": [{"content": TEXT1, "epistemic_status": "asserted",
                      "epistemic_owner": "owner",
                      "span": TEXT1[0:len(TEXT1)],
                      "extraction_confidence": 0.95, "source_reliability": 1.0}],
    "entities": [{"canonical_name": "Japan", "kind": "place",
                  "mention_span": TEXT1[11:16]}],
    "memories": [{"memory_system": "episodic", "kind": "event",
                  "content": "Went to Japan in 2024", "salience": 0.5,
                  "emotion_is_stated": False, "emotional_valence": None,
                  "emotional_intensity": None, "proficiency": None,
                  "evidence_span": TEXT1[0:len(TEXT1)]}],
    "beliefs": [], "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
}
r1 = accept("1.0 validator accepts fixture 1", MOCK1, TEXT1)
check("1.1 episodic/event memory", r1.memories[0].memory_system == "episodic" and r1.memories[0].kind == "event")
check("1.2 entity Japan/place", r1.entities[0].kind == "place")
check("1.3 no beliefs", len(r1.beliefs) == 0)
check("1.4 emotion_is_stated=false", not r1.memories[0].emotion_is_stated)


# ===========================================================================
section("FIXTURE 2: 'I learned how to use Kubernetes.'")
# ===========================================================================

TEXT2 = "I learned how to use Kubernetes."
MOCK2: dict[str, Any] = {
    "observations": [{"content": TEXT2, "epistemic_status": "asserted",
                      "epistemic_owner": "owner",
                      "span": TEXT2[0:len(TEXT2)],
                      "extraction_confidence": 0.93, "source_reliability": 1.0}],
    "entities": [{"canonical_name": "Kubernetes", "kind": "technology",
                  "mention_span": TEXT2[21:31]}],
    "memories": [{"memory_system": "procedural", "kind": "skill",
                  "content": "Using Kubernetes", "salience": 0.55,
                  "emotion_is_stated": False, "emotional_valence": None,
                  "emotional_intensity": None, "proficiency": 0.5,
                  "evidence_span": TEXT2[0:len(TEXT2)]}],
    "beliefs": [], "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
}
r2 = accept("2.0 validator accepts fixture 2", MOCK2, TEXT2)
check("2.1 procedural/skill memory", r2.memories[0].memory_system == "procedural" and r2.memories[0].kind == "skill")
check("2.2 entity Kubernetes/technology", r2.entities[0].kind == "technology")
check("2.3 no beliefs", len(r2.beliefs) == 0)
check("2.4 proficiency allowed on procedural", r2.memories[0].proficiency is not None)

MOCK2_BAD = dict(MOCK2)
MOCK2_BAD["memories"] = [{**MOCK2["memories"][0], "memory_system": "semantic", "kind": "learned"}]
reject("2.5 rejects 'learned' as memory kind", MOCK2_BAD, TEXT2, "forbidden")
MOCK2_PROF = dict(MOCK2)
MOCK2_PROF["memories"] = [{**MOCK2["memories"][0], "memory_system": "semantic", "kind": "fact", "proficiency": 0.5}]
reject("2.6 rejects proficiency on non-procedural", MOCK2_PROF, TEXT2, "procedural")


# ===========================================================================
section("FIXTURE 3: 'I learned that I should validate demand before building.'")
# ===========================================================================

TEXT3 = "I learned that I should validate demand before building."
MOCK3: dict[str, Any] = {
    "observations": [{"content": TEXT3, "epistemic_status": "asserted",
                      "epistemic_owner": "owner",
                      "span": TEXT3[0:len(TEXT3)],
                      "extraction_confidence": 0.9, "source_reliability": 1.0}],
    "entities": [], "memories": [],
    "beliefs": [{"proposition": "Validate demand before building",
                 "belief_kind": "normative", "epistemic_owner": "owner",
                 "about_entity": None, "domain": "product",
                 "holding_confidence": 0.80, "truth_confidence": 0.65,
                 "evidence_span": TEXT3[0:len(TEXT3)]}],
    "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
}
r3 = accept("3.0 validator accepts fixture 3", MOCK3, TEXT3)
check("3.1 normative belief", r3.beliefs[0].belief_kind == "normative")
check("3.2 no memory", len(r3.memories) == 0)
check("3.3 epistemic_owner=owner", r3.beliefs[0].epistemic_owner == "owner")


# ===========================================================================
section("FIXTURE 4: 'I think this startup will work.' -- world_claim not self_report")
# ===========================================================================

TEXT4 = "I think this startup will work."
MOCK4: dict[str, Any] = {
    "observations": [{"content": TEXT4, "epistemic_status": "asserted",
                      "epistemic_owner": "owner",
                      "span": TEXT4[0:len(TEXT4)],
                      "extraction_confidence": 0.92, "source_reliability": 1.0}],
    "entities": [], "memories": [],
    "beliefs": [{"proposition": "This startup will work",
                 "belief_kind": "world_claim", "epistemic_owner": "owner",
                 "about_entity": None, "domain": None,
                 "holding_confidence": 0.72, "truth_confidence": 0.35,
                 "evidence_span": TEXT4[0:len(TEXT4)]}],
    "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
}
r4 = accept("4.0 validator accepts fixture 4", MOCK4, TEXT4)
check("4.1 world_claim (not self_report)", r4.beliefs[0].belief_kind == "world_claim")
check("4.2 holding > truth (owner asserts, world decides)", r4.beliefs[0].holding_confidence > r4.beliefs[0].truth_confidence)
check("4.3 epistemic_owner=owner", r4.beliefs[0].epistemic_owner == "owner")


# ===========================================================================
section("FIXTURE 5: 'I prefer working alone.' -- DUAL OUTPUT (rule §1 + §2)")
# classification.md §1: semantic/preference memory
# classification.md §2: self_report belief (holding IS the fact)
# Both must be present.
# ===========================================================================

TEXT5 = "I prefer working alone."
MOCK5: dict[str, Any] = {
    "observations": [{"content": TEXT5, "epistemic_status": "asserted",
                      "epistemic_owner": "owner",
                      "span": TEXT5[0:len(TEXT5)],
                      "extraction_confidence": 0.95, "source_reliability": 1.0}],
    "entities": [],
    "memories": [{"memory_system": "semantic", "kind": "preference",
                  "content": "Prefers working alone", "salience": 0.55,
                  "emotion_is_stated": False, "emotional_valence": None,
                  "emotional_intensity": None, "proficiency": None,
                  "evidence_span": TEXT5[0:len(TEXT5)]}],
    "beliefs": [{"proposition": "I prefer working alone",
                 "belief_kind": "self_report", "epistemic_owner": "owner",
                 "about_entity": None, "domain": "work_style",
                 "holding_confidence": 0.82, "truth_confidence": 0.82,
                 "evidence_span": TEXT5[0:len(TEXT5)]}],
    "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
}
r5 = accept("5.0 validator accepts fixture 5", MOCK5, TEXT5)
check("5.1 self_report belief present", any(b.belief_kind == "self_report" for b in r5.beliefs))
check("5.2 semantic/preference memory present",
      any(m.memory_system == "semantic" and m.kind == "preference" for m in r5.memories))
check("5.3 BOTH outputs present (classification.md §1+§2 dual-output rule)",
      len(r5.memories) >= 1 and len(r5.beliefs) >= 1)
check("5.4 self_report: holding_confidence == truth_confidence",
      abs(r5.beliefs[0].holding_confidence - r5.beliefs[0].truth_confidence) < 0.01)

# Reject: preference produces only the memory (missing self_report)
MOCK5_ONLY_MEM = dict(MOCK5)
MOCK5_ONLY_MEM["beliefs"] = []
r5_incomplete = accept("5.5 memory-only response is structurally valid", MOCK5_ONLY_MEM, TEXT5)
check("5.6 but documented spec requires both -- absence of belief is a classification gap",
      len(r5_incomplete.beliefs) == 0,
      "validator is structural-only; completeness is a prompt concern")


# ===========================================================================
section("FIXTURE 6: 'Priya thinks our strategy is too broad.'")
# ===========================================================================

TEXT6 = "Priya thinks our strategy is too broad."
MOCK6: dict[str, Any] = {
    "observations": [{"content": TEXT6, "epistemic_status": "reported",
                      "epistemic_owner": "other_person",
                      "span": TEXT6[0:len(TEXT6)],
                      "extraction_confidence": 0.9, "source_reliability": 1.0}],
    "entities": [{"canonical_name": "Priya", "kind": "person",
                  "mention_span": TEXT6[0:5]}],
    "memories": [],
    "beliefs": [{"proposition": "Our strategy is too broad",
                 "belief_kind": "attributed", "epistemic_owner": "other_person",
                 "about_entity": "Priya", "domain": "strategy",
                 "holding_confidence": 0.72, "truth_confidence": 0.25,
                 "evidence_span": TEXT6[0:len(TEXT6)]}],
    "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
}
r6 = accept("6.0 validator accepts fixture 6", MOCK6, TEXT6)
check("6.1 attributed belief", r6.beliefs[0].belief_kind == "attributed")
check("6.2 about_entity=Priya", r6.beliefs[0].about_entity == "Priya")
check("6.3 epistemic_owner=other_person", r6.beliefs[0].epistemic_owner == "other_person")

MOCK6_NO_ABOUT = dict(MOCK6)
MOCK6_NO_ABOUT["beliefs"] = [{**MOCK6["beliefs"][0], "about_entity": None}]
reject("6.4 rejects attributed without about_entity", MOCK6_NO_ABOUT, TEXT6, "about_entity")


# ===========================================================================
section("FIXTURE 7: 'Priya leads the team.'")
# ===========================================================================

TEXT7 = "Priya leads the team."
MOCK7: dict[str, Any] = {
    "observations": [{"content": TEXT7, "epistemic_status": "asserted",
                      "epistemic_owner": "owner",
                      "span": TEXT7[0:len(TEXT7)],
                      "extraction_confidence": 0.95, "source_reliability": 1.0}],
    "entities": [{"canonical_name": "Priya", "kind": "person",
                  "mention_span": TEXT7[0:5]}],
    "memories": [{"memory_system": "semantic", "kind": "person_attribute",
                  "content": "Priya leads the team", "salience": 0.55,
                  "emotion_is_stated": False, "emotional_valence": None,
                  "emotional_intensity": None, "proficiency": None,
                  "evidence_span": TEXT7[0:len(TEXT7)],
                  "involved_entities": [{"canonical_name": "Priya", "role": "subject"}]}],
    "beliefs": [], "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
}
r7 = accept("7.0 validator accepts fixture 7", MOCK7, TEXT7)
check("7.1 semantic/person_attribute", r7.memories[0].memory_system == "semantic" and r7.memories[0].kind == "person_attribute")
check("7.2 entity Priya/person", r7.entities[0].canonical_name == "Priya")
check("7.3 no beliefs", len(r7.beliefs) == 0)
check("7.4 memory has one involved_entities link",
      len(r7.memories[0].involved_entities) == 1)
check("7.5 involved_entities link is Priya/subject",
      r7.memories[0].involved_entities[0].canonical_name == "Priya"
      and r7.memories[0].involved_entities[0].role == "subject")

# Entity name matching is case-insensitive against entities[]
MOCK7_CASE = dict(MOCK7)
MOCK7_CASE["memories"] = [{**MOCK7["memories"][0],
                            "involved_entities": [{"canonical_name": "PRIYA", "role": "subject"}]}]
r7b = accept("7.6 involved_entities matches entities[] case-insensitively", MOCK7_CASE, TEXT7)
check("7.7 canonical_name preserved as given", r7b.memories[0].involved_entities[0].canonical_name == "PRIYA")

MOCK7_UNKNOWN = dict(MOCK7)
MOCK7_UNKNOWN["memories"] = [{**MOCK7["memories"][0],
                               "involved_entities": [{"canonical_name": "Rahul", "role": "subject"}]}]
reject("7.8 rejects involved_entities not present in entities[]", MOCK7_UNKNOWN, TEXT7,
       "does not match any entity")

MOCK7_BAD_ROLE = dict(MOCK7)
MOCK7_BAD_ROLE["memories"] = [{**MOCK7["memories"][0],
                                "involved_entities": [{"canonical_name": "Priya", "role": "boss"}]}]
reject("7.9 rejects unknown involved_entities role", MOCK7_BAD_ROLE, TEXT7, "Invalid")

MOCK7_NULL_ROLE = dict(MOCK7)
MOCK7_NULL_ROLE["memories"] = [{**MOCK7["memories"][0],
                                 "involved_entities": [{"canonical_name": "Priya", "role": None}]}]
r7c = accept("7.10 accepts involved_entities with null role", MOCK7_NULL_ROLE, TEXT7)
check("7.11 role stays None", r7c.memories[0].involved_entities[0].role is None)


# ===========================================================================
section("FIXTURE 8: Belief revision -- 'I used to think X, but now I think Y.'")
# classification.md §9: retracted obs for old view + asserted obs for new +
# new normative belief + episode=reflection.
# ===========================================================================

TEXT8 = (
    "I used to think moving fast was everything, "
    "but now I think understanding the customer first matters more."
)
MOCK8: dict[str, Any] = {
    "observations": [
        {"content": "I used to think moving fast was everything",
         "epistemic_status": "retracted", "epistemic_owner": "owner",
         "span": TEXT8[0:43],
         "extraction_confidence": 0.88, "source_reliability": 1.0},
        {"content": "Understanding the customer first matters more",
         "epistemic_status": "asserted", "epistemic_owner": "owner",
         "span": TEXT8[48:len(TEXT8)],
         "extraction_confidence": 0.92, "source_reliability": 1.0},
    ],
    "entities": [], "memories": [],
    "beliefs": [{"proposition": "Understanding the customer first matters more than moving fast",
                 "belief_kind": "normative", "epistemic_owner": "owner",
                 "about_entity": None, "domain": "product",
                 "holding_confidence": 0.82, "truth_confidence": 0.65,
                 "evidence_span": TEXT8[48:len(TEXT8)]}],
    "episode_candidate": {"kind": "reflection",
                          "title": "Revision: speed vs. customer understanding",
                          "trigger": "explicit belief update"},
    "ambiguous": False, "ambiguity_note": "",
}
r8 = accept("8.0 validator accepts fixture 8", MOCK8, TEXT8)
check("8.1 retracted observation present (old view)",
      any(o.epistemic_status == "retracted" for o in r8.observations))
check("8.2 asserted observation present (new view)",
      any(o.epistemic_status == "asserted" for o in r8.observations))
check("8.3 new normative belief created", r8.beliefs[0].belief_kind == "normative")
check("8.4 no memory created (belief revision is a belief, not a memory)",
      len(r8.memories) == 0)
check("8.5 episode_candidate=reflection (classification.md §9 step requires explicit revision)",
      r8.episode_candidate is not None and r8.episode_candidate.kind == "reflection")
check("8.6 two observations (one per side of the revision)", len(r8.observations) == 2)


# ===========================================================================
section("FIXTURE 9: 'That failure really frustrated me.' -- emotion intensity")
# classification.md §8: stated emotion, intensity calibration rubric.
# "really" -> 0.6–0.7 range.
# ===========================================================================

TEXT9 = "That failure really frustrated me."
MOCK9: dict[str, Any] = {
    "observations": [{"content": TEXT9, "epistemic_status": "asserted",
                      "epistemic_owner": "owner",
                      "span": TEXT9[0:len(TEXT9)],
                      "extraction_confidence": 0.95, "source_reliability": 1.0}],
    "entities": [],
    "memories": [{"memory_system": "episodic", "kind": "event",
                  "content": "A failure that caused significant frustration",
                  "salience": 0.65,
                  "emotion_is_stated": True,
                  "emotional_valence": -0.65,
                  "emotional_intensity": 0.65,   # "really" -> 0.6–0.7
                  "proficiency": None,
                  "evidence_span": TEXT9[0:len(TEXT9)]}],
    "beliefs": [], "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
}
r9 = accept("9.0 validator accepts fixture 9", MOCK9, TEXT9)
check("9.1 episodic/event", r9.memories[0].memory_system == "episodic")
check("9.2 emotion_is_stated=true", r9.memories[0].emotion_is_stated)
check("9.3 valence is negative (frustration)", r9.memories[0].emotional_valence < 0)
check("9.4 intensity in 'really' range [0.6–0.7]",
      0.6 <= r9.memories[0].emotional_intensity <= 0.7,
      f"{r9.memories[0].emotional_intensity}")
reject("9.5 rejects emotion values when emotion_is_stated=false",
       {**MOCK9, "memories": [{**MOCK9["memories"][0], "emotion_is_stated": False}]},
       TEXT9, "emotion_is_stated")


# ===========================================================================
section("EMOTION INTENSITY RUBRIC -- linguistic anchor calibration")
# Tests that mock responses reflecting the rubric pass validation and that
# the intensity values fall in the documented ranges.
# ===========================================================================

def _make_emotion_mock(text: str, intensity: float, valence: float) -> dict:
    return {
        "observations": [{"content": text, "epistemic_status": "asserted",
                          "epistemic_owner": "owner",
                          "span": text[0:len(text)],
                          "extraction_confidence": 0.9, "source_reliability": 1.0}],
        "entities": [],
        "memories": [{"memory_system": "episodic", "kind": "event",
                      "content": text, "salience": 0.5,
                      "emotion_is_stated": True,
                      "emotional_valence": valence,
                      "emotional_intensity": intensity,
                      "proficiency": None,
                      "evidence_span": text[0:len(text)]}],
        "beliefs": [], "episode_candidate": None,
        "ambiguous": False, "ambiguity_note": "",
    }

# Mild: "a bit frustrated" -> 0.2–0.3
_r_mild = accept("E.1 mild emotion accepted",
                 _make_emotion_mock("I was a bit frustrated.", 0.25, -0.25),
                 "I was a bit frustrated.")
check("E.2 mild intensity in [0.2, 0.3]",
      0.2 <= _r_mild.memories[0].emotional_intensity <= 0.3,
      str(_r_mild.memories[0].emotional_intensity))

# Moderate: "felt frustrated" (no intensifier) -> 0.4–0.5
_r_mod = accept("E.3 moderate emotion accepted",
                _make_emotion_mock("I felt frustrated.", 0.45, -0.45),
                "I felt frustrated.")
check("E.4 moderate intensity in [0.4, 0.5]",
      0.4 <= _r_mod.memories[0].emotional_intensity <= 0.5,
      str(_r_mod.memories[0].emotional_intensity))

# Significant: "really" -> 0.6–0.7
_r_sig = accept("E.5 significant emotion accepted",
                _make_emotion_mock("That really excited me.", 0.65, 0.65),
                "That really excited me.")
check("E.6 significant intensity in [0.6, 0.7]",
      0.6 <= _r_sig.memories[0].emotional_intensity <= 0.7,
      str(_r_sig.memories[0].emotional_intensity))

# Strong: "very" -> 0.8–0.9
_r_strong = accept("E.7 strong emotion accepted",
                   _make_emotion_mock("I was very relieved.", 0.85, 0.85),
                   "I was very relieved.")
check("E.8 strong intensity in [0.8, 0.9]",
      0.8 <= _r_strong.memories[0].emotional_intensity <= 0.9,
      str(_r_strong.memories[0].emotional_intensity))

# Overwhelming: "devastated" -> 1.0
_r_over = accept("E.9 overwhelming emotion accepted",
                 _make_emotion_mock("I was devastated.", 1.0, -1.0),
                 "I was devastated.")
check("E.10 overwhelming intensity == 1.0",
      _r_over.memories[0].emotional_intensity == 1.0,
      str(_r_over.memories[0].emotional_intensity))

# Positive valence
_r_pos = accept("E.11 positive valence accepted",
                _make_emotion_mock("That was ecstatic.", 1.0, 1.0),
                "That was ecstatic.")
check("E.12 positive valence > 0", _r_pos.memories[0].emotional_valence > 0)


# ===========================================================================
section("FIXTURE 10: 'I learned a lot from that.' -- AMBIGUOUS")
# ===========================================================================

TEXT10 = "I learned a lot from that."
MOCK10: dict[str, Any] = {
    "observations": [{"content": TEXT10, "epistemic_status": "hypothesis",
                      "epistemic_owner": "owner",
                      "span": TEXT10[0:len(TEXT10)],
                      "extraction_confidence": 0.5, "source_reliability": 1.0}],
    "entities": [], "memories": [], "beliefs": [],
    "episode_candidate": None, "ambiguous": True,
    "ambiguity_note": (
        "Referent 'that' is unresolved. Cannot determine what was learned "
        "or which memory system applies."
    ),
}
r10 = accept("10.0 validator accepts fixture 10", MOCK10, TEXT10)
check("10.1 ambiguous=true", r10.ambiguous)
check("10.2 ambiguity_note non-empty", bool(r10.ambiguity_note))
check("10.3 no memories", len(r10.memories) == 0)
check("10.4 no beliefs", len(r10.beliefs) == 0)
check("10.5 observation status=hypothesis", r10.observations[0].epistemic_status == "hypothesis")

MOCK10_LESSON = dict(MOCK10)
MOCK10_LESSON["memories"] = [{"memory_system": "semantic", "kind": "lesson",
                               "content": "Learned a lot", "salience": 0.4,
                               "emotion_is_stated": False, "emotional_valence": None,
                               "emotional_intensity": None, "proficiency": None,
                               "evidence_span": TEXT10[0:len(TEXT10)]}]
reject("10.6 rejects 'lesson' memory kind", MOCK10_LESSON, TEXT10, "forbidden")


# ===========================================================================
section("CROSS-ONTOLOGY BOUNDARY CHECKS")
# ===========================================================================

TEXT_X = "Boundary check text."

reject("X.1 rejects skill under episodic", {
    "observations": [{"content": "x", "epistemic_status": "asserted",
                      "epistemic_owner": "owner", "span": TEXT_X[0:1]}],
    "memories": [{"memory_system": "episodic", "kind": "skill",
                  "content": "x", "salience": 0.5, "emotion_is_stated": False,
                  "evidence_span": TEXT_X[0:1]}],
    "beliefs": [], "entities": [], "episode_candidate": None,
    "ambiguous": False, "ambiguity_note": "",
}, TEXT_X, "does not belong to system")

reject("X.2 rejects a quote longer than the source text", {
    "observations": [{"content": "x", "epistemic_status": "asserted",
                      "epistemic_owner": "owner",
                      "span": TEXT_X + " and then some"}],
    "memories": [], "beliefs": [], "entities": [],
    "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
}, TEXT_X, "does not appear verbatim")

reject("X.3 rejects unknown epistemic_status", {
    "observations": [{"content": "x", "epistemic_status": "made_up",
                      "epistemic_owner": "owner", "span": TEXT_X[0:1]}],
    "memories": [], "beliefs": [], "entities": [],
    "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
}, TEXT_X)

reject("X.4 rejects unknown belief_kind", {
    "observations": [{"content": "x", "epistemic_status": "asserted",
                      "epistemic_owner": "owner", "span": TEXT_X[0:1]}],
    "memories": [],
    "beliefs": [{"proposition": "x", "belief_kind": "uncertain",
                 "epistemic_owner": "owner", "holding_confidence": 0.5,
                 "truth_confidence": 0.5, "evidence_span": TEXT_X[0:1]}],
    "entities": [], "episode_candidate": None,
    "ambiguous": False, "ambiguity_note": "",
}, TEXT_X)

reject("X.5 rejects attributed without about_entity", {
    "observations": [{"content": "x", "epistemic_status": "reported",
                      "epistemic_owner": "other_person", "span": TEXT_X[0:1]}],
    "memories": [],
    "beliefs": [{"proposition": "x", "belief_kind": "attributed",
                 "epistemic_owner": "other_person", "about_entity": None,
                 "holding_confidence": 0.5, "truth_confidence": 0.5,
                 "evidence_span": TEXT_X[0:1]}],
    "entities": [], "episode_candidate": None,
    "ambiguous": False, "ambiguity_note": "",
}, TEXT_X, "about_entity")

reject("X.6 rejects extraction_confidence > 1.0", {
    "observations": [{"content": "x", "epistemic_status": "asserted",
                      "epistemic_owner": "owner", "span": TEXT_X[0:1],
                      "extraction_confidence": 1.5}],
    "memories": [], "beliefs": [], "entities": [],
    "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
}, TEXT_X)


# ===========================================================================
section("SPAN RESOLUTION -- exact-quote contract regression tests")
# The live calibration run (2026-09-01) found that asking the model for
# numeric {start, end} offsets produced a constant-per-fixture overrun on
# every span reaching end-of-text, on both Haiku 4.5 and Sonnet 5 -- a model
# reliably miscounts characters near the end of a text. The contract was
# replaced: the model returns an exact quote; the application locates it in
# the (immutable) source text itself via core.extractor._resolve_span(), so
# a resolved span can never exceed the source length by construction. These
# tests cover the specific failure shapes that motivated the change, plus
# the deterministic-selection and fail-closed rules the new contract adds.
# ===========================================================================

def _span_mock(text: str, span_quote: str) -> dict:
    return {
        "observations": [{"content": text, "epistemic_status": "asserted",
                          "epistemic_owner": "owner", "span": span_quote}],
        "memories": [], "beliefs": [], "entities": [],
        "episode_candidate": None, "ambiguous": False, "ambiguity_note": "",
    }

# SPAN.1 -- final-span overrun (the original defect): a quote reaching the
# very end of the source text must resolve to end == len(text) exactly, on
# every fixture length, never past it.
_TEXT_END = "The rollout finally reached the finish line."
_r_end = accept("SPAN.1 quote reaching end-of-text resolves without overrun",
                 _span_mock(_TEXT_END, "reached the finish line."), _TEXT_END)
check("SPAN.1b resolved span.end == len(source), never beyond",
      bool(_r_end.observations) and _r_end.observations[0].span.end == len(_TEXT_END),
      f"end={_r_end.observations[0].span.end if _r_end.observations else None} len={len(_TEXT_END)}")

# SPAN.2 -- source text ending at punctuation: the quote's final character
# is the source's final character (a "?"), which must resolve cleanly.
_TEXT_PUNCT = "Should the team switch frameworks?"
_r_punct = accept("SPAN.2 quote ending at trailing punctuation resolves",
                   _span_mock(_TEXT_PUNCT, "switch frameworks?"), _TEXT_PUNCT)
check("SPAN.2b resolved span covers through the final '?'",
      bool(_r_punct.observations) and _r_punct.observations[0].span.end == len(_TEXT_PUNCT),
      f"end={_r_punct.observations[0].span.end if _r_punct.observations else None} len={len(_TEXT_PUNCT)}")

# SPAN.3 -- repeated identical phrases: the quote occurs twice: deterministic
# selection rule is documented in _resolve_span() as "first (leftmost)
# occurrence" -- verify that is exactly what gets picked, not the second.
_TEXT_REPEAT = "I called Sam about the outage. Later I called Sam again to follow up."
_first_occurrence = _TEXT_REPEAT.find("called Sam")
_second_occurrence = _TEXT_REPEAT.find("called Sam", _first_occurrence + 1)
assert _first_occurrence != -1 and _second_occurrence != -1 and _first_occurrence != _second_occurrence
_r_repeat = accept("SPAN.3 repeated quote accepted",
                    _span_mock(_TEXT_REPEAT, "called Sam"), _TEXT_REPEAT)
check("SPAN.3b resolves to the FIRST (leftmost) occurrence, not the second",
      bool(_r_repeat.observations) and _r_repeat.observations[0].span.start == _first_occurrence,
      f"resolved_start={_r_repeat.observations[0].span.start if _r_repeat.observations else None} "
      f"first={_first_occurrence} second={_second_occurrence}")

# SPAN.4 -- quote not found: fabricated text absent from the source must
# fail closed, not silently approximate to the nearest match.
reject("SPAN.4 rejects a quote absent from the source text",
       _span_mock(_TEXT_END, "reached the podium first"), _TEXT_END,
       "does not appear verbatim")

# SPAN.5 -- quote changed slightly by the model (near-verbatim, not exact):
# one real word swapped for a close paraphrase. Must fail closed -- the
# whole point of this contract is no fuzzy/partial matching.
_TEXT_SLIGHT = "Kubernetes clusters need resource limits to run smoothly."
reject("SPAN.5 rejects a near-verbatim (paraphrased) quote",
       _span_mock(_TEXT_SLIGHT, "Kubernetes cluster needs resource limits"), _TEXT_SLIGHT,
       "does not appear verbatim")

# SPAN.6 -- Unicode / non-ASCII text: accented characters, CJK, and an
# emoji. Python string indexing is codepoint-based so this should just
# work, but the interface changed specifically around character counting,
# so it is worth proving directly rather than assuming.
_TEXT_UNICODE = "Met José at the café — 会議は順調だった \U0001F389"
_r_uni = accept("SPAN.6 Unicode/non-ASCII quote resolves correctly",
                _span_mock(_TEXT_UNICODE, "café — 会議は順調だった \U0001F389"),
                _TEXT_UNICODE)
check("SPAN.6b resolved span reaches exactly len(source), incl. the emoji",
      bool(_r_uni.observations) and _r_uni.observations[0].span.end == len(_TEXT_UNICODE),
      f"end={_r_uni.observations[0].span.end if _r_uni.observations else None} len={len(_TEXT_UNICODE)}")


# ===========================================================================
section("LLM CLIENT -- content block selection")
# Regression: complete() used to return msg.content[0].text, which crashed with
# AttributeError on every thinking-enabled model (the first block is a
# ThinkingBlock with no .text). That silently confined the whole extraction
# pipeline to non-thinking models. The thinking stub below deliberately has no
# .text attribute, so indexing content[0] cannot pass this test.

class _ThinkingBlockStub:
    type = "thinking"
    thinking = "considering the ontology"


class _TextBlockStub:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _MessageStub:
    def __init__(self, content: list) -> None:
        self.content = content


class _MessagesStub:
    def __init__(self, content: list) -> None:
        self._content = content

    def create(self, **kwargs: Any) -> _MessageStub:
        return _MessageStub(self._content)


class _AnthropicModuleStub:
    """Stands in for the anthropic package -- complete() imports it lazily."""

    def __init__(self, content: list) -> None:
        self._content = content

    def Anthropic(self, *a: Any, **kw: Any) -> Any:
        client = type("ClientStub", (), {})()
        client.messages = _MessagesStub(self._content)
        return client


def _complete_with(content: list) -> str:
    from core.extractor import AnthropicLLMClient
    saved = sys.modules.get("anthropic")
    sys.modules["anthropic"] = _AnthropicModuleStub(content)
    try:
        return AnthropicLLMClient().complete("sys", "user")
    finally:
        if saved is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = saved


_JSON = '{"observations": []}'

try:
    _got = _complete_with([_ThinkingBlockStub(), _TextBlockStub(_JSON)])
    check("C.1 returns text when a ThinkingBlock precedes it", _got == _JSON, repr(_got))
except Exception as _e:
    check("C.1 returns text when a ThinkingBlock precedes it", False, f"{type(_e).__name__}: {_e}")

try:
    _got = _complete_with([_TextBlockStub(_JSON)])
    check("C.2 text-only response unchanged (Haiku 4.5 shape)", _got == _JSON, repr(_got))
except Exception as _e:
    check("C.2 text-only response unchanged (Haiku 4.5 shape)", False, f"{type(_e).__name__}: {_e}")

try:
    _got = _complete_with([_ThinkingBlockStub(), _ThinkingBlockStub(), _TextBlockStub(_JSON)])
    check("C.3 skips multiple leading non-text blocks", _got == _JSON, repr(_got))
except Exception as _e:
    check("C.3 skips multiple leading non-text blocks", False, f"{type(_e).__name__}: {_e}")


# ===========================================================================
print()
print("=" * 62)
print(f"  {len(PASSES)} passed, {len(FAILURES)} failed")
print("=" * 62)
for f in FAILURES:
    print(f"  FAILED:  {f}")
sys.exit(1 if FAILURES else 0)
