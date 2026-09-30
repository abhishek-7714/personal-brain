// web/lib/activation.ts
//
// Pure data-transform layer between a RetrievalResult (the JSON body
// POST /api/retrieval/query already returns) and the Neural Brain
// Visualization's renderer. Nothing here calls fetch, touches the DOM, or
// does any ranking/scoring -- every score, edge, and reason already came
// from core.dynamics.py / core.retrieval.py via the API. This file only
// reshapes that JSON into a stable node/edge graph, a deterministic layout,
// and a playback timeline, and is exercised directly by
// web/lib/activation.test.ts without a browser or a running backend.

import type {
  ActivationEventJSON,
  MergedCandidateJSON,
  RetrievalResult,
  SeedEntityJSON,
} from "./api";

// ---------------------------------------------------------------------------
// Graph model
// ---------------------------------------------------------------------------

export type NodeKind = "query" | "entity" | string; // otherwise a source_type (memory, belief, ...)

export interface ActivationNode {
  id: string; // "query" | "entity:<uuid>" | "<source_type>:<source_id>" -- same id used as activation_events source/target
  kind: NodeKind;
  label: string;
  sourceType: string | null; // canonical source_type, e.g. "memory" -- null only for the transient "query" node
  sourceId: string | null; // canonical source_id -- null only for the transient "query" node
  score: number; // final_score for candidates, centrality for entities, 1 for query
  included: boolean;
  isSeed: boolean;
}

export interface ActivationEdge {
  id: string; // stable key: `${source}|${target}|${channel}|${index}`
  source: string;
  target: string;
  channel: string;
  strength: number; // clamped to [0, 1]
  hops: number | null;
  reason: string;
}

export interface ActivationGraph {
  nodes: ActivationNode[];
  edges: ActivationEdge[];
}

const QUERY_NODE_ID = "query";

function clamp01(n: number): number {
  if (Number.isNaN(n)) return 0;
  return Math.max(0, Math.min(1, n));
}

function candidateLabel(mc: MergedCandidateJSON): string {
  const withPreview = mc.discoveries.find((d) => d.content_preview);
  return withPreview?.content_preview || mc.ref;
}

/**
 * Builds the node/edge graph the renderer, the layout, and the timeline all
 * consume. Node identity (`id`) is exactly the string activation_events
 * already uses as source/target, so edges never need re-resolution -- an
 * edge referencing a node this function didn't create is dropped rather
 * than rendered as a dangling pulse (see the invented-edge guard below).
 */
export function buildActivationGraph(result: RetrievalResult): ActivationGraph {
  const nodes = new Map<string, ActivationNode>();

  nodes.set(QUERY_NODE_ID, {
    id: QUERY_NODE_ID,
    kind: "query",
    label: result.query,
    sourceType: null,
    sourceId: null,
    score: 1,
    included: true,
    isSeed: false,
  });

  for (const e of result.all_seed_entities) {
    const id = `entity:${e.id}`;
    nodes.set(id, {
      id,
      kind: "entity",
      label: e.canonical_name,
      sourceType: "entity",
      sourceId: e.id,
      score: clamp01(e.centrality),
      included: true,
      isSeed: true,
    });
  }

  for (const mc of result.merged_candidates) {
    const id = mc.ref; // already "<source_type>:<source_id>" from the backend
    nodes.set(id, {
      id,
      kind: mc.source_type,
      label: candidateLabel(mc),
      sourceType: mc.source_type,
      sourceId: mc.source_id,
      score: clamp01(mc.final_score),
      included: mc.included,
      isSeed: false,
    });
  }

  const nodeList = Array.from(nodes.values());
  const edges: ActivationEdge[] = [];
  result.activation_events.forEach((ev: ActivationEventJSON, i: number) => {
    // Defensive: never render a pulse whose endpoint isn't a real node in
    // this graph -- "do not invent graph edges" applies to the renderer's
    // input just as much as to the backend that produced it.
    if (!nodes.has(ev.source) || !nodes.has(ev.target)) return;
    edges.push({
      id: `${ev.source}|${ev.target}|${ev.channel}|${i}`,
      source: ev.source,
      target: ev.target,
      channel: ev.channel,
      strength: clamp01(ev.strength),
      hops: ev.hops,
      reason: ev.reason,
    });
  });

  return { nodes: nodeList, edges };
}

/**
 * Node click -> canonical source (spec section 10): resolves a graph node
 * id back to the Postgres-backed object it represents, or null for the
 * transient "query" root, which is a UI-only visualization object and was
 * never persisted anywhere (see feedback_visualization_query_node memory).
 */
export function resolveCanonicalSource(
  node: Pick<ActivationNode, "sourceType" | "sourceId">
): { sourceType: string; sourceId: string } | null {
  if (node.sourceType === null || node.sourceId === null) return null;
  return { sourceType: node.sourceType, sourceId: node.sourceId };
}

