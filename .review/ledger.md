# Review ledger

Base: 835cb649b2d70c14710915a266b6ae571568ab19
Task: Whole-project review of the personal-brain foundation (schema, core pipeline, API, web console) after migrating the dev environment from Windows to macOS

## Issues
| ID | Severity | File | Claim | Status | Notes |
|----|----------|------|-------|--------|-------|
| ISS-1 | medium | vector/collections.py | Collection bootstrap defaults to 1536 dims while both embedders default to 1024; indexing fails without EMBEDDING_DIM set | fixed | Default now derived from core.embedder.DEFAULT_OUTPUT_DIMENSION so the two cannot drift; verified with EMBEDDING_DIM unset (both 1024). .env.example also sets EMBEDDING_DIM=1024. |

## Rounds
- Round 1: VERDICT CHANGES; tests PASS (dynamics 38, schema_static 71, invariants 36). 1 new issue (ISS-1), fixed.
