// web/lib/useActivationPlayback.ts
//
// Bridges a RetrievalResult (plain JSON from POST /api/retrieval/query) into
// the graph/layout/timeline the R3F renderer consumes. All the actual
// transform logic lives in web/lib/activation.ts (pure, unit-tested); this
// hook only memoizes it against React state and hands out a `playToken`
// that increments on every new result, so renderer components know to reset
// their internal animation clock via `key={playToken}`.

import { useMemo, useRef } from "react";
import type { RetrievalResult } from "./api";
import {
  ActivationGraph,
  buildActivationGraph,
  computeLayout,
  computeTimeline,
  LayoutPosition,
  settledAtMs,
  TimelineEvent,
} from "./activation";

export interface ActivationPlayback {
  graph: ActivationGraph;
  layout: Map<string, LayoutPosition>;
  timeline: TimelineEvent[];
  settledAtMs: number;
  playToken: number;
}

const EMPTY_GRAPH: ActivationGraph = { nodes: [], edges: [] };

export function useActivationPlayback(result: RetrievalResult | null): ActivationPlayback {
  const tokenRef = useRef(0);
  const lastResultRef = useRef<RetrievalResult | null>(null);

  return useMemo(() => {
    if (result !== lastResultRef.current) {
      tokenRef.current += 1;
      lastResultRef.current = result;
    }
    if (!result) {
      return { graph: EMPTY_GRAPH, layout: new Map(), timeline: [], settledAtMs: 0, playToken: tokenRef.current };
    }
    const graph = buildActivationGraph(result);
    const layout = computeLayout(graph);
    const timeline = computeTimeline(graph.edges);
    return { graph, layout, timeline, settledAtMs: settledAtMs(timeline), playToken: tokenRef.current };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result]);
}
