"use client";

import { useEffect, useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { getGraphLabels, listGraphNodes, getGraphNode, GraphNodeSummary, GraphNodeDetail } from "@/lib/api";

const LABELS = [
  "Owner", "Entity", "Experience", "Observation", "Episode", "CognitiveTrace",
  "Memory", "Belief", "MentalModel", "CognitivePattern", "Decision", "StateItem",
];

function ExplorerInner() {
  const searchParams = useSearchParams();
  const [counts, setCounts] = useState<Record<string, number> | null>(null);
  const [selectedLabel, setSelectedLabel] = useState<string>("Entity");
  const [nodes, setNodes] = useState<GraphNodeSummary[]>([]);
  const [detail, setDetail] = useState<GraphNodeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getGraphLabels().then(setCounts).catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    listGraphNodes(selectedLabel, 100).then(setNodes).catch((e) => setError(e.message));
  }, [selectedLabel]);

  async function openNode(id: string) {
    try {
      setDetail(await getGraphNode(id));
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    const nodeParam = searchParams.get("node");
    if (nodeParam) openNode(nodeParam);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  return (
    <div>
      <h2>Brain Explorer</h2>
      <p className="muted">
        Live Neo4j browser -- every node/relationship shown is exactly what core/projector.py wrote, nothing
        invented here. Node ids are the same Postgres UUIDs everywhere else in this console.
      </p>
      {error && <div className="error-box">{error}</div>}

      <div className="row" style={{ alignItems: "flex-start" }}>
        <div className="col" style={{ maxWidth: 180 }}>
          <h3>Labels</h3>
          <div className="card" style={{ padding: 8 }}>
            {LABELS.map((label) => (
              <div
                key={label}
                onClick={() => setSelectedLabel(label)}
                style={{
                  padding: "6px 8px",
                  cursor: "pointer",
                  borderRadius: 4,
                  background: selectedLabel === label ? "var(--panel-2)" : "transparent",
                  display: "flex",
                  justifyContent: "space-between",
                }}
              >
                <span>{label}</span>
                <span className="muted">{counts ? counts[label] : "…"}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="col" style={{ maxWidth: 320 }}>
          <h3>{selectedLabel} nodes ({nodes.length})</h3>
          <div className="card" style={{ padding: 8, maxHeight: 500, overflowY: "auto" }}>
            {nodes.length === 0 && <p className="muted">None yet.</p>}
            {nodes.map((n) => (
              <div
                key={n.id}
                onClick={() => openNode(n.id)}
                style={{
                  padding: "6px 8px",
                  cursor: "pointer",
                  borderRadius: 4,
                  background: detail?.id === n.id ? "var(--panel-2)" : "transparent",
                }}
              >
                <div>{n.summary}</div>
                <div className="id">{n.id}</div>
              </div>
            ))}
          </div>
        </div>

        <div className="col">
          <h3>Node detail</h3>
          {!detail && <p className="muted">Select a node.</p>}
          {detail && (
            <div className="card">
              <div>
                <strong>{detail.labels.join(", ")}</strong> <span className="id">{detail.id}</span>
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                source Postgres id: <span className="id">{detail.postgres_id}</span>
              </div>

              <h3>Properties</h3>
              <table>
                <tbody>
                  {Object.entries(detail.properties).map(([k, v]) => (
                    <tr key={k}>
                      <td>{k}</td>
                      <td className="mono">{String(v)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <h3>Outgoing ({detail.outgoing.length})</h3>
              {detail.outgoing.map((e, i) => (
                <div key={i} style={{ fontSize: 13, marginBottom: 6 }}>
                  --{e.rel_type}--&gt;{" "}
                  <span
                    className="id"
                    style={{ cursor: "pointer" }}
                    onClick={() => e.target_id && openNode(e.target_id)}
                  >
                    [{e.target_label}] {e.target_summary}
                  </span>
                  {Object.keys(e.properties).length > 0 && (
                    <div className="muted" style={{ fontSize: 11 }}>
                      {JSON.stringify(e.properties)}
                    </div>
                  )}
                </div>
              ))}

              <h3>Incoming ({detail.incoming.length})</h3>
              {detail.incoming.map((e, i) => (
                <div key={i} style={{ fontSize: 13, marginBottom: 6 }}>
                  <span
                    className="id"
                    style={{ cursor: "pointer" }}
                    onClick={() => e.source_id && openNode(e.source_id)}
                  >
                    [{e.source_label}] {e.source_summary}
                  </span>{" "}
                  --{e.rel_type}--&gt; (this node)
                  {Object.keys(e.properties).length > 0 && (
                    <div className="muted" style={{ fontSize: 11 }}>
                      {JSON.stringify(e.properties)}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function ExplorerPage() {
  return (
    <Suspense fallback={<p className="muted">Loading…</p>}>
      <ExplorerInner />
    </Suspense>
  );
}
