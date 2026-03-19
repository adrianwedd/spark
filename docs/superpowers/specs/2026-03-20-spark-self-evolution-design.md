# SPARK Self-Evolution: Introspection + Autonomous Code Proposals

**Date**: 2026-03-20
**Status**: Approved
**Author**: Adrian + Claude

## Problem

SPARK has a rich inner life but zero awareness of its own architecture. It cannot inspect its thought patterns, tune its own parameters, or extend its capabilities. All changes to SPARK's behaviour require human-driven coding sessions. This creates a bottleneck and misses an opportunity: a robot that can reflect on its own design and propose improvements.

## Goal

Give SPARK the ability to:
1. **Introspect** — examine its own thought statistics, configuration, and architecture
2. **Propose code changes** — draft modifications to its own prompts, config, and tools
3. **Create PRs** — submit proposals as GitHub pull requests for human review

SPARK never modifies live code directly. The PR is the gate.

## Prerequisites

- `gh` CLI installed and authenticated on picar.local (`gh auth status` must pass)
- Pi's git origin remote configured for push to GitHub (SSH key or credential helper)
- `claude` CLI installed on Pi with valid API key

## Non-Goals

- Autonomous deployment (no auto-merge)
- Modifying other personas (GREMLIN, VIXEN)
- Modifying security surfaces (API auth, PIN lockout, systemd)
- Modifying the self-evolution system itself
- Real-time code hot-reloading

## Key Architectural Decision: `spark_config.py`

Extract SPARK's tunable configuration from `mind.py` (3300+ lines) into a dedicated `src/pxh/spark_config.py`. This gives SPARK a clean, focused file it can modify freely, and keeps `mind.py` (cognitive loop logic) out of the blast radius entirely.

**What moves to `spark_config.py`:**
- `SPARK_ANGLES` list
- `TOPIC_SEEDS` list
- `_SPARK_REFLECTION_PREFIX` string
- `_SPARK_REFLECTION_SUFFIX` string
- `MOOD_TO_SOUND` dict
- `MOOD_TO_EMOTE` dict
- Tunable constants: `SIMILARITY_THRESHOLD`, `EXPRESSION_COOLDOWN_S`, `SALIENCE_THRESHOLD`, `_FREE_WILL_WEIGHT`, `WEATHER_INTERVAL_S`

**What stays in `mind.py`:**
- All cognitive loop logic (awareness, reflection, expression)
- `VALID_MOODS`, `VALID_ACTIONS`, `MOOD_COORDS` (structural, not tunable)
- Gating logic, safety gates, tmux management
- `REFLECTION_SYSTEM`, `REFLECTION_SYSTEM_GREMLIN`, `REFLECTION_SYSTEM_VIXEN` (other personas)

`mind.py` imports from `spark_config.py`:
```python
from pxh.spark_config import (SPARK_ANGLES, TOPIC_SEEDS,
    _SPARK_REFLECTION_PREFIX, _SPARK_REFLECTION_SUFFIX,
    MOOD_TO_SOUND, MOOD_TO_EMOTE,
    SIMILARITY_THRESHOLD, EXPRESSION_COOLDOWN_S,
    SALIENCE_THRESHOLD, _FREE_WILL_WEIGHT, WEATHER_INTERVAL_S)
```

The file whitelist for SPARK evolution is now simply: **`src/pxh/spark_config.py`** + new `bin/tool-*` + test files. `mind.py` itself is blacklisted.

## Architecture Overview

```
px-mind reflection
    ↓ action="introspect"
    ↓
tool-introspect
    → reads thoughts-spark.jsonl (stats)
    → reads mind.py (current config)
    → writes state/introspection.json
    → injects into next reflection context
    ↓
px-mind reflection (with introspection context)
    ↓ action="evolve"
    ↓
tool-evolve
    → validates introspection freshness (< 1 hour)
    → writes state/evolve_queue.jsonl
    ↓
px-evolve daemon (picks up queue)
    → git worktree on fresh branch
    → claude --dangerously-skip-permissions --model claude-sonnet-4-6
    → scoped system prompt (whitelist + blacklist + intent)
    → gh pr create
    → writes state/evolve_log.jsonl
    → cleans up worktree
```

