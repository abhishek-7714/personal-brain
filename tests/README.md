# Verification suite

Three suites. Two run anywhere; the third needs Postgres.

| Suite | Requires | Covers |
| --- | --- | --- |
| `test_dynamics.py` | nothing | Epistemics maths, per-system memory rules, consolidation, activation, promotion |
| `test_schema_static.py` | nothing | Migration hygiene and ordering, terminology, every structural invariant in the DDL |
| `invariants.sql` | Postgres + migrations 001-005 | Runtime behaviour: constraints actually firing, triggers actually blocking |

```bash
bash tests/run_all.sh
```

Exit codes: `0` all passed, `1` failures present, `2` in-process suites passed but
the database suite was not run.

To run the database suite:

```bash
docker compose up -d
export PG_DSN='postgresql://brain:brain@localhost:5432/brain'
for f in migrations/*.sql; do psql "$PG_DSN" -v ON_ERROR_STOP=1 -f "$f"; done
bash tests/run_all.sh
```

## Suites that write permanently: use the scratch database

`test_calibration_eval`, `test_episodes`, `test_api` and `test_e2e_console`
commit calibration fixtures, and experiences are append-only, so a run leaves
that data in the target database forever. `tests/_db_guard.py` makes each of
them exit with code 4 (`REFUSED`) before any write if the database holds an
owner other than the test placeholder `Dev Console`. `PG_DSN` defaults to the
personal `brain` database, so run them against `brain_scratch`:

```bash
PG_DSN=postgresql://brain:brain@localhost:5432/brain_scratch .venv/bin/python tests/test_episodes.py
```

`test_api` and `test_e2e_console` also write through the API to Neo4j, Qdrant
and MinIO, which `PG_DSN` does not select. Point `NEO4J_URI`, `QDRANT_URL` and
`MINIO_ENDPOINT` at scratch instances as well, or those projections receive
scratch data. `BRAIN_TESTS_ALLOW_WRITES_TO_REAL_OWNER=1` bypasses the guard;
it writes into that owner's history for good.

`invariants.sql` inserts synthetic fixtures, asserts 35 behaviours, and ends in
`ROLLBACK`. The store is left exactly as empty as it started. No real history is
ingested by any suite.

The static checker is a structural parser, not a SQL engine. It proves the
constructs are present and consistently ordered; it cannot prove they behave
correctly at runtime. `invariants.sql` is what closes that gap, and the foundation
is not green until it has run.
