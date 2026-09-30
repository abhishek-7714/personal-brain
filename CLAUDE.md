# CLAUDE.md

Project context for Claude Code. Read this before changing anything in this repo.

## What this is

The foundation layer of a single-owner "personal brain": an evidence-backed store
of one person's experiences, memories, beliefs and cognitive patterns. Postgres is
canonical. Neo4j and Qdrant are **rebuildable projections**, never sources of truth.
MinIO holds raw artifacts. Temporal runs the async pipeline.

Nothing has been ingested yet. The foundation is being validated before any real
history goes in.

## Non-negotiable decisions

These are already encoded in the schema and tests. Do not undo them without the
owner explicitly asking.

1. **Single owner.** Exactly one row in `owner`, enforced by a unique CHECK. Other
   people are `entities` of kind `person`. There is no tenancy, no user table, no
   `owner_id` that points anywhere except `owner(id)`. Do not add multi-user support.
2. **Keep the stack.** Postgres, Neo4j, Qdrant, MinIO, Temporal. The graph is part
   of the computational model, not decoration. Do not propose collapsing into
   Postgres + pgvector.
3. **Episodic memories are permanent.** They cannot be superseded, expired,
   reclassified, or deleted. Enforced by CHECK plus three triggers in migration 005.
   Correction happens through a new memory with `reinterprets_id` set.
4. **Beliefs have two axes.** `holding_*` is confidence the owner holds the belief;
   `log_odds`/`confidence` is confidence the proposition is true. A `world_claim` can
   be firmly held and evidentially false at the same time, and must stay `active`.
   Never collapse these into one number.
5. **Inference is not fact.** Anything the model derived gets
   `epistemic_owner = 'system_inference'` and is weighted at roughly a seventh of an
   owner assertion. Inferred emotion contributes exactly zero to salience.
6. **Raw history is append-only.** `experiences`, `observations`, `cognitive_traces`,
   `events`, `belief_versions`, `association_events` and `activation_traces` all have
   immutability triggers. Corrections are new rows.
7. **Every derived object needs evidence.** Rows in `evidence` link derived objects
   back to observations. Every `cognitive_traces` step must cite an observation that
   has a `span`. No span, no trace: this is what keeps the table from becoming stored
   chain-of-thought, which is out of scope.

## Layout

```
migrations/     001-005, applied in filename order
core/dynamics.py        all tuning constants and rules; pure functions, no DB coupling
core/classification.md  decision rules the extractor must follow for every enum
graph/schema.cypher     Neo4j constraints and the activation query patterns
vector/collections.py   Qdrant collections and payload index
tests/                  see tests/README.md
bootstrap.sh            stack up, migrate, verify
```

## Running things

```bash
cp .env.example .env
./bootstrap.sh          # stack + migrations + full verification
bash tests/run_all.sh   # verification only
```

Exit codes from `run_all.sh`: 0 all passed, 1 failures present, 2 in-process suites
passed but the database suite did not run.

**The database suite has never been executed.** `tests/test_dynamics.py` (38 checks)
and `tests/test_schema_static.py` (65 checks) pass. `tests/invariants.sql` (35
assertions) is written but unrun, so the migrations have never touched a real engine.
Expect a first-run SQL error, most likely in the `ALTER TYPE ADD VALUE` sequencing at
the top of 004 or the `memories.kind` type swap. Fix against the real error message.

## Changing the rules

All tuning lives in `core/dynamics.py`, deliberately separated from the logic.
Because beliefs update in additive log-odds and associations keep an append-only
event log, changing a constant means **re-running a recompute**, not migrating data.
`recompute_belief` is order-independent for exactly this reason. If you add a
constant, keep that property.

If you add or change an enum value, update `core/classification.md` in the same
change. An undocumented category becomes an inconsistent one within a week.

## Deferred on purpose

- **Situation clustering.** `situations`, `episode_situations` and
  `situation_strategies` exist as scaffolding. The clustering method is deliberately
  unimplemented, and `tests/test_schema_static.py` check 7.5 actively asserts no
  implementation has appeared. Do not implement it unprompted.
- **Extraction and ingestion.** Not written. `core/classification.md` is its spec.
- **Evaluation harness.** Deliberately skipped at the owner's instruction.
- **`is_stale` guard for episodic vector points.** Documented as a projector rule;
  no database constraint can reach into Qdrant.
- **`owner.entity_id` linkage.** Asserted in tests, not enforced by trigger.

## Style

Comments explain *why* a decision was made, not what the line does. Every constraint
that encodes a cognitive decision should say what would go wrong without it. Match
the existing tone in the migrations rather than adding boilerplate headers.

## Current implementation state (updated 2026-08-31)

**Milestone: Foundation + Ingestion + Neo4j projection — all green. 267 checks pass.**

### What is implemented and verified

- Migrations 001–006 applied and live-tested against real Postgres (36 SQL invariants pass)
- `core/dynamics.py` — two-axis belief math, memory decay, activation scoring
- `core/schema.py` — ontology constants and draft dataclasses
- `core/extractor.py` — LLM extraction with full ontology validation (10 fixture tests)
- `core/ingest.py` — full ingestion pipeline: MinIO → Postgres → events (idempotent)
- `core/storage.py` — MinIO client + MockStorageClient for tests
- `core/projector.py` — Postgres → Neo4j projection worker (35 integration tests, idempotent)
- `core/reader.py` — read-only inspection command: `python -m core.reader <uuid>`
- `core/__init__.py` — core is a proper package

### What is NOT implemented yet

- `memory_entities` not populated in `core/ingest.py` → INVOLVES edges absent from Neo4j (**next task**)
- Qdrant embedding worker (collections exist, no embedder)
- Temporal workflow definitions (server running, no workflows)
- Retrieval engine (rank_and_bound math is complete, no endpoint)
- Consolidation jobs (schema exists, no workers)
- Situation clustering (deferred by design — test 7.5 asserts it stays absent)

### Owner row

The `owner` table is empty. Seed before real ingestion:
```bash
docker compose exec postgres psql -U brain -d brain -c \
  "INSERT INTO owner (display_name, timezone) VALUES ('Abhishek', 'Asia/Kolkata');"
```

### Running the full test suite

```bash
# In-process suites (no stack required)
python tests/test_dynamics.py        # 38 checks
python tests/test_schema_static.py   # 68 checks
python tests/test_ingestion.py       # 90 checks

# Integration tests (stack required)
python tests/test_projector.py       # 35 checks
docker compose exec -T postgres psql -U brain -d brain \
  -v ON_ERROR_STOP=1 < tests/invariants.sql   # 36 checks
```

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## Review ledger
.review/ledger.md tracks reviewer issues by ID. Never mark an issue fixed without a commit that addresses it. Use /cross-review to run a Codex review loop.