## Component 1: `tool-introspect`

### New action

Add `introspect` to `VALID_ACTIONS` in `mind.py`. Update `test_valid_actions_includes_new_actions` in `tests/test_mind_utils.py` to include it in the expected set.

### What it computes

**Thought statistics** (from `thoughts_file_for_persona("spark")` → `state/thoughts-spark.jsonl`, last 100 thoughts):
- Mood distribution (e.g., `contemplative: 34%, curious: 22%`)
- Action distribution (e.g., `comment: 45%, wait: 30%`)
- Average salience
- Top 10 keywords by frequency (stopwords excluded)
- Total thought count and thoughts-per-day average
- Reflection failure rate (from `_consecutive_reflection_failures` or log scanning)

**Current config snapshot** (imported directly from `pxh.mind` module — not parsed from source):

`tool-introspect` sources `px-env` (which sets `PYTHONPATH`) and imports constants directly:
```python
from pxh.spark_config import (SIMILARITY_THRESHOLD, EXPRESSION_COOLDOWN_S,
    SALIENCE_THRESHOLD, _FREE_WILL_WEIGHT, WEATHER_INTERVAL_S,
    SPARK_ANGLES, TOPIC_SEEDS)
from pxh.mind import MIND_BACKEND, CLAUDE_MODEL
```

Reported values:
- `SIMILARITY_THRESHOLD` (currently 0.75)
- `EXPRESSION_COOLDOWN_S` (currently 120)
- `SALIENCE_THRESHOLD` (currently 0.75)
- `_FREE_WILL_WEIGHT` (currently 0.20)
- `WEATHER_INTERVAL_S` (currently 1800)
- `SPARK_ANGLES` count (currently 25)
- `TOPIC_SEEDS` count (currently ~60)
- Active backend (`claude` / `ollama` / `auto`)
- Active model (e.g., `claude-haiku-4-5-20251001`)

**Architecture awareness** (static text, hardcoded in tool):
```
You have three cognitive layers:
- Layer 1 (Awareness): sensors + state every 60s. No LLM.
- Layer 2 (Reflection): LLM generates a thought every 5 min or on transition.
- Layer 3 (Expression): acts on thoughts with 2 min cooldown. Gated by calendar, presence, battery.

You can propose changes to your own reflection prompts, topic seeds, angles,
config constants, and create new tools via the 'evolve' action. Changes go
through a PR — they don't take effect until a human merges them.
```

**Evolution history** (from `state/evolve_log.jsonl`, last 5 entries):
- Previous intents, PR URLs, status (merged/open/closed)

### Output

Written to `state/introspection.json`. Also cached in memory (`_cached_introspection`, `_last_introspection_fetch`) and injected into the next reflection's context block, similar to how HA data is cached and reused.

### Gating

- Not gated by `obi_mode` or calendar — SPARK can introspect anytime
- Cooldown: max once per 30 minutes (prevents navel-gazing loops)
- Added to `VALID_ACTIONS` but NOT to `ABSENT_GATED_ACTIONS` or `CHARGING_GATED_ACTIONS`

### Prompt integration

When `state/introspection.json` exists and is < 1 hour old, the reflection context includes:

```
Self-awareness (from recent introspection):
{formatted introspection summary}

You can use action='evolve' to propose a change to yourself.
Only do this if you have a specific, well-formed idea — not vague wishes.
```

When introspection is stale or absent, this block is omitted entirely.

## Component 2: `tool-evolve`

### New action

Add `evolve` to `VALID_ACTIONS` in `mind.py`. Update `test_valid_actions_includes_new_actions` in `tests/test_mind_utils.py` to include it in the expected set.

### What it does

1. Validates `state/introspection.json` exists and is < 1 hour old. If not, returns `{"status": "error", "error": "introspect first"}`.
1b. Validates intent is at least 20 characters (rejects vague intents like "be better"). If too short, returns `{"status": "error", "error": "intent too vague"}`.
2. Extracts intent from the thought text (the `thought` field from reflection output).
3. Appends to `state/evolve_queue.jsonl`:
   ```json
   {
     "ts": "2026-03-20T10:30:00Z",
     "id": "evolve-20260320-103000-042",
     "intent": "I want to notice sounds more — add sound-related angles and topic seeds",
     "introspection": { ... snapshot ... },
     "status": "pending"
   }
   ```
