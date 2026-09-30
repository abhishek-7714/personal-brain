// web/lib/activation.test.ts
//
// Unit tests for the pure transform layer in web/lib/activation.ts. No
// network, no DOM rendering -- these exercise the same kind of RetrievalResult
// shape POST /api/retrieval/query actually returns (see
// tests/test_api.py's NEURAL BRAIN VISUALIZATION section for the live-stack
// equivalent of the multi-seed-convergence and semantic-expanded-seed cases
// covered here).

import { describe, expect, it } from "vitest";
import type { MergedCandidateJSON, RetrievalResult } from "./api";
import {
  buildActivationGraph,
  computeAnatomicalLayoutStub,
  computeLayout,
  computeTimeline,
  convergenceCount,
  decayCurve,
  hashToUnit,
  nodeActivationAtTime,
  resolveCanonicalSource,
  settledAtMs,
  strengthToIntensity,
} from "./activation";

function mc(overrides: Partial<MergedCandidateJSON>): MergedCandidateJSON {
  return {
    ref: "memory:mem-1",
    source_type: "memory",
    source_id: "mem-1",
    final_score: 0.5,
    included: true,
    breakdown: {
      semantic: 0, association: 0, situation: 0, goal_alignment: 0,
      salience: 0, evidence_quality: 0, staleness: 0, is_contradiction: false,
    },
    discoveries: [],
    ...overrides,
  };
}

function baseResult(overrides: Partial<RetrievalResult>): RetrievalResult {
  return {
    query: "What do I know about Kubernetes?",
    seed_entities: [],
    qdrant_results: [],
    neo4j_results: [],
    postgres_results: { temporal: [], state: [], situation: [] },
    merged_candidates: [],
    trace_id: null,
    all_seed_entities: [],
    activation_events: [],
    ...overrides,
  };
}

describe("buildActivationGraph", () => {
  it("always includes exactly one query node, even with no candidates", () => {
    const { nodes } = buildActivationGraph(baseResult({}));
    expect(nodes).toHaveLength(1);
    expect(nodes[0].id).toBe("query");
    expect(nodes[0].sourceType).toBeNull();
    expect(nodes[0].sourceId).toBeNull();
  });

  it("maps seed entities and merged candidates to graph nodes with real canonical ids", () => {
    const result = baseResult({
      all_seed_entities: [{ id: "ent-1", canonical_name: "Kubernetes", kind: "concept", centrality: 0.7 }],
      merged_candidates: [
        mc({
          ref: "memory:mem-1", source_type: "memory", source_id: "mem-1", final_score: 0.8,
          discoveries: [{
            source_id: "mem-1", source_type: "memory", retrieval_channel: "graph", raw_score: 0.8,
            normalized_score: 0.8, reason: "via Kubernetes", content_preview: "Debugged a k8s outage",
            hops: 1, association_strength: 0.8, entity_ids: ["ent-1"],
          }],
        }),
      ],
    });
    const { nodes } = buildActivationGraph(result);
    const entityNode = nodes.find((n) => n.id === "entity:ent-1")!;
    expect(entityNode.isSeed).toBe(true);
    expect(entityNode.sourceType).toBe("entity");
    expect(entityNode.sourceId).toBe("ent-1");

    const candidateNode = nodes.find((n) => n.id === "memory:mem-1")!;
    expect(candidateNode.sourceType).toBe("memory");
    expect(candidateNode.sourceId).toBe("mem-1");
    expect(candidateNode.label).toBe("Debugged a k8s outage");
    expect(candidateNode.score).toBeCloseTo(0.8);
  });

  it("source -> target mapping: converts activation_events 1:1 into edges referencing real node ids", () => {
    const result = baseResult({
      all_seed_entities: [{ id: "ent-1", canonical_name: "Kubernetes", kind: "concept", centrality: 0.5 }],
      merged_candidates: [mc({ ref: "memory:mem-1", source_id: "mem-1" })],
      activation_events: [
        { source: "query", target: "entity:ent-1", channel: "seed", strength: 1, hops: null, reason: "seed" },
        { source: "entity:ent-1", target: "memory:mem-1", channel: "graph", strength: 0.8, hops: 1, reason: "via Kubernetes" },
      ],
    });
    const { edges } = buildActivationGraph(result);
    expect(edges).toHaveLength(2);
    expect(edges[1].source).toBe("entity:ent-1");
    expect(edges[1].target).toBe("memory:mem-1");
    expect(edges[1].channel).toBe("graph");
  });

  it("drops an event referencing a node outside the built graph rather than inventing an edge", () => {
    const result = baseResult({
      activation_events: [
        { source: "query", target: "entity:ghost", channel: "seed", strength: 1, hops: null, reason: "unresolvable" },
      ],
    });
    const { edges } = buildActivationGraph(result);
    expect(edges).toHaveLength(0);
  });
});

