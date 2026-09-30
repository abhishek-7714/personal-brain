"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { listExperiences } from "@/lib/api";

export default function ExperiencesListPage() {
  const [experiences, setExperiences] = useState<Awaited<ReturnType<typeof listExperiences>> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listExperiences(50).then(setExperiences).catch((e) => setError(e.message));
  }, []);

  return (
    <div>
      <h2>Experiences</h2>
      <p className="muted">
        Recently ingested experiences, most recent first. Use <a href="/ingest">Ingest</a> or{" "}
        <a href="/calibration">Calibration</a> to create one.
      </p>
      {error && <div className="error-box">{error}</div>}
      {experiences && experiences.length === 0 && <p className="muted">No experiences yet.</p>}
      {experiences && experiences.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Kind</th>
              <th>Source</th>
              <th>Occurred at</th>
              <th>Ingested at</th>
            </tr>
          </thead>
          <tbody>
            {experiences.map((e) => (
              <tr key={e.id}>
                <td>
                  <Link href={`/experiences/${e.id}`} className="id">
                    {e.id}
                  </Link>
                </td>
                <td>{e.kind}</td>
                <td>
                  {e.is_test_fixture ? (
                    <span className="badge warn">TEST FIXTURE</span>
                  ) : (
                    <span className="badge neutral">real</span>
                  )}
                </td>
                <td className="muted">{e.occurred_at}</td>
                <td className="muted">{e.ingested_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
