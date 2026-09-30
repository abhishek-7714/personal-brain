"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  listFixtures,
  ingestFixture,
  evaluateFixture,
  CalibrationFixture,
  IngestResult,
  FixtureEvaluationJSON,
} from "@/lib/api";

function EvaluationPanel({ ev }: { ev: FixtureEvaluationJSON }) {
  if (ev.status !== "scored") {
    return (
      <div className="error-box" style={{ marginTop: 10 }}>
        <strong>{ev.status === "validation_error" ? "ONTOLOGY VALIDATION FAILED" : "EXTRACTION ERROR"}</strong>
        <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, margin: "6px 0 0" }}>{ev.error}</pre>
        <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
          This is a result, not a crash — the extractor produced something the ontology rejects.
        </div>
      </div>
    );
  }

  const failures = ev.results.filter((r) => r.status === "FAIL");
  const advisories = ev.results.filter((r) => r.status === "advisory");
  const passes = ev.results.filter((r) => r.status === "pass");

  return (
    <div style={{ marginTop: 10, borderTop: "1px solid var(--border)", paddingTop: 10 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <strong>
          {ev.mode === "live" ? "LIVE extractor" : "MOCK representation layer"} —{" "}
          {ev.passed_count}/{ev.total_count} assertions passed
        </strong>
        <span className={`badge ${ev.essential_failure_count === 0 ? "ok" : "err"}`}>
          {ev.essential_failure_count === 0 ? "NO ESSENTIAL FAILURES" : `${ev.essential_failure_count} ESSENTIAL FAILURE(S)`}
        </span>
      </div>
      <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
        {ev.mode === "live"
          ? "Scores what the real LLM produced. Persists nothing."
          : "Scores ingest → Postgres → reader round-trip of a hand-written extraction. Does not evaluate the extractor."}
      </div>

      {failures.length > 0 && (
        <>
          <h3 style={{ color: "var(--err)", marginBottom: 4 }}>Essential failures ({failures.length})</h3>
          {failures.map((r) => (
            <div key={r.code} className="error-box" style={{ marginBottom: 6 }}>
              <div>
                <span className="mono">{r.code}</span> <span className="muted">[{r.dimension}]</span>
              </div>
              <div style={{ marginTop: 2 }}>expected: {r.description}</div>
              <div className="mono" style={{ fontSize: 12, marginTop: 2 }}>actual: {r.detail}</div>
            </div>
          ))}
        </>
      )}

      {advisories.length > 0 && (
        <>
          <h3 style={{ color: "var(--warn)", marginBottom: 4 }}>Advisories ({advisories.length})</h3>
          {advisories.map((r) => (
            <div key={r.code} style={{ fontSize: 12, marginBottom: 6 }}>
              <span className="badge warn">{r.code}</span>{" "}
              <span className="muted">[{r.dimension}]</span> {r.description}
              <div className="mono muted" style={{ fontSize: 11 }}>actual: {r.detail}</div>
            </div>
          ))}
        </>
      )}

      {ev.trace_grounding.length > 0 && (
        <>
          <h3 style={{ marginBottom: 4 }}>Cognitive trace grounding</h3>
          <table>
            <thead>
              <tr><th>Grounding</th><th>Operation</th><th>Target</th></tr>
            </thead>
            <tbody>
              {ev.trace_grounding.map((t, i) => (
                <tr key={i}>
                  <td className={t.grounding === 0 ? "" : "muted"}>
                    {t.grounding === 0 ? (
                      <span className="badge err">0.00 ungrounded</span>
                    ) : (
                      t.grounding.toFixed(2)
                    )}
                  </td>
                  <td className="mono">{t.operation}</td>
                  <td>{t.target}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted" style={{ fontSize: 11 }}>
            Fraction of the trace target&apos;s content words that appear in the source text. 0.00 means the step was
            described using vocabulary found nowhere in the source — the signature of invented cognition.
          </div>
        </>
      )}

      <details style={{ marginTop: 8 }}>
        <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>
          {passes.length} passing assertions
        </summary>
        {passes.map((r) => (
          <div key={r.code} style={{ fontSize: 12, marginTop: 4 }}>
            <span className="badge ok">{r.code}</span> <span className="muted">[{r.dimension}]</span>{" "}
            {r.description}
            <div className="mono muted" style={{ fontSize: 11 }}>{r.detail}</div>
          </div>
        ))}
      </details>

      {ev.experience_id && (
        <div style={{ marginTop: 8 }}>
          <Link href={`/experiences/${ev.experience_id}`}>Open complete evidence chain →</Link>
        </div>
      )}
    </div>
  );
}

export default function CalibrationPage() {
  const [fixtures, setFixtures] = useState<CalibrationFixture[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, IngestResult>>({});
  const [evals, setEvals] = useState<Record<string, FixtureEvaluationJSON>>({});

  useEffect(() => {
    listFixtures().then(setFixtures).catch((e) => setError(e.message));
  }, []);

  async function run(key: string, fn: () => Promise<void>) {
    setBusyKey(key);
    setError(null);
    try {
      await fn();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <div>
      <h2>Calibration Mode</h2>
      <p className="muted">
        Fixtures defined once in core/calibration_fixtures.py — the same file tests/test_retrieval.py and
        tests/test_episodes.py import. Expected semantic assertions live in core/calibration_assertions.py and are
        written from classification.md rules plus each fixture&apos;s source text, never from its mock JSON.
      </p>
      <div className="card" style={{ borderColor: "var(--warn)" }}>
        <strong>Mock vs. live — these answer different questions.</strong>
        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          <strong>Evaluate (mock)</strong> ingests the hand-written fixture JSON and scores what came back out of
          Postgres. It validates the representation layer, and cannot tell you anything about extractor quality.
          <br />
          <strong>Evaluate (live)</strong> calls the real LLM on the fixture text and scores its output. This is the
          only mode that tells you where the extractor is wrong. It costs an API call and persists nothing.
        </div>
      </div>
      {error && <div className="error-box">{error}</div>}

      {fixtures.map((f) => {
        const res = results[f.key];
        const ev = evals[f.key];
        return (
          <div className="card" key={f.key}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <strong>{f.label}</strong>
              <span className="row">
                <button
                  onClick={() => run(f.key, async () => {
                    const ingested = await ingestFixture(f.key);
                    setResults((p) => ({ ...p, [f.key]: ingested }));
                  })}
                  disabled={busyKey !== null}
                >
                  {busyKey === f.key ? "…" : "Ingest"}
                </button>
                <button
                  className="secondary"
                  onClick={() => run(f.key, async () => {
                    const scored = await evaluateFixture(f.key, "mock");
                    setEvals((p) => ({ ...p, [f.key]: scored }));
                  })}
                  disabled={busyKey !== null}
                >
                  Evaluate (mock)
                </button>
                <button
                  className="secondary"
                  onClick={() => run(f.key, async () => {
                    const scored = await evaluateFixture(f.key, "live");
                    setEvals((p) => ({ ...p, [f.key]: scored }));
                  })}
                  disabled={busyKey !== null}
                >
                  Evaluate (live $)
                </button>
              </span>
            </div>
            <p className="muted" style={{ fontSize: 13 }}>{f.description}</p>
            <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, margin: 0 }} className="muted">
              {f.text}
            </pre>

            {res && (
              <div style={{ marginTop: 10, fontSize: 12 }} className="muted">
                Ingested → <span className="id">{res.experience_id}</span> · {res.observation_count} obs ·{" "}
                {res.memory_count} mem · {res.belief_count} belief · {res.entity_count} entity
                {" · "}
                <Link href={`/experiences/${res.experience_id}`}>inspect →</Link>
              </div>
            )}

            {ev && <EvaluationPanel ev={ev} />}
          </div>
        );
      })}
    </div>
  );
}
