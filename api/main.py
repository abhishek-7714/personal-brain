"""
api/main.py

FastAPI app for the Brain Console. Every route is a thin wrapper over
core/*.py -- no business logic lives here or in the Next.js frontend
(web/). Postgres stays canonical; Neo4j and Qdrant stay projections, read
here exactly the way core/reader.py, core/retrieval.py and
core/query_inspector.py already read them.

Run:
    uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from neo4j import GraphDatabase
from qdrant_client import QdrantClient

from api.routers import calibration, experiences, graph, retrieval, status


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.neo4j_driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(
            os.environ.get("NEO4J_USER", "neo4j"),
            os.environ.get("NEO4J_PASSWORD", "brainbrain"),
        ),
    )
    app.state.qdrant = QdrantClient(url=os.environ.get("QDRANT_URL", "http://localhost:6333"))
    try:
        yield
    finally:
        app.state.neo4j_driver.close()


app = FastAPI(title="Brain Console API", lifespan=lifespan)

web_origins = os.environ.get(
    "WEB_ORIGINS", "http://localhost:3000,http://localhost:3001"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in web_origins if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(status.router, prefix="/api/status", tags=["status"])
app.include_router(experiences.router, prefix="/api/experiences", tags=["experiences"])
app.include_router(graph.router, prefix="/api/graph", tags=["graph"])
app.include_router(retrieval.router, prefix="/api/retrieval", tags=["retrieval"])
app.include_router(calibration.router, prefix="/api/calibration", tags=["calibration"])


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}
