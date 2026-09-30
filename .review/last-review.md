No open or disputed issue IDs are listed in `.review/ledger.md`.

- **NEW — medium — [vector/collections.py](/Users/abhis/Documents/personal-brain/vector/collections.py):** The collection bootstrap defaults to 1536 dimensions, while both embedders default to 1024. On the documented setup without `EMBEDDING_DIM` exported in the shell, indexing fails with a dimension mismatch.

VERDICT: CHANGES