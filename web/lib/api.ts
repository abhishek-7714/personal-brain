// lib/api.ts
//
// Thin fetch wrapper around the FastAPI backend (api/main.py). No business
// logic lives here -- every function is a direct call to one endpoint and a
// JSON parse. Pages compose these calls and render what comes back; they do
// not compute scores, resolve entities, or otherwise duplicate what
// core/*.py already does.

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      // ignore -- non-JSON error body
    }
    throw new Error(`${res.status} ${path}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

// --- Status -----------------------------------------------------------

export interface ServiceHealth {
  ok: boolean;
  detail: string;
}
export interface OwnerStatus {
  seeded: boolean;
  id?: string;
  display_name?: string;
  error?: string;
}
export interface SystemStatus {
  postgres: ServiceHealth;
  neo4j: ServiceHealth;
  qdrant: ServiceHealth;
  minio: ServiceHealth;
  temporal: ServiceHealth;
  owner: OwnerStatus;
}
export interface TestSuiteResult {
  file: string;
  exit_code: number | null;
  skipped?: boolean;
  passed: number | null;
  failed: number | null;
  error?: string;
}

export const getStatus = () => request<SystemStatus>("/status");
export const seedOwner = () =>
  request<{ id: string; display_name: string }>("/status/seed-owner", { method: "POST" });
export const runTests = (suite: "fast" | "full") =>
  request<{ suite: string; results: TestSuiteResult[] }>(`/status/run-tests?suite=${suite}`, {
    method: "POST",
  });

// --- Experiences --------------------------------------------------------

export interface IngestResult {
  experience_id: string;
  created: boolean;
  observation_count: number;
  entity_count: number;
  memory_count: number;
  belief_count: number;
  memory_entity_link_count: number;
  ambiguous: boolean;
  ambiguity_note: string;
  projection_error: string | null;
  indexing_error: string | null;
}

export const ingestExperience = (body: {
  text: string;
  source_type?: string;
  occurred_at?: string | null;
}) => request<IngestResult>("/experiences", { method: "POST", body: JSON.stringify(body) });

export const listExperiences = (limit = 20) =>
  request<
    Array<{
      id: string;
      kind: string;
      occurred_at: string | null;
      ingested_at: string | null;
      is_test_fixture: boolean;
    }>
  >(`/experiences?limit=${limit}`);

// The shape returned by core.reader.inspect_experience(), plus `episodes`.
// Kept loose (any) rather than fully typed -- this page is meant to reflect
// exactly what the backend returns, including fields added to reader.py in
// the future, without the frontend type lagging behind it.
export type ExperienceInspection = Record<string, any>;

export const getExperience = (id: string) => request<ExperienceInspection>(`/experiences/${id}`);

// --- Graph (Brain Explorer) ---------------------------------------------

export const getGraphLabels = () => request<Record<string, number>>("/graph/labels");

export interface GraphNodeSummary {
  id: string;
  label: string;
  summary: string;
  properties: Record<string, any>;
}
export const listGraphNodes = (label: string, limit = 50) =>
  request<GraphNodeSummary[]>(`/graph/nodes?label=${encodeURIComponent(label)}&limit=${limit}`);

export interface GraphEdge {
  rel_type: string;
  properties: Record<string, any>;
  target_id?: string;
  target_label?: string;
  target_summary?: string;
  source_id?: string;
  source_label?: string;
  source_summary?: string;
}
export interface GraphNodeDetail {
  id: string;
  labels: string[];
  properties: Record<string, any>;
  postgres_id: string;
  outgoing: GraphEdge[];
  incoming: GraphEdge[];
}
export const getGraphNode = (id: string) => request<GraphNodeDetail>(`/graph/node/${id}`);

// --- Retrieval Playground -------------------------------------------------

export interface RetrievalCandidateJSON {
  source_id: string;
  source_type: string;
  retrieval_channel: string;
  raw_score: number;
  normalized_score: number;
  reason: string;
  content_preview: string;
  hops: number | null;
  association_strength: number | null;
  entity_ids: string[];
}
export interface SeedEntityJSON {
  id: string;
  canonical_name: string;
  kind: string;
  centrality: number;
}
// One edge in the Neural Brain Visualization's activation graph, exactly as
// derived server-side by api.routers.retrieval._build_activation_events()
// from data hybrid_query() already computed -- no new scoring happens on
// either side of this type. `source`/`target` are either "query" (the
// transient visualization root -- never persisted, never a canonical
// object) or "entity:<uuid>" / "<source_type>:<source_id>" (always a real
// Postgres-backed id resolvable via the graph/retrieval endpoints).
export interface ActivationEventJSON {
  source: string;
  target: string;
  channel: string;
  strength: number;
  hops: number | null;
  reason: string;
}
export interface MergedCandidateJSON {
  ref: string;
  source_type: string;
  source_id: string;
  final_score: number;
  included: boolean;
  breakdown: {
    semantic: number;
    association: number;
    situation: number;
    goal_alignment: number;
    salience: number;
    evidence_quality: number;
    staleness: number;
    is_contradiction: boolean;
  };
  discoveries: RetrievalCandidateJSON[];
}
export interface RetrievalResult {
  query: string;
  seed_entities: SeedEntityJSON[];
  qdrant_results: RetrievalCandidateJSON[];
  neo4j_results: RetrievalCandidateJSON[];
  postgres_results: {
    temporal: RetrievalCandidateJSON[];
    state: RetrievalCandidateJSON[];
    situation: RetrievalCandidateJSON[];
  };
  merged_candidates: MergedCandidateJSON[];
  trace_id: string | null;
  // Every entity graph_retrieve() actually walked from -- a superset of
  // seed_entities, since it also includes entities pulled in from top
  // semantic hits (core.retrieval.hybrid_query()'s extra_seed_ids), which
  // never go through the query-text name/alias match seed_entities does.
  // Use this, not seed_entities, when rendering seed nodes.
  all_seed_entities: SeedEntityJSON[];
  activation_events: ActivationEventJSON[];
}

export const runRetrievalQuery = (query: string, recordTrace = true) =>
  request<RetrievalResult>("/retrieval/query", {
    method: "POST",
    body: JSON.stringify({ query, record_trace: recordTrace }),
  });

// --- Calibration ----------------------------------------------------------

export interface CalibrationFixture {
  key: string;
  label: string;
  description: string;
  text: string;
}
export const listFixtures = () => request<CalibrationFixture[]>("/calibration/fixtures");
export const ingestFixture = (key: string) =>
  request<IngestResult>(`/calibration/fixtures/${encodeURIComponent(key)}/ingest`, { method: "POST" });

// --- Calibration evaluation -------------------------------------------------

export interface AssertionResultJSON {
  code: string;
  dimension: string;
  description: string;
  essential: boolean;
  passed: boolean;
  detail: string;
  status: "pass" | "FAIL" | "advisory";
}
export interface TraceGroundingJSON {
  operation: string;
  target: string;
  grounding: number;
}
export interface FixtureEvaluationJSON {
  fixture_key: string;
  label: string;
  mode: "mock" | "live";
  status: "scored" | "validation_error" | "extraction_error";
  error: string | null;
  experience_id: string | null;
  ok: boolean;
  passed_count: number;
  total_count: number;
  essential_failure_count: number;
  advisory_count: number;
  results: AssertionResultJSON[];
  trace_grounding: TraceGroundingJSON[];
}

export const evaluateFixture = (key: string, mode: "mock" | "live") =>
  request<FixtureEvaluationJSON>(
    `/calibration/fixtures/${encodeURIComponent(key)}/evaluate?mode=${mode}`,
    { method: "POST" }
  );
