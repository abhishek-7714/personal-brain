# Extractor classification rules

Every distinction added in migration 004 is a judgment the extractor has to make
on every ingest, consistently, forever. Adding enum values without writing the
decision procedure just moves the ambiguity out of the schema and into a prompt,
which is a worse place for it. These are the rules. They belong in the extraction
prompt and in the review checklist for anything the extractor is unsure about.

The controlling principle throughout: **when a classification is genuinely
ambiguous, do not guess. Emit the observation with `epistemic_status =
'hypothesis'` and let consolidation resolve it once more evidence exists.** A
hypothesis that never resolves costs a row. A wrong confident classification
poisons retrieval for years and is invisible when it does.

---

## 1. memory_system

Ask one question: **what would make this wrong?**

- Nothing could make it wrong, because it happened → **episodic**
- New information about the world could make it wrong → **semantic**
- Nothing makes it wrong, but disuse makes it fade → **procedural**

Worked examples:

| Input | System | Kind | Why |
| --- | --- | --- | --- |
| "Shipped the Phynx CLI on the 14th" | episodic | event | Happened. Cannot become false. |
| "Priya leads the platform team" | semantic | person_attribute | She could change roles tomorrow. |
| "I set up Temporal with the auto-setup image" | procedural | tool_use | Knowing how, not knowing that. |
| "I prefer thesis-first writing" | semantic | preference | A preference is a stable fact about him that can change. |
| "The demo crashed and I felt awful" | episodic | event | The event, with stated emotion attached. |

**The "I learned X" trap.** The finalized ontology has no memory kind meaning
"something I learned", because that phrasing is unrecoverable once written. Split
it at write time:

- "I learned Kubernetes" → procedural memory, kind `skill`
- "I learned not to trust that business model" → a **belief**
  (`normative`), with the episodic memory of the failure as its evidence row

If the sentence contains "I learned", decide which of those two it is before
writing anything. If it is genuinely both, write both: the procedural memory and
the belief, linked through the same episodic memory in the evidence spine.

**Goals appear twice on purpose.** An active goal is a `state_item`. A goal that
is historical or referenced rather than pursued is a semantic memory. If he is
working on it now, it is state.

**Preferences produce two outputs simultaneously.** When the input asserts a
stable preference ("I prefer X", "I like doing Y"), write both:

1. A `semantic / preference` memory — the stable fact about the owner that can
   change as his tastes change.
2. A `self_report` belief — the owner's current dispositional state, where the
   holding *is* the fact (confidence and truth_confidence are equal).

These are not alternatives. A preference participates in memory retrieval (the
semantic memory) *and* in the evidence spine for downstream belief reasoning
(the self_report belief). Writing only one loses half the information.
Link both to the same observation via evidence rows.

---

## 2. belief_kind

Ask: **who or what makes this true?**

- The owner's own mind makes it true → **self_report**
- The world makes it true → **world_claim**
- Nothing makes it true; it is a policy → **normative**
- Someone else's mind makes it true → **attributed**

| Statement | Kind | Note |
| --- | --- | --- |
| "I prefer working alone in the mornings" | self_report | The holding *is* the fact. |
| "This startup will work" | world_claim | Owner assertion is strong on holding, weak on truth. |
| "Validate demand before major investment" | normative | A policy. No external truth-maker. |
| "Priya thinks the model is overfitting" | attributed | Set `about_entity_id` to Priya. |
| "I think this startup will work" | world_claim | The "I think" marks the epistemic owner, not the belief kind. |

That last row is the one that gets misclassified. "I think X" where X is a claim
about the world is a **world_claim** with `epistemic_owner = 'owner'`. It is not
a self_report. Self_report is reserved for propositions whose subject is the
owner's own mental or dispositional state.

The consequence is real: a world_claim tracks two confidences independently, so
the system can hold "he is quite sure of this" and "the evidence runs against it"
at the same time and say so. Classifying it as self_report collapses both axes
and destroys that ability permanently.

---

## 3. epistemic_owner

Who produced the evidence, not who it is about.

- **owner** — Abhishek said or wrote it
- **authoritative_source** — a source with standing in the relevant domain: a
  filing, a primary document, official documentation, a peer-reviewed result
- **external_source** — an article, a blog, a search result with no particular standing
- **other_person** — a named person in his world said it
- **system_inference** — no one said it; the model derived it

