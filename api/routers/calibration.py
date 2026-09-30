"""
api/routers/calibration.py

Section 7 (Calibration mode). Lists core.calibration_fixtures.FIXTURES and
lets the console ingest one through the exact same run_full_pipeline() the
free-text ingest endpoint uses -- same ingest() -> project_experience() ->
index_experience() sequence, just with a canned MockLLMClient response
instead of a real Anthropic call, so calibration needs no API key and costs
nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

import os

from api.deps import get_conn, get_embedder, get_storage
from api.pipeline import run_full_pipeline
from core.calibration_eval import evaluate_live, evaluate_mock
from core.calibration_fixtures import FIXTURES, get_fixture

router = APIRouter()
UTC = timezone.utc


class _MockLLMClient:
    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, system: str, user: str) -> str:
        return self._response


@router.get("/fixtures")
def list_fixtures() -> list[dict]:
    return [
        {"key": f.key, "label": f.label, "description": f.description, "text": f.text}
        for f in FIXTURES
    ]


@router.post("/fixtures/{key}/ingest")
def ingest_fixture(key: str, request: Request, conn=Depends(get_conn),
                    embedder=Depends(get_embedder), storage=Depends(get_storage)) -> dict:
    fixture = get_fixture(key)
    if fixture is None:
        raise HTTPException(404, f"Unknown fixture {key!r}")

    cur = conn.cursor()
    cur.execute("SELECT id FROM owner LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise HTTPException(400, "No owner seeded. POST /api/status/seed-owner first.")
    owner_id = str(row[0])

    occurred_at = None
    if fixture.occurred_at_offset_days is not None:
        occurred_at = datetime.now(UTC) + timedelta(days=fixture.occurred_at_offset_days)

    try:
        result = run_full_pipeline(
            fixture.text, fixture.source_type, conn, owner_id,
            _MockLLMClient(fixture.mock_json), storage, embedder,
            request.app.state.neo4j_driver, request.app.state.qdrant,
            occurred_at=occurred_at,
            # Stamped into experiences.source so the console (and anyone
            # reading Postgres directly) can always tell this experience is
            # synthetic calibration data, never a real memory. ingest() is
            # idempotent on (owner_id, content_hash): re-ingesting the same
            # fixture returns the same experience_id rather than creating a
            # second one -- see tests/test_e2e_console.py's rerun check.
            metadata={"source": "test_fixture", "fixture_id": fixture.key},
        )
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, f"Ingestion failed: {e}")

    return result.__dict__


@router.post("/fixtures/{key}/evaluate")
def evaluate_fixture(key: str, mode: str = "mock", conn=Depends(get_conn)) -> dict:
    """
    Score a fixture against its expected semantic assertions
    (core/calibration_assertions.py).

    mode=mock -- ingests via the hand-written MockLLM response and scores the
                 canonical representation read back from Postgres. Validates
                 the REPRESENTATION LAYER; says nothing about real extractor
                 behaviour, because the JSON was written by hand.
    mode=live -- calls the real LLM on the fixture text and scores its output
                 directly. Validates the EXTRACTOR. Persists nothing, and
                 costs a real API call.

    Never repairs output: a failing assertion is returned as a failure with
    the actual value that caused it.
    """
    fixture = get_fixture(key)
    if fixture is None:
        raise HTTPException(404, f"Unknown fixture {key!r}")
    if mode not in ("mock", "live"):
        raise HTTPException(400, "mode must be 'mock' or 'live'")

    if mode == "live":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise HTTPException(
                400,
                "ANTHROPIC_API_KEY not set -- live evaluation needs a real extraction "
                "call. Use mode=mock to score the representation layer without it.",
            )
        return evaluate_live(fixture).to_dict()

    cur = conn.cursor()
    cur.execute("SELECT id FROM owner LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise HTTPException(400, "No owner seeded. POST /api/status/seed-owner first.")
    return evaluate_mock(fixture, conn, str(row[0])).to_dict()