describe("resolveCanonicalSource — node click -> canonical source", () => {
  it("resolves a candidate node back to its source_type/source_id", () => {
    const resolved = resolveCanonicalSource({ sourceType: "memory", sourceId: "mem-1" });
    expect(resolved).toEqual({ sourceType: "memory", sourceId: "mem-1" });
  });

  it("returns null for the transient query node (never a canonical object)", () => {
    expect(resolveCanonicalSource({ sourceType: null, sourceId: null })).toBeNull();
  });
});

describe("strengthToIntensity", () => {
  it("is monotonically non-decreasing and bounded to [0.15, 1]", () => {
    const samples = [0, 0.1, 0.3, 0.5, 0.7, 0.9, 1];
    const intensities = samples.map(strengthToIntensity);
    for (let i = 1; i < intensities.length; i++) {
      expect(intensities[i]).toBeGreaterThanOrEqual(intensities[i - 1]);
    }
    expect(intensities[0]).toBeCloseTo(0.15);
    expect(intensities[intensities.length - 1]).toBeCloseTo(1);
  });

  it("clamps out-of-range input", () => {
    expect(strengthToIntensity(-5)).toBeCloseTo(0.15);
    expect(strengthToIntensity(5)).toBeCloseTo(1);
  });
});

describe("decayCurve — activation(t) = initial * decay(t)", () => {
  it("starts at `initial` and strictly decreases thereafter", () => {
    const halfLife = 1000;
    let prev = decayCurve(0, halfLife, 0.9);
    expect(prev).toBeCloseTo(0.9);
    for (const t of [100, 500, 1000, 2000, 5000]) {
      const v = decayCurve(t, halfLife, 0.9);
      expect(v).toBeLessThan(prev);
      expect(v).toBeGreaterThanOrEqual(0);
      prev = v;
    }
  });

  it("reaches exactly half of initial at one half-life", () => {
    expect(decayCurve(1000, 1000, 1)).toBeCloseTo(0.5, 5);
  });

  it("never goes negative and approaches (but never reaches) zero", () => {
    expect(decayCurve(1_000_000, 1000, 1)).toBeGreaterThan(0);
    expect(decayCurve(1_000_000, 1000, 1)).toBeLessThan(0.001);
  });
});

