# Personal Brain, v0.1 foundation

The executable layer under the architecture document. Postgres is canonical,
Neo4j and Qdrant are projections, MinIO holds raw artifacts, Temporal runs the
pipeline, and `core/dynamics.py` holds the rules that make any of it move.

## Boot

```bash
docker compose up -d
# migrations run automatically on first boot of an empty pg volume;
# to apply them to an existing database:
for f in migrations/*.sql; do psql "$PG_DSN" -v ON_ERROR_STOP=1 -f "$f"; done

cypher-shell -a bolt://localhost:7687 -u neo4j -p brainbrain -f graph/schema.cypher
python -m vector.collections
mc alias set local http://localhost:9000 brain brainbrain
mc mb local/brain-artifacts local/brain-exports
```

Seed the owner row before ingesting anything, since almost every table
references it:

```sql
INSERT INTO owner (display_name, timezone) VALUES ('Abhishek', 'Asia/Kolkata');
```

## Object storage layout

Content-addressed, so re-ingesting the same file is a no-op and the
`experiences.content_hash` unique constraint does the deduplication for free.

```
brain-artifacts/
  raw/<yyyy>/<mm>/<sha256>.<ext>      original bytes, never modified
  normalized/<sha256>.md              extracted text used for embedding
  audio/<sha256>.<ext>
  attachments/<experience_id>/<name>
brain-exports/
  snapshots/<iso8601>/                full logical dump for rebuild drills
```

## Event contracts

Every canonical mutation writes one row to `events`. Consumers are keyed off
`event_type` and must be idempotent on `event_id`.

| event_type | payload | consumers |
| --- | --- | --- |
| `experience.created` | experience_id, kind, content_uri, occurred_at | extraction workflow |
| `observation.extracted` | observation_id, experience_id, epistemic_status | episode detection, entity resolution |
| `episode.created` | episode_id, kind, experience_ids | trace extraction, memory scoring |
| `trace.recorded` | trace_id, episode_id, operation | pattern discovery, association learning |
| `memory.created` | memory_id, kind, salience | graph projector, embedder |
| `memory.superseded` | memory_id, superseded_by | graph projector, embedder |
| `entity.resolved` | entity_id, merged_from | graph projector, re-embed affected |
| `belief.updated` | belief_id, version, delta, status | graph projector, contradiction check |
| `association.reinforced` | association_id, strength_after | graph projector |
| `state.changed` | state_item_id, kind, activation | retrieval cache invalidation |

`projection_offsets` tracks how far Neo4j and Qdrant have consumed. A rebuild
sets the offset to 0, truncates the projection, and replays. Run that drill
early and on a schedule, because a rebuild path that has never been exercised
is not a rebuild path.

## v0.2 changes (migration 004)

- **Memory systems.** `memories.memory_system` is episodic, semantic or procedural,
  with a CHECK tying each `kind` to exactly one system. Episodic memories cannot be
  superseded or marked stale, only reinterpreted via `reinterprets_id`. There is no
  "something I learned" kind:
  see `core/classification.md` for how to split it.
- **Two-axis beliefs.** `belief_kind` distinguishes self_report, world_claim, normative
  and attributed. World claims carry `holding_log_odds` and `log_odds` separately, so the
  system can hold "he is sure of this" and "the evidence runs against it" at once.
  `observations.confidence` and `reliability` are renamed to `extraction_confidence`
  and `source_reliability`, which were always different quantities.
- **Situations.** `situations`, `episode_situations` and `situation_strategies` reserve
  the conditioned-activation layer. Associations are now situation-scoped.
- **Owner as entity.** `owner.entity_id` and `entities.is_owner` with a partial unique
  index, so the singleton participates in the relationship graph without becoming a tenant.
- **Association provenance.** `association_events` carries context, situation, episode kind,
  source kind and trigger. The `association_provenance` view returns the full breakdown.
- **Activation traces.** `activation_traces` and `activation_members` record what each
  query surfaced, per channel, with scores and inclusion. The activation layer is not a
  black box and this is the only data that could teach better activation later.
- **Consolidation.** `consolidation_runs` and `consolidation_candidates` support collapsing
  repeated episodic memories into semantic ones. Members are never deleted, they gain a
  `consolidated_into_id` pointer.

Read `core/classification.md` before writing the extractor. Every distinction above is a
call the extractor makes on every ingest, and the schema cannot enforce a good one.

## Where the tuning lives

All of it is in `core/dynamics.py`, deliberately separated from the logic:

- `SOURCE_WEIGHT`, `BASE_EVIDENCE_WEIGHT`, `EVIDENCE_HALF_LIFE_DAYS` control how
  fast beliefs move and how much an inference is allowed to count against an assertion
- `LEARNING_RATE`, `ASSOCIATION_HALF_LIFE_DAYS`, `CONTEXT_MULTIPLIER` control the
  associative topology
- `SALIENCE_WEIGHTS`, `MEMORY_PROMOTION_THRESHOLD` control what gets remembered
- `ACTIVATION_WEIGHTS`, `CONTRADICTION_BONUS` control what the reasoner sees

Because beliefs are updated in log-odds and associations keep an append-only
event log, changing any constant means re-running `recompute_belief` and the
association replay rather than migrating data. Build that recompute job early.

## Build sequence

1. Ingestion endpoint writes an `Experience` plus the raw artifact and emits `experience.created`.
2. Temporal workflow: extract observations with provenance, detect episodes, record traces.
3. Entity resolution against `entity_aliases` using trigram similarity, then LLM adjudication above a threshold.
4. Memory scoring through `should_promote_to_memory`, writing `evidence` rows for everything promoted.
5. Belief update through `apply_evidence`, appending a `belief_versions` row on every move.
6. Projectors: Neo4j upserts and Qdrant embeddings, both driven off the event log.
7. Retrieval: state, semantic search, graph expansion, temporal pull, then `rank_and_bound`.
8. Consolidation on a schedule: association decay sweep, pattern promotion, contradiction detection, entity centrality.

Steps 1, 2, 4, 6 and 7 are the vertical slice. Steps 3 and 8 can run naively at
first and get better without a schema change.

## Two things to decide before writing the extractor

**Latency split.** The chat path should read only. Extraction, embedding and
consolidation all belong on the async side, or every message costs multiple model
round trips before the reply starts. Write the response path against the
projections and let the write path lag.

**Model access to raw content.** This database becomes the most sensitive thing
you own. Decide now whether extraction runs against a hosted API or locally,
enable encryption at rest on the Postgres volume and the MinIO bucket, and keep
the key outside the compose file.

## Cold start

The brain is worse than a plain chatbot until it has history. Prioritize the
document ingestion path over the chat path in week one and bulk load what already
exists: chat exports, notes, journals, calendar, mail. Backdate `occurred_at`
from the source rather than the ingestion time, otherwise every recency and decay
calculation in `dynamics.py` treats a decade of history as having happened today.
