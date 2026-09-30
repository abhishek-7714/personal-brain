// web/components/brain/theme.ts
//
// Node-kind -> color mapping, extending the restrained palette already
// defined in web/app/globals.css rather than inventing a new one. Kinds
// come straight from RetrievalCandidateJSON.source_type / the "entity" /
// "query" synthetic kinds in web/lib/activation.ts.

export const NODE_COLOR: Record<string, string> = {
  query: "#e4e6eb", // --text
  entity: "#a48af2", // violet -- seed / graph anchor
  memory: "#5a8dee", // --accent
  belief: "#3ecf8e", // --ok
  mental_model: "#e0a72e", // --warn
  cognitive_pattern: "#ef8a5a",
  state_item: "#e0568e",
  episode: "#8b93a3", // --text-dim, rarely surfaced as a candidate today
  experience: "#8b93a3",
};

export function colorForKind(kind: string): string {
  return NODE_COLOR[kind] ?? "#8b93a3";
}

export const BASE_NODE_SIZE = 0.24;
export const SEED_NODE_SIZE = 0.34;
export const QUERY_NODE_SIZE = 0.42;
export const IDLE_FLOOR_INTENSITY = 0.05;
