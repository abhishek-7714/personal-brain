"use client";

import { useEffect, useState } from "react";
import { ActivationNode, resolveCanonicalSource } from "@/lib/activation";
import { getGraphNode, GraphNodeDetail, RetrievalResult } from "@/lib/api";

interface Props {
  node: ActivationNode | null;
  result: RetrievalResult | null;
  onClose: () => void;
}

/**
 * Spec section 10: visual neuron -> graph object -> canonical object ->
 * evidence -> original experience. `node` already carries the canonical
 * source_type/source_id (or null for the transient query root); this
 * component fetches the live Neo4j node (same endpoint the Brain Explorer
 * page uses) to show the relationship hop, and reads activation/breakdown
 * straight off the already-fetched RetrievalResult -- no duplicate scoring.
 */
export default function NodeInspector({ node, result, onClose }: Props) {
  const [detail, setDetail] = useState<GraphNodeDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setError(null);
    const canonical = node ? resolveCanonicalSource(node) : null;
    if (!canonical) return;
    setLoading(true);
    getGraphNode(canonical.sourceId)
      .then(setDetail)
      .catch((e: any) => setError(e.message))
      .finally(() => setLoading(false));
  }, [node]);

  if (!node) return null;

  const merged = result?.merged_candidates.find((m) => m.ref === node.id);

  return (
    <div className="node-inspector">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <strong>{node.kind === "query" ? "QUERY" : node.kind.replace("_", " ").toUpperCase()}</strong>
        <button className="secondary" onClick={onClose}>
          Close
        </button>
      </div>
      <div className="id" style={{ marginTop: 4 }}>
        {node.id}
      </div>

      <h3>Content</h3>
      <div className="muted">{node.label}</div>

      {node.kind === "query" ? (
        <p className="muted" style={{ fontSize: 12, marginTop: 12 }}>
          Transient visualization root -- not a Personal Brain entity, memory, or graph node. Every other neuron in
          this view maps back to a canonical Postgres/Neo4j object; this one exists only to anchor the animation.
        </p>
      ) : (
        <>
          <h3>Activation</h3>
          <div className="score">
            {node.score.toFixed(3)} {merged && <span className="muted">({merged.included ? "included" : "excluded"})</span>}
          </div>
        </>
      )}

      {merged && (
        <>
          <h3>Why activated</h3>
          {merged.discoveries.map((d, i) => (
            <div key={i} style={{ fontSize: 12, marginBottom: 4 }}>
              <span className="muted">[{d.retrieval_channel}]</span> {d.reason}
            </div>
          ))}
          <h3>Breakdown</h3>
          <div className="muted" style={{ fontSize: 12, fontFamily: "var(--mono)" }}>
            semantic={merged.breakdown.semantic.toFixed(2)} association={merged.breakdown.association.toFixed(2)}{" "}
            situation={merged.breakdown.situation.toFixed(2)} goal_alignment={merged.breakdown.goal_alignment.toFixed(2)}{" "}
            salience={merged.breakdown.salience.toFixed(2)} evidence_quality={merged.breakdown.evidence_quality.toFixed(2)}{" "}
            staleness={merged.breakdown.staleness.toFixed(2)}
          </div>
        </>
      )}

      {loading && <p className="muted">Loading canonical graph node…</p>}
      {error && <div className="error-box">{error}</div>}
      {detail && (
        <>
          <h3>Source</h3>
          <div className="muted" style={{ fontSize: 12 }}>
            Postgres id <span className="id">{detail.postgres_id}</span> — Neo4j labels: {detail.labels.join(", ")}
          </div>
          <h3>Relationships ({detail.outgoing.length + detail.incoming.length})</h3>
          {detail.outgoing.map((e, i) => (
            <div key={`out-${i}`} style={{ fontSize: 12, marginBottom: 3 }}>
              --{e.rel_type}--&gt; [{e.target_label}] {e.target_summary}
            </div>
          ))}
          {detail.incoming.map((e, i) => (
            <div key={`in-${i}`} style={{ fontSize: 12, marginBottom: 3 }}>
              [{e.source_label}] {e.source_summary} --{e.rel_type}--&gt; (this node)
            </div>
          ))}
        </>
      )}
    </div>
  );
}