`authoritative_source` is domain-scoped, not global. A regulator's filing is
authoritative on the filing's subject and ordinary on anything else. Where the
distinction matters, record the standing in `source_reliability` with a `domain`
rather than promoting the whole source.

**system_inference is the default for anything not directly attested.** If you
cannot point at a span in the source text, it is an inference. The weight table
in `dynamics.py` prices inference at roughly a seventh of an owner assertion,
which is the mechanism that keeps model output from becoming fact. That mechanism
only works if the extractor is honest about which rows are inferences.

---

## 4. Episode boundaries

An episode is a **cognitively continuous situation**, not a time window and not a
conversation. Boundaries fall where the thing being thought about changes.

Open an episode when: a question is posed, a problem is encountered, a decision
becomes live, or reflection begins.
Close it when: the question is answered, the decision is made or abandoned, or
attention demonstrably moves elsewhere.

One conversation frequently contains three episodes. Three conversations across
two weeks frequently contain one. Do not let the transport shape the episode.

If nothing cognitively meaningful happened, emit no episode. A message asking for
a file path is not an episode. Over-segmenting is the more common failure and it
degrades pattern discovery, because patterns are counted per distinct episode.

---

## 5. cognitive_trace

The hard constraint: **every trace step must cite an observation with a span in
the source text.** No span, no trace. This is what keeps the table from becoming
stored chain-of-thought, which is explicitly out of scope.

Record what he did, not what he must have been thinking:

- He asked what the failure rate was → `seek_information`
- He listed three options → `generate_option` (three rows)
- He said one was too risky → `evaluate_option`
- He picked one → `decide`
- He said he would wait a week → `defer`

Do not record `estimate` because his conclusion implies he must have estimated
something. If the estimation is not visible in the text, it did not happen as far
as this system is concerned.

- He guessed the outage was caused by a bad deploy, before checking → `hypothesize`
  (distinct from `identify_unknown`, which names a gap without proposing an
  answer to it, and from `generate_option`, which is specifically about
  decision options, not explanatory guesses)

**Every trace step must set `observation_index`**, pointing at the specific
entry in this same response's `observations[]` array that the step is
evidence for. Observations have no stable name to reference by, so the
index is positional — validated against the actual length of `observations[]`
at persist time; an out-of-range index is rejected outright rather than
silently falling back to "the first observation." This is the mechanical
form of the hard constraint above: a trace with no citable observation index
is a trace that does not get written.

---

## 6. situation

A situation is a **recurring class of circumstance**, never a single occurrence.

- "Facing an irreversible commitment under time pressure" → a situation
- "Deciding whether to incorporate Phynx" → an episode instantiating it

Rules:

1. Do not create a situation from one episode. Emit the episode, and let
   `situation_discovery` consolidation propose the class once three or more
   similar episodes exist.
2. The `signature` is a feature dict, not prose. Features should be things the
   query-time matcher can also produce: `irreversible`, `time_pressure`,
   `incomplete_information`, `social_stakes`, `financial_exposure`,
   `technical_ambiguity`, `reputational_risk`.
3. Name situations by their shape, not their content. "Evaluating a new tool
   under deadline" generalizes. "The Qdrant decision" does not.

`situation_strategies` records what he reached for and how it went. Negative
outcomes are recorded at half rate but never suppressed, because "you usually do
this here and it usually goes badly" is the most valuable sentence this system
can produce.

---

## 7. Entity resolution

Order of operations, cheapest first:

1. Exact match on `entity_aliases.alias`
2. Trigram similarity above 0.6 on `canonical_name` and aliases
3. Model adjudication, but only for candidates that survived step 2

Never merge on name similarity alone for `kind = 'person'`. Two people share a
first name constantly, and an incorrect person merge corrupts every relationship,
association and memory attached to both, in a way that is tedious to unpick even
with `entity_resolutions` recording the merge.

When confidence sits between 0.5 and 0.8, create the separate entity and record a
`possible_duplicate` relationship. Consolidation can merge later with more
evidence. Splitting a bad merge is harder than merging two rows.

### 7.1 Linking entities to memories (`memory_entities.role`)

Every entity a memory is about must be named in that memory's
`involved_entities`, referencing a `canonical_name` that also appears in the
top-level `entities[]` array — an entity mentioned only inside a memory and
never separately extracted does not get an `INVOLVES` edge, because there is
nothing canonical to link it to.

