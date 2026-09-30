# Review ledger

Base: 835cb649b2d70c14710915a266b6ae571568ab19
Task: Whole-project review of the personal-brain foundation (schema, core pipeline, API, web console) after migrating the dev environment from Windows to macOS

## Issues
| ID | Severity | File | Claim | Status | Notes |
|----|----------|------|-------|--------|-------|
| ISS-1 | medium | vector/collections.py | Collection bootstrap defaults to 1536 dims while both embedders default to 1024; indexing fails without EMBEDDING_DIM set | fixed | Default now derived from core.embedder.DEFAULT_OUTPUT_DIMENSION so the two cannot drift; verified with EMBEDDING_DIM unset (both 1024). .env.example also sets EMBEDDING_DIM=1024. |
| ISS-2 | medium | api/deps.py | When MinIO is unreachable get_storage returns a per-request MockStorageClient, so ingest commits an immutable experience whose raw text is lost | fixed | Fallback removed; get_storage now raises HTTP 503. Verified: MinIO up -> MinIOStorageClient, unreachable endpoint -> 503. Tests construct MockStorageClient directly, never via get_storage. Accepted side effect: GET /api/experiences/{id} also 503s while MinIO is down rather than rendering without raw text. |

## Rounds
- Round 1: VERDICT CHANGES; tests PASS (dynamics 38, schema_static 71, invariants 36). 1 new issue (ISS-1), fixed.
- Round 2: VERDICT CHANGES; tests PASS (dynamics 38, schema_static 71, invariants 36). ISS-1 confirmed fixed; 1 new issue (ISS-2), fixed.
- Round 3: VERDICT APPROVE; tests PASS (dynamics 38, schema_static 71, invariants 36). ISS-1, ISS-2 confirmed fixed; no new issues.
