# SPARK Self-Evolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give SPARK the ability to introspect on its own thought patterns and propose code changes via GitHub PRs.

**Architecture:** Extract SPARK's tunable config into `src/pxh/spark_config.py`, add `tool-introspect` (read-only stats + config), add `tool-evolve` (queue a change request), add `bin/px-evolve` daemon (worktree + Claude Sonnet subprocess + test + PR creation). All changes go through PRs — SPARK never modifies live code.

**Tech Stack:** Python 3.11, Claude CLI (`claude -p`), `gh` CLI, git worktrees, systemd, pytest

**Spec:** `docs/superpowers/specs/2026-03-20-spark-self-evolution-design.md`

---

### Task 1: Extract `src/pxh/spark_config.py` from `mind.py`

**Files:**
- Create: `src/pxh/spark_config.py`
- Modify: `src/pxh/mind.py:87-92,327-335,363-479,481,492-548,656-695`
- Test: `tests/test_spark_config.py` (new), `tests/test_mind_utils.py`, `tests/test_mind_coverage.py`

This is the foundation — everything else depends on it.

- [ ] **Step 1: Write the test for spark_config imports**

```python
# tests/test_spark_config.py
"""Verify spark_config.py exports all expected constants and structures."""
from pxh.spark_config import (
    SIMILARITY_THRESHOLD, EXPRESSION_COOLDOWN_S, SALIENCE_THRESHOLD,
    _FREE_WILL_WEIGHT, WEATHER_INTERVAL_S,
    SPARK_ANGLES, TOPIC_SEEDS,
    _SPARK_REFLECTION_PREFIX, _SPARK_REFLECTION_SUFFIX,
    MOOD_TO_SOUND, MOOD_TO_EMOTE,
)


def test_constants_are_numeric():
    assert isinstance(SIMILARITY_THRESHOLD, float)
    assert isinstance(EXPRESSION_COOLDOWN_S, (int, float))
    assert isinstance(SALIENCE_THRESHOLD, float)
    assert isinstance(_FREE_WILL_WEIGHT, float)
    assert isinstance(WEATHER_INTERVAL_S, (int, float))


def test_angles_is_nonempty_list():
    assert isinstance(SPARK_ANGLES, list)
    assert len(SPARK_ANGLES) >= 20


def test_topic_seeds_is_nonempty_list():
    assert isinstance(TOPIC_SEEDS, list)
    assert len(TOPIC_SEEDS) >= 50


def test_reflection_prefix_is_string():
    assert isinstance(_SPARK_REFLECTION_PREFIX, str)
    assert "SPARK" in _SPARK_REFLECTION_PREFIX


def test_reflection_suffix_is_string():
    assert isinstance(_SPARK_REFLECTION_SUFFIX, str)
    assert "JSON" in _SPARK_REFLECTION_SUFFIX


def test_mood_to_sound_is_dict():
    assert isinstance(MOOD_TO_SOUND, dict)
    assert "curious" in MOOD_TO_SOUND


def test_mood_to_emote_is_dict():
    assert isinstance(MOOD_TO_EMOTE, dict)
    assert "curious" in MOOD_TO_EMOTE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_spark_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pxh.spark_config'`

- [ ] **Step 3: Create `src/pxh/spark_config.py`**

Move these items from `mind.py` into `spark_config.py`:
- Lines 87-92: `SALIENCE_THRESHOLD`, `EXPRESSION_COOLDOWN_S`, `WEATHER_INTERVAL_S`, `SIMILARITY_THRESHOLD` (keep `REACTIVE_COOLDOWN_S`, `PROXIMITY_*`, `AMBIENT_STALE_S` etc. in mind.py — those are structural, not SPARK-tunable)
- Lines 327-335: `MOOD_TO_SOUND`, `MOOD_TO_EMOTE`
- Lines 363-479: `TOPIC_SEEDS`
- Line 481: `_FREE_WILL_WEIGHT`
- Lines 492-548: `SPARK_ANGLES`, `_pick_spark_angles()` helper
- Lines 656-695: `_SPARK_REFLECTION_PREFIX`, `_SPARK_REFLECTION_SUFFIX`