// ---------------------------------------------------------------------------
// Visual intensity + decay (spec sections 3, 4, 12)
// ---------------------------------------------------------------------------

const MIN_INTENSITY = 0.15; // even a weak pulse stays faintly visible, per "restrained" not "flashing"

/** Monotonic, bounded [MIN_INTENSITY, 1] mapping from raw activation strength to visual brightness. */
export function strengthToIntensity(strength: number): number {
  const s = clamp01(strength);
  return MIN_INTENSITY + (1 - MIN_INTENSITY) * s;
}

/**
 * activation(t) = initial * 0.5^(t / halfLifeMs) -- the same log-linear decay
 * shape core/dynamics.py already uses for memory/evidence decay, applied
 * here to visual glow instead of a stored score. Strictly decreasing, never
 * negative, asymptotically approaches (but never reaches) 0.
 */
export function decayCurve(elapsedMs: number, halfLifeMs: number, initial = 1): number {
  if (elapsedMs <= 0) return initial;
  if (halfLifeMs <= 0) return 0;
  return initial * Math.pow(0.5, elapsedMs / halfLifeMs);
}

// ---------------------------------------------------------------------------
// Playback timeline (spec sections 3, 5, 11, 12)
// ---------------------------------------------------------------------------

export type Stage = "seed" | "propagate";

export const STAGE_START_MS: Record<Stage, number> = {
  seed: 0,
  propagate: 500,
};

const STAGGER_MS = 45; // per-edge stagger within a stage, so propagation doesn't fire as one flat wall of pulses
const PULSE_TRAVEL_MS = { min: 260, max: 900 }; // strong = fast, weak = slow
const GLOW_HALF_LIFE_MS = { min: 500, max: 4200 }; // strong = persistent, weak = short-lived

export function stageForChannel(channel: string): Stage {
  return channel === "seed" ? "seed" : "propagate";
}

function lerp(min: number, max: number, t: number): number {
  return min + (max - min) * clamp01(t);
}

export interface TimelineEvent {
  edge: ActivationEdge;
  stage: Stage;
  startMs: number;
  travelMs: number; // how long the pulse takes to travel source -> target
  glowHalfLifeMs: number; // decay half-life of the glow left behind at the target
}

/**
 * Assigns deterministic start/duration to every edge. Order is the order
 * `edges` was given in (which is the order activation_events came back in,
 * itself insertion-ordered from hybrid_query()'s channel sequence) -- same
 * input always produces the same timeline, which is what trace replay will
 * lean on later.
 */
export function computeTimeline(edges: ActivationEdge[]): TimelineEvent[] {
  const stageCounters: Record<Stage, number> = { seed: 0, propagate: 0 };
  return edges.map((edge) => {
    const stage = stageForChannel(edge.channel);
    const indexInStage = stageCounters[stage]++;
    return {
      edge,
      stage,
      startMs: STAGE_START_MS[stage] + indexInStage * STAGGER_MS,
      travelMs: lerp(PULSE_TRAVEL_MS.max, PULSE_TRAVEL_MS.min, edge.strength),
      glowHalfLifeMs: lerp(GLOW_HALF_LIFE_MS.min, GLOW_HALF_LIFE_MS.max, edge.strength),
    };
  });
}

/** When the last pulse of `timeline` finishes travelling -- i.e. when convergence/decay can begin. */
export function settledAtMs(timeline: TimelineEvent[]): number {
  return timeline.reduce((max, t) => Math.max(max, t.startMs + t.travelMs), 0);
}

/**
 * Total visual activation at a node at time `tMs`, summing every arrived
 * pulse's decayed contribution (spec section 11: A, B, D all targeting C
 * makes C brighten more than any single contribution would). Returned
 * value is not clamped to [0,1] by design -- callers pass it through
 * strengthToIntensity (which does clamp) so convergence can still be
 * visually distinguished from a single strong pulse before the intensity
 * curve compresses it.
 */
export function nodeActivationAtTime(nodeId: string, timeline: TimelineEvent[], tMs: number): number {
  let total = 0;
  for (const t of timeline) {
    if (t.edge.target !== nodeId) continue;
    const arrivalMs = t.startMs + t.travelMs;
    if (tMs < arrivalMs) continue;
    total += decayCurve(tMs - arrivalMs, t.glowHalfLifeMs, t.edge.strength);
  }
  return total;
}

/** Number of distinct sources currently converging on a node (spec section 11's "3 visible incoming events"). */
export function convergenceCount(nodeId: string, timeline: TimelineEvent[], tMs: number): number {
  const sources = new Set<string>();
  for (const t of timeline) {
    if (t.edge.target !== nodeId) continue;
    if (tMs >= t.startMs + t.travelMs) sources.add(t.edge.source);
  }
  return sources.size;
}

