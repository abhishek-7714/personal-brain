#!/usr/bin/env bash
# tests/run_all.sh - full verification. Run from the repository root.
set -uo pipefail
fail=0

echo "### 1. dynamics (no database required)"
python3 tests/test_dynamics.py || fail=1

echo
echo "### 2. schema structure (no database required)"
python3 tests/test_schema_static.py || fail=1

echo
echo "### 3. database invariants"
if [ -n "${PG_DSN:-}" ] && command -v psql >/dev/null 2>&1; then
    psql "$PG_DSN" -v ON_ERROR_STOP=1 -f tests/invariants.sql || fail=1
else
    echo "SKIP  PG_DSN unset or psql unavailable."
    echo "      docker compose up -d"
    echo "      export PG_DSN='postgresql://brain:brain@localhost:5432/brain'"
    echo "      for f in migrations/*.sql; do psql \"\$PG_DSN\" -v ON_ERROR_STOP=1 -f \"\$f\"; done"
    echo "      bash tests/run_all.sh"
    fail=2
fi

echo
if [ "$fail" -eq 0 ]; then echo "ALL SUITES PASSED"
elif [ "$fail" -eq 2 ]; then echo "IN-PROCESS SUITES PASSED; DATABASE SUITE NOT RUN"
else echo "FAILURES PRESENT"; fi
exit "$fail"
