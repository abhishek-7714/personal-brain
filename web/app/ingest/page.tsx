"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ingestExperience, IngestResult } from "@/lib/api";

const SOURCE_TYPES = ["note", "conversation", "voice", "event", "document", "import", "external"];

export default function IngestPage() {
  const router = useRouter();
  const [text, setText] = useState("");
  const [sourceType, setSourceType] = useState("note");
  const [occurredAt, setOccurredAt] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<IngestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleIngest() {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await ingestExperience({
        text,
        source_type: sourceType,
        occurred_at: occurredAt ? new Date(occurredAt).toISOString() : null,
      });
      setResult(res);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <h2>Ingest Experience</h2>
      <p className="muted">
        Calls the real extraction pipeline (core.ingest.ingest -&gt; core.projector.project_experience -&gt;
        core.indexer.index_experience) with a real Anthropic call. Requires ANTHROPIC_API_KEY on the API server.
        For a no-API-key path, use the <a href="/calibration">Calibration</a> page instead.
      </p>

      <div className="card">
        <label>Raw text</label>
        <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="What happened?" />

        <div className="row" style={{ marginTop: 12 }}>
          <div className="col">
            <label>Source type (optional)</label>
            <select value={sourceType} onChange={(e) => setSourceType(e.target.value)}>
              {SOURCE_TYPES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <div className="col">
            <label>Occurred at (optional)</label>
            <input type="datetime-local" value={occurredAt} onChange={(e) => setOccurredAt(e.target.value)} />
          </div>
        </div>

        <div style={{ marginTop: 12 }}>
          <button onClick={handleIngest} disabled={loading || !text.trim()}>
            {loading ? "Ingesting…" : "Ingest Experience"}
          </button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}

      {result && (
        <div className="card">
          <h3>Result</h3>
          <table>
            <tbody>
              <tr>
                <td>experience_id</td>
                <td className="id">{result.experience_id}</td>
              </tr>
              <tr>
                <td>created</td>
                <td>{String(result.created)}</td>
              </tr>
              <tr>
                <td>observations / entities</td>
                <td>
                  {result.observation_count} / {result.entity_count}
                </td>
              </tr>
              <tr>
                <td>memories / beliefs</td>
                <td>
                  {result.memory_count} / {result.belief_count}
                </td>
              </tr>
              <tr>
                <td>memory_entity_links</td>
                <td>{result.memory_entity_link_count}</td>
              </tr>
              {result.ambiguous && (
                <tr>
                  <td>ambiguous</td>
                  <td>{result.ambiguity_note}</td>
                </tr>
              )}
              {result.projection_error && (
                <tr>
                  <td>Neo4j projection</td>
                  <td className="error-box">{result.projection_error}</td>
                </tr>
              )}
              {result.indexing_error && (
                <tr>
                  <td>Qdrant indexing</td>
                  <td className="error-box">{result.indexing_error}</td>
                </tr>
              )}
            </tbody>
          </table>
          <div style={{ marginTop: 12 }}>
            <button onClick={() => router.push(`/experiences/${result.experience_id}`)}>
              Open Inspection View →
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
