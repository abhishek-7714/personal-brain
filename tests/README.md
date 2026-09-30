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

`invariants.sql` inserts synthetic fixtures, asserts 35 behaviours, and ends in
`ROLLBACK`. The store is left exactly as empty as it started. No real history is
ingested by any suite.

The static checker is a structural parser, not a SQL engine. It proves the
constructs are present and consistently ordered; it cannot prove they behave
correctly at runtime. `invariants.sql` is what closes that gap, and the foundation
is not green until it has run.
