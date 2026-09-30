"use client";

import { useEffect, useState } from "react";
import { getStatus, seedOwner, runTests, SystemStatus, TestSuiteResult } from "@/lib/api";
import Badge from "@/components/Badge";

const SERVICE_ORDER: Array<keyof Omit<SystemStatus, "owner">> = [
  "postgres",
  "neo4j",
  "qdrant",
  "minio",
  "temporal",
];

export default function StatusPage() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [seeding, setSeeding] = useState(false);
  const [testResults, setTestResults] = useState<TestSuiteResult[] | null>(null);
  const [testsRunning, setTestsRunning] = useState<"fast" | "full" | null>(null);

  async function refresh() {
    try {
      setStatus(await getStatus());
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function handleSeed() {
    setSeeding(true);
    try {
      await seedOwner();
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSeeding(false);
    }
  }

  async function handleRunTests(suite: "fast" | "full") {
    setTestsRunning(suite);
    setTestResults(null);
    try {
      const res = await runTests(suite);
      setTestResults(res.results);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setTestsRunning(null);
    }
  }

  return (
    <div>
      <h2>System Status</h2>
      {error && <div className="error-box">{error}</div>}

      <div className="section-grid">
        {status &&
          SERVICE_ORDER.map((key) => (
            <div className="card" key={key}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <strong style={{ textTransform: "capitalize" }}>{key}</strong>
                <Badge ok={status[key].ok} label={status[key].ok ? "OK" : "DOWN"} />
              </div>
              <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>
                {status[key].detail}
              </div>
            </div>
          ))}
      </div>

      <h3>Owner</h3>
      <div className="card">
        {status?.owner.seeded ? (
          <div>
            <Badge ok={true} label="SEEDED" /> <span className="id">{status.owner.id}</span> —{" "}
            {status.owner.display_name}
          </div>
        ) : (
          <div>
            <Badge ok={false} label="NOT SEEDED" />{" "}
            <span className="muted">
              No owner row exists yet. The owner table is a strict singleton -- exactly one row, enforced by a
              DB constraint.
            </span>
            <div style={{ marginTop: 8 }}>
              <button onClick={handleSeed} disabled={seeding}>
                {seeding ? "Seeding…" : "Seed Dev Console Owner"}
              </button>
            </div>
          </div>
        )}
      </div>

      <h3>Test Status</h3>
      <div className="card">
        <div className="row">
          <button onClick={() => handleRunTests("fast")} disabled={testsRunning !== null}>
            {testsRunning === "fast" ? "Running…" : "Run Fast Suite (no stack writes)"}
          </button>
          <button className="secondary" onClick={() => handleRunTests("full")} disabled={testsRunning !== null}>
            {testsRunning === "full"
              ? "Running… (writes + cleans up test data, can take a minute)"
              : "Run Full Suite (writes test data to Qdrant/Neo4j, cleans up after itself)"}
          </button>
        </div>
        {testResults && (
          <table style={{ marginTop: 12 }}>
            <thead>
              <tr>
                <th>Suite file</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {testResults.map((r) => (
                <tr key={r.file}>
                  <td className="mono">{r.file}</td>
                  <td>
                    {r.skipped ? (
                      <Badge ok="warn" label="SKIPPED (stack not reachable)" />
                    ) : r.error ? (
                      <Badge ok={false} label={r.error} />
                    ) : r.failed === 0 ? (
                      <Badge ok={true} label={`${r.passed} passed`} />
                    ) : (
                      <Badge ok={false} label={`${r.passed} passed, ${r.failed} failed`} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
