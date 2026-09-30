"""
api/deps.py

FastAPI dependency factories. Thin wiring only -- every actual connection
type (psycopg2, neo4j driver, qdrant client, embedder, storage client) is
the same class core/*.py already defines and tests/*.py already uses. This
module does not add a second implementation of any of them.

Transaction ownership: get_conn() yields a plain psycopg2 connection with
autocommit=False and does NOT commit or rollback on teardown -- every route
handler that writes must call conn.commit()/conn.rollback() itself, exactly
like every core/*.py docstring already documents ("caller controls the
transaction"). This keeps the API layer consistent with how ingest()/
project_experience()/index_experience() are used everywhere else in this
codebase, rather than inventing a second transaction convention.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import psycopg2
from fastapi import HTTPException, Request

from core.embedder import Embedder, MockEmbedder, VoyageEmbedder
from core.storage import StorageClient

REPO_ROOT = Path(__file__).resolve().parents[1]
PG_DSN = os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain")


def get_conn() -> Iterator:
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.close()


def get_neo4j_driver(request: Request):
    return request.app.state.neo4j_driver


def get_qdrant_client(request: Request):
    return request.app.state.qdrant


def get_embedder() -> Embedder:
    """
    Defaults to MockEmbedder -- no cost, no API key required -- exactly like
    core/query_inspector.py's CLI default. Set EMBEDDER_PROVIDER=voyage to
    use the real model (bills your Voyage account on every ingest/query from
    the console).
    """
    provider = os.environ.get("EMBEDDER_PROVIDER", "mock").lower()
    if provider == "voyage":
        return VoyageEmbedder()
    return MockEmbedder()


def get_storage() -> StorageClient:
    """
    Real MinIO only, and a 503 when it is unreachable. There is deliberately
    no in-memory fallback: experiences are append-only and cannot be deleted
    (migration 003), so an ingest against a per-request mock would commit a
    permanent experience whose content_uri points at raw text that vanished
    with the request -- an unrepairable break in the evidence chain. Failing
    the request costs a retry; the fallback cost the raw record.
    """
    try:
        from core.storage import from_env
        client = from_env()
        client._client.bucket_exists(client.BUCKET)  # cheap reachability probe
        return client
    except Exception as e:
        raise HTTPException(503, f"MinIO unreachable, refusing to ingest without raw storage: {e}")