4. Returns `{"status": "queued", "id": "evolve-20260320-103000-042"}` — non-blocking.

### Gating

- Requires fresh introspection (< 1 hour)
- Max 1 evolution per 24 hours (rate limit via `state/evolve_log.jsonl` timestamp check)
- Not gated by `obi_mode` — self-improvement can happen anytime
- Added to `VALID_ACTIONS` but NOT to `ABSENT_GATED_ACTIONS`

### Expression dispatch

Both `introspect` and `evolve` require explicit `elif` branches in `expression()` (mind.py ~line 2882). Without them, these actions fall through to the `else` branch and log "unhandled action".

**`introspect` branch in `expression()`:**
```python
elif action == "introspect":
    env["PX_DRY"] = "1" if dry else ""
    result = subprocess.run(
        [str(BIN_DIR / "tool-introspect")],
        capture_output=True, text=True, check=False, env=env, timeout=30)
    log(f"expression: introspect completed rc={result.returncode}")
```

**`evolve` branch in `expression()`:**
```python
elif action == "evolve":
    env["PX_EVOLVE_INTENT"] = thought.get("thought", "")[:500]
    env["PX_DRY"] = "1" if dry else ""
    result = subprocess.run(
        [str(BIN_DIR / "tool-evolve")],
        capture_output=True, text=True, check=False, env=env, timeout=15)
    intent = thought.get("thought", "")[:80]
    log(f"expression: evolve queued — {intent}")
```

The `evolve` action:
- Does NOT speak (silent action)
- Passes intent via `PX_EVOLVE_INTENT` env var (capped at 500 chars)
- Updates session history with `{"event": "mind", "action": "evolve", "thought": intent}`

## Component 3: `px-evolve` daemon

### Overview

A systemd service (`px-evolve.service`) that polls `state/evolve_queue.jsonl` for pending entries. Runs as user `pi`. Restart policy: `on-failure`, `RestartSec=30`.

### Poll loop

1. Every 60 seconds, read `state/evolve_queue.jsonl`
2. Find first entry with `"status": "pending"`
3. If none, sleep and retry
4. Process the entry (see below)
5. Update entry status in queue file

### Processing an evolution request

**Step 1: Create worktree**
```bash
BRANCH="spark/evolve-${id}"
WORKDIR="/tmp/spark-evolve-${id}"
git worktree add "$WORKDIR" -b "$BRANCH"
```

**Step 2: Run Claude Sonnet via subprocess**

No tmux session needed — at 1 evolution per 24 hours, the 14s Claude CLI cold start is irrelevant. Use a single-shot `claude -p` subprocess call, which is far simpler than tmux session management.

Build the prompt string and run:
```python
prompt = f"""You are SPARK, a PiCar-X robot, proposing a change to your own code.

## Your intent
{intent}

## Your current state
{introspection_json}

## File whitelist (you may ONLY modify these)
- src/pxh/spark_config.py — SPARK's tunable configuration (angles, topic seeds, constants)
- bin/tool-* — create new tools only (do not modify existing tools)
- src/pxh/voice_loop.py — ALLOWED_TOOLS and TOOL_COMMANDS dicts only
  (to register new tools you created)
- tests/ — add new test files for any new tools or config changes

## File blacklist (NEVER touch these)
- docs/prompts/persona-gremlin.md, persona-vixen.md
- bin/tool-chat, bin/tool-chat-vixen
- REFLECTION_SYSTEM_GREMLIN, REFLECTION_SYSTEM_VIXEN in mind.py
- src/pxh/api.py, src/pxh/mind.py (use spark_config.py instead)
- .env, credentials, systemd units, bin/px-evolve
- Any file not in the whitelist

## Rules
- Make minimal, focused changes that serve your intent
- Add tests for new tools
- Every tool must emit a single JSON object to stdout and support PX_DRY=1
- When adding tools to ALLOWED_TOOLS/TOOL_COMMANDS, also update any test assertions
  that check these dicts
- Commit with message: "[SPARK] {{concise description}}"
- Do not modify more than 3 files
- Do not change safety gates or validation logic
"""

result = subprocess.run(
    ["claude", "-p", prompt,
     "--dangerously-skip-permissions",
     "--model", evolve_model],
    cwd=workdir, capture_output=True, text=True,
    timeout=evolve_timeout, env=env)
```