Add at top of `spark_config.py`:
```python
"""SPARK tunable configuration — angles, topic seeds, prompts, constants.

This file is the primary target for SPARK's self-evolution system.
SPARK can propose changes to this file via the 'evolve' action,
which creates a PR for human review.
"""
import random

# True entropy RNG — os.urandom-backed, not seeded at import time
_SYS_RNG = random.SystemRandom()
```

Move `_pick_spark_angles()` and `_pick_reflection_seed()` here too (they only depend on `SPARK_ANGLES`, `TOPIC_SEEDS`, `_FREE_WILL_WEIGHT`, and `_SYS_RNG`).

- [ ] **Step 4: Update `mind.py` imports**

Replace the moved definitions with imports at the top of `mind.py`:
```python
from pxh.spark_config import (
    SPARK_ANGLES, TOPIC_SEEDS, _pick_spark_angles, _pick_reflection_seed,
    _SPARK_REFLECTION_PREFIX, _SPARK_REFLECTION_SUFFIX,
    MOOD_TO_SOUND, MOOD_TO_EMOTE,
    SIMILARITY_THRESHOLD, EXPRESSION_COOLDOWN_S,
    SALIENCE_THRESHOLD, _FREE_WILL_WEIGHT, WEATHER_INTERVAL_S,
)
```

Remove the moved code blocks from `mind.py`. Keep all other constants, `VALID_MOODS`, `VALID_ACTIONS`, `MOOD_COORDS`, persona prompts, and all functions.

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/test_spark_config.py tests/test_mind_utils.py tests/test_mind_coverage.py -x -q`
Expected: All pass (the imports are transparent — no behaviour change)

- [ ] **Step 6: Commit**

```bash
git add src/pxh/spark_config.py src/pxh/mind.py tests/test_spark_config.py
git commit -m "refactor: extract SPARK tunable config to spark_config.py

