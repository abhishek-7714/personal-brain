"use client";

import dynamic from "next/dynamic";
import { useState } from "react";
import { runRetrievalQuery, RetrievalResult } from "@/lib/api";

// R3F's <Canvas> touches WebGL/window at module scope -- must never be
// evaluated during Next.js's server-side static prerender, or the build
// worker crashes. ssr: false is the standard fix for any react-three-fiber
// component used inside the Next.js app router.
const BrainCanvas = dynamic(() => import("@/components/brain/BrainCanvas"), { ssr: false });

export default function BrainPage() {
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<RetrievalResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [debug, setDebug] = useState(false);

  async function handleRun() {
    setLoading(true);
    setError(null);
    try {
      setResult(await runRetrievalQuery(query, true));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "calc(100vh - 48px)" }}>
      <h2 style={{ marginBottom: 4 }}>Neural Brain Visualization</h2>
      <p className="muted" style={{ marginTop: 0, marginBottom: 12, fontSize: 12 }}>
        Graph Mode -- real retrieval activation (POST /api/retrieval/query) animated as current flowing through the
        actual Neo4j/Postgres/Qdrant topology. No fabricated nodes or edges; no LLM call in this path.
      </p>
      <div className="row" style={{ marginBottom: 12 }}>
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
      {error && (
        <div className="error-box" style={{ marginBottom: 12 }}>
          {error}
        </div>
      )}
      <div style={{ flex: 1, minHeight: 0 }}>
        <BrainCanvas result={result} debug={debug} onDebugToggle={setDebug} />
      </div>
    </div>
  );
}
