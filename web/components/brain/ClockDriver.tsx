"use client";

import { useFrame } from "@react-three/fiber";
import { MutableRefObject } from "react";
import { PlaybackClock } from "./clock";

/**
 * Renders nothing -- just advances clockRef.current.tMs every frame,
 * relative to the moment this component mounted for the current playToken
 * (so replaying/re-running a query restarts the clock at 0 without
 * unmounting the whole <Canvas>).
 */
export default function ClockDriver({ clockRef }: { clockRef: MutableRefObject<PlaybackClock> }) {
  useFrame((state) => {
    const nowSec = state.clock.elapsedTime;
    if (clockRef.current.startedAtSec === null) {
      clockRef.current.startedAtSec = nowSec;
    }
    clockRef.current.tMs = (nowSec - clockRef.current.startedAtSec) * 1000;
  });
  return null;
}
