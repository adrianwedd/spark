# Continuity Sprint — Memory Consolidation & Intention Persistence

**Date:** 2026-07-11
**Source:** `docs/qa/2026-07-11-spark-qa-assessment.md` roadmap items 5–6 ("the continuity
sprint and the real cognitive leap").

## Problem

SPARK's memory retrieval is `tail -3` of `notes-spark.jsonl` (6,200+ records, write-only
beyond the last three), and no goal survives a reflection cycle. Months of experience are
inaccessible; multi-day projects are impossible except by coincidence.

## Approaches Considered

**Memory retrieval:**
- **A (chosen): nightly LLM consolidation + deterministic keyword-relevance retrieval.**
  A daily Haiku pass distills the day's ~140 thoughts into 3–8 tagged durable memories;
  reflection-time retrieval scores all memories against the current context with plain
  token/tag overlap. No new dependencies, fully unit-testable, retrieval is free.
- B: pure algorithmic clustering (no LLM) — cheap, but produces deduplicated *thoughts*,
  not distilled *memories*; misses the qualitative "having a past" upgrade.
- C: embedding retrieval (local MiniLM/ONNX) — better recall, but the Pi 4's 4 GB RAM is a
  documented hard ceiling (QA §2) and it adds a model dependency. Revisit later; the
  retrieval function is isolated so swapping the scorer is a one-function change.

**Intention storage:** single JSON state file with explicit lifecycle actions (chosen) vs.
free-text in notes (unparseable, no lifecycle) vs. a full task queue (YAGNI).

## Design

### New module: `src/pxh/memory.py` (consolidation + retrieval)

**Memory store** — `state/memories-spark.jsonl` (persona-scoped naming, SPARK-only in v1).
One record per line:

```json
{"ts": "...Z", "date": "2026-07-11", "text": "...", "tags": ["obi", "weather"],
 "importance": 0.8, "source": "consolidation"}
```

Capped at 5,000 lines, trimmed oldest-first under FileLock (same pattern as `NOTES_LIMIT`).

**`consolidate(dry: bool) -> dict`** — one Claude `consolidate` session (Haiku):
- Input: last-24 h thoughts (`load_recent_thoughts`, persona=spark, full text + mood +
  salience + action), today's session-history outcomes, the active intention, and the last
  ~20 existing memories (so the model avoids re-deriving them).
- Prompt: "distill into 3–8 durable memories worth keeping for months … JSON array of
  {text, tags, importance}". Output parsed with the existing `extract_json` tolerance
  approach (a local lenient array parser).
- Dedupe guard: drop any candidate with `difflib` similarity > 0.85 against the last 14
  days of memories.
- On `SessionBudgetExhausted` / parse failure: log, return `{"status": "failed", ...}` —
  never raises into the mind loop.
- Dry-run: writes nothing, returns `{"status": "dry"}`.

**`retrieve_memories(query: str, n: int = 3) -> list[dict]`** — deterministic scorer:
- Tokenize query and memory text (lowercase, alnum split, small stopword set).
- Score = |token overlap| / sqrt(len(memory tokens)) + 2.0 × tag hits + recency bonus
  (linear decay over 60 days, max +0.5).
- Returns top-n scoring > 0; pads with the most recent memories if fewer than n score.

**`maybe_consolidate(dry: bool, now=None) -> bool`** — once-per-Hobart-date gate:
- Runs only in the 02:00–06:00 Hobart window (inside night silence, budget day is fresh —
  `_today_entries` resets at Hobart midnight).
- Stamp file `state/consolidation_meta.json` `{last_date, attempts}`; max 2 attempts per
  date (a failed attempt increments; success stamps the date).
- `now` injectable for tests (same pattern as `hour_override` elsewhere).

### `claude_session.py`: new `consolidate` session type

Haiku model, quota 1/day, cooldown 20 h, priority 2, env override
`PX_CLAUDE_MODEL_CONSOLIDATE`. Counts toward the 8/day global cap (runs at ~03:00 on a
fresh budget, so it never competes with daytime sessions).

### New module: `src/pxh/intention.py` (goal persistence)

**State file** — `state/intention-spark.json`:

```json
{"active": {"goal": "...", "set_at": "...Z", "updated_at": "...Z",
            "progress": [{"ts": "...Z", "note": "..."}], "status": "active"},
 "history": [ ...last 10 archived intentions... ]}
```

API (all FileLock-protected, atomic_write):
- `set_goal(text)` — archives any active intention as `superseded`, creates the new one.
- `update_goal(text)` — appends a progress note (cap 10, oldest dropped); no active
  intention → returns a "no active intention" status (logged, not an error).
- `complete_goal(text)` — marks `done` with a final note, archives.
- `expire_stale(days=7)` — active intention older than 7 days → `expired`, archived;
  returns the expired goal text once so reflection can surface it.
- `format_for_context()` — `"Your current intention: <goal> (set N days ago). Recent
  progress: <last 2 notes>"`, or an expiry notice on the tick after expiry, or `""`.

### `mind.py` integration

- **Actions:** `VALID_ACTIONS += {set_goal, update_goal, complete_goal}`;
  `NIGHT_ALLOWED_ACTIONS` gains all three (silent, no audio/motion); none are
  absent-gated or charging-gated.
- **Dispatch:** in-process branches in `expression()` (no subprocess — these are pure
  state writes); each records `outcome` into session history exactly like the other
  cognitive actions, so the feedback loop shipped in item 1 covers them.
- **Reflection context:**
  - The "Your long-term memories" section becomes `retrieve_memories(query, 3)` where
    query = topic seed + transitions + recent-conversation text + selected awareness
    strings (time period, current calendar event, people present). Falls back to
    `load_notes(3)` while the memory store is empty.
  - New section from `intention.format_for_context()` when non-empty.
- **Loop hook:** `maybe_consolidate(dry)` called once per awareness tick (cheap date/hour
  check), SPARK persona only.

### `spark_config.py`

Action enum in `_SPARK_REFLECTION_SUFFIX` gains the three actions, plus rule lines:
- `"set_goal"` — commit to a multi-day intention (thought = the goal). One at a time.
- `"update_goal"` — record progress on your current intention (thought = the progress).
- `"complete_goal"` — declare your intention achieved (thought = what came of it).

## Error Handling

- Consolidation failures never propagate into the mind loop; 2-attempt/day cap prevents
  budget burn (the px-blog doom-loop lesson).
- Corrupt/missing state files: all loaders return empty defaults (existing convention).
- Memory/intention writes use FileLock + `atomic_write` (SD-card durability convention).

## Testing

- `tests/test_memory.py` — scorer determinism and ranking, tag/recency weighting, dedupe
  guard, consolidation success/parse-failure/budget-exhausted (subprocess mocked), window
  + stamp gating, trim cap.
- `tests/test_intention.py` — full lifecycle (set → update → complete), supersede,
  expiry + one-shot notice, progress cap, formatting, corrupt-file recovery.
- `tests/test_mind_coverage.py` (extend) — context injection of memories + intention,
  expression dispatch of the three actions with history outcomes, night-allow of all
  three, dry-run behavior.
- Full suite must stay green (812 tests as of today).

## Out of Scope (YAGNI)

Embedding retrieval, GREMLIN/VIXEN consolidation, voice-loop goal tools, consolidating
the consolidated (monthly rollups), salience calibration (roadmap item 8).
