"""
api/routers/experiences.py

Section 2 (Ingest Experience) and Section 3 (Experience Inspector) of the
Brain Console. GET /{id} wraps core.reader.inspect_experience() directly --
as of migrations/007_episode_model.sql, that function's own result already
carries `episodes` (with nested cognitive_traces/state_items/decisions),
so this router does not re-derive any of that itself.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.deps import get_conn, get_embedder, get_qdrant_client, get_storage
from api.pipeline import run_full_pipeline
from core.reader import inspect_experience

router = APIRouter()


class IngestRequest(BaseModel):
    text: str
    source_type: str = "note"
    occurred_at: Optional[datetime] = None


def _require_owner(conn) -> str:
    cur = conn.cursor()
    cur.execute("SELECT id FROM owner LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise HTTPException(400, "No owner seeded. POST /api/status/seed-owner first.")
    return str(row[0])


def _llm_client():
    """
    Real Anthropic extraction -- the same core.extractor.AnthropicLLMClient
    every ingest() call in this codebase already uses, not a new LLM
    integration. This is extraction (turning text into observations/
    memories/beliefs), a pipeline stage that existed before this session;
    it is not the retrieval-answering "LLM reasoner" this task explicitly
    says not to build. See the Calibration page for a no-API-key path.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(
            400,
            "ANTHROPIC_API_KEY not set -- free-text ingestion needs a real "
            "extraction call. Use the Calibration page for fixtures that "
            "need no API key.",
        )
    from core.extractor import AnthropicLLMClient
    return AnthropicLLMClient()


@router.post("")
def create_experience(
    body: IngestRequest,
    request: Request,
    conn=Depends(get_conn),
    embedder=Depends(get_embedder),
    storage=Depends(get_storage),
) -> dict:
    owner_id = _require_owner(conn)
    client = _llm_client()
    try:
        result = run_full_pipeline(
            body.text, body.source_type, conn, owner_id, client, storage, embedder,
            request.app.state.neo4j_driver, request.app.state.qdrant,
            occurred_at=body.occurred_at,
        )
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, f"Ingestion failed: {e}")
    return result.__dict__


@router.get("")
def list_experiences(conn=Depends(get_conn), limit: int = 20) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, kind, occurred_at, ingested_at, source FROM experiences "
        "ORDER BY ingested_at DESC LIMIT %s",
        (limit,),
    )
    rows = cur.fetchall()
    conn.rollback()
    return [
        {
            "id": str(r[0]), "kind": r[1],
            "occurred_at": r[2].isoformat() if r[2] else None,
            "ingested_at": r[3].isoformat() if r[3] else None,
            # {"source": "test_fixture", "fixture_id": ...} for anything
            # ingested via the Calibration page -- see api/pipeline.py.
            "is_test_fixture": bool((r[4] or {}).get("source") == "test_fixture"),
        }
        for r in rows
    ]


@router.get("/{experience_id}")
def get_experience(experience_id: str, conn=Depends(get_conn), storage=Depends(get_storage)) -> dict:
    # inspect_experience() already catches storage.get_text() failures
    # internally and reports them in raw_text -- no extra try/except needed.
    data = inspect_experience(experience_id, conn, storage=storage)
    if "error" in data:
        conn.rollback()
        raise HTTPException(404, data["error"])
    conn.rollback()
    return data
