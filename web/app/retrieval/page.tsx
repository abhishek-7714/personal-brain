"use client";

import dynamic from "next/dynamic";
import { useState } from "react";
import { runRetrievalQuery, RetrievalResult, RetrievalCandidateJSON, MergedCandidateJSON } from "@/lib/api";
import Badge from "@/components/Badge";

// See web/app/brain/page.tsx for why this must be a client-only dynamic import.
const BrainCanvas = dynamic(() => import("@/components/brain/BrainCanvas"), { ssr: false });

function averageScore(candidates: RetrievalCandidateJSON[]): number {
  if (candidates.length === 0) return 0;
  return candidates.reduce((s, c) => s + c.normalized_score, 0) / candidates.length;
}

function ChannelBar({ label, candidates }: { label: string; candidates: RetrievalCandidateJSON[] }) {
  const value = averageScore(candidates);
  return (
    <div className="channel-bar-row">
      <span className="channel-bar-label">{label}</span>
      <div className="channel-bar-track">
        <div className="channel-bar-fill" style={{ width: `${Math.round(value * 100)}%` }} />
      </div>
      <span className="channel-bar-value">
        {value.toFixed(2)} <span className="muted">({candidates.length})</span>
      </span>
    </div>
  );
}

function CandidateRow({ c }: { c: RetrievalCandidateJSON }) {
  return (
    <tr>
      <td className="score">{c.normalized_score.toFixed(3)}</td>
      <td>
        {c.source_type}:<span className="id">{c.source_id.slice(0, 8)}…</span>
      </td>
      <td className="muted">{c.content_preview}</td>
      <td className="muted" style={{ fontSize: 12 }}>
        {c.reason}
      </td>
    </tr>
  );
}

function MergedRow({ m }: { m: MergedCandidateJSON }) {
  const b = m.breakdown;
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span>
          <Badge ok={m.included} label={m.included ? "INCLUDED" : "excluded"} />{" "}
          <span className="score">{m.final_score.toFixed(3)}</span> <span className="id">{m.ref}</span>
        </span>
      </div>
      <div className="muted" style={{ fontSize: 12, marginTop: 6, fontFamily: "var(--mono)" }}>
        semantic={b.semantic.toFixed(2)} association={b.association.toFixed(2)} situation={b.situation.toFixed(2)}{" "}
        goal_alignment={b.goal_alignment.toFixed(2)} salience={b.salience.toFixed(2)} evidence_quality=
        {b.evidence_quality.toFixed(2)} staleness={b.staleness.toFixed(2)}{" "}
        {b.is_contradiction && <Badge ok="warn" label="CONTRADICTION" />}
      </div>
      {m.discoveries.map((d, i) => (
        <div key={i} style={{ fontSize: 12, marginTop: 4 }}>
          <span className="muted">[{d.retrieval_channel}]</span> {d.reason}
        </div>
      ))}
    </div>
  );
}

