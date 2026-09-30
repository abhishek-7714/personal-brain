"use client";

import { Line } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import { ActivationGraph, LayoutPosition, nodeActivationAtTime, strengthToIntensity, TimelineEvent } from "@/lib/activation";
import { PlaybackClock } from "./clock";

interface Props {
  graph: ActivationGraph;
  layout: Map<string, LayoutPosition>;
  timeline: TimelineEvent[];
  clockRef: React.MutableRefObject<PlaybackClock>;
}

const INACTIVE_OPACITY = 0.06; // thin, low-opacity when idle (spec section 19)
const ACTIVE_OPACITY = 0.75;

/**
 * One thin line per real edge -- thin and low-opacity when idle, brightening
 * only for edges a pulse has actually travelled along recently (spec
 * sections 3, 19). Edge count at this milestone's scale (dozens, not
 * thousands) makes one <Line> per edge fine; a merged BufferGeometry is the
 * obvious next step if the topology grows much larger.
 */
export default function ConnectionLines({ graph, layout, timeline, clockRef }: Props) {
  const materialRefs = useRef<(THREE.Material | null)[]>([]);

  const segments = useMemo(
    () =>
      graph.edges.map((e) => {
        const from = layout.get(e.source) ?? { x: 0, y: 0, z: 0 };
        const to = layout.get(e.target) ?? { x: 0, y: 0, z: 0 };
        return { edge: e, points: [new THREE.Vector3(from.x, from.y, from.z), new THREE.Vector3(to.x, to.y, to.z)] };
      }),
    [graph, layout]
  );

  useFrame(() => {
    const tMs = clockRef.current.tMs;
    segments.forEach((seg, i) => {
      const mat = materialRefs.current[i] as THREE.LineBasicMaterial | undefined;
      if (!mat) return;
      // "recently active" = this edge's target currently carries visible pulse energy
      const targetActivation = Math.min(1, nodeActivationAtTime(seg.edge.target, timeline, tMs));
      const active = strengthToIntensity(targetActivation) * (targetActivation > 0.02 ? 1 : 0);
      mat.opacity = INACTIVE_OPACITY + (ACTIVE_OPACITY - INACTIVE_OPACITY) * active;
    });
  });

  return (
    <group>
      {segments.map((seg, i) => (
        <Line
          key={seg.edge.id}
          points={seg.points}
          color={seg.edge.channel === "graph" ? "#a48af2" : "#5a8dee"}
          transparent
          opacity={INACTIVE_OPACITY}
          lineWidth={1}
          ref={(el: any) => {
            if (el) materialRefs.current[i] = el.material;
          }}
        />
      ))}
    </group>
  );
}
