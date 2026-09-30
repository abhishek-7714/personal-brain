# Handoff: Neural Brain Visualization — Milestone 1 (Graph Mode)

_Written 2026-09-01, end of the session that implemented this. Read this
before continuing the Neural Brain Visualization work._

## What this milestone is

A full request (22 sections) asked for a cinematic, WebGL, biologically-
inspired visualization of the Personal Brain's activation/retrieval process
— live "current flowing through neurons," a Graph/Brain mode toggle, query
history/replay, an anatomical layer, debug tooling, the works.

We scoped this session down to one milestone, confirmed with the owner
before writing any code (see the two `AskUserQuestion` decisions below) and
explicitly deferred the rest. The full original request and the approved
plan are preserved at `C:\Users\abhis\.claude\plans\modular-moseying-wave.md`
— read that first if you need the complete spec text or the original
reasoning; this doc only covers what actually landed.

**Scope decisions made with the owner:**
- Graph Mode only. Brain/anatomical mode is **deferred** — the toggle
  button exists in the UI (`GraphModeToggle.tsx`) but is disabled.
- Query history panel + persisted-trace replay endpoint are **deferred**.
- Hard constraint from the owner: the visualization's central "query" node
  must stay a transient UI-only object, never written to Postgres/Neo4j/
  Qdrant as a real entity/memory/node. This is enforced in code (see below)
  and saved as a standing project memory
  (`feedback_visualization_query_node.md`).

## What was built

**One live query animates end to end**: query → seed entities fire →
pulses propagate along real Neo4j/Qdrant/Postgres-derived edges →
convergence (multiple sources lighting the same target) → decay to a
subtly-lit settled state. Verified visually via headless Chrome (see
Verification below) and via the automated test suites.

### Backend (Python) — additive only, no ranking/scoring changes

| File | Change |
|---|---|
| `core/retrieval.py` | `HybridQueryResult` gained one field, `all_seed_ids` — a local variable `hybrid_query()` already computed, now returned instead of discarded. |
| `api/routers/retrieval.py` | Added `_resolve_entities()` and `_build_activation_events()` (pure serialization — no new scoring/traversal). `entity_ids` added to serialized candidates. `POST /api/retrieval/query` response gained `all_seed_entities` and `activation_events`. |
| `tests/test_activation_events.py` | **New.** Pure unit tests for `_build_activation_events()` — no live stack needed. 17 checks. |
| `tests/test_api.py` | Added a `NEURAL BRAIN VISUALIZATION` section exercising the new response fields against the real `priya_kubernetes_mixed` fixture over the live stack. |

One real gap was caught and fixed mid-session: `resolve_seed_entities()`
only returns name-matched seeds, but `graph_retrieve()` also walks entities
pulled from top semantic hits (`extra_seed_ids` inside `hybrid_query()`).
Those extra seeds were never resolved to `{canonical_name, kind}` for the
API response, which would have left the frontend with graph edges pointing
at seed nodes it knew nothing about. Fixed by resolving the **full**
`all_seed_ids` set (`_resolve_entities()`), not just the text-matched
subset — response field is `all_seed_entities`, a superset of the older
`seed_entities`.

`activation_events` shape: `{source, target, channel, strength, hops, reason}`.
`source`/`target` are either the literal string `"query"` (never persisted,
never a `source_type`/`source_id` — this is the owner's transient-node
constraint) or `"entity:<uuid>"` / `"<source_type>:<source_id>"`, always
resolvable back to a real Postgres/Neo4j row.

### Frontend (Next.js / React Three Fiber) — new stack for this repo

`web/package.json` gained `three`, `@react-three/fiber`, `@react-three/drei`
(none previously installed) and `vitest` + `@testing-library/react` for
frontend unit testing (also new to this repo).

| Layer | Files |
|---|---|
| Pure data transform (unit-tested, no DOM/network) | `web/lib/activation.ts`, `web/lib/activation.test.ts` (18 tests) |
| React bridge | `web/lib/useActivationPlayback.ts` |
| R3F renderer | `web/components/brain/{BrainCanvas,NeuronField,ConnectionLines,PulseSystem,ClockDriver,clock,brightness,theme,IdleField}.tsx/.ts` |
| Interaction | `web/components/brain/{NodeInspector,DebugOverlay,GraphModeToggle}.tsx` |
| Pages | `web/app/brain/page.tsx` (new full-screen view, nav entry "6. Neural Brain"), `web/app/retrieval/page.tsx` (embedded Activation Visualization + channel bars + active memories, wired to the same node inspector) |
| Types | `web/lib/api.ts` extended: `ActivationEventJSON`, `SeedEntityJSON`, `entity_ids`, `all_seed_entities`, `activation_events` |

