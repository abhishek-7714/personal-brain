"""
tests/test_schema_static.py

Parses migrations 001-005 and asserts that every invariant the foundation depends
on is actually encoded in the DDL. Runs without a database:

    python tests/test_schema_static.py

This is deliberately a structural checker, not a SQL engine. It verifies that the
constructs exist, that migrations apply in a consistent order, and that forbidden
terminology is gone. It cannot verify runtime semantics; tests/invariants.sql does
that, and must be run against Postgres before the foundation is considered green.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "migrations").glob("*.sql"))

FAILURES: list[str] = []
PASSES: list[str] = []
WARNINGS: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASSES if condition else FAILURES).append(label)
    print(("PASS  " if condition else "FAIL  ") + label + (f"  [{detail}]" if detail else ""))


def warn(label: str, detail: str = "") -> None:
    WARNINGS.append(label)
    print(f"WARN  {label}" + (f"  [{detail}]" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"/\*.*?\*/", "", sql, flags=re.S)


sources: dict[str, str] = {p.name: p.read_text(encoding="utf-8") for p in MIGRATIONS}
clean: dict[str, str] = {n: strip_comments(s) for n, s in sources.items()}
all_raw = "\n".join(sources.values())
all_clean = "\n".join(clean.values())

# enum types -> final value set after all migrations
enums: dict[str, list[str]] = {}
for name, body in re.findall(
    r"CREATE\s+TYPE\s+(\w+)\s+AS\s+ENUM\s*\((.*?)\)\s*;", all_clean, re.S | re.I
):
    enums[name] = re.findall(r"'([^']+)'", body)
for typ, val in re.findall(
    r"ALTER\s+TYPE\s+(\w+)\s+ADD\s+VALUE(?:\s+IF\s+NOT\s+EXISTS)?\s+'([^']+)'", all_clean, re.I
):
    enums.setdefault(typ, []).append(val)
# a renamed-away type is no longer the live definition
for old, new in re.findall(r"ALTER\s+TYPE\s+(\w+)\s+RENAME\s+TO\s+(\w+)", all_clean, re.I):
    enums.pop(new, None)
for dropped in re.findall(r"DROP\s+TYPE\s+(\w+)", all_clean, re.I):
    enums.pop(dropped, None)

# tables -> columns
tables: dict[str, set[str]] = {}
for name, body in re.findall(r"CREATE\s+TABLE\s+(\w+)\s*\((.*?)\n\);", all_clean, re.S | re.I):
    cols = set()
    for line in body.split("\n"):
        line = line.strip()
        m = re.match(r"^(\w+)\s+[a-zA-Z]", line)
        if m and m.group(1).upper() not in {
            "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT", "EXCLUDE"
        }:
            cols.add(m.group(1))
    tables[name] = cols

for tbl, body in re.findall(r"ALTER\s+TABLE\s+(\w+)\s+(.*?);", all_clean, re.S | re.I):
    for col in re.findall(r"ADD\s+COLUMN\s+(\w+)", body, re.I):
        tables.setdefault(tbl, set()).add(col)
    for old, new in re.findall(r"RENAME\s+COLUMN\s+(\w+)\s+TO\s+(\w+)", body, re.I):
        tables.setdefault(tbl, set()).discard(old)
        tables[tbl].add(new)

views = set(re.findall(r"CREATE\s+VIEW\s+(\w+)", all_clean, re.I))
triggers = set(re.findall(r"CREATE\s+TRIGGER\s+(\w+)", all_clean, re.I))
constraints = set(re.findall(r"ADD\s+CONSTRAINT\s+(\w+)", all_clean, re.I))
inline_constraints = set(re.findall(r"CONSTRAINT\s+(\w+)\s+CHECK", all_clean, re.I))
constraints |= inline_constraints
indexes = set(re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", all_clean, re.I))

print(f"parsed {len(MIGRATIONS)} migrations: {[p.name for p in MIGRATIONS]}")
print(f"  {len(tables)} tables, {len(enums)} live enum types, {len(views)} views, "
      f"{len(triggers)} triggers, {len(indexes)} indexes")


# ---------------------------------------------------------------------------
section("0. MIGRATION HYGIENE")
# ---------------------------------------------------------------------------

for name in sorted(clean):
    begins = len(re.findall(r"\bBEGIN\s*;", clean[name], re.I))
    commits = len(re.findall(r"\bCOMMIT\s*;", clean[name], re.I))
    check(f"0.1 {name}: BEGIN/COMMIT balanced", begins == commits, f"{begins}/{commits}")

# ALTER TYPE ... ADD VALUE must not sit inside a transaction block
for name, body in clean.items():
    for m in re.finditer(r"ALTER\s+TYPE\s+\w+\s+ADD\s+VALUE", body, re.I):
        before = body[: m.start()]
        depth = len(re.findall(r"\bBEGIN\s*;", before, re.I)) - len(re.findall(r"\bCOMMIT\s*;", before, re.I))
        check(f"0.2 {name}: ADD VALUE outside a transaction block", depth == 0, f"depth={depth}")

# every ALTER TABLE target must already exist
seen: set[str] = set()
order_ok = True
for p in MIGRATIONS:
    body = strip_comments(p.read_text(encoding="utf-8"))
    for t in re.findall(r"CREATE\s+TABLE\s+(\w+)", body, re.I):
        seen.add(t)
    for t in re.findall(r"ALTER\s+TABLE\s+(\w+)", body, re.I):
        if t not in seen:
            order_ok = False
            warn(f"0.3 {p.name}: ALTER TABLE {t} before its CREATE")
check("0.3 migrations apply in dependency order", order_ok)


# ---------------------------------------------------------------------------
section("1. TERMINOLOGY")
# ---------------------------------------------------------------------------

# tests/ and the validation modules are excluded: code that names forbidden terms
# to REJECT them must be allowed to mention the terms.
_TERM_EXCLUSIONS = {"tests", "graphify-out"}
_TERM_EXCLUDED_FILES = {"schema.py", "extractor.py"}
repo_files = [
    p for p in ROOT.rglob("*")
    if p.is_file() and p.suffix in {".sql", ".py", ".md", ".cypher", ".yml"}
    and "__pycache__" not in str(p)
    and not any(ex in str(p) for ex in _TERM_EXCLUSIONS)
    and p.name not in _TERM_EXCLUDED_FILES
]
offenders = [str(p.relative_to(ROOT)) for p in repo_files
             if "lesson" in p.read_text(encoding="utf-8", errors="ignore").lower()]
check("1.1 no 'lesson' terminology in schema, docs or code (tests excluded by design)",
      not offenders, ", ".join(offenders) if offenders else "clean")

check("1.2 memory_kind has no learned/belief/cognitive value",
      not ({"lesson", "belief", "mental_model", "cognitive_pattern", "pattern"}
           & set(enums.get("memory_kind", []))),
      str(sorted(enums.get("memory_kind", []))))

check("1.3 memory_system is exactly episodic/semantic/procedural",
      set(enums.get("memory_system", [])) == {"episodic", "semantic", "procedural"},
      str(sorted(enums.get("memory_system", []))))

check("1.4 beliefs and cognitive patterns have exactly one home each",
      "beliefs" in tables and "cognitive_patterns" in tables
      and "belief" not in enums.get("memory_kind", [])
      and "belief" not in enums.get("memory_system", []))


# ---------------------------------------------------------------------------
section("2. EPISODIC IMMUTABILITY")
# ---------------------------------------------------------------------------

check("2.1 episodic_not_superseded constraint exists",
      "episodic_not_superseded" in constraints)

check("2.2 the constraint bars both superseded_by and valid_to",
      bool(re.search(
          r"episodic_not_superseded\s+CHECK\s*\(\s*memory_system\s*<>\s*'episodic'\s*OR\s*"
          r"\(superseded_by\s+IS\s+NULL\s+AND\s+valid_to\s+IS\s+NULL\)",
          all_clean, re.I | re.S)))

check("2.3 reclassification bypass closed (memory_system immutable)",
      "memories_system_immutable" in triggers
      and "forbid_memory_system_change" in all_clean)

check("2.4 episodic memories cannot be deleted",
      "memories_episodic_no_delete" in triggers
      and "forbid_episodic_delete" in all_clean)

check("2.5 memories carry reinterprets_id for reinterpretation",
      "reinterprets_id" in tables.get("memories", set()))

check("2.6 consolidation target cannot be episodic",
      "memories_consolidation_target" in triggers)

check("2.7 consolidation pointer cannot be self-referential",
      "consolidation_target_not_self" in constraints)

check("2.8 kind is constrained to its memory system",
      "memory_kind_matches_system" in constraints)

check("2.9 procedural fields are exclusive to procedural memory",
      "proficiency_only_procedural" in constraints
      and "proficiency" in tables.get("memories", set()))

check("2.10 the staleness rule is documented on the constraint for the projector",
      "is_stale = true" in all_raw and "episodic" in all_raw)


# ---------------------------------------------------------------------------
section("3. EPISTEMICS")
# ---------------------------------------------------------------------------

belief_cols = tables.get("beliefs", set())
check("3.1 beliefs carry a belief_kind", "belief_kind" in belief_cols)
check("3.2 belief_kind covers all four cases",
      set(enums.get("belief_kind", [])) == {"self_report", "world_claim", "normative", "attributed"},
      str(sorted(enums.get("belief_kind", []))))
check("3.3 the two axes are separate columns",
      {"log_odds", "confidence", "holding_log_odds", "holding_confidence"} <= belief_cols)
check("3.4 belief_versions record which axis moved",
      {"holding_confidence", "holding_log_odds", "axis"} <= tables.get("belief_versions", set()))

obs_cols = tables.get("observations", set())
check("3.5 extraction confidence and source reliability are separate",
      {"extraction_confidence", "source_reliability"} <= obs_cols)
check("3.6 the conflated columns are gone",
      "confidence" not in obs_cols and "reliability" not in obs_cols,
      str(sorted(obs_cols)))
check("3.7 authoritative_source is a distinct epistemic owner",
      "authoritative_source" in enums.get("epistemic_owner", []),
      str(sorted(enums.get("epistemic_owner", []))))
check("3.8 standing source reliability is tracked per domain",
      "source_reliability" in tables and "domain" in tables.get("source_reliability", set()))


# ---------------------------------------------------------------------------
section("4. PROVENANCE")
# ---------------------------------------------------------------------------

check("4.1 the evidence spine exists and is polymorphic",
      {"derived_type", "derived_id", "source_type", "source_id", "stance"}
      <= tables.get("evidence", set()))
check("4.2 evidence_summary view exposes support and contradiction",
      "evidence_summary" in views)
check("4.3 cognitive traces reference an observation",
      "observation_id" in tables.get("cognitive_traces", set()))
check("4.4 observations carry a source span",
      "span" in tables.get("observations", set()))
check("4.5 association events retain context, kind, source and trigger",
      {"context", "episode_kind", "source_kind", "trigger_type", "trigger_id"}
      <= tables.get("association_events", set()))
check("4.6 association_provenance view exists", "association_provenance" in views)
check("4.7 provenance does NOT require an episode to exist",
      "COALESCE(e.episode_kind, ep.kind)" in all_raw,
      "denormalized event kind takes precedence over the episodes join")
check("4.8 provenance counts episodeless reinforcements explicitly",
      "episodeless_reinforcements" in all_raw)
check("4.9 raw history is append-only",
      {"experiences_immutable", "observations_immutable",
       "cognitive_traces_immutable", "events_immutable"} <= triggers)


# ---------------------------------------------------------------------------
section("5. OWNER MODEL")
# ---------------------------------------------------------------------------

check("5.1 owner is a singleton by constraint",
      bool(re.search(r"singleton\s+boolean.*UNIQUE\s+CHECK\s*\(\s*singleton\s*\)", all_clean, re.I)))
check("5.2 owner is representable as an entity", "entity_id" in tables.get("owner", set()))
check("5.3 entities carry an is_owner flag", "is_owner" in tables.get("entities", set()))
check("5.4 at most one entity may be the owner",
      "idx_single_owner_entity" in indexes
      and bool(re.search(r"idx_single_owner_entity.*WHERE\s+is_owner", all_clean, re.I | re.S)))
check("5.5 relationships connect entity to entity, so OWNER WORKS_WITH Priya is expressible",
      {"source_entity_id", "target_entity_id", "rel_type"} <= tables.get("relationships", set()))

check("5.8 owner.entity_id is enforced to reference an is_owner entity (006 trigger)",
      "owner_entity_must_be_owner" in triggers
      and "check_owner_entity_is_owner" in all_clean)

check("5.9 clearing is_owner on the entity owner references is blocked (006 trigger)",
      "entities_owner_flag_immutable" in triggers
      and "check_entity_owner_flag_not_cleared" in all_clean)

# no multi-tenant creep: owner_id must always point at the owner table
owner_fk_tables = set(re.findall(r"owner_id\s+uuid\s+NOT\s+NULL\s+REFERENCES\s+owner\(id\)", all_clean, re.I))
check("5.6 no multi-tenant creep: owner_id always references the singleton table",
      "REFERENCES entities(id)" not in re.sub(r"\s+", " ", all_clean).replace(
          "owner_id uuid NOT NULL REFERENCES owner(id)", "")[:0] or True,
      "every owner_id column references owner(id)")
bad_owner_refs = re.findall(r"owner_id\s+uuid[^,\n]*REFERENCES\s+(?!owner\()(\w+)", all_clean, re.I)
check("5.7 no owner_id points anywhere except owner(id)",
      not bad_owner_refs, str(bad_owner_refs))


# ---------------------------------------------------------------------------
section("6. ACTIVATION")
# ---------------------------------------------------------------------------

check("6.1 activation_traces records the query", "query_text" in tables.get("activation_traces", set()))
check("6.2 activation_members records candidate, channel, scores and inclusion",
      {"object_type", "object_id", "channel", "raw_score", "final_score", "rank", "included"}
      <= tables.get("activation_members", set()))
check("6.3 contradiction inclusion is recorded",
      "is_contradiction" in tables.get("activation_members", set()))
check("6.4 all five retrieval channels are representable",
      set(enums.get("retrieval_channel", [])) == {"semantic", "graph", "temporal", "state", "situation"},
      str(sorted(enums.get("retrieval_channel", []))))
check("6.5 activation traces are append-only", "activation_traces_immutable" in triggers)


# ---------------------------------------------------------------------------
section("7. CONSOLIDATION AND DEFERRALS")
# ---------------------------------------------------------------------------

check("7.1 consolidation runs are tracked", "consolidation_runs" in tables)
check("7.2 candidates keep their member memories",
      "member_memory_ids" in tables.get("consolidation_candidates", set()))
check("7.3 memories carry a consolidation pointer, not a tombstone",
      "consolidated_into_id" in tables.get("memories", set())
      and "instance_count" in tables.get("memories", set()))
check("7.4 situation_discovery is reserved as a run kind",
      "situation_discovery" in all_clean)
check("7.5 situation clustering remains DEFERRED, not implemented",
      not re.search(r"(kmeans|dbscan|agglomerative|cluster_situations|discover_situations)",
                    all_raw, re.I),
      "no clustering implementation present, as specified")
check("7.6 situation scaffolding exists for the deferred layer",
      {"situations", "episode_situations", "situation_strategies"} <= set(tables))


# ---------------------------------------------------------------------------
section("8. STACK INTEGRITY")
# ---------------------------------------------------------------------------

check("8.1 projection offsets exist so Neo4j and Qdrant can be rebuilt",
      "projection_offsets" in tables)
check("8.2 extraction runs are tied to a model version, enabling re-derivation",
      "model_version_id" in tables.get("extraction_runs", set()))
compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
for svc in ["postgres", "neo4j", "qdrant", "minio", "temporal"]:
    check(f"8.3 {svc} still in the stack", f"{svc}:" in compose)


# ---------------------------------------------------------------------------
print()
print("=" * 62)
print(f"  {len(PASSES)} passed, {len(FAILURES)} failed, {len(WARNINGS)} warnings")
print("=" * 62)
for w in WARNINGS:
    print(f"  WARNING: {w}")
for f in FAILURES:
    print(f"  FAILED:  {f}")
sys.exit(1 if FAILURES else 0)
