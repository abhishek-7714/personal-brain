"use client";

import { useFrame, ThreeEvent } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import { ActivationGraph, LayoutPosition, TimelineEvent } from "@/lib/activation";
import { nodeBrightness } from "./brightness";
import { PlaybackClock } from "./clock";
import { BASE_NODE_SIZE, colorForKind, QUERY_NODE_SIZE, SEED_NODE_SIZE } from "./theme";

function sizeForNode(kind: string, isSeed: boolean, score: number): number {
  const base = kind === "query" ? QUERY_NODE_SIZE : isSeed ? SEED_NODE_SIZE : BASE_NODE_SIZE;
  return base * (0.75 + 0.5 * Math.max(0, Math.min(1, score))); // score nudges size, never the sole driver
}

interface Props {
  graph: ActivationGraph;
  layout: Map<string, LayoutPosition>;
  timeline: TimelineEvent[];
  settledAtMs: number;
  clockRef: React.MutableRefObject<PlaybackClock>;
  onNodeClick: (nodeId: string) => void;
}

const tmpColor = new THREE.Color();
const tmpObject = new THREE.Object3D();

export default function NeuronField({ graph, layout, timeline, settledAtMs, clockRef, onNodeClick }: Props) {
  const coreRef = useRef<THREE.InstancedMesh>(null);
  const haloRef = useRef<THREE.InstancedMesh>(null);

  const nodes = graph.nodes;
  const positions = useMemo(
    () => nodes.map((n) => layout.get(n.id) ?? { x: 0, y: 0, z: 0 }),
    [nodes, layout]
  );

  useFrame(() => {
    const core = coreRef.current;
    const halo = haloRef.current;
    if (!core || !halo) return;
    const tMs = clockRef.current.tMs;

    nodes.forEach((node, i) => {
      const p = positions[i];
      const brightness = nodeBrightness(node, timeline, tMs, settledAtMs);
      const size = sizeForNode(node.kind, node.isSeed, node.score);

      tmpObject.position.set(p.x, p.y, p.z);
      tmpObject.scale.setScalar(size);
      tmpObject.updateMatrix();
      core.setMatrixAt(i, tmpObject.matrix);

      tmpObject.scale.setScalar(size * (2.2 + brightness * 1.4));
      tmpObject.updateMatrix();
      halo.setMatrixAt(i, tmpObject.matrix);

      const baseColor = colorForKind(node.kind);
      tmpColor.set(baseColor).multiplyScalar(0.35 + brightness * 0.9);
      core.setColorAt(i, tmpColor);
      tmpColor.set(baseColor);
      halo.setColorAt(i, tmpColor);
    });

    core.instanceMatrix.needsUpdate = true;
    halo.instanceMatrix.needsUpdate = true;
    if (core.instanceColor) core.instanceColor.needsUpdate = true;
    if (halo.instanceColor) halo.instanceColor.needsUpdate = true;
  });

  function handleClick(e: ThreeEvent<MouseEvent>) {
    e.stopPropagation();
    const id = e.instanceId;
    if (id === undefined) return;
    const node = nodes[id];
    if (node) onNodeClick(node.id);
  }

  if (nodes.length === 0) return null;

  return (
    <group>
      {/* soft additive halo, rendered behind the core so nodes read as glowing points rather than solid cartoon spheres */}
      <instancedMesh ref={haloRef} args={[undefined, undefined, nodes.length]} renderOrder={0}>
        <sphereGeometry args={[1, 12, 12]} />
        <meshBasicMaterial transparent opacity={0.14} depthWrite={false} blending={THREE.AdditiveBlending} />
      </instancedMesh>
      <instancedMesh
        ref={coreRef}
        args={[undefined, undefined, nodes.length]}
        onClick={handleClick}
        renderOrder={1}
      >
        <sphereGeometry args={[1, 16, 16]} />
        {/* basic (unlit) material -- brightness comes entirely from per-instance
            color intensity (see brightness.ts), independent of scene lighting,
            so glow reads consistently regardless of camera angle */}
        <meshBasicMaterial toneMapped={false} />
      </instancedMesh>
    </group>
  );
}
