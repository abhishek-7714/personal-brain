"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getExperience, ExperienceInspection } from "@/lib/api";
import Badge from "@/components/Badge";

function EvidenceLinks({ evidence }: { evidence: Array<{ source_type: string; source_id: string; stance: string }> }) {
  if (!evidence || evidence.length === 0) return <span className="muted">no evidence rows</span>;
  return (
    <span>
      {evidence.map((ev, i) => (
        <span key={i}>
          {i > 0 && ", "}
          <Link href={`/explorer?node=${ev.source_id}`} className="id" title="Open in Brain Explorer">
            {ev.stance}:{ev.source_type}:{ev.source_id.slice(0, 8)}…
          </Link>
        </span>
      ))}
    </span>
  );
}

export default function ExperienceInspectorPage({ params }: { params: { id: string } }) {
  const [data, setData] = useState<ExperienceInspection | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getExperience(params.id)
      .then(setData)
      .catch((e) => setError(e.message));
  }, [params.id]);

  if (error) return <div className="error-box">{error}</div>;
  if (!data) return <p className="muted">Loading…</p>;

  const exp = data.experience;
  const isTestFixture = exp.source_metadata?.source === "test_fixture";

  return (
    <div>
      <h2>Experience Inspector</h2>

      {isTestFixture && (
        <div
          className="error-box"
          style={{ borderColor: "var(--warn)", color: "var(--warn)", background: "rgba(224,167,46,0.12)", marginBottom: 16 }}
        >
          ⚠ SYNTHETIC TEST FIXTURE — not a real memory. Created by the Calibration page
          (fixture_id: <code>{exp.source_metadata?.fixture_id}</code>) for validating the retrieval/activation
          pipeline. Every field below is fabricated demonstration content.
        </div>
      )}

      <div className="muted" style={{ marginBottom: 16 }}>
        <Link href={`/explorer?node=${exp.id}`}>Open Experience node in Brain Explorer →</Link>
      </div>

      <h3>Experience</h3>
      <div className="card">
        <table>
          <tbody>
            <tr>
              <td>id</td>
              <td className="id">{exp.id}</td>
            </tr>
            <tr>
              <td>kind (source type)</td>
              <td>{exp.kind}</td>
            </tr>
            <tr>
              <td>occurred_at</td>
              <td>{exp.occurred_at}</td>
            </tr>
            <tr>
              <td>ingested_at</td>
              <td>{exp.ingested_at}</td>
            </tr>
            <tr>
              <td>content_uri</td>
              <td className="muted">{exp.content_uri || "(not stored)"}</td>
            </tr>
            <tr>
              <td>raw source</td>
              <td>
                <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>{exp.raw_text || "(unavailable)"}</pre>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <h3>Observations ({data.observations.length})</h3>
      {data.observations.map((o: any) => (
        <div className="card" key={o.id}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="id">{o.id}</span>
            <span>
              <Badge ok="warn" label={o.epistemic_status} /> <Badge ok="warn" label={o.epistemic_owner} />
            </span>
          </div>
          <p>{o.content}</p>
          <div className="muted" style={{ fontSize: 12 }}>
            confidence={o.extraction_confidence} reliability={o.source_reliability} span={JSON.stringify(o.span)}
          </div>
        </div>
      ))}

      <h3>Episodes ({data.episodes.length})</h3>
      {data.episodes.length === 0 && (
        <p className="muted">
          None. Most experiences contain no episode -- the extractor only emits one when something cognitively
          meaningful happened (classification.md §4).
        </p>
      )}
      {data.episodes.map((ep: any) => (
        <div className="card" key={ep.id}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <strong>[{ep.kind}] {ep.title}</strong>
            <span className="id">{ep.id}</span>
          </div>
          {ep.narrative && <p>{ep.narrative}</p>}
          <div className="muted" style={{ fontSize: 12 }}>
            trigger: {ep.trigger || "—"} · resolution: {ep.resolution || "—"} · salience: {ep.salience}
          </div>
          {ep.outcome !== null && (
            <div style={{ fontSize: 12, marginTop: 4 }}>
              <Badge ok="warn" label="EPISODE OUTCOME (no linked decision)" /> {ep.outcome}
              {ep.outcome_valence !== null && <span className="muted"> (valence={ep.outcome_valence})</span>}
            </div>
          )}
          <div style={{ fontSize: 12, marginTop: 4 }}>
            evidence: <EvidenceLinks evidence={ep.evidence} />
          </div>

          {ep.cognitive_traces.length > 0 && (
            <>
              <h3 style={{ marginBottom: 4 }}>Cognitive traces ({ep.cognitive_traces.length})</h3>
              <table>
                <thead>
                  <tr><th>#</th><th>Operation</th><th>Target</th><th>Conf.</th><th>Observation</th></tr>
                </thead>
                <tbody>
                  {ep.cognitive_traces.map((t: any) => (
                    <tr key={t.seq}>
                      <td>{t.seq}</td>
                      <td><Badge ok="warn" label={t.operation} /></td>
                      <td>{t.target}{t.detail && <div className="muted" style={{ fontSize: 11 }}>{t.detail}</div>}</td>
                      <td>{t.confidence}</td>
                      <td>
                        <Link href={`/explorer?node=${t.observation_id}`} className="id">
                          {t.observation_id.slice(0, 8)}…
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          {ep.state_items.length > 0 && (
            <>
              <h3 style={{ marginBottom: 4 }}>Episode-scoped state items ({ep.state_items.length})</h3>
              {ep.state_items.map((s: any) => (
                <div key={s.id} style={{ fontSize: 13, marginBottom: 4 }}>
                  <Badge ok="warn" label={s.kind} /> {s.content}
                  {s.about_entity && <span className="muted"> — about {s.about_entity}</span>}
                  <span className="muted"> priority={s.priority}</span>
                </div>
              ))}
            </>
          )}

          {ep.decisions.length > 0 && (
            <>
              <h3 style={{ marginBottom: 4 }}>Decisions ({ep.decisions.length})</h3>
              {ep.decisions.map((d: any) => (
                <div className="card" key={d.id} style={{ background: "var(--panel-2)" }}>
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <strong>&ldquo;{d.question}&rdquo;</strong>
                    <span className="id">{d.id}</span>
                  </div>
                  <div className="muted" style={{ fontSize: 12 }}>
                    chosen: {d.chosen_option || "—"} · reversibility: {d.reversibility || "—"} · confidence_at_time=
                    {d.confidence_at_time ?? "—"}
                  </div>
                  {d.rationale && <p style={{ fontSize: 13 }}>{d.rationale}</p>}
                  {d.outcome !== null && (
                    <div style={{ fontSize: 12 }}>
                      <Badge ok={true} label="DECISION OUTCOME (canonical)" /> {d.outcome}
                      {d.outcome_valence !== null && <span className="muted"> (valence={d.outcome_valence})</span>}
                    </div>
                  )}
                  {d.reflection && <div className="muted" style={{ fontSize: 12 }}>reflection: {d.reflection}</div>}
                  <div style={{ fontSize: 12, marginTop: 4 }}>
                    evidence: <EvidenceLinks evidence={d.evidence} />
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
      ))}

      <h3>Memories ({data.memories.length})</h3>
      {data.memories.map((m: any) => (
        <div className="card" key={m.id}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="id">{m.id}</span>
            <span>
              <Badge ok="warn" label={`${m.memory_system}/${m.kind}`} />
              {m.episode_id && (
                <>
                  {" "}
                  <Link href={`/explorer?node=${m.episode_id}`}>
                    <Badge ok={true} label="EPISODE ACTION" />
                  </Link>
                </>
              )}
            </span>
          </div>
          <p>{m.content}</p>
          <div className="muted" style={{ fontSize: 12 }}>
            confidence={m.confidence ?? "—"} salience={m.salience} valid_from={m.valid_from} valid_to=
            {m.valid_to || "null (currently valid)"}
          </div>
          <div style={{ fontSize: 12, marginTop: 4 }}>
            evidence: <EvidenceLinks evidence={m.evidence} />
          </div>
        </div>
      ))}

      <h3>Beliefs ({data.beliefs.length})</h3>
      {data.beliefs.map((b: any) => (
        <div className="card" key={b.id}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="id">{b.id}</span>
            <span>
              <Badge ok="warn" label={b.belief_kind} /> <Badge ok="warn" label={b.status} />
            </span>
          </div>
          <p>&ldquo;{b.proposition}&rdquo;{b.about_entity && <span className="muted"> — about {b.about_entity}</span>}</p>
          <div className="row">
            <div className="col muted" style={{ fontSize: 12 }}>
              truth: log_odds={b.truth.log_odds.toFixed(3)} confidence={b.truth.confidence.toFixed(3)}
            </div>
            <div className="col muted" style={{ fontSize: 12 }}>
              holding: log_odds={b.holding.log_odds.toFixed(3)} confidence={b.holding.confidence.toFixed(3)}
            </div>
          </div>
          <div style={{ fontSize: 12, marginTop: 4 }}>
            evidence: <EvidenceLinks evidence={b.evidence} />
          </div>
          {b.versions.length > 0 && (
            <div style={{ fontSize: 12, marginTop: 4 }}>
              history: {b.versions.map((v: any) => `v${v.version}[${v.status}]`).join(" → ")}
            </div>
          )}
        </div>
      ))}

      <h3>Entities ({data.entities.length})</h3>
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th>Canonical name</th>
              <th>Centrality</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.entities.map((e: any) => (
              <tr key={e.id}>
                <td>{e.kind}</td>
                <td>{e.canonical_name}</td>
                <td>{e.centrality}</td>
                <td>
                  <Link href={`/explorer?node=${e.id}`}>Explore →</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3>Relationships ({data.relationships.length})</h3>
      {data.relationships.length === 0 ? (
        <p className="muted">
          None. The `relationships` table has no writer in core/ingest.py yet, so this is expected to be empty
          for every experience today.
        </p>
      ) : (
        <div className="card">
          <table>
            <tbody>
              {data.relationships.map((r: any) => (
                <tr key={r.id}>
                  <td>{r.source}</td>
                  <td>--{r.rel_type}--&gt;</td>
                  <td>{r.target}</td>
                  <td className="muted">
                    conf={r.confidence} [{r.epistemic_owner}]
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3>Events ({data.events.length})</h3>
      <div className="card">
        <table>
          <tbody>
            {data.events.map((ev: any) => (
              <tr key={ev.event_id}>
                <td>{ev.event_type}</td>
                <td className="muted">{ev.occurred_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