describe("computeTimeline + nodeActivationAtTime — convergence and decay", () => {
  it("a target reached by 3 sources shows a higher activation than any single contribution alone (spec section 11)", () => {
    const graph = buildActivationGraph(
      baseResult({
        all_seed_entities: [
          { id: "a", canonical_name: "A", kind: "concept", centrality: 0.5 },
          { id: "b", canonical_name: "B", kind: "concept", centrality: 0.5 },
          { id: "d", canonical_name: "D", kind: "concept", centrality: 0.5 },
        ],
        merged_candidates: [mc({ ref: "memory:c", source_id: "c", final_score: 0.9 })],
        activation_events: [
          { source: "entity:a", target: "memory:c", channel: "graph", strength: 0.6, hops: 1, reason: "a" },
          { source: "entity:b", target: "memory:c", channel: "graph", strength: 0.6, hops: 1, reason: "b" },
          { source: "entity:d", target: "memory:c", channel: "graph", strength: 0.6, hops: 1, reason: "d" },
        ],
      })
    );
    const timeline = computeTimeline(graph.edges);
    const settled = settledAtMs(timeline);

    const single = nodeActivationAtTime("memory:c", timeline.slice(0, 1), settled + 50);
    const all3 = nodeActivationAtTime("memory:c", timeline, settled + 50);
    expect(all3).toBeGreaterThan(single);
    expect(convergenceCount("memory:c", timeline, settled + 50)).toBe(3);
  });

  it("decays toward zero well after all pulses have settled", () => {
    const graph = buildActivationGraph(
      baseResult({
        all_seed_entities: [{ id: "a", canonical_name: "A", kind: "concept", centrality: 0.5 }],
        merged_candidates: [mc({ ref: "memory:c", source_id: "c" })],
        activation_events: [
          { source: "entity:a", target: "memory:c", channel: "graph", strength: 0.5, hops: 1, reason: "a" },
        ],
      })
    );
    const timeline = computeTimeline(graph.edges);
    const settled = settledAtMs(timeline);
    const soonAfter = nodeActivationAtTime("memory:c", timeline, settled + 10);
    const longAfter = nodeActivationAtTime("memory:c", timeline, settled + 60_000);
    expect(longAfter).toBeLessThan(soonAfter);
    expect(longAfter).toBeGreaterThanOrEqual(0);
  });

  it("strong edges travel faster and glow longer than weak edges", () => {
    const strongWeak = computeTimeline([
      { id: "s", source: "query", target: "memory:a", channel: "semantic", strength: 0.95, hops: null, reason: "" },
      { id: "w", source: "query", target: "memory:b", channel: "semantic", strength: 0.1, hops: null, reason: "" },
    ]);
    const [strong, weak] = strongWeak;
    expect(strong.travelMs).toBeLessThan(weak.travelMs);
    expect(strong.glowHalfLifeMs).toBeGreaterThan(weak.glowHalfLifeMs);
  });
});

describe("computeTimeline determinism (what persisted-trace replay will lean on)", () => {
  it("the same edge list always produces the same timeline", () => {
    const edges = [
      { id: "1", source: "query", target: "entity:a", channel: "seed", strength: 1, hops: null, reason: "" },
      { id: "2", source: "entity:a", target: "memory:m", channel: "graph", strength: 0.7, hops: 1, reason: "" },
    ];
    expect(computeTimeline(edges)).toEqual(computeTimeline(edges));
  });
});

describe("hashToUnit", () => {
  it("is deterministic and bounded to [0, 1)", () => {
    expect(hashToUnit("abc")).toBe(hashToUnit("abc"));
    for (const s of ["a", "abc", "memory:1234", ""]) {
      const v = hashToUnit(s);
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThan(1);
    }
  });
});

describe("Graph Mode vs Brain Mode share the same underlying objects", () => {
  it("computeLayout and computeAnatomicalLayoutStub both position the exact same node id set", () => {
    const graph = buildActivationGraph(
      baseResult({
        all_seed_entities: [{ id: "ent-1", canonical_name: "Kubernetes", kind: "concept", centrality: 0.5 }],
        merged_candidates: [mc({ ref: "memory:m1", source_id: "m1" }), mc({ ref: "belief:b1", source_type: "belief", source_id: "b1" })],
        activation_events: [
          { source: "entity:ent-1", target: "memory:m1", channel: "graph", strength: 0.6, hops: 1, reason: "" },
        ],
      })
    );
    const graphLayout = computeLayout(graph);
    const brainLayout = computeAnatomicalLayoutStub(graph);

    const graphIds = new Set(graphLayout.keys());
    const brainIds = new Set(brainLayout.keys());
    expect(graphIds).toEqual(brainIds);
    expect(graphIds).toEqual(new Set(graph.nodes.map((n) => n.id)));
  });

  it("mode toggle changes position only, not node score/identity/canonical source", () => {
    const graph = buildActivationGraph(
      baseResult({
        merged_candidates: [mc({ ref: "memory:m1", source_id: "m1", final_score: 0.42 })],
      })
    );
    const before = JSON.stringify(graph.nodes.find((n) => n.id === "memory:m1"));
    computeLayout(graph);
    computeAnatomicalLayoutStub(graph);
    const after = JSON.stringify(graph.nodes.find((n) => n.id === "memory:m1"));
    expect(after).toBe(before);
  });
});
