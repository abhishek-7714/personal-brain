"use client";

import { useEffect, useState } from "react";
import { ActivationGraph, nodeActivationAtTime, TimelineEvent } from "@/lib/activation";
import { PlaybackClock } from "./clock";

interface Props {
  graph: ActivationGraph;
  timeline: TimelineEvent[];
  settledAtMs: number;
  clockRef: React.MutableRefObject<PlaybackClock>;
}

/**
 * Spec section 20: node ids, source ids, activation scores, channel, graph
 * relationship, pulse source/target, current activation, decay value --
 * read directly off the same graph/timeline/clock the renderer uses (no
 * separate fetch). Refreshed on a 150ms interval rather than every R3F
 * frame -- this is a debugging readout, not part of the animation.
 */
export default function DebugOverlay({ graph, timeline, settledAtMs, clockRef }: Props) {
  const [, forceTick] = useState(0);

  useEffect(() => {
    const id = setInterval(() => forceTick((n) => n + 1), 150);
    return () => clearInterval(id);
  }, []);

  const tMs = clockRef.current.tMs;
  const activePulses = timeline.filter((t) => tMs >= t.startMs && tMs <= t.startMs + t.travelMs);

  return (
    <div className="debug-overlay">
      <div className="debug-overlay-section">
        <strong>t = {Math.round(tMs)}ms</strong> <span className="muted">(settled at {Math.round(settledAtMs)}ms)</span>
      </div>
      <div className="debug-overlay-section">
        <strong>Nodes ({graph.nodes.length})</strong>
        <div className="debug-overlay-scroll">
          {graph.nodes.map((n) => (
            <div key={n.id} style={{ fontSize: 11 }}>
              <span className="id">{n.id}</span> {n.kind} score={n.score.toFixed(2)} act=
              {nodeActivationAtTime(n.id, timeline, tMs).toFixed(2)}
            </div>
          ))}
        </div>
      </div>
      <div className="debug-overlay-section">
        <strong>Active pulses ({activePulses.length})</strong>
        <div className="debug-overlay-scroll">
          {activePulses.map((t, i) => (
            <div key={i} style={{ fontSize: 11 }}>
              <span className="id">{t.edge.source}</span> {"->"} <span className="id">{t.edge.target}</span> [
              {t.edge.channel}] strength={t.edge.strength.toFixed(2)} hops={t.edge.hops ?? "-"}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
