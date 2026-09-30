"""
api/pipeline.py

The one place the "ingest -> project -> index" sequence is written, shared
by the free-text /api/experiences endpoint and the /api/calibration/fixtures
endpoint so the two don't duplicate transaction handling. Wraps
core.ingest.ingest(), core.projector.project_experience(), and
core.indexer.index_experience() exactly as tests/test_retrieval.py and
tests/test_indexer.py already call them -- no reimplemented business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.indexer import index_experience
from core.ingest import ingest
from core.projector import project_experience


@dataclass
class PipelineResult:
    experience_id: str
    created: bool
    observation_count: int
    entity_count: int
    memory_count: int
    belief_count: int
    memory_entity_link_count: int
    ambiguous: bool
    ambiguity_note: str
    projection_error: Optional[str]
    indexing_error: Optional[str]


def run_full_pipeline(
    text: str,
    source_type: str,
    conn,
    owner_id: str,
    llm_client,
    storage,
    embedder,
    neo4j_driver,
    qdrant_client,
    occurred_at: Optional[datetime] = None,
    metadata: Optional[dict] = None,
) -> PipelineResult:
    """
    Ingest one text, then project it to Neo4j and index it to Qdrant. Each
    stage commits (or rolls back) its own work independently -- a
    projection or indexing failure does not undo a successful ingest, since
    Postgres is canonical and the projections are rebuildable from it
    regardless (the same guarantee core/projector.py and core/indexer.py's
    own module docstrings already rely on).

    `metadata` is stored verbatim in experiences.source (core.ingest.ingest()
    already supports this) and surfaces back out via
    core.reader.inspect_experience()'s `experience.source_metadata` --
    api/routers/calibration.py uses this to stamp {"source": "test_fixture",
    "fixture_id": ...} so synthetic fixture data is always identifiable,
    without adding any new column or changing the ownership/data model.

    experiences and observations are append-only (immutability triggers,
    migration 003) -- there is deliberately no delete/reset path here.
    ingest() is idempotent on (owner_id, content_hash), so calling this
    again with the same text and owner is always safe and never creates a
    duplicate experience; callers that need repeatable, non-growing test
    runs should rely on that rather than trying to clean up afterward.
    """
    result = ingest(text, source_type, conn=conn, owner_id=owner_id,
                     client=llm_client, storage=storage, occurred_at=occurred_at,
                     metadata=metadata)
    conn.commit()

    exp_id = str(result.experience_id)

    projection_error: Optional[str] = None
    try:
        project_experience(exp_id, conn, neo4j_driver)
        conn.commit()
    except Exception as e:
        conn.rollback()
        projection_error = str(e)

    indexing_error: Optional[str] = None
    try:
        index_experience(exp_id, conn, embedder, qdrant_client, storage=storage)
        conn.commit()
    except Exception as e:
        conn.rollback()
        indexing_error = str(e)

    return PipelineResult(
        experience_id=exp_id, created=result.created,
        observation_count=result.observation_count, entity_count=result.entity_count,
        memory_count=result.memory_count, belief_count=result.belief_count,
        memory_entity_link_count=result.memory_entity_link_count,
        ambiguous=result.ambiguous, ambiguity_note=result.ambiguity_note,
        projection_error=projection_error, indexing_error=indexing_error,
    )
