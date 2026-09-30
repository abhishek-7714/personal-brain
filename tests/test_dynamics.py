"""
tests/test_dynamics.py

Executable verification of the rules layer. Runs with no database:

    python tests/test_dynamics.py

Covers the epistemics cases, the per-system memory rules, consolidation and
activation. The database-level counterparts live in tests/invariants.sql.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root so `core` is importable as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.dynamics as d  # noqa: E402

NOW = datetime.now(timezone.utc)
FAILURES: list[str] = []
PASSES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSES.append(label)
        print(f"PASS  {label}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILURES.append(label)
        print(f"FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ===========================================================================
section("1. EPISTEMICS: the two axes")
# ===========================================================================

owner_says = [d.EvidenceItem(d.Stance.SUPPORTS, d.EpistemicOwner.OWNER,
                             extraction_confidence=0.95, observed_at=NOW)]
authoritative = [d.EvidenceItem(d.Stance.SUPPORTS, d.EpistemicOwner.AUTHORITATIVE_SOURCE,
                                extraction_confidence=0.95, observed_at=NOW)]

# 1.1 owner says the startup will work (a world claim)
r1 = d.recompute_belief(d.BeliefKind.WORLD_CLAIM, owner_says)
check("1.1 owner world claim: holding exceeds truth",
      r1.holding_confidence > r1.truth_confidence,
      f"holding={r1.holding_confidence:.3f} truth={r1.truth_confidence:.3f}")
check("1.1b owner world claim: holding is high",
      r1.holding_confidence > 0.70, f"{r1.holding_confidence:.3f}")

# 1.2 owner reports own preference (a self report): the axes coincide
r2 = d.recompute_belief(d.BeliefKind.SELF_REPORT, owner_says)
check("1.2 self report: the two axes coincide",
      abs(r2.holding_confidence - r2.truth_confidence) < 1e-9,
      f"holding={r2.holding_confidence:.3f} truth={r2.truth_confidence:.3f}")

# 1.3 authoritative source makes a world claim: truth moves, holding barely does
r3 = d.recompute_belief(d.BeliefKind.WORLD_CLAIM, authoritative)
check("1.3 authoritative world claim: truth exceeds holding",
      r3.truth_confidence > r3.holding_confidence,
      f"holding={r3.holding_confidence:.3f} truth={r3.truth_confidence:.3f}")
check("1.3b authoritative source barely moves holding",
      abs(r3.holding_confidence - 0.5) < 0.05, f"{r3.holding_confidence:.3f}")

# 1.4 THE KEY CASE: firmly held, evidence against, still active
contradicted = owner_says + [
    d.EvidenceItem(d.Stance.CONTRADICTS, d.EpistemicOwner.AUTHORITATIVE_SOURCE,
                   extraction_confidence=0.9, observed_at=NOW)
    for _ in range(3)
]
r4 = d.recompute_belief(d.BeliefKind.WORLD_CLAIM, contradicted)
check("1.4 contradicted world claim REMAINS ACTIVE",
      r4.status == "active", f"status={r4.status}")
check("1.4b holding stays high",
      r4.holding_confidence > 0.6, f"holding={r4.holding_confidence:.3f}")
check("1.4c world truth collapses",
      r4.truth_confidence < 0.15, f"truth={r4.truth_confidence:.3f}")
check("1.4d the divergence is flagged for retrieval",
      "world evidence" in r4.reason, r4.reason)

# 1.5 the axes are genuinely independent, not one derived from the other
check("1.5 axes are independent quantities",
      r1.holding_log_odds != r1.truth_log_odds and r4.holding_log_odds > 0 > r4.truth_log_odds,
      f"h={r4.holding_log_odds:.3f} t={r4.truth_log_odds:.3f}")

# 1.6 system inference cannot outweigh an owner assertion
inferences = [d.EvidenceItem(d.Stance.CONTRADICTS, d.EpistemicOwner.SYSTEM_INFERENCE,
                             extraction_confidence=1.0, observed_at=NOW) for _ in range(5)]
r6 = d.recompute_belief(d.BeliefKind.SELF_REPORT, owner_says + inferences)
check("1.6 five inferences do not overturn one owner assertion",
      r6.holding_confidence > 0.5, f"holding={r6.holding_confidence:.3f}")

# 1.7 recomputation is order independent, so a full rebuild is safe
import random  # noqa: E402
shuffled = contradicted[:]
random.Random(7).shuffle(shuffled)
r7 = d.recompute_belief(d.BeliefKind.WORLD_CLAIM, shuffled)
check("1.7 recomputation is order independent",
      abs(r7.truth_log_odds - r4.truth_log_odds) < 1e-9)

# 1.8 no amount of evidence produces certainty
flood = [d.EvidenceItem(d.Stance.SUPPORTS, d.EpistemicOwner.OWNER, observed_at=NOW)] * 200
r8 = d.recompute_belief(d.BeliefKind.SELF_REPORT, flood)
check("1.8 confidence never reaches 1.0",
      r8.holding_confidence < 1.0, f"{r8.holding_confidence:.6f}")

# 1.9 attributed beliefs point the holding axis at someone else
r9 = d.recompute_belief(
    d.BeliefKind.ATTRIBUTED,
    [d.EvidenceItem(d.Stance.SUPPORTS, d.EpistemicOwner.OTHER_PERSON, observed_at=NOW)])
check("1.9 attributed belief: holding moves, world truth barely does",
      r9.holding_confidence > r9.truth_confidence,
      f"holding={r9.holding_confidence:.3f} truth={r9.truth_confidence:.3f}")


# ===========================================================================
section("2. MEMORY SYSTEMS")
# ===========================================================================

check("2.1 exactly three memory systems",
      {m.value for m in d.MemorySystem} == {"episodic", "semantic", "procedural"},
      str(sorted(m.value for m in d.MemorySystem)))

check("2.2 no memory system means 'something I learned'",
      not any(m.value in {"lesson", "belief", "cognitive"} for m in d.MemorySystem))

# 2.3 episodic memories fade in accessibility but are never stale
old_episodic = d.Candidate(
    ref="memory:ep", memory_system=d.MemorySystem.EPISODIC,
    semantic=0.8, occurred_at=NOW - timedelta(days=1500), staleness=1.0)
old_semantic = d.Candidate(
    ref="memory:sem", memory_system=d.MemorySystem.SEMANTIC,
    semantic=0.8, occurred_at=NOW - timedelta(days=1500), staleness=1.0)
ep_score = d.activation_score(old_episodic, NOW)
sem_score = d.activation_score(old_semantic, NOW)
check("2.3 staleness penalty does not apply to episodic memories",
      ep_score > sem_score, f"episodic={ep_score:.3f} semantic={sem_score:.3f}")

# 2.4 retrievability decays, and rehearsal slows the decay
cold = d.episodic_retrievability(None, NOW - timedelta(days=1095), 0, NOW)
rehearsed = d.episodic_retrievability(None, NOW - timedelta(days=1095), 20, NOW)
check("2.4 episodic retrievability decays with time",
      cold < 0.5, f"{cold:.3f}")
check("2.4b rehearsal flattens the decay curve",
      rehearsed > cold, f"cold={cold:.3f} rehearsed={rehearsed:.3f}")

# 2.5 semantic memories do go stale, at kind-specific rates
person_stale = d.semantic_staleness("person_attribute", NOW - timedelta(days=365), NOW)
concept_stale = d.semantic_staleness("concept", NOW - timedelta(days=365), NOW)
check("2.5 semantic memories become stale",
      person_stale > 0.5, f"person_attribute={person_stale:.3f}")
check("2.5b staleness is kind specific: attributes age faster than concepts",
      person_stale > concept_stale, f"{person_stale:.3f} vs {concept_stale:.3f}")

# 2.6 procedural memory is distinct: proficiency, not validity
decayed = d.procedural_proficiency(0.9, NOW - timedelta(days=365), False, NOW)
restored = d.procedural_proficiency(0.9, NOW - timedelta(days=365), True, NOW)
check("2.6 proficiency decays with disuse", decayed < 0.5, f"{decayed:.3f}")
check("2.6b practice restores proficiency", restored > decayed,
      f"decayed={decayed:.3f} practiced={restored:.3f}")

# 2.7 promotion thresholds differ by system: episodic is admitted more freely
signals = d.SalienceSignals(novelty=0.6, goal_relevance=0.4, decision_linked=0.5)
ep_ok, ep_s = d.should_promote_to_memory(signals, d.MemorySystem.EPISODIC)
sem_ok, sem_s = d.should_promote_to_memory(signals, d.MemorySystem.SEMANTIC)
check("2.7 identical salience, different admission by system",
      ep_ok and not sem_ok, f"salience={ep_s:.3f} episodic={ep_ok} semantic={sem_ok}")

# 2.8 inferred emotion never counts toward salience
# novelty defaults to 0.5, so it must be zeroed to isolate the emotional channel.
stated = d.SalienceSignals(novelty=0.0, emotional=1.0, emotion_is_stated=True)
inferred = d.SalienceSignals(novelty=0.0, emotional=1.0, emotion_is_stated=False)
check("2.8 inferred emotion contributes exactly nothing to salience",
      d.salience(inferred) == 0.0
      and abs(d.salience(stated) - d.SALIENCE_WEIGHTS["emotional"]) < 1e-9,
      f"stated={d.salience(stated):.3f} inferred={d.salience(inferred):.3f}")


# ===========================================================================
section("3. CONSOLIDATION")
# ===========================================================================

check("3.1 one instance never consolidates", not d.should_consolidate(1, 0.99)[0])
check("3.2 dissimilar instances never consolidate", not d.should_consolidate(20, 0.4)[0])
ok, conf = d.should_consolidate(8, 0.88)
check("3.3 repeated similar instances do consolidate", ok, f"confidence={conf:.3f}")
check("3.4 consolidation requires the minimum instance count",
      d.MIN_INSTANCES_FOR_CONSOLIDATION >= 4, str(d.MIN_INSTANCES_FOR_CONSOLIDATION))


# ===========================================================================
section("4. ACTIVATION AND RE-RANKING")
# ===========================================================================

candidates = [
    d.Candidate(f"memory:{i}", channel="semantic", semantic=0.9 - i * 0.02,
                occurred_at=NOW, memory_system=d.MemorySystem.SEMANTIC)
    for i in range(60)
] + [
    d.Candidate("belief:contra", channel="graph", semantic=0.05,
                occurred_at=NOW - timedelta(days=800), is_contradiction=True)
]

ranked = d.rank_and_bound(candidates, max_items=20, as_of=NOW)
refs = {c.ref for c, _ in ranked}
check("4.1 the context set is bounded", len(ranked) <= 20, f"{len(ranked)} items")
check("4.2 a weak contradiction survives a flood of strong support",
      "belief:contra" in refs)
check("4.3 results are ordered by score",
      all(ranked[i][1] >= ranked[i + 1][1] for i in range(len(ranked) - 1)))

# 4.4 the situation channel contributes independently
without = d.activation_score(d.Candidate("m:1", semantic=0.5), NOW)
with_sit = d.activation_score(d.Candidate("m:1", semantic=0.5, situation=0.9), NOW)
check("4.4 situation-conditioned activation raises the score",
      with_sit > without, f"{without:.3f} -> {with_sit:.3f}")

# 4.5 a strategy that historically went badly is still surfaced, at lower weight
good = d.strategy_activation(0.8, 0.9, 0.7)
bad = d.strategy_activation(0.8, 0.9, -0.7)
check("4.5 failed strategies are down-weighted but not suppressed",
      0 < bad < good, f"bad={bad:.3f} good={good:.3f}")


# ===========================================================================
section("5. PATTERN AND SITUATION PROMOTION")
# ===========================================================================

check("5.1 one episode cannot establish a pattern",
      d.pattern_epistemic_status(1, 0) == "hypothesis")
check("5.2 a supported pattern is inferred, never asserted",
      d.pattern_epistemic_status(9, 1) == "inferred")
check("5.3 a contradicted pattern is marked so",
      d.pattern_epistemic_status(5, 9) == "contradicted")
check("5.4 situations stay hypotheses below the instance floor",
      d.situation_epistemic_status(2) == "hypothesis"
      and d.situation_epistemic_status(5) == "inferred")


# ===========================================================================
print()
print("=" * 58)
print(f"  {len(PASSES)} passed, {len(FAILURES)} failed")
print("=" * 58)
if FAILURES:
    for f in FAILURES:
        print(f"  FAILED: {f}")
    sys.exit(1)
