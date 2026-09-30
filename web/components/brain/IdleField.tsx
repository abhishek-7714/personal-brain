"use client";

import { useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";

const COUNT = 220;

/**
 * Pure background ambience for the pre-query idle state (spec section 14) --
 * a slow depth-drifting dust field, deliberately NOT a set of labeled
 * neurons: rendering a fake topology before any real retrieval has run
 * would mean inventing nodes/edges that don't correspond to canonical data,
 * which spec section 2 explicitly forbids. The real brain (NeuronField /
 * ConnectionLines) only appears once a query result exists.
 */
export default function IdleField() {
  const meshRef = useRef<THREE.InstancedMesh>(null);
  const positions = useMemo(
    () =>
      Array.from({ length: COUNT }, () => ({
        x: (Math.random() - 0.5) * 40,
        y: (Math.random() - 0.5) * 40,
        z: (Math.random() - 0.5) * 20 - 5,
        phase: Math.random() * Math.PI * 2,
      })),
    []
  );

  const tmpObject = useMemo(() => new THREE.Object3D(), []);

  useFrame((state) => {
    const mesh = meshRef.current;
    if (!mesh) return;
    const t = state.clock.elapsedTime;
    positions.forEach((p, i) => {
      const breathe = 0.5 + 0.5 * Math.sin(t * 0.4 + p.phase);
      tmpObject.position.set(p.x, p.y, p.z);
      tmpObject.scale.setScalar(0.02 + breathe * 0.02);
      tmpObject.updateMatrix();
      mesh.setMatrixAt(i, tmpObject.matrix);
    });
    mesh.instanceMatrix.needsUpdate = true;
  });

  return (
    <instancedMesh ref={meshRef} args={[undefined, undefined, COUNT]}>
      <sphereGeometry args={[1, 6, 6]} />
      <meshBasicMaterial color="#3a4155" transparent opacity={0.5} toneMapped={false} />
    </instancedMesh>
  );
}
