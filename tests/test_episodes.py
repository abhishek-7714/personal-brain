"""
tests/test_episodes.py

Persistence tests for the episode model (migrations/007_episode_model.sql):
proves core.ingest.ingest() actually creates episodes / cognitive_traces /
episode-scoped state_items / decisions / action-memories, not just that
core.extractor validates a candidate that then gets discarded.

Same convention as tests/test_api.py and tests/test_e2e_console.py: reuses
the existing owner (a strict singleton), relies on ingest()'s
(owner_id, content_hash) idempotency rather than any cleanup
(experiences/observations/cognitive_traces are append-only by design, and
episodes/decisions/state_items linked to them are left in place rather than
force-deleted), and every fixture comes from core.calibration_fixtures --
the same file the Brain Console's Calibration page reads, so clicking a
fixture there exercises exactly what this file asserts.

Run:
    python tests/test_episodes.py

Exit codes: 0 all passed, 1 failures present, 3 stack not reachable.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
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
    print("      Start with: docker compose up -d")
    sys.exit(3)

# Before any write: this suite seeds an owner and commits fixture ingests.
from _db_guard import require_scratch_database
require_scratch_database("test_episodes")

import psycopg2

from core.calibration_fixtures import get_fixture
from core.ingest import ingest
from core.storage import MockStorageClient

UTC = timezone.utc
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


class MockLLMClient:
    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, system: str, user: str) -> str:
        return self._response


PG_DSN = os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain")
conn = psycopg2.connect(PG_DSN)
conn.autocommit = False
cur = conn.cursor()

cur.execute("SELECT id FROM owner LIMIT 1")
row = cur.fetchone()
if row is None:
    cur.execute(
        "INSERT INTO owner (display_name, timezone) VALUES (%s, %s) RETURNING id",
        ("Dev Console", "UTC"),
    )
    owner_id = str(cur.fetchone()[0])
    conn.commit()
else:
    owner_id = str(row[0])


def _ingest_fixture(key: str):
    fixture = get_fixture(key)
    assert fixture is not None, f"unknown fixture {key}"
    occurred_at = None
    if fixture.occurred_at_offset_days is not None:
        occurred_at = datetime.now(UTC) + timedelta(days=fixture.occurred_at_offset_days)
    result = ingest(
        fixture.text, fixture.source_type, conn=conn, owner_id=owner_id,
        client=MockLLMClient(fixture.mock_json), storage=MockStorageClient(),
        occurred_at=occurred_at,
    )
    conn.commit()
    return fixture, result


def _fetch_episode(experience_id: str):
    cur.execute(
        """
        SELECT ep.id, ep.kind, ep.title, ep.outcome, ep.outcome_valence
        FROM episodes ep
        JOIN episode_experiences ee ON ee.episode_id = ep.id
        WHERE ee.experience_id = %s
        """,
        (experience_id,),
    )
    return cur.fetchone()


def _fetch_traces(episode_id: str):
    cur.execute(
        "SELECT seq, operation, target, observation_id FROM cognitive_traces "
        "WHERE episode_id = %s ORDER BY seq",
        (episode_id,),
    )
    return cur.fetchall()


def _fetch_state_items(episode_id: str):
    cur.execute(
        "SELECT kind, content, priority FROM state_items WHERE episode_id = %s",
        (episode_id,),
    )
    return cur.fetchall()


def _fetch_decisions(episode_id: str):
    cur.execute(
        "SELECT id, question, chosen_option, outcome, outcome_valence FROM decisions "
        "WHERE episode_id = %s",
        (episode_id,),
    )
    return cur.fetchall()


def _fetch_observation_ids(experience_id: str) -> set[str]:
    cur.execute("SELECT id FROM observations WHERE experience_id = %s", (experience_id,))
    return {str(r[0]) for r in cur.fetchall()}


def verify_common(key: str, expect_traces: int, expect_state_items: int, expect_decision: bool):
    """The eight things every episode-bearing fixture must satisfy,
    regardless of its specific content."""
    fixture, result = _ingest_fixture(key)
    exp_id = str(result.experience_id)

    # result.episode_created means "created by THIS call" -- on a rerun of
    # this file (a later process, not the idempotency check further down),
    # ingest() correctly returns early with created=False/episode_created=
    # False because the fixture already exists from a prior run. The actual
    # invariant this file cares about is "the episode exists", checked via
    # _fetch_episode below regardless of which call created it.
    if result.created:
        check(f"{key}: episode creation", result.episode_created)
    ep = _fetch_episode(exp_id)
    check(f"{key}: episode row exists", ep is not None)
    check(f"{key}: episode_experiences linkage", ep is not None)  # _fetch_episode already joins through it
    if ep is None:
        return None

    episode_id = str(ep[0])
    obs_ids = _fetch_observation_ids(exp_id)

    traces = _fetch_traces(episode_id)
    check(f"{key}: cognitive trace sequence has {expect_traces} step(s), seq 1..N",
          len(traces) == expect_traces and [t[0] for t in traces] == list(range(1, expect_traces + 1)),
          str([t[0] for t in traces]))
    check(f"{key}: every trace cites a real observation from this experience (provenance)",
          all(str(t[3]) in obs_ids for t in traces))

    state_items = _fetch_state_items(episode_id)
    check(f"{key}: state_item episode scoping ({expect_state_items} expected)",
          len(state_items) == expect_state_items)

    decisions = _fetch_decisions(episode_id)
    check(f"{key}: decision linkage where appropriate (expected={expect_decision})",
          bool(decisions) == expect_decision)

    # Outcome convention: decision.outcome canonical when a decision exists;
    # episode.outcome null in that case. When no decision, episode.outcome
    # may be populated directly.
    episode_outcome = ep[3]
    if decisions:
        check(f"{key}: outcome convention -- episode.outcome is NULL when a decision exists",
              episode_outcome is None)
    # (episode.outcome may or may not be set when there's no decision --
    # fixture-specific assertions below check the cases that matter.)

    # Idempotency: re-ingesting the same fixture must not create a second episode.
    _, result2 = _ingest_fixture(key)
    check(f"{key}: no duplicate episode on idempotent ingestion",
          result2.created is False and str(result2.experience_id) == exp_id)
    cur.execute(
        "SELECT count(*) FROM episode_experiences WHERE experience_id = %s", (exp_id,)
    )
    check(f"{key}: exactly one episode_experiences row after rerun", cur.fetchone()[0] == 1)

    return fixture, result, ep, traces, state_items, decisions


# ===========================================================================
section("1/9. Decision episode: unknown -> information seeking -> decision")
# ===========================================================================
r = verify_common("episode_decision_unknown_to_decision", expect_traces=3, expect_state_items=0, expect_decision=True)
if r:
    _, result, ep, traces, _, decisions = r
    ops = [t[1] for t in traces]
    check("F: trace ops are identify_unknown -> seek_information -> decide",
          ops == ["identify_unknown", "seek_information", "decide"], str(ops))
    cur.execute("SELECT id FROM memories WHERE owner_id = %s AND episode_id = %s",
                (owner_id, str(ep[0])))
    action_mems = cur.fetchall()
    check("F: action memory linked to the episode", len(action_mems) >= 1)


# ===========================================================================
section("2. Learning episode")
# ===========================================================================
r = verify_common("episode_learning", expect_traces=2, expect_state_items=0, expect_decision=False)
if r:
    _, result, ep, traces, _, _ = r
    check("G: episode kind = learning", ep[1] == "learning")


# ===========================================================================
section("3. Reflection episode (belief revision)")
# ===========================================================================
r = verify_common("episode_reflection_belief_revision", expect_traces=2, expect_state_items=0, expect_decision=False)
if r:
    fixture, result, ep, traces, _, _ = r
    check("H: episode kind = reflection", ep[1] == "reflection")
    exp_id = str(result.experience_id)
    cur.execute(
        "SELECT epistemic_status FROM observations WHERE experience_id = %s ORDER BY observed_at", (exp_id,)
    )
    statuses = [row[0] for row in cur.fetchall()]
    check("H: one retracted + one asserted observation (belief revision shape)",
          "retracted" in statuses and "asserted" in statuses, str(statuses))
    cur.execute(
        "SELECT proposition, belief_kind FROM beliefs WHERE owner_id = %s AND proposition LIKE %s",
        (owner_id, "Validating demand%"),
    )
    check("H: new belief recorded (not merged into the episode)", cur.fetchone() is not None)


# ===========================================================================
section("4. Social/conflict episode")
# ===========================================================================
r = verify_common("episode_social_conflict", expect_traces=2, expect_state_items=1, expect_decision=True)
if r:
    _, result, ep, traces, state_items, decisions = r
    check("I: episode kind = conflict", ep[1] == "conflict")
    check("I: state item is kind='uncertainty'", state_items[0][0] == "uncertainty")
    check("I: decision left undecided (deferred, chosen_option NULL)", decisions[0][2] is None)


# ===========================================================================
section("5. Creative/ideation episode")
# ===========================================================================
r = verify_common("episode_creative_ideation", expect_traces=2, expect_state_items=0, expect_decision=False)
if r:
    _, result, ep, traces, _, _ = r
    check("J: episode kind = creative", ep[1] == "creative")
    ops = [t[1] for t in traces]
    check("J: generate_option then evaluate_option", ops == ["generate_option", "evaluate_option"], str(ops))


# ===========================================================================
section("6. Episode with explicit goal")
# ===========================================================================
r = verify_common("episode_explicit_goal", expect_traces=2, expect_state_items=1, expect_decision=False)
if r:
    _, result, ep, traces, state_items, _ = r
    check("K: state item is kind='goal', not episodes.goal (no such column)", state_items[0][0] == "goal")


# ===========================================================================
section("7. Episode with attention shift")
# ===========================================================================
r = verify_common("episode_attention_shift", expect_traces=1, expect_state_items=2, expect_decision=False)
if r:
    _, result, ep, traces, state_items, _ = r
    kinds = {s[0] for s in state_items}
    check("L: both state items are kind='attention' (before/after shift)", kinds == {"attention"})
    priorities = sorted(s[2] for s in state_items)
    check("L: priorities differ (old focus deprioritized, new one prioritized)",
          priorities[0] < priorities[1], str(priorities))


# ===========================================================================
section("8. Episode with hypothesis")
# ===========================================================================
r = verify_common("episode_hypothesis", expect_traces=2, expect_state_items=0, expect_decision=False)
if r:
    _, result, ep, traces, _, _ = r
    ops = [t[1] for t in traces]
    check("M: uses the new 'hypothesize' op", "hypothesize" in ops, str(ops))


# ===========================================================================
section("9. Decision episode with outcome (decision.outcome canonical)")
# ===========================================================================
r = verify_common("episode_decision_with_outcome", expect_traces=1, expect_state_items=0, expect_decision=True)
if r:
    _, result, ep, traces, _, decisions = r
    check("N: episode.outcome is NULL (decision exists)", ep[3] is None)
    check("N: decision.outcome is populated", decisions[0][3] is not None)
    check("N: decision.outcome_valence is populated", decisions[0][4] is not None)


# ===========================================================================
section("10. Episode without a decision but with outcome")
# ===========================================================================
r = verify_common("episode_outcome_without_decision", expect_traces=1, expect_state_items=0, expect_decision=False)
if r:
    _, result, ep, traces, _, decisions = r
    check("O: no decision row", len(decisions) == 0)
    check("O: episode.outcome IS populated (no decision to be canonical instead)", ep[3] is not None)
    check("O: episode.outcome_valence is populated", ep[4] is not None)


# ===========================================================================
print()
print("=" * 62)
print(f"  {len(PASSES)} passed, {len(FAILURES)} failed")
print("=" * 62)
for f in FAILURES:
    print(f"  FAILED:  {f}")

conn.close()

print(
    "\nNote: this file permanently ingests deterministic episode calibration "
    "fixtures (idempotent). No cleanup is performed or possible; "
    "experiences/observations/cognitive_traces are append-only by design."
)

sys.exit(1 if FAILURES else 0)
