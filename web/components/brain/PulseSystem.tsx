"use client";

import { useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import { LayoutPosition, TimelineEvent } from "@/lib/activation";
import { PlaybackClock } from "./clock";
import { colorForKind } from "./theme";

interface Props {
  layout: Map<string, LayoutPosition>;
  timeline: TimelineEvent[];
  clockRef: React.MutableRefObject<PlaybackClock>;
}

const tmpObject = new THREE.Object3D();
const tmpColor = new THREE.Color();
const HIDDEN_SCALE = 0.0001;

/**
 * One travelling marker per timeline event, visible only while its pulse is
 * mid-flight (source -> target). This is the literal "current flowing
 * through the wiring" from spec section 3/11: a bright point interpolating
 * along the edge over `travelMs`, sized/brightened by the edge's strength.
 */
export default function PulseSystem({ layout, timeline, clockRef }: Props) {
  const meshRef = useRef<THREE.InstancedMesh>(null);

  const segments = useMemo(
    () =>
      timeline.map((t) => {
        const from = layout.get(t.edge.source) ?? { x: 0, y: 0, z: 0 };
        const to = layout.get(t.edge.target) ?? { x: 0, y: 0, z: 0 };
        return { t, from: new THREE.Vector3(from.x, from.y, from.z), to: new THREE.Vector3(to.x, to.y, to.z) };
      }),
    [layout, timeline]
  );

  useFrame(() => {
    const mesh = meshRef.current;
    if (!mesh) return;
    const tMs = clockRef.current.tMs;

    segments.forEach((seg, i) => {
      const elapsed = tMs - seg.t.startMs;
      const inFlight = elapsed >= 0 && elapsed <= seg.t.travelMs;
      if (!inFlight) {
        tmpObject.position.set(0, 0, 0);
        tmpObject.scale.setScalar(HIDDEN_SCALE);
        tmpObject.updateMatrix();
        mesh.setMatrixAt(i, tmpObject.matrix);
        return;
      }
      const progress = elapsed / seg.t.travelMs;
      const pos = seg.from.clone().lerp(seg.to, progress);
      const size = 0.09 + seg.t.edge.strength * 0.12;

      tmpObject.position.copy(pos);
      tmpObject.scale.setScalar(size);
      tmpObject.updateMatrix();
      mesh.setMatrixAt(i, tmpObject.matrix);

      tmpColor.set(colorForKind(seg.t.edge.channel === "graph" ? "entity" : "memory"));
      mesh.setColorAt(i, tmpColor);
    });

    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  });

  if (segments.length === 0) return null;

  return (
    <instancedMesh ref={meshRef} args={[undefined, undefined, segments.length]} renderOrder={2}>
      <sphereGeometry args={[1, 8, 8]} />
      <meshBasicMaterial toneMapped={false} />
    </instancedMesh>
  );
}