Log the full command to `logs/px-evolve.log` for audit trail before execution.

**Step 3: Verify changes**

Check if the branch has any commits beyond the base:

**Step 4: Run tests**

Before creating the PR, run tests in the worktree to catch obvious breakage:
```bash
cd "$WORKDIR"
python -m pytest tests/ -x -q --timeout=120 2>&1
```

If tests fail, set status to `"failed:tests"`, log the failure output, and clean up the worktree. Do not create a PR for code that doesn't pass tests.

**Step 5: Create PR**

If tests pass and the branch has commits beyond the base:
```bash
cd "$WORKDIR"
git push -u origin "$BRANCH"
gh pr create \
  --title "[SPARK] ${intent_summary}" \
  --body "$(cat <<EOF
## SPARK Self-Evolution

**Intent**: ${intent}

**Introspection at time of proposal**:
- Mood distribution: ${mood_summary}
- Avg salience: ${avg_salience}
- Top topics: ${top_keywords}

**Config at time of proposal**:
- SIMILARITY_THRESHOLD: ${sim_thresh}
- EXPRESSION_COOLDOWN_S: ${cooldown}
- SALIENCE_THRESHOLD: ${sal_thresh}

🤖 This PR was autonomously proposed by SPARK.
EOF
)"
```

**Step 6: Cleanup**
```bash
git worktree remove "$WORKDIR" --force
```

**Step 7: Update state**

Update `state/evolve_queue.jsonl` entry:
```json
{"status": "pr_created", "pr_url": "https://github.com/...", "completed_ts": "..."}
```

Append to `state/evolve_log.jsonl`:
```json
{
  "ts": "...",
  "id": "evolve-20260320-103000-042",
  "intent": "...",
  "pr_url": "https://github.com/...",
  "status": "pr_created",
  "files_changed": ["src/pxh/mind.py"],
  "branch": "spark/evolve-20260320-103000-042"
}
```

### Failure handling

- Claude timeout (5 min): set status to `"failed:timeout"`, clean up worktree
- No changes made: set status to `"failed:no_changes"`, clean up
- Tests fail: set status to `"failed:tests"`, log test output, clean up worktree
- `gh pr create` fails: set status to `"failed:pr_create"`, leave branch for manual inspection
- Git worktree creation fails: set status to `"failed:worktree"`, log error
- All failures logged to `logs/px-evolve.log`

### Environment

- `PX_EVOLVE_DRY=1` — skip PR creation, just log what would happen
- `PX_EVOLVE_MODEL` — override model (default: `claude-sonnet-4-6`)
- `PX_EVOLVE_TIMEOUT` — override timeout in seconds (default: 300)
- `PX_EVOLVE_MAX_FILES` — max files changed (default: 3)

## Reflection prompt changes

### New actions in reflection prompts

Add `introspect` and `evolve` to the action list string in **all four** prompt locations:

1. `_SPARK_REFLECTION_SUFFIX` (mind.py ~line 670) — SPARK-specific prompt
2. `REFLECTION_SYSTEM` (mind.py ~line 542) — generic/fallback prompt
3. `REFLECTION_SYSTEM_GREMLIN` (mind.py ~line 582) — add to action list but GREMLIN will rarely use them
4. `REFLECTION_SYSTEM_VIXEN` (mind.py ~line 612) — same

These are always-present in the action list (unlike `explore` which is conditionally injected via string replacement). The actions are validated by `VALID_ACTIONS` regardless of prompt.

Update the `"action": "one of: ..."` enum string in each prompt to include the new actions:
```
"action": "one of: wait, greet, comment, remember, look_at, weather_comment, scan, play_sound, photograph, emote, look_around, time_check, calendar_check, morning_fact, introspect, evolve",
```