export default function RetrievalPlaygroundPage() {
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<RetrievalResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [debug, setDebug] = useState(false);
  const [focusNodeId, setFocusNodeId] = useState<string | null>(null);

  async function handleRun() {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      setResult(await runRetrievalQuery(query, true));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <h2>Retrieval Playground</h2>
      <p className="muted">
        Runs core.retrieval.hybrid_query() end to end -- Qdrant + Neo4j + Postgres candidates, scored by
        core.dynamics.activation_score()/rank_and_bound() unmodified. No LLM call, no generated answer. Every
        query writes an append-only activation_traces row.
      </p>

      <div className="card">
        <div className="row">
          <input
            style={{ flex: 1 }}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="What do I know about...?"
            onKeyDown={(e) => e.key === "Enter" && handleRun()}
          />
          <button onClick={handleRun} disabled={loading || !query.trim()}>
            {loading ? "Running…" : "Run Query"}
          </button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}

      {result && (
        <>
          <h3>Query</h3>
          <div className="card">{result.query}</div>

          <h3>Activation Visualization</h3>
          <p className="muted" style={{ fontSize: 12 }}>
            Same activation_events this run produced, animated as current flowing through the real topology --
            nothing re-scored, nothing invented. Click a firing node for the same inspector as below.
          </p>
          <div className="row" style={{ alignItems: "flex-start" }}>
            <div className="col" style={{ flex: 2, minWidth: 420 }}>
              <BrainCanvas result={result} debug={debug} onDebugToggle={setDebug} focusNodeId={focusNodeId} heightPx={480} />
            </div>
            <div className="col" style={{ flex: 1, minWidth: 260 }}>
              <h3 style={{ marginTop: 0 }}>Activation Channels</h3>
              <div className="card">
                <ChannelBar label="Semantic" candidates={result.qdrant_results} />
                <ChannelBar label="Graph" candidates={result.neo4j_results} />
                <ChannelBar label="Temporal" candidates={result.postgres_results.temporal} />
                <ChannelBar label="State" candidates={result.postgres_results.state} />
              </div>

              <h3>Active Memories</h3>
              <div className="card">
                {result.merged_candidates.filter((m) => m.included).length === 0 && (
                  <span className="muted">Nothing cleared the activation threshold.</span>
                )}
                {result.merged_candidates
                  .filter((m) => m.included)
                  .slice(0, 8)
                  .map((m, i) => {
                    const label = m.discoveries.find((d) => d.content_preview)?.content_preview || m.ref;
                    return (
                      <div key={m.ref} className="active-memory-row" onClick={() => setFocusNodeId(m.ref)}>
                        <span className="active-memory-rank">#{i + 1}</span>
                        <div>
                          <div style={{ fontSize: 13 }}>{label}</div>
                          <div className="muted" style={{ fontSize: 11 }}>
                            {m.source_type} — score={m.final_score.toFixed(2)}
                          </div>
                        </div>
                      </div>
                    );
                  })}
              </div>
            </div>
          </div>

          <h3>Activation Seeds</h3>
          <div className="card">
            {result.seed_entities.length === 0 ? (
              <span className="muted">
                None matched by name in the query text -- graph retrieval, if any, was seeded entirely from top
                semantic hits&apos; entities instead.
              </span>
            ) : (
              result.seed_entities.map((e) => (
                <div key={e.id}>
                  {e.canonical_name} <span className="muted">({e.kind}, centrality={e.centrality})</span>{" "}
                  <span className="id">{e.id}</span>
                </div>
              ))
            )}
            {result.all_seed_entities.length > result.seed_entities.length && (
              <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
                +{result.all_seed_entities.length - result.seed_entities.length} more entity(ies) pulled in from top
                semantic hits and also walked by graph retrieval (all_seed_entities) -- these are what the brain
                visualization above renders as additional seed neurons.
              </p>
            )}
          </div>

          <h3>Qdrant Results ({result.qdrant_results.length})</h3>
          <table>
            <thead>
              <tr>
                <th>Cosine</th>
                <th>Source</th>
                <th>Preview</th>
                <th>Reason / collection</th>
              </tr>
            </thead>
            <tbody>
              {result.qdrant_results.map((c, i) => (
                <CandidateRow key={i} c={c} />
              ))}
            </tbody>
          </table>

          <h3>Neo4j Results ({result.neo4j_results.length})</h3>
          <table>
            <thead>
              <tr>
                <th>Strength</th>
                <th>Source</th>
                <th>Preview</th>
                <th>Relationship / hops</th>
              </tr>
            </thead>
            <tbody>
              {result.neo4j_results.map((c, i) => (
                <CandidateRow key={i} c={c} />
              ))}
            </tbody>
          </table>

          <h3>Postgres Results</h3>
          <div className="row">
            <div className="col">
              <strong>Temporal ({result.postgres_results.temporal.length})</strong>
              {result.postgres_results.temporal.slice(0, 8).map((c, i) => (
                <div key={i} style={{ fontSize: 12 }} className="muted">
                  {c.normalized_score.toFixed(2)} — {c.reason}
                </div>
              ))}
            </div>
            <div className="col">
              <strong>Current state ({result.postgres_results.state.length})</strong>
              {result.postgres_results.state.map((c, i) => (
                <div key={i} style={{ fontSize: 12 }} className="muted">
                  {c.normalized_score.toFixed(2)} — {c.reason}
                </div>
              ))}
            </div>
            <div className="col">
              <strong>Mental models / patterns ({result.postgres_results.situation.length})</strong>
              {result.postgres_results.situation.map((c, i) => (
                <div key={i} style={{ fontSize: 12 }} className="muted">
                  {c.normalized_score.toFixed(2)} — {c.reason}
                </div>
              ))}
            </div>
          </div>

          <h3>Merged Candidates ({result.merged_candidates.length})</h3>
          <p className="muted" style={{ fontSize: 12 }}>
            Same list as Final Ranking below -- shown once, sorted by final score, since a merged candidate IS
            its own ranking entry (core.retrieval.merge_and_score() already returns them pre-sorted).
          </p>

          <h3>Final Ranking (core.dynamics.activation_score / rank_and_bound, unmodified)</h3>
          {result.merged_candidates.map((m) => (
            <MergedRow key={m.ref} m={m} />
          ))}

          <h3>Activation Trace ID</h3>
          <div className="card id">{result.trace_id || "(not recorded)"}</div>
        </>
      )}
    </div>
  );
}