`role` is optional (`null` when the extractor cannot tell) and one of:

- `subject` — the memory is primarily about this entity ("Priya's promotion")
- `object` — the entity is acted upon or is the target ("gave Priya feedback")
- `context` — the entity is present but incidental ("met at Priya's office")

Do not invent a fourth role. When unsure between `subject` and `object`,
prefer `subject` for the entity the sentence's grammatical subject refers to.

---

## 8. Emotion

Only `emotion_is_stated = true` counts for anything. The salience function zeroes
inferred emotion entirely.

- "That was frustrating" → stated, intensity from the language used
- Terse replies and a short sentence → **not** evidence of frustration

The system may note affect as an observation with
`epistemic_status = 'inferred'`, and it must never let that observation reach the
memory salience calculation or a belief update. Inferring mood from writing style
is the single easiest way to build a system that confidently misunderstands its
owner.

**Intensity calibration — linguistic anchors.**

Use these anchors to set `emotional_intensity`. Apply them only when
`emotion_is_stated = true`. Never interpolate from brevity or tone.

| Range | Anchors | Examples |
| --- | --- | --- |
| 0.2–0.3 (mild) | "a bit", "slightly", "somewhat", "a little" | "a bit frustrated", "slightly annoyed" |
| 0.4–0.5 (moderate) | bare verb with no intensifier | "felt frustrated", "was nervous", "enjoyed it" |
| 0.6–0.7 (significant) | "really", "quite", "rather", "pretty" | "really enjoyed it", "quite frustrated" |
| 0.8–0.9 (strong) | "very", "extremely", "deeply", "terribly" | "very relieved", "deeply uncomfortable" |
| 1.0 (overwhelming) | single-word peak states | "devastated", "ecstatic", "overwhelmed", "elated" |

`emotional_valence` encodes direction: negative (−1 to 0) for fear, sadness,
anger, frustration, and anxiety; positive (0 to +1) for joy, relief,
satisfaction, excitement, and pride.

**Stated vs. inferred — the hard line:**

| Stated (emotion_is_stated = true) | Inferred (NEVER use) |
| --- | --- |
| "I felt frustrated" | Short/terse message |
| "That was exciting" | No exclamation but positive topic |
| "I was devastated" | Long gap before reply |
| "Really glad it worked out" | "Must have been happy" |

If the text does not name an emotion, `emotion_is_stated` is `false` and
`emotional_valence` and `emotional_intensity` must both be `null`. There is
no in-between.

---

## 9. Belief revision