Key design points, if you pick this back up:
- **Clock is a `useRef`, not React state** (`clock.ts`/`ClockDriver.tsx`).
  Every renderer component reads `clockRef.current.tMs` inside its own
  `useFrame()`. This is what lets the scene animate at 60fps without a
  React re-render every frame — don't "fix" this into `useState`.
- **Layout is deterministic and pure** (`computeLayout()` in
  `activation.ts`) — no force-simulation library. Query at origin, seeds on
  a fixed ring, candidates angled toward the circular mean of the seeds
  that activated them, radius shrinking with score. Swappable later without
  touching any renderer component (they only ever consume `{id,x,y,z}`).
- **`BrainCanvas` is loaded via `next/dynamic(..., { ssr: false })`** in
  both pages. R3F's `<Canvas>` crashes Next.js's static-prerender build
  worker (access violation, not a normal error) if imported directly —
  this is the standard fix for any R3F component under the Next app router.
  Don't remove the dynamic import.
- **`computeAnatomicalLayoutStub()`** in `activation.ts` exists only to
  prove, at the data layer, that a future Brain Mode would reposition the
  exact same node set rather than needing different objects — see the
  "Graph Mode vs Brain Mode share the same underlying objects" tests. It is
  not wired into any UI yet.

## Verification performed this session

- `python tests/test_activation_events.py` — 17/17 (pure, no stack).
- `python tests/test_api.py` — 38/38 against the live Docker stack.
- `cd web && npx vitest run` — 18/18.
- `cd web && npm run build` — succeeds.
- Headless Chrome (via a one-off `puppeteer-core` script in the session's
  scratchpad, since the Claude-in-Chrome extension wasn't connected):
  confirmed idle ambience, live propagation/convergence with real fixture
  data, node-click → inspector drawer (including the transient-query-node
  disclaimer text), and the full Retrieval Playground integration
  (channels bars + active memories list). Screenshots are in that session's
  scratchpad temp directory, not the repo.

## Environment quirks you should know about

- **A stray API process was already running on port 8000 with a wrong
  `WEB_ORIGIN`** (pre-existing, not caused by this work) — it 400s every
  browser CORS preflight, which silently broke *every* console page's live
  calls, not just this feature. It could not be killed this session
  (`Stop-Process` → `Access is denied`, likely owned by a different Windows
  session/user).
- **Workaround in place**: a second API instance was started on **port
  8001** with correct CORS, and `web/.env.local` (gitignored, not in git
  status) sets `NEXT_PUBLIC_API_BASE=http://localhost:8001/api` so the dev
  frontend talks to it. This incidentally fixes browser access for the
  whole console, not just the new pages.
- **To return to normal**: find and stop whatever owns port 8000
  (`netstat -ano | grep :8000` for the PID, then investigate/stop it with
  sufficient privileges), then either delete `web/.env.local` to go back to
  the default `:8000`, or just keep using `:8001` — both work.
- Both the web dev server (`:3000`) and the extra API instance (`:8001`)
  were left running at the end of this session so the feature is
  immediately usable; nothing else was started or left behind.

## What's deferred (not started)

- **Brain Mode / anatomical layer** — biologically-inspired region mapping,
  disclaimer UI text (spec explicitly requires "biologically inspired
  computational visualization" language, never "this is how the owner's
  brain physically works"), cluster layout. `computeAnatomicalLayoutStub()`
  is the only groundwork laid.
- **Query history panel + persisted-trace replay** — needs a new
  `GET /api/retrieval/trace/{id}` endpoint reading `activation_traces`/
  `activation_members` (already written by `record_activation_trace()`,
  untouched this session) and a small "recent queries" UI that replays from
  that persisted data rather than re-running a live query.
- Everything else in the original 22-section request beyond items 1–7, 9,
  11, 12 of the deliverable list (see the plan file for the full mapping).

## Do not re-touch (per CLAUDE.md, unaffected by this work anyway)

Ontology, Postgres canonical schema, Neo4j schema, Qdrant collections,
retrieval scoring formula (`core/dynamics.py`), activation scoring formula,
epistemic model, memory semantics. Nothing in this milestone changed any of
these — every backend change was additive serialization/plumbing.
