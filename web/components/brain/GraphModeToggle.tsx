"use client";

/**
 * Spec section 8's Graph/Brain mode toggle. Graph Mode (real Neo4j
 * topology) is the only implemented mode this milestone -- Brain Mode
 * (biologically-inspired projection layer) is deferred to a follow-up task,
 * so its button is present but disabled rather than missing, so the
 * intended UI shape is visible now.
 */
export default function GraphModeToggle() {
  return (
    <div className="row" style={{ gap: 4 }}>
      <button className="secondary" style={{ opacity: 1, borderColor: "var(--accent)" }} disabled>
        Graph Mode
      </button>
      <button className="secondary" disabled title="Anatomical mode — coming next">
        Brain Mode
      </button>
    </div>
  );
}
