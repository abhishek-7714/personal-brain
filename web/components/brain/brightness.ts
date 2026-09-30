// web/components/brain/brightness.ts
//
// Maps a node + current playback time to a single [0,1] visual brightness,
// combining: the transient pulse glow from web/lib/activation.ts's
// nodeActivationAtTime()/strengthToIntensity(), a permanent low idle floor
// (spec section 14 -- the brain is never fully dark), and a settled floor
// for included candidates once every pulse has finished travelling (spec
// stage 6 -- "the final activated neighborhood remains subtly illuminated",
// not faded back to nothing).

import { ActivationNode, decayCurve, nodeActivationAtTime, strengthToIntensity, TimelineEvent } from "@/lib/activation";
import { IDLE_FLOOR_INTENSITY } from "./theme";

const SETTLED_FADE_HALF_LIFE_MS = 9000; // how slowly the settled glow itself fades toward its long-term floor

export function nodeBrightness(
  node: ActivationNode,
  timeline: TimelineEvent[],
  tMs: number,
  settledAtMsValue: number
): number {
  if (node.kind === "query") {
    return 0.75 + Math.sin(tMs / 1400) * 0.08; // gentle breathing while the query root is present
  }

  const rawPulse = Math.min(2, nodeActivationAtTime(node.id, timeline, tMs));
  const pulseIntensity = rawPulse > 0 ? strengthToIntensity(Math.min(1, rawPulse)) * Math.min(1, rawPulse + 0.3) : 0;

  if (tMs <= settledAtMsValue || rawPulse > 0.05) {
    return Math.max(IDLE_FLOOR_INTENSITY, pulseIntensity);
  }

  // past settling and no live pulse left -- decay toward a permanent settled floor rather than to zero
  const settledPeak = node.included ? strengthToIntensity(node.score) * 0.55 : IDLE_FLOOR_INTENSITY;
  const elapsedSinceSettle = tMs - settledAtMsValue;
  const decayed = decayCurve(elapsedSinceSettle, SETTLED_FADE_HALF_LIFE_MS, settledPeak);
  return Math.max(IDLE_FLOOR_INTENSITY, decayed);
}
