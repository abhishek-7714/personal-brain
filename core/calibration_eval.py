"""
core/calibration_eval.py

Scores what the extraction pipeline produced for a calibration fixture
against the expected semantic assertions in core/calibration_assertions.py.

THIS MODULE NEVER REPAIRS ANYTHING. It does not normalize, coerce, retry,
re-prompt, or "helpfully" reinterpret extractor output. A failing assertion
is reported as a failure with the actual value that caused it, because the
entire point is to see exactly where extraction is wrong. If you find
yourself wanting to add a fallback here, add a finding to the report
instead.

Two modes, deliberately not interchangeable:

  mock  -- ingest the fixture through the pipeline with its hand-written
           MockLLM response, then read the canonical representation back out
           of Postgres via core.reader. Scores the REPRESENTATION LAYER:
           does extract -> validate -> ingest -> Postgres -> reader preserve
           a correct extraction faithfully? It says nothing about whether a
           real extractor would produce that extraction, because the JSON
           was written by hand.

  live  -- call the real LLM on the fixture's source text and score its
           ExtractionResult directly. Scores the EXTRACTOR. It does not
           persist: the mock fixture already occupies that text's
           content_hash, so ingest() would dedupe against it and the live
           extraction would never be stored. Nothing is written to Postgres
           in this mode at all.

A live run that fails ontology validation outright is itself a first-class
result (status="validation_error"), not a crash -- "the extractor emitted
JSON the ontology rejects" is exactly the kind of finding this pass exists
to surface.

CLI:
    python -m core.calibration_eval                 # mock, all fixtures
    python -m core.calibration_eval --live          # real extractor (costs money)
    python -m core.calibration_eval --live --model claude-sonnet-5
    python -m core.calibration_eval --fixture KEY
    python -m core.calibration_eval --json
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.calibration_assertions import Assertion, assertions_for, trace_grounding
from core.calibration_fixtures import FIXTURES, CalibrationFixture, get_fixture
from core.calibration_view import CanonicalView, view_from_extraction, view_from_inspection

UTC = timezone.utc


@dataclass
class AssertionResult:
    code: str
    dimension: str
    description: str
    essential: bool
    passed: bool
    detail: str

    @property
    def status(self) -> str:
        if self.passed:
            return "pass"
        return "FAIL" if self.essential else "advisory"


@dataclass
class FixtureEvaluation:
    fixture_key: str
    label: str
    mode: str                       # "mock" | "live"
    status: str                     # "scored" | "validation_error" | "extraction_error"
    error: Optional[str] = None
    results: list[AssertionResult] = field(default_factory=list)
    trace_grounding: list[tuple[str, str, float]] = field(default_factory=list)
    experience_id: Optional[str] = None   # mock mode only

    @property
    def essential_failures(self) -> list[AssertionResult]:
        return [r for r in self.results if not r.passed and r.essential]

    @property
    def advisories(self) -> list[AssertionResult]:
        return [r for r in self.results if not r.passed and not r.essential]

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def ok(self) -> bool:
        return self.status == "scored" and not self.essential_failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_key": self.fixture_key,
            "label": self.label,
            "mode": self.mode,
            "status": self.status,
            "error": self.error,
            "experience_id": self.experience_id,
            "ok": self.ok,
            "passed_count": self.passed_count,
            "total_count": len(self.results),
            "essential_failure_count": len(self.essential_failures),
            "advisory_count": len(self.advisories),
            "results": [
                {**asdict(r), "status": r.status} for r in self.results
            ],
            "trace_grounding": [
                {"operation": op, "target": tgt, "grounding": round(g, 3)}
                for op, tgt, g in self.trace_grounding
            ],
        }


class _MockLLMClient:
    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, system: str, user: str) -> str:
        return self._response


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_view(fixture_key: str, view: CanonicalView) -> list[AssertionResult]:
    out: list[AssertionResult] = []
    for a in assertions_for(fixture_key):
        try:
            passed, detail = a.check(view)
        except Exception as e:
            # An assertion that blows up is reported as a failure with the
            # exception text -- never swallowed, never treated as a pass.
            passed, detail = False, f"assertion raised {type(e).__name__}: {e}"
        out.append(AssertionResult(
            code=a.code, dimension=a.dimension, description=a.description,
            essential=a.essential, passed=passed, detail=detail,
        ))
    return out


# ---------------------------------------------------------------------------
# Mock mode -- scores the representation layer (ingest -> Postgres -> reader)
# ---------------------------------------------------------------------------

def evaluate_mock(fixture: CalibrationFixture, conn, owner_id: str) -> FixtureEvaluation:
    from core.ingest import ingest
    from core.reader import inspect_experience
    from core.storage import MockStorageClient

    ev = FixtureEvaluation(fixture_key=fixture.key, label=fixture.label, mode="mock", status="scored")

    occurred_at = None
    if fixture.occurred_at_offset_days is not None:
        occurred_at = datetime.now(UTC) + timedelta(days=fixture.occurred_at_offset_days)

    storage = MockStorageClient()
    try:
        result = ingest(
            fixture.text, fixture.source_type, conn=conn, owner_id=owner_id,
            client=_MockLLMClient(fixture.mock_json), storage=storage,
            occurred_at=occurred_at,
            metadata={"source": "test_fixture", "fixture_id": fixture.key},
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        ev.status = "extraction_error"
        ev.error = f"{type(e).__name__}: {e}"
        return ev

    ev.experience_id = str(result.experience_id)
    inspection = inspect_experience(ev.experience_id, conn, storage=storage)
    conn.rollback()

    if "error" in inspection:
        ev.status = "extraction_error"
        ev.error = inspection["error"]
        return ev

    view = view_from_inspection(inspection)
    # The fixture's own text IS the source text, authoritatively. Do not
    # take it from reader's raw_text: on a deduped re-ingest the fresh
    # MockStorageClient never received the artifact, and reader reports that
    # as the string "<error fetching from storage: ...>" rather than an
    # empty value -- which silently scored every lexical-grounding check
    # against an error message.
    view.source_text = fixture.text

    ev.results = score_view(fixture.key, view)
    ev.trace_grounding = trace_grounding(view)
    return ev


# ---------------------------------------------------------------------------
# Live mode -- scores the real extractor, persists nothing
# ---------------------------------------------------------------------------

def evaluate_live(fixture: CalibrationFixture, client=None) -> FixtureEvaluation:
    from core.extractor import ExtractionValidationError, extract

    ev = FixtureEvaluation(fixture_key=fixture.key, label=fixture.label, mode="live", status="scored")

    if client is None:
        from core.extractor import AnthropicLLMClient
        client = AnthropicLLMClient()

    try:
        result = extract(fixture.text, client)
    except ExtractionValidationError as e:
        # A first-class finding: the extractor produced something the
        # ontology rejects. Reported, not repaired.
        ev.status = "validation_error"
        ev.error = str(e)
        return ev
    except Exception as e:
        ev.status = "extraction_error"
        ev.error = f"{type(e).__name__}: {e}"
        return ev

    view = view_from_extraction(result, fixture.text)
    ev.results = score_view(fixture.key, view)
    ev.trace_grounding = trace_grounding(view)
    return ev


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

def evaluate_all(mode: str = "mock", fixture_keys: Optional[list[str]] = None,
                  conn=None, owner_id: Optional[str] = None, client=None) -> list[FixtureEvaluation]:
    keys = fixture_keys or [f.key for f in FIXTURES]
    out: list[FixtureEvaluation] = []
    for key in keys:
        fixture = get_fixture(key)
        if fixture is None:
            continue
        if mode == "live":
            out.append(evaluate_live(fixture, client=client))
        else:
            out.append(evaluate_mock(fixture, conn, owner_id))
    return out


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_evaluation(ev: FixtureEvaluation) -> str:
    lines: list[str] = []
    header = f"{ev.fixture_key}  [{ev.mode}]"
    lines.append("=" * 78)
    lines.append(header)
    lines.append("=" * 78)

    if ev.status != "scored":
        lines.append(f"  {ev.status.upper()}: {ev.error}")
        return "\n".join(lines)

    for r in ev.results:
        mark = {"pass": "  ok ", "FAIL": " FAIL", "advisory": " adv "}[r.status]
        lines.append(f"{mark}  {r.code:<10} [{r.dimension}] {r.description}")
        if not r.passed:
            lines.append(f"           actual: {r.detail}")

    if ev.trace_grounding:
        lines.append("  -- trace grounding (fraction of target words present in source) --")
        for op, tgt, g in ev.trace_grounding:
            flag = "  <-- ungrounded" if g == 0.0 else ""
            lines.append(f"     {g:.2f}  {op}: {tgt[:60]}{flag}")

    lines.append(f"  {ev.passed_count}/{len(ev.results)} passed, "
                 f"{len(ev.essential_failures)} essential failures, "
                 f"{len(ev.advisories)} advisories")
    return "\n".join(lines)


def format_summary(evaluations: list[FixtureEvaluation]) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 78)
    lines.append("SUMMARY")
    lines.append("=" * 78)
    lines.append(f"{'fixture':<42} {'status':<18} {'pass':>7}  {'fail':>4} {'adv':>4}")
    lines.append("-" * 78)
    for ev in evaluations:
        if ev.status != "scored":
            lines.append(f"{ev.fixture_key:<42} {ev.status:<18} {'-':>7}  {'-':>4} {'-':>4}")
            continue
        status = "OK" if ev.ok else "ESSENTIAL FAILURES"
        lines.append(
            f"{ev.fixture_key:<42} {status:<18} "
            f"{ev.passed_count:>3}/{len(ev.results):<3}  "
            f"{len(ev.essential_failures):>4} {len(ev.advisories):>4}"
        )
    lines.append("-" * 78)
    scored = [e for e in evaluations if e.status == "scored"]
    total_fail = sum(len(e.essential_failures) for e in scored)
    total_adv = sum(len(e.advisories) for e in scored)
    unscored = [e for e in evaluations if e.status != "scored"]
    lines.append(f"{len(scored)} scored, {len(unscored)} unscored, "
                 f"{total_fail} essential failures, {total_adv} advisories")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Score calibration fixtures against expected semantic assertions. "
                    "Never repairs output -- failures are reported as-is."
    )
    parser.add_argument("--live", action="store_true",
                         help="Score the REAL extractor (requires ANTHROPIC_API_KEY, costs API calls). "
                              "Default is mock mode, which scores the representation layer only.")
    parser.add_argument("--fixture", action="append", default=None,
                         help="Fixture key (repeatable). Default: all.")
    parser.add_argument("--model", default=None,
                         help="Model ID for live mode. Default: AnthropicLLMClient's own default -- "
                              "deliberately not repeated here, so the production extractor default "
                              "lives in exactly one place (core/extractor.py).")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument("--dsn", default=os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain"))
    args = parser.parse_args()

    mode = "live" if args.live else "mock"

    client = None
    if mode == "live":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is not set -- live mode needs a real extraction call.")
            print("Mock mode (the default) scores the representation layer without it.")
            sys.exit(3)
        from core.extractor import AnthropicLLMClient
        client = AnthropicLLMClient(model=args.model) if args.model else AnthropicLLMClient()

    conn = None
    owner_id = None
    if mode == "mock":
        import psycopg2
        conn = psycopg2.connect(args.dsn)
        cur = conn.cursor()
        cur.execute("SELECT id FROM owner LIMIT 1")
        row = cur.fetchone()
        if row is None:
            print("No owner seeded. Seed one first (Brain Console -> Status -> Seed Dev Console Owner).")
            sys.exit(3)
        owner_id = str(row[0])

    try:
        evaluations = evaluate_all(mode=mode, fixture_keys=args.fixture, conn=conn,
                                    owner_id=owner_id, client=client)
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        print(json.dumps([e.to_dict() for e in evaluations], indent=2, default=str))
    else:
        for ev in evaluations:
            print(format_evaluation(ev))
            print()
        print(format_summary(evaluations))

    sys.exit(0)


if __name__ == "__main__":
    main()
