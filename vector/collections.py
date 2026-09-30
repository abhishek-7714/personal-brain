"""
collections.py

Bootstraps the Qdrant collections. The vector store is a semantic index and
nothing else: every point carries source_type and source_id pointing back into
PostgreSQL, and the entire store can be dropped and rebuilt from the canonical
tables without losing anything.

Run: python -m vector.collections
"""

from __future__ import annotations

import os

from qdrant_client import QdrantClient, models

from core.embedder import DEFAULT_OUTPUT_DIMENSION

# Defaults to the embedder's own output size rather than a separate literal. A
# collection's vector size is fixed at creation, so a mismatched default here
# only surfaces later as every upsert failing core/indexer.py:_check_dimension.
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", str(DEFAULT_OUTPUT_DIMENSION)))
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")

# Four collections rather than one, because the re-ranker weights each channel
# differently and filtering across a single mixed collection is slower and blunter.
COLLECTIONS: dict[str, str] = {
    "episodic_memory": "Experiences, episodes and decisions. What happened.",
    "semantic_memory": "Facts, concepts, definitions, preferences, mental models. What is known.",
    "cognitive_memory": "Cognitive traces and patterns. How the owner approaches things.",
    "documents": "Chunks of ingested external material. Source text, not conclusions.",
}

# Payload fields that get an index, because the activation engine filters on all
# of them before scoring. Anything not listed here is stored but not filterable.
PAYLOAD_SCHEMA: dict[str, models.PayloadSchemaType] = {
    "source_type": models.PayloadSchemaType.KEYWORD,   # memory | belief | episode | ...
    "source_id": models.PayloadSchemaType.KEYWORD,     # canonical Postgres uuid
    "owner_id": models.PayloadSchemaType.KEYWORD,
    "kind": models.PayloadSchemaType.KEYWORD,
    "epistemic_owner": models.PayloadSchemaType.KEYWORD,
    "epistemic_status": models.PayloadSchemaType.KEYWORD,
    "entity_ids": models.PayloadSchemaType.KEYWORD,    # list; enables entity-scoped search
    "domain": models.PayloadSchemaType.KEYWORD,
    "salience": models.PayloadSchemaType.FLOAT,
    "confidence": models.PayloadSchemaType.FLOAT,
    "occurred_at": models.PayloadSchemaType.DATETIME,
    "valid_from": models.PayloadSchemaType.DATETIME,
    "valid_to": models.PayloadSchemaType.DATETIME,     # null means currently valid
    "is_stale": models.PayloadSchemaType.BOOL,  # retrieval-state hint, never a truth marker -- see core/indexer.py module docstring
    "model_version_id": models.PayloadSchemaType.KEYWORD,  # for selective re-embedding
}


def build(client: QdrantClient, recreate: bool = False) -> None:
    for name, description in COLLECTIONS.items():
        exists = client.collection_exists(name)
        if exists and recreate:
            client.delete_collection(name)
            exists = False
        if not exists:
            client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=EMBEDDING_DIM,
                    distance=models.Distance.COSINE,
                ),
                # Quantization keeps the working set in memory as the archive grows;
                # a personal brain is small by warehouse standards but grows forever.
                quantization_config=models.ScalarQuantization(
                    scalar=models.ScalarQuantizationConfig(
                        type=models.ScalarType.INT8,
                        always_ram=True,
                    )
                ),
                hnsw_config=models.HnswConfigDiff(m=32, ef_construct=200),
            )
            print(f"created {name}: {description}")

        for field, schema in PAYLOAD_SCHEMA.items():
            client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=schema,
                wait=True,
            )
        print(f"indexed payload fields on {name}")


def default_filter(owner_id: str, include_stale: bool = False) -> models.Filter:
    """
    Baseline filter for retrieval: this owner, currently valid unless the query
    is explicitly historical. Temporal questions pass include_stale=True and
    let the re-ranker apply the staleness penalty instead.
    """
    must: list[models.Condition] = [
        models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id))
    ]
    if not include_stale:
        must.append(
            models.FieldCondition(key="is_stale", match=models.MatchValue(value=False))
        )
    return models.Filter(must=must)


if __name__ == "__main__":
    build(QdrantClient(url=QDRANT_URL), recreate=os.getenv("RECREATE") == "1")
