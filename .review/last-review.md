No ledger issues are open or disputed. ISS-1 is already marked fixed.

- **NEW — medium — [api/deps.py](/Users/abhis/Documents/personal-brain/api/deps.py:64):** If MinIO is unreachable, each request gets a fresh in-memory mock storage client. Ingestion can then commit an experience whose raw text disappears with that request, so later inspection cannot retrieve it.

VERDICT: CHANGES