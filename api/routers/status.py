"""
api/routers/status.py

Live health for every backend service (Postgres, Neo4j, Qdrant, MinIO,
Temporal), owner-seed state, and an on-demand test runner. All checks are
read-only reachability probes -- nothing here is a new health-check
implementation beyond "call the existing client and see if it errors."
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys

import psycopg2
from fastapi import APIRouter, HTTPException, Request

from api.deps import PG_DSN, REPO_ROOT

router = APIRouter()


def _check_postgres() -> dict:
    try:
        conn = psycopg2.connect(PG_DSN, connect_timeout=3)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        return {"ok": True, "detail": "reachable"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _check_neo4j(driver) -> dict:
    try:
        driver.verify_connectivity()
        return {"ok": True, "detail": "reachable"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _check_qdrant(qc) -> dict:
    try:
        cols = qc.get_collections()
        return {"ok": True, "detail": f"{len(cols.collections)} collections"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _check_minio() -> dict:
    try:
        from core.storage import from_env
        client = from_env()
        exists = client._client.bucket_exists(client.BUCKET)
        return {"ok": True, "detail": f"bucket '{client.BUCKET}' exists={exists}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _check_temporal() -> dict:
    """
    No Temporal SDK client is wired up anywhere in this codebase (only the
    server runs, via docker-compose) -- this is a plain TCP reachability
    check on the gRPC port, not a real workflow-service health check.
    Labeled accordingly rather than implying more than it verifies.
    """
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    host, _, port = address.partition(":")
    try:
        with socket.create_connection((host, int(port or 7233)), timeout=2):
            pass
        return {"ok": True, "detail": "TCP reachable (no workflow-level check -- no Temporal SDK client in this codebase)"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _check_owner() -> dict:
    try:
        conn = psycopg2.connect(PG_DSN, connect_timeout=3)
        cur = conn.cursor()
        cur.execute("SELECT id, display_name FROM owner LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row:
            return {"seeded": True, "id": str(row[0]), "display_name": row[1]}
        return {"seeded": False}
    except Exception as e:
        return {"seeded": False, "error": str(e)}


@router.get("")
def get_status(request: Request) -> dict:
    return {
        "postgres": _check_postgres(),
        "neo4j": _check_neo4j(request.app.state.neo4j_driver),
        "qdrant": _check_qdrant(request.app.state.qdrant),
        "minio": _check_minio(),
        "temporal": _check_temporal(),
        "owner": _check_owner(),
    }


@router.post("/seed-owner")
def seed_owner() -> dict:
    """
    Explicit, user-triggered only -- never auto-seeded on startup. Uses an
    obviously-placeholder display name ("Dev Console") so it can never be
    mistaken for real owner data; the owner table's singleton CHECK means
    this can only ever run once until that row is removed.
    """
    conn = psycopg2.connect(PG_DSN)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM owner LIMIT 1")
        if cur.fetchone():
            raise HTTPException(400, "Owner already seeded")
        cur.execute(
            "INSERT INTO owner (display_name, timezone) VALUES (%s, %s) RETURNING id",
            ("Dev Console", "UTC"),
        )
        owner_id = cur.fetchone()[0]
        conn.commit()
        return {"id": str(owner_id), "display_name": "Dev Console"}
    finally:
        conn.close()


# Suites that need no live stack -- safe to run on every click, no writes.
_FAST_SUITES = ["tests/test_dynamics.py", "tests/test_schema_static.py"]
# Suites that write real (test) data to Postgres/Neo4j/Qdrant and clean up
# after themselves (rollback + explicit delete) -- opt-in only, per the
# owner's instruction not to make this routine.
_FULL_SUITES = _FAST_SUITES + [
    "tests/test_ingestion.py", "tests/test_projector.py",
    "tests/test_indexer.py", "tests/test_retrieval.py",
]

_RESULT_RE = re.compile(r"(\d+) passed, (\d+) failed")


@router.post("/run-tests")
def run_tests(suite: str = "fast") -> dict:
    if suite not in ("fast", "full"):
        raise HTTPException(400, "suite must be 'fast' or 'full'")
    files = _FAST_SUITES if suite == "fast" else _FULL_SUITES

    results = []
    for f in files:
        try:
            proc = subprocess.run(
                [sys.executable, f], capture_output=True, text=True,
                timeout=180, cwd=str(REPO_ROOT),
            )
            m = _RESULT_RE.search(proc.stdout)
            results.append({
                "file": f,
                "exit_code": proc.returncode,
                "skipped": proc.returncode == 3,
                "passed": int(m.group(1)) if m else None,
                "failed": int(m.group(2)) if m else None,
            })
        except subprocess.TimeoutExpired:
            results.append({"file": f, "exit_code": None, "error": "timeout"})

    return {"suite": suite, "results": results}
