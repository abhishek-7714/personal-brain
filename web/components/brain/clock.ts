// web/components/brain/clock.ts
//
// A plain mutable ref for "how many ms into this activation playback are
// we" -- deliberately NOT React state. Every renderer component reads
// clockRef.current.tMs inside its own useFrame() callback; only ClockDriver
// writes to it. Keeping the clock out of React state is what lets the scene
// animate at 60fps without triggering a React re-render every frame.

export interface PlaybackClock {
  tMs: number;
  startedAtSec: number | null; // three.js clock.elapsedTime when this playback started, or null before first frame
}

export function createPlaybackClock(): PlaybackClock {
  return { tMs: 0, startedAtSec: null };
}
