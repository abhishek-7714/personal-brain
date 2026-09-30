"""
core/reader.py

Read-only inspection of a persisted experience and all its derived objects.
Canonical debugging surface before the Neo4j / Qdrant projection layers
are wired up.

Library usage:
    data = inspect_experience(experience_id, conn)
    print(format_inspection(data))

CLI usage:
    python -m core.reader <experience_id>
    python -m core.reader <experience_id> --json
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Any, Optional
from uuid import UUID


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def inspect_experience(
    experience_id: str,
    conn,
    storage=None,
) -> dict[str, Any]:
    """
    Fetch all data for an experience and return a structured dict.

    The dict is self-contained: every UUID is accompanied by its human-readable
    content so the output is useful without a second DB query.

    storage: optional StorageClient used to fetch the raw artifact text.
             If None, the raw_text is omitted from the result.
    """
    cur = conn.cursor()
    exp_id = str(experience_id)

    # --- Experience ----------------------------------------------------------
    cur.execute(
        """
        SELECT id, kind, summary, content_uri, content_hash,
               occurred_at, ingested_at, source
        FROM experiences WHERE id = %s
        """,
        (exp_id,),
    )
    row = cur.fetchone()
    if row is None:
        return {"error": f"Experience {exp_id} not found"}

    raw_text: Optional[str] = None
    if storage and row[3]:
        try:
            raw_text = storage.get_text(row[3])
        except Exception as e:
            raw_text = f"<error fetching from storage: {e}>"

    experience = {
        "id": str(row[0]),
        "kind": row[1],
        "summary": row[2],
        "content_uri": row[3],
        "content_hash": row[4][:12] + "…" if row[4] else None,
        "occurred_at": _fmt_ts(row[5]),
        "ingested_at": _fmt_ts(row[6]),
        "source_metadata": {k: v for k, v in (row[7] or {}).items() if k != "raw_text"},
        "raw_text": raw_text,
    }

    # --- Extraction runs -----------------------------------------------------
    cur.execute(
        """
        SELECT er.stage, er.status, er.started_at, er.finished_at, er.error,
               mv.provider, mv.model_name
        FROM extraction_runs er
        JOIN model_versions mv ON mv.id = er.model_version_id
        WHERE er.experience_id = %s
        ORDER BY er.created_at
        """,
        (exp_id,),
    )
    extraction_runs = [
        {
            "stage": r[0], "status": r[1],
            "started_at": _fmt_ts(r[2]), "finished_at": _fmt_ts(r[3]),
            "error": r[4],
            "model": f"{r[5]}/{r[6]}",
        }
        for r in cur.fetchall()
    ]

    # --- Observations --------------------------------------------------------
    cur.execute(
        """
        SELECT id, content, epistemic_status, epistemic_owner,
               extraction_confidence, source_reliability, span, observed_at
        FROM observations WHERE experience_id = %s
        ORDER BY observed_at
        """,
        (exp_id,),
    )
    observations = [
        {
            "id": str(r[0]),
            "content": r[1],
            "epistemic_status": r[2],
            "epistemic_owner": r[3],
            "extraction_confidence": r[4],
            "source_reliability": r[5],
            "span": r[6],
            "observed_at": _fmt_ts(r[7]),
        }
        for r in cur.fetchall()
    ]

    # --- Episodes (+ nested cognitive_traces, state_items, decisions) --------
    # Linked directly via episode_experiences, unlike memories/beliefs below
    # which trace back through the evidence spine -- an episode either
    # belongs to this experience or it doesn't, there is no derived-object
    # indirection here.
    cur.execute(
        """
        SELECT ep.id, ep.kind, ep.title, ep.narrative, ep.trigger, ep.resolution,
               ep.salience, ep.outcome, ep.outcome_valence, ep.outcome_recorded_at,
               ep.started_at, ep.ended_at
        FROM episodes ep
        JOIN episode_experiences ee ON ee.episode_id = ep.id
        WHERE ee.experience_id = %s
        ORDER BY ep.started_at
        """,
        (exp_id,),
    )
    raw_episodes = cur.fetchall()
    episode_ids = [str(r[0]) for r in raw_episodes]

    traces_by_episode: dict[str, list[dict]] = {eid: [] for eid in episode_ids}
    if episode_ids:
        cur.execute(
            """
            SELECT episode_id, seq, operation, target, detail, observation_id,
                   confidence, occurred_at
            FROM cognitive_traces
            WHERE episode_id = ANY(%s::uuid[])
            ORDER BY episode_id, seq
            """,
            (episode_ids,),
        )
        for r in cur.fetchall():
            traces_by_episode[str(r[0])].append({
                "seq": r[1], "operation": r[2], "target": r[3], "detail": r[4],
                "observation_id": str(r[5]), "confidence": r[6],
                "occurred_at": _fmt_ts(r[7]),
            })

    state_items_by_episode: dict[str, list[dict]] = {eid: [] for eid in episode_ids}
    if episode_ids:
        cur.execute(
            """
            SELECT si.episode_id, si.id, si.kind, si.content, si.priority,
                   si.activation, si.opened_at, si.closed_at, e.canonical_name
            FROM state_items si
            LEFT JOIN entities e ON e.id = si.entity_id
            WHERE si.episode_id = ANY(%s::uuid[])
            ORDER BY si.priority DESC
            """,
            (episode_ids,),
        )
        for r in cur.fetchall():
            state_items_by_episode[str(r[0])].append({
                "id": str(r[1]), "kind": r[2], "content": r[3],
                "priority": r[4], "activation": r[5],
                "opened_at": _fmt_ts(r[6]), "closed_at": _fmt_ts(r[7]),
                "about_entity": r[8],
                "evidence": _fetch_evidence(cur, "state_item", str(r[1])),
            })

    decisions_by_episode: dict[str, list[dict]] = {eid: [] for eid in episode_ids}
    if episode_ids:
        cur.execute(
            """
            SELECT episode_id, id, question, context, options, chosen_option,
                   rationale, confidence_at_time, reversibility, decided_at,
                   outcome, outcome_valence, outcome_recorded_at, reflection
            FROM decisions
            WHERE episode_id = ANY(%s::uuid[])
            """,
            (episode_ids,),
        )
        for r in cur.fetchall():
            decisions_by_episode[str(r[0])].append({
                "id": str(r[1]), "question": r[2], "context": r[3], "options": r[4],
                "chosen_option": r[5], "rationale": r[6], "confidence_at_time": r[7],
                "reversibility": r[8], "decided_at": _fmt_ts(r[9]),
                "outcome": r[10], "outcome_valence": r[11],
                "outcome_recorded_at": _fmt_ts(r[12]), "reflection": r[13],
                "evidence": _fetch_evidence(cur, "decision", str(r[1])),
            })

    episodes = [
        {
            "id": str(r[0]), "kind": r[1], "title": r[2], "narrative": r[3],
            "trigger": r[4], "resolution": r[5], "salience": r[6],
            "outcome": r[7], "outcome_valence": r[8],
            "outcome_recorded_at": _fmt_ts(r[9]),
            "started_at": _fmt_ts(r[10]), "ended_at": _fmt_ts(r[11]),
            "evidence": _fetch_evidence(cur, "episode", str(r[0])),
            "cognitive_traces": traces_by_episode[str(r[0])],
            "state_items": state_items_by_episode[str(r[0])],
            "decisions": decisions_by_episode[str(r[0])],
        }
        for r in raw_episodes
    ]

    # --- Memories ------------------------------------------------------------
    cur.execute(
        """
        SELECT m.id, m.memory_system, m.kind, m.content, m.salience,
               m.epistemic_status, m.emotion_is_stated,
               m.emotional_valence, m.emotional_intensity, m.proficiency,
               m.valid_from, m.valid_to, m.superseded_by, m.reinterprets_id,
               m.episode_id
        FROM memories m
        JOIN evidence ev ON ev.derived_type = 'memory' AND ev.derived_id = m.id
        WHERE ev.source_type = 'experience' AND ev.source_id = %s
           OR m.id IN (
               SELECT ev2.derived_id FROM evidence ev2
               WHERE ev2.derived_type = 'memory'
                 AND ev2.source_type = 'observation'
                 AND ev2.source_id IN (
                     SELECT id FROM observations WHERE experience_id = %s
                 )
           )
        ORDER BY m.valid_from
        """,
        (exp_id, exp_id),
    )
    raw_mems = cur.fetchall()
    mem_ids = [str(r[0]) for r in raw_mems]
    memories = [
        {
            "id": str(r[0]),
            "memory_system": r[1], "kind": r[2], "content": r[3],
            "salience": r[4], "epistemic_status": r[5],
            "emotion_is_stated": r[6],
            "emotional_valence": r[7], "emotional_intensity": r[8],
            "proficiency": r[9],
            "valid_from": _fmt_ts(r[10]), "valid_to": _fmt_ts(r[11]),
            "superseded_by": str(r[12]) if r[12] else None,
            "reinterprets_id": str(r[13]) if r[13] else None,
            # Set only for the episodic memory recording the action taken --
            # see core/classification.md §10.
            "episode_id": str(r[14]) if r[14] else None,
            "evidence": _fetch_evidence(cur, "memory", str(r[0])),
        }
        for r in raw_mems
    ]

    # --- Beliefs + both axes -------------------------------------------------
    cur.execute(
        """
        SELECT b.id, b.proposition, b.belief_kind, b.domain,
               b.status, b.epistemic_owner,
               b.log_odds, b.confidence,
               b.holding_log_odds, b.holding_confidence,
               b.held_since, b.last_evidence_at,
               b.about_entity_id
        FROM beliefs b
        WHERE b.id IN (
            SELECT ev.derived_id FROM evidence ev
            WHERE ev.derived_type = 'belief'
              AND (
                  (ev.source_type = 'experience' AND ev.source_id = %s)
                  OR ev.source_id IN (
                      SELECT id FROM observations WHERE experience_id = %s
                  )
              )
        )
        ORDER BY b.held_since
        """,
        (exp_id, exp_id),
    )
    beliefs = []
    for r in cur.fetchall():
        about_name = None
        if r[12]:
            about_row = _fetch_one(cur, "SELECT canonical_name FROM entities WHERE id = %s", (str(r[12]),))
            about_name = about_row[0] if about_row else str(r[12])
        beliefs.append({
            "id": str(r[0]),
            "proposition": r[1],
            "belief_kind": r[2], "domain": r[3], "status": r[4],
            "epistemic_owner": r[5],
            "truth": {"log_odds": r[6], "confidence": r[7]},
            "holding": {"log_odds": r[8], "confidence": r[9]},
            "held_since": _fmt_ts(r[10]),
            "last_evidence_at": _fmt_ts(r[11]),
            "about_entity": about_name,
            "evidence": _fetch_evidence(cur, "belief", str(r[0])),
            "versions": _fetch_belief_versions(cur, str(r[0])),
        })

    # --- Entities + relationships --------------------------------------------
    obs_ids_placeholder = ",".join(["%s"] * len(observations)) if observations else "''"
    entity_ids: list[str] = []
    if observations:
        obs_id_vals = tuple(o["id"] for o in observations)
        # Entities referenced from memories, beliefs, OR episode-scoped
        # state_items derived from this experience. The state_items branch
        # matters for episodes that produce no memory and no belief at all
        # (a conflict episode whose only entity reference is the person the
        # open question is about) -- without it, an entity that resolved
        # correctly in Postgres is invisible in the inspection surface.
        cur.execute(
            """
            SELECT DISTINCT e.id, e.kind, e.canonical_name, e.description, e.centrality
            FROM entities e
            WHERE e.id IN (
                SELECT me.entity_id FROM memory_entities me
                WHERE me.memory_id = ANY(%s::uuid[])
            )
            OR e.id IN (
                SELECT b.about_entity_id FROM beliefs b
                WHERE b.id IN (
                    SELECT DISTINCT ev.derived_id FROM evidence ev
                    WHERE ev.derived_type = 'belief'
                      AND ev.source_id IN (
                          SELECT id FROM observations WHERE experience_id = %s
                      )
                ) AND b.about_entity_id IS NOT NULL
            )
            OR e.id IN (
                SELECT si.entity_id FROM state_items si
                WHERE si.entity_id IS NOT NULL
                  AND si.episode_id = ANY(%s::uuid[])
            )
            ORDER BY e.kind, e.canonical_name
            """,
            ([str(mid) for mid in mem_ids], exp_id, episode_ids),
        )
    else:
        cur.execute(
            "SELECT e.id, e.kind, e.canonical_name, e.description, e.centrality "
            "FROM entities e WHERE false"
        )
    raw_entities = cur.fetchall()
    entity_ids = [str(r[0]) for r in raw_entities]

    relationships: list[dict] = []
    if entity_ids:
        cur.execute(
            """
            SELECT r.id, r.rel_type,
                   se.canonical_name, te.canonical_name,
                   r.confidence, r.epistemic_owner, r.valid_from, r.valid_to
            FROM relationships r
            JOIN entities se ON se.id = r.source_entity_id
            JOIN entities te ON te.id = r.target_entity_id
            WHERE r.source_entity_id = ANY(%s::uuid[])
               OR r.target_entity_id = ANY(%s::uuid[])
            """,
            (entity_ids, entity_ids),
        )
        relationships = [
            {
                "id": str(r[0]), "rel_type": r[1],
                "source": r[2], "target": r[3],
                "confidence": r[4], "epistemic_owner": r[5],
                "valid_from": _fmt_ts(r[6]), "valid_to": _fmt_ts(r[7]),
            }
            for r in cur.fetchall()
        ]

    entities = [
        {
            "id": str(r[0]), "kind": r[1], "canonical_name": r[2],
            "description": r[3], "centrality": r[4],
        }
        for r in raw_entities
    ]

    # --- Events emitted ------------------------------------------------------
    cur.execute(
        """
        SELECT event_id, event_type, occurred_at, payload
        FROM events
        WHERE aggregate_type = 'experience' AND aggregate_id = %s
        ORDER BY id
        """,
        (exp_id,),
    )
    events = [
        {
            "event_id": str(r[0]), "event_type": r[1],
            "occurred_at": _fmt_ts(r[2]), "payload": r[3],
        }
        for r in cur.fetchall()
    ]

    return {
        "experience": experience,
        "extraction_runs": extraction_runs,
        "observations": observations,
        "episodes": episodes,
        "memories": memories,
        "beliefs": beliefs,
        "entities": entities,
        "relationships": relationships,
        "events": events,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_one(cur, sql: str, params: tuple) -> Optional[tuple]:
    cur.execute(sql, params)
    return cur.fetchone()


def _fmt_ts(ts: Optional[datetime]) -> Optional[str]:
    if ts is None:
        return None
    return ts.isoformat(timespec="seconds")


def _fetch_evidence(cur, derived_type: str, derived_id: str) -> list[dict]:
    cur.execute(
        """
        SELECT source_type, source_id, stance, weight, recorded_at
        FROM evidence
        WHERE derived_type = %s AND derived_id = %s
        ORDER BY recorded_at
        """,
        (derived_type, derived_id),
    )
    return [
        {
            "source_type": r[0], "source_id": str(r[1]),
            "stance": r[2], "weight": r[3],
            "recorded_at": _fmt_ts(r[4]),
        }
        for r in cur.fetchall()
    ]


def _fetch_belief_versions(cur, belief_id: str) -> list[dict]:
    cur.execute(
        """
        SELECT version, status, confidence, log_odds,
               holding_confidence, holding_log_odds, axis, reason, recorded_at
        FROM belief_versions
        WHERE belief_id = %s
        ORDER BY version
        """,
        (belief_id,),
    )
    return [
        {
            "version": r[0], "status": r[1],
            "confidence": r[2], "log_odds": r[3],
            "holding_confidence": r[4], "holding_log_odds": r[5],
            "axis": r[6], "reason": r[7],
            "recorded_at": _fmt_ts(r[8]),
        }
        for r in cur.fetchall()
    ]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_inspection(data: dict[str, Any]) -> str:
    if "error" in data:
        return f"ERROR: {data['error']}\n"

    lines: list[str] = []

    def h1(title: str) -> None:
        lines.append(f"\n{'='*60}")
        lines.append(f"  {title}")
        lines.append(f"{'='*60}")

    def h2(title: str) -> None:
        lines.append(f"\n--- {title} ---")

    def kv(key: str, value: Any, indent: int = 2) -> None:
        pad = " " * indent
        lines.append(f"{pad}{key}: {value}")

    exp = data["experience"]
    h1(f"EXPERIENCE  {exp['id'][:8]}…")
    kv("kind", exp["kind"])
    kv("occurred_at", exp["occurred_at"])
    kv("ingested_at", exp["ingested_at"])
    kv("content_uri", exp["content_uri"] or "(not stored)")
    kv("content_hash", exp["content_hash"])
    if exp.get("raw_text"):
        h2("Raw text")
        for line in exp["raw_text"].splitlines():
            lines.append(f"  {line}")

    # Extraction runs
    h2("Extraction runs")
    for r in data["extraction_runs"]:
        lines.append(f"  [{r['status']:>10}]  {r['stage']:<22}  {r['model']}")

    # Observations
    h2(f"Observations ({len(data['observations'])})")
    for i, o in enumerate(data["observations"], 1):
        lines.append(f"  {i}. [{o['epistemic_status']}/{o['epistemic_owner']}]  "
                     f"conf={o['extraction_confidence']:.2f}  "
                     f"span={o['span']}")
        lines.append(f"     {o['content']}")

    # Episodes (+ nested cognitive traces, state items, decisions)
    h2(f"Episodes ({len(data['episodes'])})")
    if not data["episodes"]:
        lines.append("  (none -- most experiences contain no episode; see classification.md §4)")
    for ep in data["episodes"]:
        lines.append(f"  [{ep['kind']}] {ep['title']}")
        if ep["trigger"]:
            lines.append(f"    trigger: {ep['trigger']}")
        if ep["narrative"]:
            lines.append(f"    narrative: {ep['narrative']}")
        if ep["resolution"]:
            lines.append(f"    resolution: {ep['resolution']}")
        if ep["outcome"] is not None:
            lines.append(f"    outcome: {ep['outcome']}  (valence={ep['outcome_valence']})")
        for ev in ep["evidence"]:
            lines.append(f"    evidence: {ev['stance']}  {ev['source_type']}:{ev['source_id'][:8]}…")

        if ep["cognitive_traces"]:
            lines.append(f"    -- cognitive traces ({len(ep['cognitive_traces'])}) --")
            for t in ep["cognitive_traces"]:
                lines.append(
                    f"      {t['seq']}. {t['operation']}: {t['target']}  "
                    f"(conf={t['confidence']:.2f}, obs={t['observation_id'][:8]}…)"
                )

        if ep["state_items"]:
            lines.append(f"    -- episode-scoped state items ({len(ep['state_items'])}) --")
            for s in ep["state_items"]:
                about = f"  about={s['about_entity']}" if s["about_entity"] else ""
                lines.append(f"      [{s['kind']}] {s['content']}  priority={s['priority']:.2f}{about}")

        if ep["decisions"]:
            lines.append(f"    -- decisions ({len(ep['decisions'])}) --")
            for d in ep["decisions"]:
                lines.append(f"      \"{d['question']}\"  chosen={d['chosen_option']}")
                if d["outcome"] is not None:
                    lines.append(f"        outcome: {d['outcome']}  (valence={d['outcome_valence']})")

    # Memories
    h2(f"Memories ({len(data['memories'])})")
    for m in data["memories"]:
        emotion = ""
        if m["emotion_is_stated"] and m["emotional_valence"] is not None:
            emotion = f"  emotion val={m['emotional_valence']:+.2f} int={m['emotional_intensity']:.2f}"
        episode_tag = "  [episode action]" if m.get("episode_id") else ""
        lines.append(f"  [{m['memory_system']}/{m['kind']}]  salience={m['salience']:.2f}{emotion}{episode_tag}")
        lines.append(f"    {m['content']}")
        for ev in m["evidence"]:
            lines.append(f"    evidence: {ev['stance']}  {ev['source_type']}:{ev['source_id'][:8]}…")

    # Beliefs
    h2(f"Beliefs ({len(data['beliefs'])})")
    for b in data["beliefs"]:
        about = f"  about={b['about_entity']}" if b["about_entity"] else ""
        lines.append(f"  [{b['belief_kind']}]  status={b['status']}{about}")
        lines.append(f"    \"{b['proposition']}\"")
        lines.append(f"    truth:   log_odds={b['truth']['log_odds']:+.3f}  "
                     f"confidence={b['truth']['confidence']:.3f}")
        lines.append(f"    holding: log_odds={b['holding']['log_odds']:+.3f}  "
                     f"confidence={b['holding']['confidence']:.3f}")
        for ev in b["evidence"]:
            lines.append(f"    evidence: {ev['stance']}  {ev['source_type']}:{ev['source_id'][:8]}…")
        for v in b["versions"]:
            lines.append(f"    v{v['version']} [{v['status']}]  {v['reason']}")

    # Entities
    h2(f"Entities ({len(data['entities'])})")
    for e in data["entities"]:
        lines.append(f"  [{e['kind']}]  {e['canonical_name']}  centrality={e['centrality']:.3f}")
        if e["description"]:
            lines.append(f"    {e['description']}")

    # Relationships
    if data["relationships"]:
        h2(f"Relationships ({len(data['relationships'])})")
        for r in data["relationships"]:
            lines.append(f"  {r['source']}  --{r['rel_type']}-->  {r['target']}  "
                         f"conf={r['confidence']:.2f}  [{r['epistemic_owner']}]")

    # Events
    h2(f"Events emitted ({len(data['events'])})")
    for ev in data["events"]:
        lines.append(f"  {ev['event_type']}  at={ev['occurred_at']}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import psycopg2

    parser = argparse.ArgumentParser(
        description="Inspect a persisted experience and all its derived objects."
    )
    parser.add_argument("experience_id", help="UUID of the experience to inspect")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("PG_DSN", "postgresql://brain:brain@localhost:5432/brain"),
        help="Postgres connection string (default: $PG_DSN)",
    )
    args = parser.parse_args()

    conn = psycopg2.connect(args.dsn)
    try:
        storage = None
        try:
            from core.storage import from_env
            storage = from_env()
        except Exception:
            pass  # storage unavailable; raw_text will be omitted

        data = inspect_experience(args.experience_id, conn, storage=storage)

        if args.json:
            print(json.dumps(data, default=str, indent=2))
        else:
            print(format_inspection(data))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
