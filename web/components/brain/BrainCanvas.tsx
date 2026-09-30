"use client";

import { Canvas } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import { useEffect, useRef, useState } from "react";
import { RetrievalResult } from "@/lib/api";
import { useActivationPlayback } from "@/lib/useActivationPlayback";
import ClockDriver from "./ClockDriver";
import { createPlaybackClock } from "./clock";
import ConnectionLines from "./ConnectionLines";
import DebugOverlay from "./DebugOverlay";
import GraphModeToggle from "./GraphModeToggle";
import IdleField from "./IdleField";
import NeuronField from "./NeuronField";
import NodeInspector from "./NodeInspector";
import PulseSystem from "./PulseSystem";

interface Props {
  result: RetrievalResult | null;
  debug?: boolean;
  onDebugToggle?: (next: boolean) => void;
  heightPx?: number;
  /** Set (to a node id) from outside -- e.g. the Retrieval Playground's "Active Memories" list -- to open the inspector for that node without going through a canvas click. */
  focusNodeId?: string | null;
}

/**
 * Top-level Neural Brain Visualization (Graph Mode). Owns the playback
 * clock (see clock.ts -- deliberately a ref, not React state, so the scene
 * animates at 60fps without re-rendering React) and the selected-node
 * drawer state. Everything data-bearing comes from `result`, the exact JSON
 * POST /api/retrieval/query already returns; this component and its
 * children never call Postgres/Neo4j/Qdrant directly (spec section 18).
 */
export default function BrainCanvas({ result, debug = false, onDebugToggle, heightPx, focusNodeId }: Props) {
  const { graph, layout, timeline, settledAtMs, playToken } = useActivationPlayback(result);
  const clockRef = useRef(createPlaybackClock());
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  useEffect(() => {
    clockRef.current = createPlaybackClock();
    setSelectedNodeId(null);
  }, [playToken]);

  useEffect(() => {
    if (focusNodeId) setSelectedNodeId(focusNodeId);
  }, [focusNodeId]);

  const selectedNode = graph.nodes.find((n) => n.id === selectedNodeId) ?? null;
  const idle = graph.nodes.length === 0;

  return (
    <div className="brain-canvas-wrap" style={{ height: heightPx ?? "100%", position: "relative" }}>
      <div className="brain-canvas-chrome">
        <GraphModeToggle />
        {onDebugToggle && (
          <button className={debug ? "" : "secondary"} onClick={() => onDebugToggle(!debug)}>
            {debug ? "Debug: ON" : "Debug: OFF"}
          </button>
        )}
      </div>

      <Canvas camera={{ position: [0, 0, 22], fov: 50 }} dpr={[1, 1.5]}>
        <color attach="background" args={["#05060a"]} />
        <ambientLight intensity={0.6} />
        <ClockDriver clockRef={clockRef} />
        {idle && <IdleField />}
        {!idle && (
          <group key={playToken}>
            <ConnectionLines graph={graph} layout={layout} timeline={timeline} clockRef={clockRef} />
            <NeuronField
              graph={graph}
              layout={layout}
              timeline={timeline}
              settledAtMs={settledAtMs}
              clockRef={clockRef}
              onNodeClick={setSelectedNodeId}
            />
            <PulseSystem layout={layout} timeline={timeline} clockRef={clockRef} />
          </group>
        )}
        <OrbitControls enablePan enableZoom enableRotate minDistance={6} maxDistance={60} />
      </Canvas>

      {idle && <div className="brain-canvas-idle-hint muted">Run a query to see activation fire through the brain.</div>}

      {debug && <DebugOverlay graph={graph} timeline={timeline} settledAtMs={settledAtMs} clockRef={clockRef} />}
      <NodeInspector node={selectedNode} result={result} onClose={() => setSelectedNodeId(null)} />
    </div>
  );
}