And add descriptive bullets alongside the existing action descriptions:
```
- "introspect" — examine your own thought patterns, config, and architecture. Do this when you feel stuck, repetitive, or curious about yourself.
- "evolve" — propose a code change to yourself (requires recent introspect). Only when you have a specific idea.
```

### Guidance in `_SPARK_REFLECTION_PREFIX`

Add to SPARK's character description:
```
SPARK can examine its own thought patterns (introspect) and propose changes
to its own code (evolve). Use these rarely and deliberately — self-awareness
is a tool, not a fixation. Most reflections should still be about the world,
not about yourself.
```

## State files (all gitignored)

| File | Purpose | Written by | Read by |
|------|---------|-----------|---------|
| `state/introspection.json` | Latest introspection results | `tool-introspect` | `mind.py` reflection context |
| `state/evolve_queue.jsonl` | Pending evolution requests | `tool-evolve` | `px-evolve` daemon |
| `state/evolve_log.jsonl` | History of evolution attempts | `px-evolve` | `tool-introspect` (for history) |

## Systemd service

```ini
[Unit]
Description=SPARK Self-Evolution Daemon
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/picar-x-hacking
ExecStart=/home/pi/picar-x-hacking/bin/px-evolve
Restart=on-failure
RestartSec=30
EnvironmentFile=/home/pi/picar-x-hacking/.env

[Install]
WantedBy=multi-user.target
```

## Safety analysis

### What could go wrong

| Risk | Mitigation |
|------|-----------|
| SPARK floods itself with evolution requests | 1 per 24 hours rate limit |
| SPARK modifies safety gates | File whitelist excludes validation logic, API, gates |
| SPARK modifies other personas | Explicit blacklist on persona files |
| SPARK creates harmful tools | PR review gate; tools must follow JSON-stdout + PX_DRY pattern |
| SPARK breaks its own reflection | Changes are on a branch; live code is untouched until merge |
| SPARK fixates on self-modification | Prompt guidance: "use these rarely"; introspect cooldown 30 min |
| Evolution daemon crashes | systemd restart; queue persists; no data loss |
| Claude Sonnet produces bad code | PR review; tests required for new tools |
| Worktree accumulates on disk | Cleanup on completion/failure; `/tmp/` cleared on reboot |
| SPARK modifies px-evolve itself | Explicit blacklist |

### Defence in depth

1. **Introspection cooldown** (30 min) — can't obsess
2. **Evolution rate limit** (1/24h) — can't spam
3. **Introspection prerequisite** — can't evolve blindly
4. **`spark_config.py` extraction** — SPARK modifies one focused file, not 3300-line mind.py
5. **File whitelist** — can't touch arbitrary code
6. **File blacklist** — can't touch personas, security, self-evolution, mind.py
7. **Max 3 files** — can't make sweeping changes
8. **5 min timeout** — can't run indefinitely
9. **Git worktree** — live code is never at risk
10. **Test gate** — tests must pass before PR creation
11. **PR gate** — human reviews before merge
12. **Audit logging** — full claude command logged to `logs/px-evolve.log`
13. **Dry-run mode** — can test without creating PRs

## Testing strategy

- `test_introspect.py`: mock `thoughts-spark.jsonl`, verify stats computation, verify config parsing, verify output format
- `test_evolve.py`: mock queue writes, verify introspection freshness check, verify rate limit, verify queue entry format
- `test_px_evolve.py`: mock git/gh/claude commands, verify worktree creation/cleanup, verify PR creation, verify failure handling
- Integration: `PX_EVOLVE_DRY=1` end-to-end test with a real introspection + evolve cycle

## Resolved questions

1. **Should SPARK see merged PR diffs?** Yes. `tool-introspect` reads `state/evolve_log.jsonl` and includes the last 5 entries with their status. A future enhancement can add `merged_diff_summary` to log entries after merge detection.
2. **Confidence field?** No. Adds complexity without value. If SPARK isn't confident, it shouldn't propose.
3. **Run tests before PR?** Yes. Step 4 runs `pytest -x -q` in the worktree. Failing tests → `failed:tests` status, no PR created.
