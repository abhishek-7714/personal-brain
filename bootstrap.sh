#!/usr/bin/env bash
# bootstrap.sh - bring up the stack, apply migrations, verify. Idempotent.
#
#   cp .env.example .env && ./bootstrap.sh
#
# Does NOT seed the owner row and does NOT ingest anything. Foundation only.
set -uo pipefail
cd "$(dirname "$0")"

[ -f .env ] && set -a && . ./.env && set +a
PG_DSN="${PG_DSN:-postgresql://brain:brain@localhost:5432/brain}"

step() { echo; echo "### $*"; }
fail=0

step "1. stack"
if command -v docker >/dev/null 2>&1; then
    docker compose up -d || { echo "compose failed"; exit 1; }
    echo "waiting for postgres..."
    for i in $(seq 1 60); do
        docker compose exec -T postgres pg_isready -U brain -d brain >/dev/null 2>&1 && break
        sleep 2
    done
else
    echo "docker not found; assuming services are already reachable"
fi

step "2. migrations"
if command -v psql >/dev/null 2>&1; then
    for f in migrations/*.sql; do
        echo "-- $f"
        psql "$PG_DSN" -v ON_ERROR_STOP=1 -q -f "$f" || { echo "MIGRATION FAILED: $f"; exit 1; }
    done
elif command -v docker >/dev/null 2>&1; then
    for f in migrations/*.sql; do
        echo "-- $f"
        docker compose exec -T postgres psql -U brain -d brain -v ON_ERROR_STOP=1 -q < "$f" \
            || { echo "MIGRATION FAILED: $f"; exit 1; }
    done
else
    echo "no psql and no docker; cannot apply migrations"; exit 1
fi

step "3. graph constraints"
if command -v cypher-shell >/dev/null 2>&1; then
    cypher-shell -a "${NEO4J_URI:-bolt://localhost:7687}" \
        -u "${NEO4J_USER:-neo4j}" -p "${NEO4J_PASSWORD:-brainbrain}" \
        -f graph/schema.cypher || fail=1
elif command -v docker >/dev/null 2>&1; then
    docker compose exec -T neo4j cypher-shell \
        -u "${NEO4J_USER:-neo4j}" -p "${NEO4J_PASSWORD:-brainbrain}" < graph/schema.cypher || fail=1
else
    echo "SKIP  no cypher-shell"; fail=2
fi

step "4. vector collections"
if python3 -c "import qdrant_client" 2>/dev/null; then
    python3 -m vector.collections || fail=1
else
    echo "SKIP  pip install qdrant-client"; fail=2
fi

step "5. object storage buckets"
if command -v mc >/dev/null 2>&1; then
    mc alias set brainlocal "${MINIO_ENDPOINT:-http://localhost:9000}" \
        "${MINIO_ACCESS_KEY:-brain}" "${MINIO_SECRET_KEY:-brainbrain}" >/dev/null 2>&1
    mc mb --ignore-existing brainlocal/brain-artifacts brainlocal/brain-exports || fail=1
else
    echo "SKIP  mc not installed"; fail=2
fi

step "6. verification"
PG_DSN="$PG_DSN" bash tests/run_all.sh
rc=$?
[ "$rc" -eq 1 ] && fail=1

echo
if [ "$fail" -eq 0 ] && [ "$rc" -eq 0 ]; then
    echo "FOUNDATION GREEN. Seed the owner row when ready:"
    echo "  psql \"\$PG_DSN\" -c \"INSERT INTO owner (display_name, timezone) VALUES ('\${OWNER_DISPLAY_NAME:-Owner}', '\${OWNER_TIMEZONE:-UTC}');\""
elif [ "$fail" -eq 1 ]; then
    echo "FAILURES PRESENT. Do not seed or ingest."
else
    echo "PARTIAL: some optional tooling was skipped. See SKIP lines above."
fi
exit "$fail"
