// schema.cypher
// Neo4j is a rebuildable projection. Every node carries the Postgres uuid in `id`
// so the whole graph can be dropped and replayed from the event log at any time.
// Run once at boot; all statements are idempotent.

// ---------------------------------------------------------------------------
// Uniqueness constraints (these also create the backing index)
// ---------------------------------------------------------------------------

CREATE CONSTRAINT owner_id IF NOT EXISTS
  FOR (n:Owner) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT entity_id IF NOT EXISTS
  FOR (n:Entity) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT experience_id IF NOT EXISTS
  FOR (n:Experience) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT observation_id IF NOT EXISTS
  FOR (n:Observation) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT episode_id IF NOT EXISTS
  FOR (n:Episode) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT trace_id IF NOT EXISTS
  FOR (n:CognitiveTrace) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT memory_id IF NOT EXISTS
  FOR (n:Memory) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT belief_id IF NOT EXISTS
  FOR (n:Belief) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT mental_model_id IF NOT EXISTS
  FOR (n:MentalModel) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT pattern_id IF NOT EXISTS
  FOR (n:CognitivePattern) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT decision_id IF NOT EXISTS
  FOR (n:Decision) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT state_item_id IF NOT EXISTS
  FOR (n:StateItem) REQUIRE n.id IS UNIQUE;

// ---------------------------------------------------------------------------
// Property indexes for traversal entry points and re-ranking
// ---------------------------------------------------------------------------

CREATE INDEX entity_kind IF NOT EXISTS FOR (n:Entity) ON (n.kind);
CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.canonical_name);
CREATE FULLTEXT INDEX entity_search IF NOT EXISTS
  FOR (n:Entity) ON EACH [n.canonical_name, n.description];

CREATE INDEX memory_salience IF NOT EXISTS FOR (n:Memory) ON (n.salience);
CREATE INDEX memory_valid IF NOT EXISTS FOR (n:Memory) ON (n.valid_to);
CREATE INDEX belief_status IF NOT EXISTS FOR (n:Belief) ON (n.status);
CREATE INDEX episode_started IF NOT EXISTS FOR (n:Episode) ON (n.started_at);
CREATE INDEX decision_decided IF NOT EXISTS FOR (n:Decision) ON (n.decided_at);
CREATE INDEX state_open IF NOT EXISTS FOR (n:StateItem) ON (n.closed_at);

// Association strength drives graph expansion during activation, so it is indexed
// as a relationship property.
CREATE INDEX assoc_strength IF NOT EXISTS
  FOR ()-[r:ASSOCIATED_WITH]-() ON (r.strength);
CREATE INDEX assoc_context IF NOT EXISTS
  FOR ()-[r:ASSOCIATED_WITH]-() ON (r.context);
CREATE INDEX derived_from_idx IF NOT EXISTS
  FOR ()-[r:DERIVED_FROM]-() ON (r.stance);

// ---------------------------------------------------------------------------
// Projection upsert patterns. The projector applies these per event; all use
// MERGE on the Postgres id so replay is idempotent.
// ---------------------------------------------------------------------------

// :param props => {id:'...', kind:'project', canonical_name:'...', centrality:0.0}
// MERGE (e:Entity {id: $props.id}) SET e += $props;

// Evidence edge. Every derived node must have at least one of these, which is
// what makes "why do you think that" answerable by traversal alone.
// MATCH (d {id: $derived_id}), (s {id: $source_id})
// MERGE (d)-[r:DERIVED_FROM {stance: $stance}]->(s)
// SET r.weight = $weight, r.recorded_at = datetime($recorded_at);

// Learned association. Directional and context-scoped: A->B in 'decision' is a
// different edge from A->B in 'conversation', and from B->A in either.
// MATCH (a {id: $source_id}), (b {id: $target_id})
// MERGE (a)-[r:ASSOCIATED_WITH {context: $context}]->(b)
// SET r.strength = $strength,
//     r.confidence = $confidence,
//     r.observation_count = $observation_count,
//     r.source_kind = $source_kind,
//     r.first_observed_at = datetime($first_observed_at),
//     r.last_observed_at = datetime($last_observed_at);

// ---------------------------------------------------------------------------
// Activation query used by the retrieval engine.
// Spreads outward from seed nodes, attenuating by association strength at each
// hop, so distant weakly-linked material does not flood the context set.
// ---------------------------------------------------------------------------

// :param seeds => ['uuid-1','uuid-2']
// :param min_strength => 0.15
// :param max_hops => 3
//
// MATCH path = (seed)-[rels:ASSOCIATED_WITH*1..3]->(reached)
// WHERE seed.id IN $seeds
//   AND ALL(r IN rels WHERE r.strength >= $min_strength)
//   AND (reached:Memory OR reached:Belief OR reached:Entity
//        OR reached:Decision OR reached:MentalModel)
// WITH reached,
//      reduce(activation = 1.0, r IN rels | activation * r.strength) AS activation,
//      length(path) AS hops
// RETURN reached.id AS id, labels(reached)[0] AS type,
//        max(activation) AS activation, min(hops) AS hops
// ORDER BY activation DESC
// LIMIT 200;

// ---------------------------------------------------------------------------
// Contradiction surfacing. Run alongside activation so counter-evidence reaches
// the reasoner instead of being outranked by the supporting narrative.
// ---------------------------------------------------------------------------

// MATCH (b:Belief)-[:CONTRADICTS]-(other:Belief)
// WHERE b.id IN $activated_belief_ids
// RETURN b.id AS belief_id, other.id AS contradicting_id,
//        other.proposition AS proposition, other.confidence AS confidence;