When the owner explicitly revises a belief ("I used to think X, but now I
think Y" or equivalent):

1. **Record both sides as observations.** The old view gets
   `epistemic_status = 'retracted'`; the new view gets `epistemic_status =
   'asserted'`. Both are sourced to the same experience.

2. **Create the new belief** as a fresh belief row with the new proposition,
   the correct `belief_kind`, and evidence pointing at the `asserted`
   observation.

3. **Search for the old belief** in the database (by proposition similarity
   or entity). If found:
   - Add a `contradicts` evidence row against the old belief, sourced to the
     `retracted` observation.
   - Let `apply_evidence` in `dynamics.py` recompute its status. It may move
     to `weakened` or `contradicted` without being deleted.
   - Record a `belief_relations` row: `(new_belief_id, old_belief_id,
     relation='supersedes')`.

4. **If no matching old belief exists** (first ingest, no prior state): store
   the `retracted` observation alone. It is evidence that will be available
   when the old belief is eventually created via earlier ingestion.

**Never delete the old belief.** The belief trajectory — "he used to think X,
now thinks Y" — is precisely what this system is for. Deletion destroys the
record of a mind changing.

**Reflection episodes are the primary trigger for this procedure.** When
`episode_candidate.kind = 'reflection'` and the text contains a revision
("I used to think X, but now I think Y"), run the belief-revision procedure
above exactly as written — an episode of kind `reflection` does not get its
own separate belief-handling path. The episode's `narrative`/`resolution`
may summarize *that* a revision happened; the revision itself is still
recorded the normal way, as observations and a new belief with a
`supersedes` relation, never as free text on the episode.

---

## 10. Episode internals: goal, action, outcome, decision

Everything below is about what goes *inside* an episode once you've decided
one exists (§4 governs whether one exists at all). None of it introduces new
storage beyond `migrations/007_episode_model.sql`; each concept is deliberately
routed to something that already carries provenance rather than becoming a
free-floating field.

**Goal.** Never `episode_candidate.goal` — that field does not exist. A goal
discovered during an episode is an `episode_state_items[]` entry with
`kind='goal'`, which persists as a `state_items` row with `episode_id` set.
The same applies to `attention`, `question`, and `uncertainty` — all reuse
the existing `state_item_kind` values, scoped to the episode instead of
standing owner-level state. Do not write the goal into `episode_candidate.narrative`
as well as a state item; narrative is for the prose account, the state item
is the structured, evidence-linked fact.

**Action.** Never a new field. The concrete thing the owner actually did as
a result of the episode is an ordinary episodic memory
(`memory_system='episodic'`) with `is_episode_action=true` set. At most one
memory per episode may carry this flag. If nothing was actually done yet
(the episode ended in a decision but no action followed in the text), do
not invent one — leave every memory's `is_episode_action` false.

**Decision.** A decision is never merged into the episode or into a
`cognitive_traces` row. Emit all three when they're all present:
- the episode (`episode_candidate`) is the situation — what was going on
- a `cognitive_traces` row with `operation='decide'` is the observable
  moment of deciding, cited to its own source span like any other trace step
- `decision` is the decision's own structure: the question, the options
  actually considered, what was chosen and why, its reversibility, and
  (eventually) its outcome

These three answer different questions ("what was happening", "what did he
visibly do", "what was decided and why") and collapsing any two of them
loses information the others can't recover.

**Outcome invariant.** `decision.outcome`/`decision.reflection` are
canonical whenever a `decision` object is present — `episode_candidate.outcome`
MUST be null in that case (enforced by `core/extractor.py`, not just this
convention). `episode_candidate.outcome` exists only for episodes with no
decision at all — a `learning`, `reflection`, `social`, `creative`, or
`routine` episode that nonetheless has a clear result ("the code review
went well", "we didn't reach the CFO in time"). Do not populate both.

**Knowns are not a field.** What the owner already knew going into or during
the episode is simply whichever observations/beliefs the episode's
`cognitive_traces` steps cite via `observation_index`, plus any memory or
belief already on record about the same entities. There is nothing to
extract here beyond citing the right `observation_index` on each trace step.

### 10.1 Timestamp semantics (current state, deliberately not yet redesigned)

Only one timestamp in the episode layer is genuinely meaningful today:

- **`occurred_at`** — the event time of the experience. Passed into
  `ingest()` by the caller, defaults to now(). This is the real one.
- **`episodes.started_at`** — set to the experience's `occurred_at`.
- **`cognitive_traces.occurred_at`** — also set to the experience's
  `occurred_at`. Trace steps therefore all share one timestamp and are
  ordered by `seq`, not by time. `seq` is the ordering authority; do not
  read trace timestamps as a real chronology.
- **`decisions.decided_at`** — set to `occurred_at` when `chosen_option`
  is non-null, otherwise NULL. It approximates "the decision was made in
  this experience," not a separately observed moment.
- **`decisions.outcome_recorded_at` / `episodes.outcome_recorded_at`** —
  set to `occurred_at` when an outcome is present. This is the weakest
  approximation of the set: an outcome described retrospectively ("three
  months ago I decided X, and it worked out") is recorded with the
  *narrating* experience's timestamp, not the time the outcome actually
  became apparent.

**Retrospective outcome-recording is deferred.** Properly modelling "this
outcome became known at time T, about a decision made at time T-90d"
requires either updating an existing decision row from a later experience
(a write path that does not exist — nothing currently amends a prior
decision) or a separate outcome-observation object. Neither is built. Until
then, treat `decided_at` and `outcome_recorded_at` as *provenance of when
this was written down*, not as measured event times, and do not compute
durations from them.

---

## Review loop

For the first few hundred experiences, sample twenty per week and check three
things by hand:

1. Did any "I learned X" input get written as a single memory instead of split?
2. Did any "I think X about the world" get classified as `self_report`?
3. Did any trace step get written without a source span?

Those three are the failure modes that compound silently. Everything else in the
store can be re-derived from the raw archive once the rules improve, which is the
whole reason the experience table is immutable.
