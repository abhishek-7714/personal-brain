"""
tests/test_calibration_eval.py

Regression tests for the calibration evaluation framework itself.

These test the INSTRUMENT, not the extractor: that assertions actually fire
on bad input, that the framework never silently repairs anything, and that
the mock path scores the full 15-fixture corpus. Live-extractor behaviour is
evaluated separately (core.calibration_eval --live) and is deliberately not
part of the routine suite -- it costs API calls and its results are findings
to read, not assertions to pass.

Run:
    python tests/test_calibration_eval.py

Exit codes: 0 all passed, 1 failures present, 3 stack not reachable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _check_stack() -> tuple[bool, str]:
    try:
        import psycopg2
        conn = psycopg2.connect(
            os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain"),
            connect_timeout=5,
        )
        conn.close()
    except Exception as e:
        return False, f"Postgres unreachable: {e}"
    return True, "ok"


ok, msg = _check_stack()
if not ok:
    print(f"SKIP  Stack not reachable: {msg}")
    sys.exit(3)

import psycopg2

from core.calibration_assertions import (
    DIMENSIONS,
    FIXTURE_ASSERTIONS,
    UNIVERSAL_ASSERTIONS,
    assertions_for,
    trace_grounding,
)
from core.calibration_eval import evaluate_all, evaluate_mock, score_view
from core.calibration_fixtures import FIXTURES, get_fixture
from core.calibration_view import (
    CanonicalView,
    ViewBelief,
    ViewEpisode,
    ViewMemory,
    ViewTrace,
    view_from_extraction,
)

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


# ===========================================================================
section("COVERAGE: every fixture has assertions across meaningful dimensions")
# ===========================================================================

check("C.1 every fixture in the corpus has its own assertions",
      all(f.key in FIXTURE_ASSERTIONS for f in FIXTURES),
      str([f.key for f in FIXTURES if f.key not in FIXTURE_ASSERTIONS]))

check("C.2 all 15 fixtures covered", len(FIXTURE_ASSERTIONS) == 15, str(len(FIXTURE_ASSERTIONS)))

all_dims = {a.dimension for a in UNIVERSAL_ASSERTIONS}
for key in FIXTURE_ASSERTIONS:
    all_dims |= {a.dimension for a in FIXTURE_ASSERTIONS[key]}
check("C.3 the assertion corpus spans all 12 evaluation dimensions",
      all_dims == set(DIMENSIONS), f"missing={set(DIMENSIONS) - all_dims}")

check("C.4 every assertion code is unique within its fixture",
      all(len({a.code for a in assertions_for(f.key)}) == len(assertions_for(f.key)) for f in FIXTURES))

check("C.5 every fixture has at least one essential assertion",
      all(any(a.essential for a in assertions_for(f.key)) for f in FIXTURES))


# ===========================================================================
section("THE INSTRUMENT ACTUALLY FIRES: assertions fail on deliberately bad input")
# ===========================================================================

def _bad_view(**overrides) -> CanonicalView:
    base = dict(
        source_text="I read the docs and then decided to ship it.",
        observations=[], memories=[], beliefs=[], entities=[], episode=None,
    )
    base.update(overrides)
    return CanonicalView(**base)


# Memory with no evidence must trip U-PROV-1.
v = _bad_view(memories=[ViewMemory(content="x", memory_system="episodic", kind="event",
                                    salience=0.5, emotion_is_stated=False,
                                    emotional_valence=None, emotional_intensity=None,
                                    evidence_count=0)])
res = {r.code: r for r in score_view("old_episodic", v)}
check("I.1 U-PROV-1 fails when a memory carries no evidence", not res["U-PROV-1"].passed, res["U-PROV-1"].detail)

# Emotion claimed with no emotion word in the text must trip U-OVR-1.
v = _bad_view(memories=[ViewMemory(content="x", memory_system="episodic", kind="event",
                                    salience=0.5, emotion_is_stated=True,
                                    emotional_valence=0.5, emotional_intensity=0.5,
                                    evidence_count=1)])
res = {r.code: r for r in score_view("old_episodic", v)}
check("I.2 U-OVR-1 fails when stated emotion is claimed but the text names none",
      not res["U-OVR-1"].passed, res["U-OVR-1"].detail)

# The same claim IS allowed when the text actually names an emotion.
v = _bad_view(source_text="I was frustrated by the outage.",
              memories=[ViewMemory(content="x", memory_system="episodic", kind="event",
                                    salience=0.5, emotion_is_stated=True,
                                    emotional_valence=-0.5, emotional_intensity=0.6,
                                    evidence_count=1)])
res = {r.code: r for r in score_view("old_episodic", v)}
check("I.3 U-OVR-1 passes when the text does name an emotion", res["U-OVR-1"].passed, res["U-OVR-1"].detail)

# Empty episode shell must trip U-OVR-2.
v = _bad_view(episode=ViewEpisode(kind="decision", title="t", trigger=None, narrative=None,
                                   resolution=None, outcome=None, outcome_valence=None))
res = {r.code: r for r in score_view("old_episodic", v)}
check("I.4 U-OVR-2 fails on an empty episode shell", not res["U-OVR-2"].passed, res["U-OVR-2"].detail)

# Outcome invariant violation must trip U-EPI-1.
from core.calibration_view import ViewDecision
v = _bad_view(episode=ViewEpisode(
    kind="decision", title="t", trigger=None, narrative=None, resolution=None,
    outcome="it went fine", outcome_valence=0.5,
    decisions=[ViewDecision(question="q", chosen_option="a", options_count=1,
                             reversibility=None, outcome=None, outcome_valence=None, reflection=None)],
))
res = {r.code: r for r in score_view("old_episodic", v)}
check("I.5 U-EPI-1 fails when both decision and episode.outcome are set",
      not res["U-EPI-1"].passed, res["U-EPI-1"].detail)

# Dangling about_entity must trip U-ENT-1.
v = _bad_view(beliefs=[ViewBelief(proposition="p", belief_kind="attributed", epistemic_owner="other_person",
                                   about_entity="Nobody", domain=None, truth_confidence=0.5,
                                   holding_confidence=0.5, evidence_count=1)])
res = {r.code: r for r in score_view("old_episodic", v)}
check("I.6 U-ENT-1 fails on a belief.about_entity that resolves to nothing",
      not res["U-ENT-1"].passed, res["U-ENT-1"].detail)

# An assertion that raises is reported as a failure, never as a pass.
from core.calibration_assertions import Assertion
boom = Assertion("X-BOOM", "over_inference", "raises", True,
                  lambda v: (_ for _ in ()).throw(RuntimeError("kaboom")))
r = score_view("old_episodic", _bad_view())
import core.calibration_assertions as ca
_saved = ca.FIXTURE_ASSERTIONS.get("old_episodic")
ca.FIXTURE_ASSERTIONS["old_episodic"] = [boom]
res = {x.code: x for x in score_view("old_episodic", _bad_view())}
ca.FIXTURE_ASSERTIONS["old_episodic"] = _saved
check("I.7 an assertion that raises is reported as a FAILURE, not swallowed",
      not res["X-BOOM"].passed and "kaboom" in res["X-BOOM"].detail, res["X-BOOM"].detail)


# ===========================================================================
section("TRACE GROUNDING heuristic")
# ===========================================================================

v = _bad_view(
    source_text="I read the docs and then decided to ship it.",
    episode=ViewEpisode(
        kind="decision", title="t", trigger=None, narrative=None, resolution=None,
        outcome=None, outcome_valence=None,
        traces=[
            ViewTrace(seq=1, operation="seek_information", target="read the docs", detail=None, cites_observation=True),
            ViewTrace(seq=2, operation="estimate", target="quarterly revenue projections", detail=None, cites_observation=True),
        ],
    ),
)
g = {op: ratio for op, _, ratio in trace_grounding(v)}
check("G.1 a trace grounded in the source text scores > 0", g["seek_information"] > 0, str(g))
check("G.2 a trace using vocabulary absent from the source scores 0.0 (invented cognition)",
      g["estimate"] == 0.0, str(g))


# ===========================================================================
section("MOCK PATH: full 15-fixture corpus scores cleanly")
# ===========================================================================

conn = psycopg2.connect(os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain"))
cur = conn.cursor()
cur.execute("SELECT id FROM owner LIMIT 1")
row = cur.fetchone()
if row is None:
    cur.execute("INSERT INTO owner (display_name, timezone) VALUES (%s,%s) RETURNING id", ("Dev Console", "UTC"))
    owner_id = str(cur.fetchone()[0])
    conn.commit()
else:
    owner_id = str(row[0])

evaluations = evaluate_all(mode="mock", conn=conn, owner_id=owner_id)
check("M.1 all 15 fixtures scored", len(evaluations) == 15 and all(e.status == "scored" for e in evaluations),
      str([(e.fixture_key, e.status) for e in evaluations if e.status != "scored"]))

essential_failures = [(e.fixture_key, [f.code for f in e.essential_failures])
                      for e in evaluations if e.essential_failures]
check("M.2 no essential failures on the mock path (representation layer is faithful)",
      not essential_failures, str(essential_failures))

check("M.3 every fixture produced a non-trivial number of assertions",
      all(len(e.results) >= 12 for e in evaluations),
      str([(e.fixture_key, len(e.results)) for e in evaluations]))

# The framework must not be silently vacuous: a corpus where nothing is ever
# checked would also report zero failures.
total_assertions = sum(len(e.results) for e in evaluations)
check("M.4 the corpus actually evaluates a substantial number of assertions",
      total_assertions >= 200, f"{total_assertions} assertions scored")

conn.close()


# ===========================================================================
print()
print("=" * 62)
print(f"  {len(PASSES)} passed, {len(FAILURES)} failed")
print("=" * 62)
for f in FAILURES:
    print(f"  FAILED:  {f}")

sys.exit(1 if FAILURES else 0)
