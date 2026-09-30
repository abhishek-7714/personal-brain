# Personal Brain v0.1 — Implementation Status Report

_Regenerated 2026-08-31. Verified against a live Docker stack (Postgres, Neo4j,
Qdrant, MinIO, Temporal, all healthy) — not a static read of the source tree._

---

## 1. What Is Implemented and Verified

Fully written, functional, and confirmed against real services (no stubs, no
placeholders):

| Component | File | What's done |
|---|---|---|
| SQL schema (migrations 001–006) | `migrations/001–006` | 35+ tables, 100+ constraints, indexes, immutability triggers, views, all enum types. **36/36 invariants pass on live Postgres.** |
| Two-axis belief engine | `core/dynamics.py` | `apply_evidence()`, `recompute_belief()`, log-odds conversion, evidence decay at 540-day half-life |
| Memory dynamics | `core/dynamics.py` | Episodic retrievability, per-kind semantic staleness (5 half-lives), procedural proficiency |
| Association learning | `core/dynamics.py` | `reinforce()`, `decay()`, situation context multipliers |
| Activation & ranking (math only) | `core/dynamics.py` | 7-channel `activation_score()`, contradiction reserve, `rank_and_bound()` — pure functions, not wired to live data yet (see §3) |
| Classification rules | `core/classification.md` | Decision frameworks for memory system, belief kind, epistemic owner, episode boundaries, trace, entity resolution, emotion, belief revision, and entity-to-memory linking (§7.1) |
| Graph constraints | `graph/schema.cypher` | 12 node types, idempotent constraints — enforced live via `SHOW CONSTRAINTS` |
| Vector collection schemas | `vector/collections.py` | 4 collections bootstrapped live against Qdrant; HNSW m=32/ef=200, INT8 scalar quantization |
| **Extraction** | `core/extractor.py` | LLM extraction + full ontology validation, incl. `memories[].involved_entities` cross-validated against `entities[]` |
| **Ingestion orchestrator** | `core/ingest.py` | text → MinIO → experience → observations → entities → memories → `memory_entities` → beliefs → projection-stage placeholders → `experience.created` event. Idempotent on `(owner_id, content_hash)`. |
| **MinIO raw storage** | `core/storage.py` | `MinioStorageClient` + `MockStorageClient`; write/read/idempotency verified against live MinIO |
| **Neo4j projector** | `core/projector.py` | Postgres → Neo4j projection, all 12 relationship types incl. `Memory -[:INVOLVES]-> Entity`; MERGE-keyed on Postgres UUIDs, confirmed idempotent across repeated runs |
| Inspection tool | `core/reader.py` | `python -m core.reader <experience_uuid>` — human-readable or `--json` dump of the full derived record |
| Bootstrap orchestration | `bootstrap.sh` | Idempotent: up → migrate → cypher → vector → mc → tests |

### Test tally: 292/292 passing, 0 failures

| Suite | Checks | Requires |
|---|---|---|
| `tests/test_dynamics.py` | 38 | none (pure Python) |
| `tests/test_schema_static.py` | 68 | none (parses migration text) |
| `tests/test_ingestion.py` | 98 | none (drives `validate_extraction_response()` directly) |
| `tests/test_projector.py` | 52 | live Postgres + Neo4j |
| `tests/invariants.sql` | 36 | live Postgres |

`tests/test_projector.py` includes an explicit canonical-chain proof: a memory
that involves an entity is confirmed to resolve **Experience → Observation →
Memory → `memory_entities` → Entity in Postgres** before projection runs, and
then **`Memory -[:INVOLVES]-> Entity` in Neo4j** after it, holding across three
re-projections.

---

## 2. What Is Bootstrapped / Scaffolded but Has No Execution Code

These have live schema, constraints, or bootstrapped state, but nothing
processes them yet:

- **Qdrant embedding/indexing** — `vector/collections.py` has created all 4
  collections live. `core/ingest.py` already creates a `pending`
  `extraction_runs` row with `stage='qdrant_embedding'` for every experience.
  No embedder and no code calls `qdrant_client.upsert()`. All 4 collections
  are empty.
- **Temporal workflows** — the Temporal server and UI are running via
  `docker-compose.yml`. Zero workflow definitions exist; nothing calls
  `project_experience()` or a future embedding worker automatically. For now
  both must be invoked manually (as the test suites and `bootstrap.sh` do).
- **Consolidation jobs** — `consolidation_candidates` and `consolidation_runs`
  tables exist with full review-workflow fields (7 run kinds defined). No
  worker executes any of them.
- **Situation clustering** — `situations`, `episode_situations`,
  `situation_strategies` exist as scaffolding. Discovery is **intentionally
  unimplemented**; `tests/test_schema_static.py` check 7.5 actively asserts no
  clustering code exists. Do not implement without the owner asking.
