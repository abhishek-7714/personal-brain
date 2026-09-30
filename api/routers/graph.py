"""
api/routers/graph.py

Section 4 (Brain Explorer) backend. Every query here reads whatever is
actually in Neo4j and nothing else -- no relationship type or node shape is
assumed beyond what core/projector.py actually writes (see its module
docstring for the full list: EXPERIENCED, PRODUCED, ASSERTED_BY,
DERIVED_FROM, INVOLVES, ABOUT, PART_OF, {SUPERSEDES,CONTRADICTS,SUPPORTS,
REFINES}, RELATED_TO). Labels come from graph/schema.cypher's own
constraint list.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

# graph/schema.cypher's own uniqueness constraints -- the complete, real
# label set the projector can produce. Not all of these are populated yet
# (Episode/Decision/CognitiveTrace have no producer in core/ingest.py).
LABELS = [
    "Owner", "Entity", "Experience", "Observation", "Episode",
    "CognitiveTrace", "Memory", "Belief", "MentalModel",
    "CognitivePattern", "Decision", "StateItem",
]


def _node_summary(props: dict, label: str) -> str:
    for key in ("canonical_name", "content", "proposition", "title", "display_name", "name", "kind"):
        if props.get(key):
            return str(props[key])[:120]
    return f"{label} ({props.get('id', '?')[:8]}…)"


@router.get("/labels")
def list_labels(request: Request) -> dict:
    driver = request.app.state.neo4j_driver
    counts = {}
    with driver.session() as session:
        for label in LABELS:
            rec = session.run(f"MATCH (n:{label}) RETURN count(n) AS c").single()
            counts[label] = rec["c"] if rec else 0
    return counts


@router.get("/nodes")
def list_nodes(request: Request, label: str, limit: int = 50) -> list[dict]:
    if label not in LABELS:
        raise HTTPException(400, f"Unknown label {label!r}. Valid: {LABELS}")
    driver = request.app.state.neo4j_driver
    with driver.session() as session:
        records = session.run(f"MATCH (n:{label}) RETURN n LIMIT $limit", limit=limit)
        nodes = []
        for rec in records:
            props = dict(rec["n"])
            nodes.append({"id": props.get("id"), "label": label, "summary": _node_summary(props, label), "properties": props})
    return nodes


@router.get("/node/{node_id}")
def get_node(node_id: str, request: Request) -> dict:
    driver = request.app.state.neo4j_driver
    with driver.session() as session:
        rec = session.run(
            "MATCH (n {id: $id}) RETURN n, labels(n) AS labels", id=node_id
        ).single()
        if rec is None:
            raise HTTPException(404, "Node not found")
        props = dict(rec["n"])
        labels = rec["labels"]

        outgoing = [
            {
                "rel_type": r["rel_type"], "properties": dict(r["props"]),
                "target_id": r["target_id"], "target_label": r["target_label"],
                "target_summary": _node_summary(dict(r["target_props"]), r["target_label"] or ""),
            }
            for r in session.run(
                "MATCH (n {id: $id})-[r]->(m) "
                "RETURN type(r) AS rel_type, properties(r) AS props, "
                "m.id AS target_id, labels(m)[0] AS target_label, properties(m) AS target_props",
                id=node_id,
            )
        ]
        incoming = [
            {
                "rel_type": r["rel_type"], "properties": dict(r["props"]),
                "source_id": r["source_id"], "source_label": r["source_label"],
                "source_summary": _node_summary(dict(r["source_props"]), r["source_label"] or ""),
            }
            for r in session.run(
                "MATCH (n {id: $id})<-[r]-(m) "
                "RETURN type(r) AS rel_type, properties(r) AS props, "
                "m.id AS source_id, labels(m)[0] AS source_label, properties(m) AS source_props",
                id=node_id,
            )
        ]

    return {
        "id": node_id, "labels": labels, "properties": props,
        "postgres_id": props.get("id"),  # the projector keys every node's `id` on the Postgres uuid -- same value
        "outgoing": outgoing, "incoming": incoming,
    }
