"""
tests/_db_guard.py

Refuses to let a database-writing suite run against a real brain.

test_calibration_eval, test_episodes, test_api and test_e2e_console ingest
calibration fixtures for real and commit them. Experiences and observations
are append-only (migration 003) and the owner is a singleton, so a run
against the owner's personal database leaves fixture data in their history
permanently -- there is no cleanup path by design. PG_DSN silently defaults
to that personal database, which is what made the accident easy.

The rule, checked before any write: the target database may hold no owner
(a fresh scratch database; the suites seed the placeholder themselves) or
only the placeholder owner these suites create, "Dev Console". Any other
owner means a real brain, and the suite exits with REFUSED_EXIT. It fails
closed: if the owner table cannot be read, the suite refuses rather than
guessing.

Bypass only with the explicit opt-in below, set to exactly "1".

Scope: this guards Postgres, the canonical store. test_api and
test_e2e_console also write through the API to Neo4j, Qdrant and MinIO, which
PG_DSN does not select -- point NEO4J_URI, QDRANT_URL and MINIO_ENDPOINT at
scratch instances too, or those projections receive scratch data.
"""

from __future__ import annotations

import os
import re
import sys

import psycopg2

OPT_IN_ENV = "BRAIN_TESTS_ALLOW_WRITES_TO_REAL_OWNER"
PLACEHOLDER_OWNER = "Dev Console"
DEFAULT_DSN = "postgresql://brain:brain@localhost:5432/brain"
SCRATCH_DSN = "postgresql://brain:brain@localhost:5432/brain_scratch"
# Distinct from the suites' own codes: 0 pass, 1 fail, 3 stack unreachable
# (reported as SKIPPED by api/routers/status.py). A refusal must never read
# as a skip.
REFUSED_EXIT = 4


def _redact(dsn: str) -> str:
    return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", dsn)


def _refuse(suite: str, dsn: str, reason: str) -> None:
    print(f"REFUSED  {suite} will not write to {_redact(dsn)}")
    print(f"         {reason}")
    print("         This suite commits fixture data permanently (experiences are append-only).")
    # SCRATCH_DSN is printed in full so the line can be pasted as-is; its
    # credentials are the local defaults from docker-compose.yml. The refused
    # target stays redacted -- it may carry a real password.
    print("         Run it against the scratch database instead:")
    print(f"           PG_DSN={SCRATCH_DSN} .venv/bin/python tests/{suite}.py")
    print(f"         Deliberate override (writes into that owner's history for good): {OPT_IN_ENV}=1")
    sys.exit(REFUSED_EXIT)


def require_scratch_database(suite: str) -> str:
    """Exit with REFUSED_EXIT unless PG_DSN is safe to write fixtures into.
    Returns the DSN the suite should use. Call before any write."""
    dsn = os.environ.get("PG_DSN", DEFAULT_DSN)

    if os.environ.get(OPT_IN_ENV) == "1":
        print(f"WARNING  {OPT_IN_ENV}=1: write guard bypassed for {_redact(dsn)}")
        return dsn

    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
        try:
            cur = conn.cursor()
            cur.execute("SELECT display_name FROM owner")
            owners = [row[0] for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        _refuse(suite, dsn, f"Could not read the owner table, so it cannot be shown safe: {e}")

    real = [name for name in owners if name != PLACEHOLDER_OWNER]
    if real:
        _refuse(suite, dsn, f"It belongs to owner {real[0]!r}, not the test placeholder {PLACEHOLDER_OWNER!r}.")
    return dsn