// ---------------------------------------------------------------------------
// Deterministic layout (spec section 13) -- no force-simulation dependency;
// swappable for one later without touching renderer components, which only
// ever consume {id, x, y, z}.
// ---------------------------------------------------------------------------

export interface LayoutPosition {
  x: number;
  y: number;
  z: number;
}

/** Deterministic string -> [0, 1) hash (FNV-1a), used for stable jitter/angle assignment. */
export function hashToUnit(input: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return ((h >>> 0) % 100000) / 100000;
}

const SEED_RING_RADIUS = 4;
const CANDIDATE_RING_MIN = 6;
const CANDIDATE_RING_MAX = 14;

function circularMeanAngle(angles: number[]): number {
  if (angles.length === 0) return 0;
  const sumSin = angles.reduce((s, a) => s + Math.sin(a), 0);
  const sumCos = angles.reduce((s, a) => s + Math.cos(a), 0);
  return Math.atan2(sumSin, sumCos);
}

/**
 * Graph Mode layout: query at the origin, seed entities on a fixed inner
 * ring, every other node's angle inherited from the seed(s) that activated
 * it (circular mean, so multi-source convergence pulls a node toward the
 * "average direction" of its sources) with radius shrinking as final_score
 * rises -- stronger candidates sit visibly closer to the center.
 */
export function computeLayout(graph: ActivationGraph): Map<string, LayoutPosition> {
  const positions = new Map<string, LayoutPosition>();
  positions.set(QUERY_NODE_ID, { x: 0, y: 0, z: 0 });

  const seedNodes = graph.nodes.filter((n) => n.isSeed).sort((a, b) => a.id.localeCompare(b.id));
  const seedAngle = new Map<string, number>();
  seedNodes.forEach((n, i) => {
    const angle = (i / Math.max(1, seedNodes.length)) * Math.PI * 2 + hashToUnit(n.id) * 0.3;
    seedAngle.set(n.id, angle);
    positions.set(n.id, {
      x: Math.cos(angle) * SEED_RING_RADIUS,
      y: Math.sin(angle) * SEED_RING_RADIUS,
      z: (hashToUnit(n.id + ":z") - 0.5) * 1.5,
    });
  });

  const incomingBySeed = new Map<string, string[]>(); // targetId -> [seedId, ...]
  for (const e of graph.edges) {
    if (!seedAngle.has(e.source)) continue;
    const list = incomingBySeed.get(e.target) ?? [];
    list.push(e.source);
    incomingBySeed.set(e.target, list);
  }

  for (const node of graph.nodes) {
    if (positions.has(node.id)) continue; // query + seeds already placed
    const sourceAngles = (incomingBySeed.get(node.id) ?? []).map((sid) => seedAngle.get(sid)!).filter((a) => a !== undefined);
    const angle = sourceAngles.length > 0 ? circularMeanAngle(sourceAngles) : hashToUnit(node.id) * Math.PI * 2;
    const radius = CANDIDATE_RING_MAX - clamp01(node.score) * (CANDIDATE_RING_MAX - CANDIDATE_RING_MIN);
    positions.set(node.id, {
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius,
      z: (hashToUnit(node.id + ":z") - 0.5) * 2.5,
    });
  }

  return positions;
}

/**
 * Stub anatomical layout (Brain Mode is deferred past this milestone -- the
 * toggle exists in the UI but is disabled). Exists only to prove, at the
 * data layer, that switching modes changes position/grouping and nothing
 * else: it consumes the exact same ActivationGraph as computeLayout() and
 * returns positions for the identical node id set -- see
 * activation.test.ts's "graph mode and brain mode use the same underlying
 * objects" case. Groups nodes into fixed clusters by kind rather than doing
 * any real anatomical mapping.
 */
export function computeAnatomicalLayoutStub(graph: ActivationGraph): Map<string, LayoutPosition> {
  const CLUSTER_CENTERS: Record<string, LayoutPosition> = {
    query: { x: 0, y: 0, z: 0 },
    entity: { x: -8, y: 4, z: 0 },
    memory: { x: 8, y: 4, z: 0 },
    belief: { x: 8, y: -4, z: 0 },
    mental_model: { x: -8, y: -4, z: 0 },
    cognitive_pattern: { x: 0, y: 8, z: 0 },
    state_item: { x: 0, y: -8, z: 0 },
  };
  const positions = new Map<string, LayoutPosition>();
  for (const node of graph.nodes) {
    const center = CLUSTER_CENTERS[node.kind] ?? { x: 0, y: 0, z: 0 };
    const jitterAngle = hashToUnit(node.id) * Math.PI * 2;
    const jitterR = hashToUnit(node.id + ":r") * 2;
    positions.set(node.id, {
      x: center.x + Math.cos(jitterAngle) * jitterR,
      y: center.y + Math.sin(jitterAngle) * jitterR,
      z: center.z,
    });
  }
  return positions;
}