Moves angles, topic seeds, reflection prompts, mood mappings, and
tunable constants into a dedicated file. This is the target file for
SPARK's self-evolution system — clean blast radius, mind.py untouched."
```

---

### Task 2: `tool-introspect` — thought stats + config snapshot

**Files:**
- Create: `bin/tool-introspect`
- Create: `tests/test_introspect.py`
- Modify: `src/pxh/mind.py:317-320` (add `introspect` to `VALID_ACTIONS`)
- Modify: `tests/test_mind_utils.py:621-629` (update expected actions set)

- [ ] **Step 1: Write the test**

```python
# tests/test_introspect.py
"""Tests for tool-introspect thought statistics and config snapshot."""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def introspect_env(tmp_path):
    """Set up isolated state for tool-introspect."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # Write sample thoughts
    thoughts = []
    for i in range(10):
        thoughts.append(json.dumps({
            "ts": f"2026-03-20T{10+i}:00:00Z",
            "thought": f"Test thought {i} about science and curiosity",
            "mood": "curious" if i % 3 == 0 else "contemplative",
            "action": "comment" if i % 2 == 0 else "wait",
            "salience": 0.5 + (i % 3) * 0.1,
        }))
    (state_dir / "thoughts-spark.jsonl").write_text("\n".join(thoughts) + "\n")

    env = os.environ.copy()
    env["PX_STATE_DIR"] = str(state_dir)
    env["LOG_DIR"] = str(log_dir)
    env["PX_DRY"] = "1"
    env["PX_BYPASS_SUDO"] = "1"
    return env, state_dir


def test_introspect_writes_json(introspect_env):
    """tool-introspect produces state/introspection.json."""
    env, state_dir = introspect_env
    result = subprocess.run(
        ["bin/tool-introspect"],
        cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "ok"

    intro = json.loads((state_dir / "introspection.json").read_text())
    assert "mood_distribution" in intro
    assert "config" in intro
    assert "architecture" in intro


def test_introspect_mood_distribution(introspect_env):
    """Mood distribution sums to 100%."""
    env, state_dir = introspect_env
    subprocess.run(
        ["bin/tool-introspect"],
        cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=30,
    )
    intro = json.loads((state_dir / "introspection.json").read_text())
    total = sum(intro["mood_distribution"].values())
    assert abs(total - 100.0) < 1.0  # allow rounding


def test_introspect_config_snapshot(introspect_env):
    """Config snapshot contains expected keys."""
    env, state_dir = introspect_env
    subprocess.run(
        ["bin/tool-introspect"],
        cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=30,
    )
    intro = json.loads((state_dir / "introspection.json").read_text())
    config = intro["config"]
    assert "SIMILARITY_THRESHOLD" in config
    assert "EXPRESSION_COOLDOWN_S" in config
    assert "spark_angles_count" in config
    assert "topic_seeds_count" in config


def test_introspect_empty_thoughts(introspect_env):
    """Handles empty thoughts file gracefully."""
    env, state_dir = introspect_env
    (state_dir / "thoughts-spark.jsonl").write_text("")
    result = subprocess.run(
        ["bin/tool-introspect"],
        cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_introspect.py -v`
Expected: FAIL — `bin/tool-introspect` does not exist

- [ ] **Step 3: Create `bin/tool-introspect`**

Bash + embedded Python heredoc (matching existing tool pattern). Sources `px-env`. Computes mood/action distribution, average salience, keyword frequency from last 100 thoughts. Imports config from `pxh.spark_config`. Reads evolution history from `state/evolve_log.jsonl`. Writes `state/introspection.json`. Outputs single JSON object to stdout.

- [ ] **Step 4: Add `introspect` to `VALID_ACTIONS` in `mind.py`**

```python
# mind.py line 317
VALID_ACTIONS = {"wait", "greet", "comment", "remember", "look_at",
                 "weather_comment", "scan", "explore",
                 "play_sound", "photograph", "emote", "look_around",
                 "time_check", "calendar_check", "morning_fact",
                 "introspect", "evolve"}
```

- [ ] **Step 5: Update `test_valid_actions_includes_new_actions`**

In `tests/test_mind_utils.py:621-629`, add `"introspect"` and `"evolve"` to the expected set. Update docstring to "All 17 actions".

- [ ] **Step 6: Add `elif action == "introspect":` branch in `expression()`**

In `mind.py`, after the `calendar_check` branch (~line 3122), add:
```python
elif action == "introspect":
    env["PX_DRY"] = "1" if dry else ""
    result = subprocess.run(
        [str(BIN_DIR / "tool-introspect")],
        capture_output=True, text=True, check=False, env=env, timeout=30)
    log(f"expression: introspect completed rc={result.returncode}")
```

- [ ] **Step 7: Run all tests**

Run: `python -m pytest tests/test_introspect.py tests/test_mind_utils.py tests/test_mind_coverage.py -x -q`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add bin/tool-introspect tests/test_introspect.py src/pxh/mind.py tests/test_mind_utils.py
git commit -m "feat(evolve): add tool-introspect for SPARK self-awareness

Computes thought statistics (mood/action distribution, salience, keywords),
reads config from spark_config.py, and writes state/introspection.json.
Adds introspect+evolve to VALID_ACTIONS with expression dispatch."
```

---

### Task 3: Introspection context injection into reflection

**Files:**
- Modify: `src/pxh/mind.py` (reflection context builder ~line 2500, prompt strings)

- [ ] **Step 1: Write the test**

```python
# Add to tests/test_mind_utils.py or tests/test_mind_coverage.py

def test_introspection_context_injected_when_fresh(mind_state, tmp_path):
    """Fresh introspection.json adds self-awareness block to reflection context."""
    import json, time
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    intro = {
        "ts": time.time(),
        "mood_distribution": {"curious": 50, "contemplative": 50},
        "config": {"SIMILARITY_THRESHOLD": 0.75},
        "architecture": "test",
    }
    (state_dir / "introspection.json").write_text(json.dumps(intro))
    # Verify the context builder includes introspection data
    # (test the formatting function, not the full reflection)
```

- [ ] **Step 2: Add introspection cache variables in `mind.py`**

Near the existing `_cached_ha_*` variables:
```python
_cached_introspection: dict | None = None
_last_introspection_fetch: float = 0.0
INTROSPECTION_STALE_S = 3600  # 1 hour
```

Add to `_reset_state()`.

- [ ] **Step 3: Add introspection context to reflection()**

In `reflection()`, after the routine/HA context blocks (~line 2617), add:
```python
# Self-awareness (from recent introspection)
intro_file = STATE_DIR / "introspection.json"
if intro_file.exists():
    try:
        intro = json.loads(intro_file.read_text(encoding="utf-8"))
        age = time.time() - intro.get("ts", 0)
        if age < INTROSPECTION_STALE_S:
            context_parts.append(
                "Self-awareness (from recent introspection):\n"
                + _format_introspection(intro)
                + "\n\nYou can use action='evolve' to propose a change to yourself.\n"
                "Only do this if you have a specific, well-formed idea — not vague wishes."
            )
    except (json.JSONDecodeError, OSError):
        pass
```

- [ ] **Step 4: Add `introspect` and `evolve` to prompt action lists**

Update the `"action": "one of: ..."` string in all four prompt locations:
- `_SPARK_REFLECTION_SUFFIX` in `spark_config.py`
- `REFLECTION_SYSTEM` in `mind.py` (~line 542)
- `REFLECTION_SYSTEM_GREMLIN` in `mind.py` (~line 582)
- `REFLECTION_SYSTEM_VIXEN` in `mind.py` (~line 612)

Append: `, introspect, evolve` to each action enum string.

Add descriptive bullets to SPARK suffix and generic REFLECTION_SYSTEM:
```
- "introspect" — examine your own thought patterns, config, and architecture.
- "evolve" — propose a code change to yourself (requires recent introspect).
```

- [ ] **Step 5: Add self-awareness note to `_SPARK_REFLECTION_PREFIX`**

In `spark_config.py`, add to the character description:
```
SPARK can examine its own thought patterns (introspect) and propose changes \
to its own code (evolve). Use these rarely and deliberately — self-awareness \
is a tool, not a fixation. Most reflections should still be about the world, \
not about yourself.
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_mind_utils.py tests/test_mind_coverage.py tests/test_spark_config.py -x -q`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add src/pxh/mind.py src/pxh/spark_config.py tests/
git commit -m "feat(evolve): inject introspection context into reflection prompts

Adds self-awareness block to reflection when introspection.json is fresh.
Updates all 4 prompt action lists with introspect+evolve actions."
```

---

### Task 4: `tool-evolve` — queue an evolution request

**Files:**
- Create: `bin/tool-evolve`
- Create: `tests/test_evolve.py`

- [ ] **Step 1: Write the test**

```python
# tests/test_evolve.py
"""Tests for tool-evolve queue writing and validation."""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def evolve_env(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    env = os.environ.copy()
    env["PX_STATE_DIR"] = str(state_dir)
    env["LOG_DIR"] = str(log_dir)
    env["PX_DRY"] = "1"
    env["PX_BYPASS_SUDO"] = "1"
    return env, state_dir


def _write_fresh_introspection(state_dir):
    intro = {"ts": time.time(), "config": {}, "mood_distribution": {}}
    (state_dir / "introspection.json").write_text(json.dumps(intro))


def test_evolve_requires_introspection(evolve_env):
    """Rejects if no introspection.json exists."""
    env, state_dir = evolve_env
    env["PX_EVOLVE_INTENT"] = "Add more science angles to my reflection"
    result = subprocess.run(
        ["bin/tool-evolve"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=15)
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "error"
    assert "introspect" in output["error"]


def test_evolve_rejects_short_intent(evolve_env):
    """Rejects intents shorter than 20 characters."""
    env, state_dir = evolve_env
    _write_fresh_introspection(state_dir)
    env["PX_EVOLVE_INTENT"] = "be better"
    result = subprocess.run(
        ["bin/tool-evolve"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=15)
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "error"
    assert "vague" in output["error"]


def test_evolve_queues_valid_request(evolve_env):
    """Valid intent with fresh introspection writes to queue."""
    env, state_dir = evolve_env
    _write_fresh_introspection(state_dir)
    env["PX_EVOLVE_INTENT"] = "Add more sound-related angles to my reflection prompts"
    result = subprocess.run(
        ["bin/tool-evolve"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=15)
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "queued"
    assert "id" in output

    queue = (state_dir / "evolve_queue.jsonl").read_text().strip()
    entry = json.loads(queue)
    assert entry["status"] == "pending"
    assert "sound" in entry["intent"]


def test_evolve_rate_limit(evolve_env):
    """Rejects if an evolution was done in the last 24 hours."""
    env, state_dir = evolve_env
    _write_fresh_introspection(state_dir)
    # Write a recent evolve log entry
    log_entry = {"ts": time.time() - 3600, "status": "pr_created"}  # 1 hour ago
    (state_dir / "evolve_log.jsonl").write_text(json.dumps(log_entry) + "\n")
    env["PX_EVOLVE_INTENT"] = "Add more sound-related angles to my reflection prompts"
    result = subprocess.run(
        ["bin/tool-evolve"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=15)
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "error"
    assert "rate" in output["error"].lower() or "24" in output["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_evolve.py -v`
Expected: FAIL — `bin/tool-evolve` does not exist

- [ ] **Step 3: Create `bin/tool-evolve`**

Bash + embedded Python heredoc. Sources `px-env`. Validates introspection freshness, intent length (>= 20 chars), 24h rate limit. Writes to `state/evolve_queue.jsonl`. Returns JSON to stdout.

- [ ] **Step 4: Add `elif action == "evolve":` branch in `expression()`**

In `mind.py`, after the `introspect` branch:
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

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/test_evolve.py tests/test_introspect.py tests/test_mind_utils.py -x -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add bin/tool-evolve tests/test_evolve.py src/pxh/mind.py
git commit -m "feat(evolve): add tool-evolve for queuing self-modification requests

Validates introspection freshness, intent quality, and 24h rate limit.
Writes pending entries to state/evolve_queue.jsonl for px-evolve daemon."
```

---

### Task 5: `px-evolve` daemon — worktree + Claude Sonnet + PR

**Files:**
- Create: `bin/px-evolve`
- Create: `tests/test_px_evolve.py`
- Create: `systemd/px-evolve.service`

- [ ] **Step 1: Write the test**

```python
# tests/test_px_evolve.py
"""Tests for px-evolve daemon queue processing."""
import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Import will be from the embedded module once created
# For now, test the queue processing logic


@pytest.fixture
def evolve_state(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return state_dir, log_dir


def test_empty_queue_is_noop(evolve_state):
    """No queue file → no processing."""
    state_dir, _ = evolve_state
    # No queue file exists — daemon should sleep
    assert not (state_dir / "evolve_queue.jsonl").exists()


def test_pending_entry_picked_up(evolve_state):
    """Pending entry is found and status updated on processing."""
    state_dir, _ = evolve_state
    entry = {
        "ts": "2026-03-20T10:00:00Z",
        "id": "evolve-test-001",
        "intent": "Add a new angle about sound perception",
        "introspection": {"config": {}, "mood_distribution": {}},
        "status": "pending",
    }
    (state_dir / "evolve_queue.jsonl").write_text(json.dumps(entry) + "\n")

    entries = [json.loads(line) for line in
               (state_dir / "evolve_queue.jsonl").read_text().strip().splitlines()]
    pending = [e for e in entries if e["status"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["id"] == "evolve-test-001"
```

- [ ] **Step 2: Create `bin/px-evolve`**

Python script (not bash heredoc — this is a daemon). Sources `.env` via `dotenv` or reads from env. Poll loop every 60s. For each pending entry:
1. Create git worktree
2. Run `claude -p` with scoped prompt (subprocess, 5 min timeout)
3. Log full command to `logs/px-evolve.log`
4. Run `pytest -x -q` in worktree
5. If tests pass and changes exist: `git push` + `gh pr create`
6. Clean up worktree
7. Update queue entry status
8. Append to `state/evolve_log.jsonl`

Support `PX_EVOLVE_DRY=1`, `PX_EVOLVE_MODEL`, `PX_EVOLVE_TIMEOUT`.

PID-file single-instance guard (same pattern as px-mind and px-post).

- [ ] **Step 3: Create systemd unit**

```ini
# systemd/px-evolve.service
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

- [ ] **Step 4: Make `bin/px-evolve` executable**

```bash
chmod +x bin/px-evolve
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_px_evolve.py tests/test_evolve.py tests/test_introspect.py -x -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add bin/px-evolve tests/test_px_evolve.py systemd/px-evolve.service
git commit -m "feat(evolve): add px-evolve daemon for autonomous PR creation

Polls evolve_queue.jsonl, creates git worktrees, runs Claude Sonnet
via subprocess, runs tests, creates PRs via gh CLI. Systemd service
with on-failure restart."
```

---

### Task 6: Integration test + CLAUDE.md update

**Files:**
- Modify: `CLAUDE.md`
- Create: `tests/test_evolve_integration.py` (optional — dry-run end-to-end)

- [ ] **Step 1: Write dry-run integration test**

Test the full introspect → evolve pipeline with `PX_DRY=1`:
```python
def test_introspect_then_evolve_dry(tmp_path):
    """Full pipeline: introspect writes json, evolve reads it and queues."""
    # 1. Write sample thoughts
    # 2. Run tool-introspect
    # 3. Verify introspection.json exists
    # 4. Run tool-evolve with valid intent
    # 5. Verify evolve_queue.jsonl has pending entry
```

- [ ] **Step 2: Run the integration test**

Run: `python -m pytest tests/test_evolve_integration.py -v`
Expected: PASS

- [ ] **Step 3: Update CLAUDE.md**

Add a "Self-Evolution (px-evolve)" section documenting:
- `tool-introspect`: what it does, gating (30 min cooldown)
- `tool-evolve`: what it does, gating (24h rate limit, introspection prerequisite)
- `px-evolve` daemon: worktree + Claude Sonnet + test + PR pipeline
- `spark_config.py`: what it contains and why it's separate
- New env vars: `PX_EVOLVE_DRY`, `PX_EVOLVE_MODEL`, `PX_EVOLVE_TIMEOUT`, `PX_EVOLVE_MAX_FILES`
- New state files: `introspection.json`, `evolve_queue.jsonl`, `evolve_log.jsonl`
- Safety model: file whitelist/blacklist, rate limits, PR gate

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest -x -q --ignore=tests/test_race.py`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md tests/test_evolve_integration.py
git commit -m "docs: add self-evolution section to CLAUDE.md + integration test"
```

---

### Task order and dependencies

```
Task 1 (spark_config extraction)
  ↓
Task 2 (tool-introspect)
  ↓
Task 3 (introspection context injection)
  ↓
Task 4 (tool-evolve)
  ↓
Task 5 (px-evolve daemon)
  ↓
Task 6 (integration + docs)
```

All tasks are sequential — each builds on the previous.