- **Entity resolution, LLM adjudication pass** — trigram-similarity merging
  (Pass 3, non-person entities) works; the LLM adjudication call for
  ambiguous candidates is not implemented. Uncertain merges create a separate
  entity with a `possible_duplicate` relationship instead.
- **Belief revision lookup** — `ingest.py` handles new beliefs and emits a
  `retracted` observation when the LLM signals a revision, but does not yet
  search Postgres for the prior belief to add contradicting evidence and a
  `belief_relations.supersedes` row (documented in `classification.md §9`).

---

## 3. What Is Not Implemented at All

| What | Why it matters |
|---|---|
| **Retrieval engine** | No endpoint, no semantic search, no graph traversal. `rank_and_bound()` math (`core/dynamics.py`) is complete but has no live candidates to rank. |
| **Activation / reranking beyond the dynamics primitives** | The 7-channel `activation_score()` function is correct and unit-tested, but two of its channels (semantic via Qdrant, association via Neo4j graph walks) have no data source feeding them yet — semantic is empty (no embedder), association walks aren't wired to a query path. |
| **Real personal history ingestion** | The `owner` table is empty. No real experiences have been ingested — only test fixtures inside rolled-back transactions. |

---

## 4. Known, Still-Accurate Inconsistencies

These were true when the project was audited early this session and remain
true now — they are not blockers, just gaps worth knowing about before
building on top of the affected tables:

- **`instance_count` is always 1.** `consolidation_confidence()` uses it, but
  no code increments it on repeated similar instances, so natural consolidation
  can never reach the 0.5 threshold without a manual override.
- **`source_reliability` is static.** Written once at ingest time;
  `confirmed_count`/`contradicted_count` on the `source_reliability` table are
  never updated from evidence outcomes — no feedback loop yet.
- **`last_practiced_at` never advances.** `procedural_proficiency()` decays
  from it, but nothing records a practice event, so procedural memories decay
  monotonically with no recovery path.
- **`model_versions` for unwired stages are placeholders.** `qdrant_embedding`
  and `neo4j_projection` extraction-run rows register under
  `provider='unassigned'` until their real workers exist and register
  themselves.
- **Situation signatures are never populated.** `match_situation()` in
  `core/dynamics.py` expects a feature dict that nothing extracts yet — dead
  code until situation clustering is implemented (deferred by design).

---

## 5. Exact Next Blocker

**The Qdrant embedding worker** — the next milestone, not started here.

It should mirror `core/projector.py`'s pattern exactly: query
`extraction_runs WHERE stage = 'qdrant_embedding' AND status = 'pending'`
(rows `core/ingest.py` already creates on every ingest), fetch the
corresponding canonical rows from Postgres, embed, and upsert idempotently
into the 4 already-bootstrapped Qdrant collections — then mark the run
`succeeded`, the same way `_mark_projection_succeeded()` does for Neo4j.

```
Extraction (done) → Ingestion (done) → Neo4j projection (done)
                                     → Qdrant embedding (missing — next)
                                            │
                                            └─→ Retrieval endpoint can query both
                                                       │
                                                       └─→ rank_and_bound() has candidates to rank
                                                                  │
                                                                  └─→ First end-to-end retrieval loop closes
```

---

## Summary Table

| Layer | Status | Notes |
|---|---|---|
| Schema & DDL | ✅ Complete | Migrations 001–006, live-verified, 36/36 invariants |
| Epistemics (math) | ✅ Complete | Two-axis beliefs, evidence weights, log-odds decay |
| Memory dynamics | ✅ Complete | Episodic/semantic/procedural rules, staleness, proficiency |
| Associations | ✅ Complete | Reinforcement, decay, situation context weighting |
| Activation & ranking (math) | ✅ Complete | Pure functions; not yet fed by live semantic/association data |
| Classification rules | ✅ Complete | `core/classification.md`, incl. entity-linking roles (§7.1) |
| Extraction service | ✅ Complete | `core/extractor.py`, LLM + ontology validation |
| Ingestion orchestrator | ✅ Complete | `core/ingest.py`, idempotent, incl. `memory_entities` |
| MinIO raw storage | ✅ Complete | `core/storage.py`, live-verified |
| Neo4j projection | ✅ Complete | `core/projector.py`, all 12 relationship types, idempotent |
| Vector collections | ⚠️ Bootstrapped only | Schema + live collections exist; no embedder/upsert code |
| Temporal workflows | ⚠️ Server only | Server + UI running; zero workflow definitions |
| Consolidation jobs | ⚠️ Schema only | 7 run kinds defined; no workers |
| Situation clustering | ⛔ Deferred by design | Scaffolding exists; discovery intentionally unimplemented |
| Qdrant embedder | ❌ **Next blocker** | Gate for the retrieval loop |
| Retrieval engine | ❌ Missing | No query endpoint, no semantic search, no graph traversal |
| Real history ingested | ❌ None yet | `owner` table empty by design, pending foundation validation |
