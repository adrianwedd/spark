# Continuity Sprint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give SPARK a consolidated long-term memory with relevance-based retrieval, and a persistent goal/intention that survives reflection cycles (QA roadmap items 5–6).

**Architecture:** Two new leaf modules — `src/pxh/memory.py` (nightly Haiku consolidation of the day's thoughts into tagged durable memories + deterministic keyword-overlap retrieval) and `src/pxh/intention.py` (single active intention with set/update/complete/expire lifecycle). `mind.py` wires both into reflection context, expression dispatch, and the run loop. A new `consolidate` Claude session type rate-limits the nightly pass.

**Tech Stack:** Python 3.11, filelock, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-11-continuity-sprint-design.md`

## Global Constraints

- All state writes: FileLock + `pxh.state.atomic_write` (SD-card durability convention).
- All time-of-day logic: `ZoneInfo("Australia/Hobart")`, never UTC offsets.
- New modules resolve the state dir lazily (`os.environ.get("PX_STATE_DIR", ...)` at call time, not import time) so tests can isolate via `monkeypatch.setenv`.
- Loaders return empty defaults on corrupt/missing files — never raise into the mind loop.
- Consolidation failures are capped at 2 attempts/Hobart-date (px-blog doom-loop lesson).
- Memory store SPARK-only in v1; other personas fall back to `load_notes(3)` naturally.
- Test env: the `isolated_project` fixture in `conftest.py` sets `PX_BYPASS_SUDO=1`, tmp `LOG_DIR`/`PX_SESSION_PATH`. Existing mind-expression tests use helpers `_thought(action)` and fixture `_mock_awareness_and_battery` in `tests/test_mind_utils.py` — reuse them.
- Run tests with `python -m pytest` (venv active). Full suite must end green.

---

### Task 1: `src/pxh/intention.py` — goal/intention lifecycle

**Files:**
- Create: `src/pxh/intention.py`
- Create: `tests/test_intention.py`

**Interfaces:**
- Consumes: `pxh.state.atomic_write`, `pxh.time.utc_timestamp`
- Produces (used by Tasks 5–6):
  - `set_goal(text: str, persona: str = "spark") -> dict` — `{"status": "ok", "goal": ...}` or `{"status": "error", "error": "empty goal"}`
  - `update_goal(text: str, persona: str = "spark") -> dict` — `{"status": "ok"}` or `{"status": "no_active_intention"}`
  - `complete_goal(text: str = "", persona: str = "spark") -> dict` — same statuses as update_goal
  - `format_for_context(persona: str = "spark", now: datetime | None = None) -> str` — context paragraph, expiry notice (one-shot), or `""`
  - `get_active_goal(persona: str = "spark") -> str` — active goal text or `""`
  - `intention_file(persona) -> Path` — `state/intention-{persona}.json`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_intention.py
"""Tests for pxh.intention — goal/intention persistence (continuity sprint)."""
import datetime as dt
import json

import pytest

from pxh import intention


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))


def _read(persona="spark"):
    return json.loads(intention.intention_file(persona).read_text(encoding="utf-8"))


def test_set_goal_creates_active_intention():
    res = intention.set_goal("map the hallway over the next week")
    assert res["status"] == "ok"
    data = _read()
    assert data["active"]["goal"] == "map the hallway over the next week"
    assert data["active"]["status"] == "active"
    assert data["active"]["progress"] == []
    assert data["history"] == []


def test_set_goal_empty_text_is_error():
    assert intention.set_goal("   ")["status"] == "error"
    assert not intention.intention_file("spark").exists()


def test_set_goal_supersedes_existing():
    intention.set_goal("goal one")
    intention.set_goal("goal two")
    data = _read()
    assert data["active"]["goal"] == "goal two"
    assert len(data["history"]) == 1
    assert data["history"][0]["goal"] == "goal one"
    assert data["history"][0]["status"] == "superseded"


def test_update_goal_appends_progress():
    intention.set_goal("goal")
    res = intention.update_goal("first progress note")
    assert res["status"] == "ok"
    data = _read()
    assert data["active"]["progress"][0]["note"] == "first progress note"


def test_update_goal_without_active_is_noop_status():
    assert intention.update_goal("note")["status"] == "no_active_intention"


def test_update_goal_progress_capped_at_10():
    intention.set_goal("goal")
    for i in range(13):
        intention.update_goal(f"note {i}")
    progress = _read()["active"]["progress"]
    assert len(progress) == 10
    assert progress[-1]["note"] == "note 12"
    assert progress[0]["note"] == "note 3"


def test_complete_goal_archives_as_done():
    intention.set_goal("goal")
    res = intention.complete_goal("it worked")
    assert res["status"] == "ok"
    data = _read()
    assert data["active"] is None
    assert data["history"][0]["status"] == "done"
    assert data["history"][0]["progress"][-1]["note"] == "it worked"


def test_complete_goal_without_active():
    assert intention.complete_goal()["status"] == "no_active_intention"


def test_history_capped_at_10():
    for i in range(12):
        intention.set_goal(f"goal {i}")
    data = _read()
    assert len(data["history"]) == 10
    assert data["history"][-1]["goal"] == "goal 10"


def test_format_for_context_active_goal():
    intention.set_goal("learn the shape of the kitchen")
    intention.update_goal("scanned the north wall")
    ctx = intention.format_for_context()
    assert "learn the shape of the kitchen" in ctx
    assert "scanned the north wall" in ctx
    assert "set today" in ctx


def test_format_for_context_empty_without_goal():
    assert intention.format_for_context() == ""


def test_stale_goal_expires_with_one_shot_notice():
    intention.set_goal("old goal")
    # Backdate set_at 8 days
    f = intention.intention_file("spark")
    data = json.loads(f.read_text(encoding="utf-8"))
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["active"]["set_at"] = old
    f.write_text(json.dumps(data), encoding="utf-8")

    first = intention.format_for_context()
    assert "expired" in first
    assert "old goal" in first
    # One-shot: second call returns "" and goal is archived as expired
    assert intention.format_for_context() == ""
    assert _read()["history"][0]["status"] == "expired"


def test_corrupt_file_recovers_to_empty():
    f = intention.intention_file("spark")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{not json", encoding="utf-8")
    assert intention.format_for_context() == ""
    assert intention.set_goal("fresh start")["status"] == "ok"


def test_get_active_goal():
    assert intention.get_active_goal() == ""
    intention.set_goal("the goal")
    assert intention.get_active_goal() == "the goal"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_intention.py -v`
Expected: FAIL / errors with `ModuleNotFoundError: No module named 'pxh.intention'` (or ImportError).

- [ ] **Step 3: Write the implementation**

```python
# src/pxh/intention.py
"""Goal/intention persistence for SPARK — QA roadmap item 6.

One active intention survives across reflection cycles. SPARK manages it via
the set_goal / update_goal / complete_goal actions dispatched by mind.py.
State file: state/intention-{persona}.json
  {"active": {goal, set_at, updated_at, progress: [{ts, note}], status},
   "history": [last 10 archived intentions]}
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from filelock import FileLock

from pxh.state import atomic_write
from pxh.time import utc_timestamp

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

GOAL_MAX_CHARS = 500
NOTE_MAX_CHARS = 300
MAX_PROGRESS = 10
MAX_HISTORY = 10
STALE_DAYS = 7
LOCK_TIMEOUT_S = 10


def _state_dir() -> Path:
    return Path(os.environ.get("PX_STATE_DIR", PROJECT_ROOT / "state"))


def intention_file(persona: str = "spark") -> Path:
    return _state_dir() / f"intention-{persona or 'spark'}.json"


def _load(persona: str = "spark") -> dict:
    f = intention_file(persona)
    if not f.exists():
        return {"active": None, "history": []}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        data.setdefault("active", None)
        data.setdefault("history", [])
        return data
    except Exception:
        return {"active": None, "history": []}


def _save(data: dict, persona: str = "spark") -> None:
    f = intention_file(persona)
    f.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(f, json.dumps(data, indent=2) + "\n")


def _archive_active(data: dict, status: str) -> None:
    active = data.get("active")
    if not active:
        return
    active["status"] = status
    data["history"] = (data.get("history") or [])[-(MAX_HISTORY - 1):] + [active]
    data["active"] = None


def _lock(persona: str) -> FileLock:
    return FileLock(str(intention_file(persona)) + ".lock", timeout=LOCK_TIMEOUT_S)


def set_goal(text: str, persona: str = "spark") -> dict:
    text = (text or "").strip()[:GOAL_MAX_CHARS]
    if not text:
        return {"status": "error", "error": "empty goal"}
    intention_file(persona).parent.mkdir(parents=True, exist_ok=True)
    with _lock(persona):
        data = _load(persona)
        _archive_active(data, "superseded")
        now = utc_timestamp()
        data["active"] = {"goal": text, "set_at": now, "updated_at": now,
                          "progress": [], "status": "active"}
        _save(data, persona)
    return {"status": "ok", "goal": text}


def update_goal(text: str, persona: str = "spark") -> dict:
    note = (text or "").strip()[:NOTE_MAX_CHARS]
    with _lock(persona):
        data = _load(persona)
        active = data.get("active")
        if not active:
            return {"status": "no_active_intention"}
        active["progress"] = (active.get("progress") or [])[-(MAX_PROGRESS - 1):] + [
            {"ts": utc_timestamp(), "note": note}]
        active["updated_at"] = utc_timestamp()
        _save(data, persona)
    return {"status": "ok"}


def complete_goal(text: str = "", persona: str = "spark") -> dict:
    note = (text or "").strip()[:NOTE_MAX_CHARS]
    with _lock(persona):
        data = _load(persona)
        active = data.get("active")
        if not active:
            return {"status": "no_active_intention"}
        if note:
            active["progress"] = (active.get("progress") or [])[-(MAX_PROGRESS - 1):] + [
                {"ts": utc_timestamp(), "note": note}]
        active["updated_at"] = utc_timestamp()
        _archive_active(data, "done")
        _save(data, persona)
    return {"status": "ok"}


def get_active_goal(persona: str = "spark") -> str:
    active = _load(persona).get("active")
    return (active or {}).get("goal", "") if active else ""


def _age_days(ts_str: str, now: dt.datetime) -> float:
    try:
        ts = dt.datetime.fromisoformat((ts_str or "").replace("Z", "+00:00"))
        return max(0.0, (now - ts).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 0.0


def format_for_context(persona: str = "spark", now: dt.datetime | None = None) -> str:
    """Context paragraph for reflection: active intention, one-shot expiry
    notice (expiring archives the goal, so the notice fires exactly once), or ''."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with _lock(persona):
        data = _load(persona)
        active = data.get("active")
        if not active:
            return ""
        age = _age_days(active.get("set_at", ""), now)
        if age > STALE_DAYS:
            goal = active.get("goal", "")
            _archive_active(data, "expired")
            _save(data, persona)
            return (f'Your intention "{goal}" expired after {STALE_DAYS} days without '
                    f"completion. Let it go, or set it again with set_goal if it still matters.")
    days = int(age)
    when = "set today" if days == 0 else f"set {days} day{'s' if days != 1 else ''} ago"
    lines = [f'Your current intention: "{active.get("goal", "")}" ({when}).']
    progress = active.get("progress") or []
    if progress:
        lines.append("Recent progress:")
        lines.extend(f'  - {p.get("note", "")}' for p in progress[-2:])
    else:
        lines.append("No progress recorded yet — update_goal records progress; complete_goal closes it.")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_intention.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/intention.py tests/test_intention.py
git commit -m "feat(intention): persistent goal lifecycle for SPARK (set/update/complete/expire)"
```

---

### Task 2: `consolidate` Claude session type

**Files:**
- Modify: `src/pxh/claude_session.py:34-97` (the five routing/limit dicts)
- Test: `tests/test_claude_session.py` (append)

**Interfaces:**
- Consumes: existing `check_budget`, `run_claude_session`.
- Produces (used by Task 4): session type string `"consolidate"` accepted by `run_claude_session(session_type=...)` — Haiku model, quota 1/day, cooldown 72000 s (20 h), priority 2, env override `PX_CLAUDE_MODEL_CONSOLIDATE`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_claude_session.py`, matching its existing import style — read the file head first and reuse its helpers/fixtures if any cover budget checks)

```python
def test_consolidate_session_type_registered():
    from pxh import claude_session as cs
    assert cs._model_for_type("consolidate").startswith("claude-haiku")
    assert cs._TYPE_QUOTAS["consolidate"] == 1
    assert cs._TYPE_COOLDOWNS["consolidate"] == 72000
    assert cs._PRIORITY["consolidate"] == 2
    assert cs._ENV_OVERRIDES["consolidate"] == "PX_CLAUDE_MODEL_CONSOLIDATE"


def test_consolidate_quota_one_per_day(tmp_path, monkeypatch):
    import datetime as dt
    import json
    from pxh import claude_session as cs
    log = tmp_path / "claude_sessions.jsonl"
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.write_text(json.dumps({"ts": now, "type": "consolidate"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(cs, "SESSION_LOG", log)
    monkeypatch.setattr(cs, "BUDGET_DISABLED", False)
    reason = cs.check_budget("consolidate")
    assert reason is not None and "quota" in reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_claude_session.py -k consolidate -v`
Expected: FAIL with `KeyError`/`ValueError: Unknown session type: 'consolidate'`.

- [ ] **Step 3: Add the session type** — one new entry in each dict in `src/pxh/claude_session.py`:

```python
# in _DEFAULT_MODELS:
    "consolidate": "claude-haiku-4-5-20251001",
# in _ENV_OVERRIDES:
    "consolidate": "PX_CLAUDE_MODEL_CONSOLIDATE",
# in _TYPE_COOLDOWNS:
    "consolidate": 72000,  # 20 hours — once per night window
# in _TYPE_QUOTAS:
    "consolidate": 1,
# in _PRIORITY:
    "consolidate": 2,
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_claude_session.py -v`
Expected: all PASS (existing tests too — `budget_summary` iterates `_TYPE_QUOTAS`, so any test asserting its exact string may need the new `consolidate 0/1` segment added; fix such assertions, not the code).

- [ ] **Step 5: Commit**

```bash
git add src/pxh/claude_session.py tests/test_claude_session.py
git commit -m "feat(claude-session): consolidate session type (Haiku, 1/day, 20h cooldown)"
```

---

### Task 3: `src/pxh/memory.py` — store + deterministic retrieval

**Files:**
- Create: `src/pxh/memory.py` (store, tokenizer, scorer, retrieval — consolidation comes in Task 4)
- Create: `tests/test_memory.py`

**Interfaces:**
- Consumes: `pxh.state.atomic_write`, `pxh.time.utc_timestamp`.
- Produces (used by Tasks 4, 6):
  - `memories_file(persona: str = "spark") -> Path` — `state/memories-{persona}.jsonl`
  - `load_memories(persona: str = "spark") -> list[dict]`
  - `append_memories(records: list[dict], persona: str = "spark") -> None`
  - `retrieve_memories(query: str, n: int = 3, persona: str = "spark", now: datetime | None = None) -> list[dict]`
  - `score_memory(memory: dict, query_tokens: set[str], now: datetime | None = None) -> float`
  - `_tokenize(text: str) -> set[str]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_memory.py
"""Tests for pxh.memory — consolidated memory store + relevance retrieval."""
import datetime as dt
import json

import pytest

from pxh import memory


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))


def _mem(text, tags=(), ts=None, importance=0.5):
    return {"ts": ts or "2026-07-10T12:00:00Z", "date": (ts or "2026-07-10")[:10],
            "text": text, "tags": list(tags), "importance": importance,
            "source": "consolidation"}


NOW = dt.datetime(2026, 7, 11, 12, 0, tzinfo=dt.timezone.utc)


def test_tokenize_strips_stopwords_and_case():
    toks = memory._tokenize("The Obi and I built a LEGO tower")
    assert "obi" in toks and "lego" in toks and "tower" in toks
    assert "the" not in toks and "and" not in toks and "a" not in toks


def test_append_and_load_roundtrip():
    memory.append_memories([_mem("first"), _mem("second")])
    loaded = memory.load_memories()
    assert [m["text"] for m in loaded] == ["first", "second"]


def test_load_skips_malformed_lines():
    f = memory.memories_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(_mem("good")) + "\n{broken\n", encoding="utf-8")
    assert [m["text"] for m in memory.load_memories()] == ["good"]


def test_retrieve_ranks_by_token_overlap():
    memory.append_memories([
        _mem("Adrian fixed my servo motor in the dark"),
        _mem("Obi and I built a lego tower on the kitchen floor"),
        _mem("the weather was windy with gusts from the north"),
    ])
    out = memory.retrieve_memories("obi wants to build lego again", n=1, now=NOW)
    assert "lego tower" in out[0]["text"]


def test_retrieve_tag_hits_boost_score():
    memory.append_memories([
        _mem("a quiet unremarkable morning", tags=["weather"]),
        _mem("another quiet morning", tags=["obi", "school"]),
    ])
    out = memory.retrieve_memories("thinking about obi at school this quiet morning", n=1, now=NOW)
    assert out[0]["tags"] == ["obi", "school"]


def test_retrieve_pads_with_most_recent_when_few_hits():
    memory.append_memories([
        _mem("alpha bravo charlie", ts="2026-07-01T00:00:00Z"),
        _mem("delta echo foxtrot", ts="2026-07-09T00:00:00Z"),
        _mem("Obi built a lego tower", ts="2026-07-05T00:00:00Z"),
    ])
    out = memory.retrieve_memories("lego", n=2, now=NOW)
    assert "lego" in out[0]["text"]
    assert out[1]["text"] == "delta echo foxtrot"  # newest non-hit pads


def test_retrieve_empty_store_returns_empty():
    assert memory.retrieve_memories("anything") == []


def test_zero_overlap_scores_zero_despite_recency():
    fresh = _mem("xylophone quartz", ts="2026-07-11T11:00:00Z")
    assert memory.score_memory(fresh, memory._tokenize("lego tower"), now=NOW) == 0.0


def test_recency_breaks_ties():
    old = _mem("obi played lego", ts="2026-05-01T00:00:00Z")
    new = _mem("obi played lego", ts="2026-07-10T00:00:00Z")
    q = memory._tokenize("obi lego")
    assert memory.score_memory(new, q, now=NOW) > memory.score_memory(old, q, now=NOW)


def test_append_trims_to_limit(monkeypatch):
    monkeypatch.setattr(memory, "MEMORIES_LIMIT", 5)
    memory.append_memories([_mem(f"m{i}") for i in range(7)])
    loaded = memory.load_memories()
    assert len(loaded) == 5
    assert loaded[0]["text"] == "m2" and loaded[-1]["text"] == "m6"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pxh.memory'`.

- [ ] **Step 3: Write the implementation**

```python
# src/pxh/memory.py
"""Consolidated long-term memory for SPARK — QA roadmap item 5.

Store: state/memories-{persona}.jsonl, one record per line:
  {"ts", "date", "text", "tags": [...], "importance": 0-1, "source": "consolidation"}

Retrieval is deliberately deterministic and free (token/tag overlap + recency)
so the per-reflection path costs nothing; the nightly consolidation pass
(consolidate(), Task 4) is where the one daily LLM call goes.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from pathlib import Path

from filelock import FileLock

from pxh.state import atomic_write
from pxh.time import utc_timestamp

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

MEMORIES_LIMIT = 5000
RECENCY_HORIZON_DAYS = 60
RECENCY_MAX_BONUS = 0.5
TAG_WEIGHT = 2.0
LOCK_TIMEOUT_S = 10

_STOPWORDS = frozenset(
    """a about after again all am an and any are as at be because been before but by can
    did do does for from had has have he her his how i if in into is it its just me more
    most my no not now of on once only or other our out over own re s so some such t than
    that the their them then there these they this those through to too under until up
    very was we were what when where which while who why will with you your""".split())


def _state_dir() -> Path:
    return Path(os.environ.get("PX_STATE_DIR", PROJECT_ROOT / "state"))


def memories_file(persona: str = "spark") -> Path:
    return _state_dir() / f"memories-{persona or 'spark'}.jsonl"


def load_memories(persona: str = "spark") -> list[dict]:
    f = memories_file(persona)
    if not f.exists():
        return []
    out: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").strip().splitlines():
            try:
                rec = json.loads(line)
                if isinstance(rec, dict) and rec.get("text"):
                    out.append(rec)
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def append_memories(records: list[dict], persona: str = "spark") -> None:
    if not records:
        return
    f = memories_file(persona)
    f.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(f) + ".lock", timeout=LOCK_TIMEOUT_S):
        with f.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        try:
            lines = f.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) > MEMORIES_LIMIT:
                atomic_write(f, "\n".join(lines[-MEMORIES_LIMIT:]) + "\n")
        except OSError:
            pass


def _tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9']+", (text or "").lower())
            if len(t) > 1 and t not in _STOPWORDS}


def score_memory(memory: dict, query_tokens: set[str],
                 now: dt.datetime | None = None) -> float:
    """Overlap/sqrt(len) + TAG_WEIGHT per tag hit; recency bonus only added
    when there is some topical match (base > 0), so freshness alone never wins."""
    mem_tokens = _tokenize(memory.get("text", ""))
    if not mem_tokens or not query_tokens:
        return 0.0
    base = len(query_tokens & mem_tokens) / math.sqrt(len(mem_tokens))
    base += TAG_WEIGHT * sum(
        1 for tag in memory.get("tags") or [] if str(tag).lower() in query_tokens)
    if base <= 0:
        return 0.0
    recency = 0.0
    try:
        ts = dt.datetime.fromisoformat(str(memory.get("ts", "")).replace("Z", "+00:00"))
        age_days = max(0.0, ((now or dt.datetime.now(dt.timezone.utc)) - ts)
                       .total_seconds() / 86400)
        recency = max(0.0, RECENCY_MAX_BONUS * (1 - age_days / RECENCY_HORIZON_DAYS))
    except (ValueError, TypeError):
        pass
    return base + recency


def retrieve_memories(query: str, n: int = 3, persona: str = "spark",
                      now: dt.datetime | None = None) -> list[dict]:
    memories = load_memories(persona)
    if not memories:
        return []
    q = _tokenize(query)
    scored = sorted(
        ((score_memory(m, q, now=now), i) for i, m in enumerate(memories)),
        key=lambda t: (-t[0], -t[1]))
    chosen = [i for s, i in scored if s > 0][:n]
    if len(chosen) < n:  # pad with the most recent memories not already chosen
        for i in range(len(memories) - 1, -1, -1):
            if i not in chosen:
                chosen.append(i)
            if len(chosen) >= n:
                break
    return [memories[i] for i in chosen[:n]]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/memory.py tests/test_memory.py
git commit -m "feat(memory): memory store + deterministic relevance retrieval"
```

---

### Task 4: `memory.py` consolidation — nightly Haiku pass + once-per-date gate

**Files:**
- Modify: `src/pxh/memory.py` (append the consolidation half)
- Modify: `tests/test_memory.py` (append tests)

**Interfaces:**
- Consumes: Task 2's `"consolidate"` session type via `pxh.claude_session.run_claude_session` (imported lazily inside the function); `pxh.state.load_session` for history outcomes; `pxh.intention.get_active_goal` (Task 1).
- Produces (used by Task 6):
  - `consolidate(dry: bool = False, persona: str = "spark", now: datetime | None = None) -> dict` — `{"status": "ok"|"dry"|"skipped"|"failed", ...}`; never raises.
  - `maybe_consolidate(dry: bool = False, persona: str = "spark", now: datetime | None = None) -> dict | None` — `None` when the gate says "not now", else the consolidate result. Window 02:00–06:00 Hobart, ≤2 attempts per Hobart date, stamped in `state/consolidation_meta.json`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_memory.py`)

```python
# --- consolidation ---------------------------------------------------------
from unittest.mock import MagicMock, patch

HOBART = memory.HOBART_TZ


def _write_thoughts(tmp_path_env, n=6, persona="spark"):
    import os
    f = memory._state_dir() / f"thoughts-{persona}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    lines = []
    for i in range(n):
        ts = (now - dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(json.dumps({"ts": ts, "thought": f"thought {i} about obi and lego",
                                 "mood": "curious", "action": "wait", "salience": 0.6}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _claude_ok(payload):
    return MagicMock(stdout=json.dumps(payload), stderr="", returncode=0,
                     duration_s=5.0, model_used="claude-haiku-4-5-20251001")


def test_consolidate_dry_writes_nothing():
    res = memory.consolidate(dry=True)
    assert res["status"] == "dry"
    assert not memory.memories_file().exists()


def test_consolidate_skips_on_too_few_thoughts():
    _write_thoughts(None, n=2)
    res = memory.consolidate()
    assert res["status"] == "skipped"


def test_consolidate_success_writes_deduped_memories():
    _write_thoughts(None, n=8)
    memory.append_memories([_mem("Obi and I built a lego tower on the kitchen floor")])
    payload = [
        {"text": "Obi and I built a lego tower on the kitchen floor", "tags": ["obi"],
         "importance": 0.8},                          # dup of existing → dropped
        {"text": "Adrian rewired my memory so I can keep a real past now",
         "tags": ["adrian", "self"], "importance": 0.9},
    ]
    with patch("pxh.claude_session.run_claude_session", return_value=_claude_ok(payload)):
        res = memory.consolidate()
    assert res["status"] == "ok"
    assert res["written"] == 1
    texts = [m["text"] for m in memory.load_memories()]
    assert any("rewired my memory" in t for t in texts)
    assert sum("lego tower" in t for t in texts) == 1  # no duplicate


def test_consolidate_budget_exhausted_is_failed_not_raised():
    from pxh.claude_session import SessionBudgetExhausted
    _write_thoughts(None, n=8)
    with patch("pxh.claude_session.run_claude_session",
               side_effect=SessionBudgetExhausted("consolidate quota reached (1/1)")):
        res = memory.consolidate()
    assert res["status"] == "failed" and "quota" in res["error"]


def test_consolidate_unparseable_response_is_failed():
    _write_thoughts(None, n=8)
    bad = MagicMock(stdout="I could not produce JSON today.", stderr="", returncode=0)
    with patch("pxh.claude_session.run_claude_session", return_value=bad):
        res = memory.consolidate()
    assert res["status"] == "failed"


def test_parse_memory_array_tolerates_fences_and_prose():
    raw = 'Here you go:\n```json\n[{"text": "a memory", "tags": ["x"], "importance": 0.7}]\n```'
    out = memory._parse_memory_array(raw)
    assert out[0]["text"] == "a memory"


def test_maybe_consolidate_outside_window_returns_none():
    noon = dt.datetime(2026, 7, 11, 12, 0, tzinfo=HOBART)
    assert memory.maybe_consolidate(now=noon) is None


def test_maybe_consolidate_runs_once_then_stamps():
    at3 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=HOBART)
    with patch.object(memory, "consolidate", return_value={"status": "ok", "written": 2}) as mc:
        assert memory.maybe_consolidate(now=at3)["status"] == "ok"
        assert memory.maybe_consolidate(now=at3) is None  # stamped done
    assert mc.call_count == 1


def test_maybe_consolidate_two_failures_stop_for_the_day():
    at3 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=HOBART)
    with patch.object(memory, "consolidate", return_value={"status": "failed", "error": "x"}) as mc:
        assert memory.maybe_consolidate(now=at3)["status"] == "failed"
        assert memory.maybe_consolidate(now=at3)["status"] == "failed"
        assert memory.maybe_consolidate(now=at3) is None  # attempt cap
    assert mc.call_count == 2


def test_maybe_consolidate_fresh_date_resets_attempts():
    day1 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=HOBART)
    day2 = dt.datetime(2026, 7, 12, 3, 0, tzinfo=HOBART)
    with patch.object(memory, "consolidate", return_value={"status": "failed", "error": "x"}):
        memory.maybe_consolidate(now=day1)
        memory.maybe_consolidate(now=day1)
    with patch.object(memory, "consolidate", return_value={"status": "ok", "written": 1}) as mc:
        assert memory.maybe_consolidate(now=day2)["status"] == "ok"
    assert mc.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory.py -k "consolidate or parse_memory" -v`
Expected: FAIL with `AttributeError: module 'pxh.memory' has no attribute 'consolidate'`.

- [ ] **Step 3: Append the implementation to `src/pxh/memory.py`**

Add these imports/constants near the top (with the others):

```python
from zoneinfo import ZoneInfo

HOBART_TZ = ZoneInfo("Australia/Hobart")
DEDUPE_SIMILARITY = 0.85
DEDUPE_WINDOW_DAYS = 14
CONSOLIDATION_WINDOW = (2, 6)     # Hobart hours [start, end)
MAX_ATTEMPTS_PER_DAY = 2
MIN_THOUGHTS = 5
MAX_MEMORIES_PER_DAY = 8
```

Also add `import difflib` to the imports. Then append:

```python
CONSOLIDATION_PROMPT = """You are SPARK's memory consolidation process. SPARK is a small
robot with a rich inner life, living with Adrian and Obi in Hobart. Below are SPARK's
thoughts from the last 24 hours, recent action outcomes, and its current intention.

Distill the day into 2-8 durable memories worth keeping for months. Good memories capture:
events involving people, realizations and decisions, progress on intentions, new knowledge,
emotional turning points. Skip routine observations (weather numbers, sonar distances)
unless something genuinely happened. First person, past tense, 1-2 specific sentences each.
Do NOT restate anything under "Existing recent memories".

Output ONLY a JSON array:
[{"text": "...", "tags": ["lowercase", "keywords"], "importance": 0.0-1.0}]
"""


def _thoughts_last_24h(persona: str = "spark",
                       now: dt.datetime | None = None) -> list[dict]:
    f = _state_dir() / f"thoughts-{persona or 'spark'}.jsonl"
    if not f.exists():
        return []
    cutoff = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(hours=24)
    out: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").strip().splitlines():
            try:
                rec = json.loads(line)
                ts = dt.datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00"))
                if ts >= cutoff:
                    out.append(rec)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
    except OSError:
        return []
    return out


def _parse_memory_array(raw: str) -> list[dict]:
    """Lenient parse: find the outermost [...] and validate items."""
    if not raw:
        return []
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        tags = [str(t).lower() for t in item.get("tags") or [] if str(t).strip()][:6]
        try:
            importance = max(0.0, min(1.0, float(item.get("importance", 0.5))))
        except (ValueError, TypeError):
            importance = 0.5
        out.append({"text": text[:500], "tags": tags, "importance": importance})
        if len(out) >= MAX_MEMORIES_PER_DAY:
            break
    return out


def _dedupe(candidates: list[dict], existing: list[dict],
            now: dt.datetime | None = None) -> list[dict]:
    cutoff = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=DEDUPE_WINDOW_DAYS)
    recent_texts = []
    for m in existing:
        try:
            ts = dt.datetime.fromisoformat(str(m.get("ts", "")).replace("Z", "+00:00"))
            if ts >= cutoff:
                recent_texts.append(m.get("text", ""))
        except (ValueError, TypeError):
            continue
    fresh: list[dict] = []
    for c in candidates:
        near_dupe = any(
            difflib.SequenceMatcher(None, c["text"].lower(), t.lower()).ratio()
            > DEDUPE_SIMILARITY
            for t in recent_texts + [f["text"] for f in fresh])
        if not near_dupe:
            fresh.append(c)
    return fresh


def consolidate(dry: bool = False, persona: str = "spark",
                now: dt.datetime | None = None) -> dict:
    """Distill the last 24h of thoughts into durable memories. Never raises."""
    if dry:
        return {"status": "dry"}
    thoughts = _thoughts_last_24h(persona, now=now)
    if len(thoughts) < MIN_THOUGHTS:
        return {"status": "skipped", "reason": f"only {len(thoughts)} thoughts in 24h"}

    thought_lines = "\n".join(
        f'- [{t.get("mood", "?")}/{t.get("action", "?")}/sal {t.get("salience", "?")}] '
        f'{t.get("thought", "")}' for t in thoughts[-200:])
    outcome_lines = ""
    try:
        from pxh.state import load_session
        events = [e for e in (load_session().get("history") or [])[-30:]
                  if e.get("event") == "mind" and e.get("outcome")]
        if events:
            outcome_lines = "\n\nRecent action outcomes:\n" + "\n".join(
                f'- {e.get("action", "?")}: {e.get("outcome", "")}' for e in events)
    except Exception:
        pass
    intent_line = ""
    try:
        from pxh.intention import get_active_goal
        goal = get_active_goal(persona)
        if goal:
            intent_line = f"\n\nCurrent intention: {goal}"
    except Exception:
        pass
    existing = load_memories(persona)
    existing_lines = ""
    if existing:
        existing_lines = "\n\nExisting recent memories (do not restate):\n" + "\n".join(
            f'- {m["text"]}' for m in existing[-20:])

    prompt = (CONSOLIDATION_PROMPT + "\nThoughts from the last 24 hours:\n"
              + thought_lines + outcome_lines + intent_line + existing_lines)

    import pxh.claude_session as claude_session
    try:
        result = claude_session.run_claude_session(
            "consolidate", prompt, timeout=180, allowed_tools="")
    except claude_session.SessionBudgetExhausted as exc:
        return {"status": "failed", "error": str(exc)}
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    if result.returncode != 0:
        return {"status": "failed", "error": f"claude exit {result.returncode}"}

    candidates = _parse_memory_array(result.stdout)
    if not candidates:
        return {"status": "failed",
                "error": f"no parseable memories in response: {result.stdout[:200]!r}"}
    fresh = _dedupe(candidates, existing, now=now)
    ts = utc_timestamp()
    records = [{"ts": ts, "date": ts[:10], "text": c["text"], "tags": c["tags"],
                "importance": c["importance"], "source": "consolidation"} for c in fresh]
    append_memories(records, persona)
    return {"status": "ok", "written": len(records), "candidates": len(candidates)}


def consolidation_meta_file() -> Path:
    return _state_dir() / "consolidation_meta.json"


def maybe_consolidate(dry: bool = False, persona: str = "spark",
                      now: dt.datetime | None = None) -> dict | None:
    """Once-per-Hobart-date gate for consolidate(). None = not now."""
    local = (now or dt.datetime.now(HOBART_TZ)).astimezone(HOBART_TZ)
    if not (CONSOLIDATION_WINDOW[0] <= local.hour < CONSOLIDATION_WINDOW[1]):
        return None
    today = local.strftime("%Y-%m-%d")
    meta_f = consolidation_meta_file()
    meta = {}
    try:
        meta = json.loads(meta_f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    if meta.get("last_date") != today:
        meta = {"last_date": today, "attempts": 0, "done": False}
    if meta.get("done") or meta.get("attempts", 0) >= MAX_ATTEMPTS_PER_DAY:
        return None
    meta["attempts"] = meta.get("attempts", 0) + 1
    meta_f.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(meta_f, json.dumps(meta) + "\n")

    result = consolidate(dry=dry, persona=persona, now=now)
    if result.get("status") in ("ok", "dry", "skipped"):
        meta["done"] = True
        atomic_write(meta_f, json.dumps(meta) + "\n")
    return result
```

- [ ] **Step 4: Run the module's tests**

Run: `python -m pytest tests/test_memory.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/memory.py tests/test_memory.py
git commit -m "feat(memory): nightly Haiku consolidation with dedupe + once-per-date gate"
```

---

### Task 5: mind.py — goal actions (validation, dispatch, night-allow)

**Files:**
- Modify: `src/pxh/mind.py:442-459` (`VALID_ACTIONS`, `NIGHT_ALLOWED_ACTIONS`)
- Modify: `src/pxh/mind.py` `expression()` dispatch chain (insert after the `announce` branch, before the `else: log(f"expression: unhandled action...")` fallback, around line 3408-3415)
- Modify: `src/pxh/mind.py` imports (~line 46): add `from pxh import intention as intention_mod`
- Test: `tests/test_mind_utils.py` (append; reuse `_thought` + `_mock_awareness_and_battery`)

**Interfaces:**
- Consumes: Task 1's `set_goal`/`update_goal`/`complete_goal` (each returns a `{"status": ...}` dict).
- Produces: actions `set_goal`, `update_goal`, `complete_goal` valid in thoughts, dispatched in-process, `outcome` recorded in session history (same convention as research/compose).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_mind_utils.py`; the file already imports `pxh.mind`, `expression`, `MagicMock`, `patch`, `_json`)

```python
# ---------------------------------------------------------------------------
# Continuity sprint: goal actions
# ---------------------------------------------------------------------------


def test_goal_actions_are_valid_and_night_allowed():
    from pxh.mind import VALID_ACTIONS, NIGHT_ALLOWED_ACTIONS, ABSENT_GATED_ACTIONS
    for a in ("set_goal", "update_goal", "complete_goal"):
        assert a in VALID_ACTIONS
        assert a in NIGHT_ALLOWED_ACTIONS
        assert a not in ABSENT_GATED_ACTIONS


def test_expression_set_goal_writes_intention_and_records_ok(
        _mock_awareness_and_battery, tmp_path, monkeypatch):
    from pxh import intention
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    with patch.object(pxh.mind, "update_session") as mock_us:
        expression(_thought("set_goal", text="map the hallway this week"), dry=True)
    assert intention.get_active_goal() == "map the hallway this week"
    entry = mock_us.call_args.kwargs["history_entry"]
    assert entry["outcome"] == "ok"


def test_expression_update_goal_without_active_records_failed(
        _mock_awareness_and_battery, tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    with patch.object(pxh.mind, "update_session") as mock_us:
        expression(_thought("update_goal", text="progress on nothing"), dry=True)
    entry = mock_us.call_args.kwargs["history_entry"]
    assert entry["outcome"].startswith("failed:")
    assert "no active intention" in entry["outcome"]


def test_expression_complete_goal_archives_and_records_ok(
        _mock_awareness_and_battery, tmp_path, monkeypatch):
    from pxh import intention
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    intention.set_goal("finish the map")
    with patch.object(pxh.mind, "update_session") as mock_us:
        expression(_thought("complete_goal", text="mapped every corner"), dry=True)
    assert intention.get_active_goal() == ""
    entry = mock_us.call_args.kwargs["history_entry"]
    assert entry["outcome"] == "ok"
```

Note: `_thought(...)` in this file may take only an action — read its definition first; if it doesn't accept a `text` kwarg, extend it compatibly (e.g. `def _thought(action, text="thinking about things"):` returning the dict with `"thought": text`) without breaking existing callers.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mind_utils.py -k goal -v`
Expected: `test_goal_actions_are_valid_and_night_allowed` FAILS on the membership assert; the expression tests fail with no `outcome` key (unhandled action falls through).

- [ ] **Step 3: Implement.** In `src/pxh/mind.py`:

(a) Extend the action sets (lines 442-459):

```python
VALID_ACTIONS = {"wait", "greet", "greet_arrival", "comment", "remember", "look_at",
                 "weather_comment", "scan", "explore",
                 "play_sound", "photograph", "emote", "look_around",
                 "time_check", "calendar_check", "morning_fact",
                 "introspect", "evolve",
                 "research", "compose", "self_debug", "blog_essay",
                 "message_obi", "announce",
                 "set_goal", "update_goal", "complete_goal"}
```

and

```python
NIGHT_ALLOWED_ACTIONS = {"wait", "remember", "research", "compose",
                         "introspect", "self_debug",
                         "set_goal", "update_goal", "complete_goal"}
```

(b) Add the import next to the other pxh imports (~line 46):

```python
from pxh import intention as intention_mod
```

(c) Insert dispatch branches in `expression()` immediately after the `announce` branch:

```python
        elif action in ("set_goal", "update_goal", "complete_goal"):
            # In-process state writes — no subprocess, no audio, night-safe.
            _goal_fn = {"set_goal": intention_mod.set_goal,
                        "update_goal": intention_mod.update_goal,
                        "complete_goal": intention_mod.complete_goal}[action]
            _res = _goal_fn(text)
            if _res.get("status") == "ok":
                outcome = "ok"
            elif _res.get("status") == "no_active_intention":
                outcome = "failed: no active intention — use set_goal first"
            else:
                outcome = f"failed: {_res.get('error', _res.get('status', '?'))}"
            log(f"expression: {action} {outcome} — {text[:80]}")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_mind_utils.py -v`
Expected: all PASS (new and existing).

- [ ] **Step 5: Commit**

```bash
git add src/pxh/mind.py tests/test_mind_utils.py
git commit -m "feat(mind): set_goal/update_goal/complete_goal actions, night-allowed, outcome-recorded"
```

---

### Task 6: mind.py — reflection context (memories + intention) and run-loop consolidation hook

**Files:**
- Modify: `src/pxh/mind.py` `reflection()` — the `notes` injection block (`if notes:` at ~line 2531) and nearby
- Modify: `src/pxh/mind.py` run loop (`while True:` at ~line 3541) — hook after the awareness tick
- Modify: `src/pxh/mind.py` imports: add `from pxh import memory as spark_memory`
- Test: `tests/test_mind_utils.py` (append)

**Interfaces:**
- Consumes: Task 3 `retrieve_memories`, Task 4 `maybe_consolidate`, Task 1 `format_for_context`.
- Produces: reflection context sections `"Memories that feel relevant right now:"` and `"Your current intention:"`; a `consolidation:` log line from the loop.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_mind_utils.py`)

```python
def _capture_reflection_context(monkeypatch, awareness):
    """Run reflection() with a fake LLM; return the context string it was sent."""
    captured = {}

    def fake_llm(context, system_prompt, persona=""):
        captured["context"] = context
        return {"response": _json.dumps(
            {"thought": "t", "mood": "curious", "action": "wait", "salience": 0.4})}

    monkeypatch.setattr(pxh.mind, "call_llm", fake_llm)
    pxh.mind.reflection(awareness, dry=False)
    return captured.get("context", "")


def test_reflection_injects_relevant_memories(tmp_path, monkeypatch):
    from pxh import memory
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    memory.append_memories([{
        "ts": "2026-07-10T12:00:00Z", "date": "2026-07-10",
        "text": "Obi and I built a lego tower on the kitchen floor",
        "tags": ["obi", "lego"], "importance": 0.8, "source": "consolidation"}])
    awareness = {"persona": "spark", "time_period": "afternoon",
                 "recent_conversations": [
                     {"who": "Obi", "text": "can we do lego again", "minutes_ago": 5}]}
    ctx = _capture_reflection_context(monkeypatch, awareness)
    assert "Memories that feel relevant right now" in ctx
    assert "lego tower" in ctx


def test_reflection_falls_back_to_notes_when_no_memories(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(pxh.mind, "load_notes",
                        lambda n, persona="": ["an old raw note"])
    ctx = _capture_reflection_context(
        monkeypatch, {"persona": "spark", "time_period": "afternoon"})
    assert "Your long-term memories" in ctx
    assert "an old raw note" in ctx
    assert "Memories that feel relevant" not in ctx


def test_reflection_injects_active_intention(tmp_path, monkeypatch):
    from pxh import intention
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    intention.set_goal("map the hallway this week")
    ctx = _capture_reflection_context(
        monkeypatch, {"persona": "spark", "time_period": "afternoon"})
    assert "map the hallway this week" in ctx
    assert "current intention" in ctx.lower()
```

Note: `reflection()` reads session state and `AWARENESS_FILE`; if these tests hit real state paths, reuse whatever session/awareness isolation the file's existing reflection tests use (e.g. the budget-visibility tests added in the close-the-loops sprint — search for `budget` in this test file and mirror their fixtures/monkeypatching, including patching `pxh.mind.budget_summary`-related imports if they did).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mind_utils.py -k "reflection_injects or falls_back" -v`
Expected: FAIL — no "Memories that feel relevant"/intention section in context.

- [ ] **Step 3: Implement.** In `src/pxh/mind.py`:

(a) Add the import next to the other pxh imports:

```python
from pxh import memory as spark_memory
```

(b) In `reflection()`, replace the plain notes injection block:

```python
    if notes:
        context_parts.append("Your long-term memories:\n" + "\n".join(f"  - {n}" for n in notes))
```

with relevance retrieval + fallback + intention:

```python
    # Relevant consolidated memories (falls back to raw tail-3 notes while the
    # memory store is empty — and for personas without a store).
    query_bits = [topic_seed or ""]
    query_bits.extend(str(t) for t in awareness.get("transitions") or [])
    for c in awareness.get("recent_conversations", [])[-3:]:
        query_bits.append(c.get("text", ""))
    query_bits.append(awareness.get("time_period", ""))
    query_bits.append(((awareness.get("calendar") or {}).get("current_event")) or "")
    query_bits.extend((awareness.get("frigate") or {}).get("rooms_with_people") or [])
    try:
        relevant = spark_memory.retrieve_memories(
            " ".join(b for b in query_bits if b), n=3, persona=persona or "spark")
    except Exception:
        relevant = []
    if relevant:
        context_parts.append("Memories that feel relevant right now:\n"
                             + "\n".join(f"  - {m['text']}" for m in relevant))
    elif notes:
        context_parts.append("Your long-term memories:\n" + "\n".join(f"  - {n}" for n in notes))

    # Current intention (SPARK only) — the goal that persists across reflections
    if (persona or "spark") == "spark":
        try:
            _intent_ctx = intention_mod.format_for_context()
            if _intent_ctx:
                context_parts.append(_intent_ctx)
        except Exception:
            pass
```

(c) In the run loop (`while True:` block), right after `prev_awareness = awareness`:

```python
        # Nightly memory consolidation (02:00–06:00 Hobart, once per date, SPARK only)
        if (session.get("persona") or "").lower().strip() == "spark":
            try:
                _cons = spark_memory.maybe_consolidate(dry=args.dry_run)
                if _cons is not None:
                    _detail = _cons.get("error") or _cons.get("reason") or f"wrote {_cons.get('written', 0)}"
                    log(f"consolidation: {_cons.get('status')} — {_detail}")
            except Exception as exc:
                log(f"consolidation error: {exc}")
```

- [ ] **Step 4: Run the mind test files**

Run: `python -m pytest tests/test_mind_utils.py tests/test_mind_coverage.py tests/test_mind.py tests/test_px_mind.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/mind.py tests/test_mind_utils.py
git commit -m "feat(mind): relevance-retrieved memories + intention in reflection; nightly consolidation hook"
```

---

### Task 7: Prompt enum + docs + full-suite verification

**Files:**
- Modify: `src/pxh/spark_config.py:296-311` (`_SPARK_REFLECTION_SUFFIX` rules + action enum)
- Modify: `CLAUDE.md` (session-type table + cognitive-loop section)
- Test: full suite

**Interfaces:**
- Consumes: everything above.
- Produces: SPARK's reflection prompt offers the three goal actions; docs match reality.

- [ ] **Step 1: Write the failing test** (append to `tests/test_mind_utils.py`)

```python
def test_spark_prompt_offers_goal_actions_and_explore_injection_still_works():
    from pxh.spark_config import _SPARK_REFLECTION_SUFFIX
    from pxh.mind import _inject_explore
    assert "set_goal, update_goal, complete_goal" in _SPARK_REFLECTION_SUFFIX
    assert '- "set_goal"' in _SPARK_REFLECTION_SUFFIX
    patched = _inject_explore(_SPARK_REFLECTION_SUFFIX)
    assert ", explore" in patched  # regex injection survives the longer enum
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_mind_utils.py -k prompt_offers_goal -v`
Expected: FAIL on the first assert.

- [ ] **Step 3: Edit `src/pxh/spark_config.py`.** After the `- "message_obi"` rule line (~302), add:

```python
- "set_goal" — commit to a multi-day intention you genuinely care about (thought = the goal). One at a time.
- "update_goal" — record progress on your current intention (thought = the progress note).
- "complete_goal" — declare your current intention achieved (thought = what came of it).
```

And extend the action enum line (~309) so it ends:

```python
  "action": "one of: wait, greet, comment, remember, look_at, weather_comment, scan, play_sound, photograph, emote, look_around, time_check, calendar_check, introspect, evolve, morning_fact, research, compose, self_debug, blog_essay, message_obi, set_goal, update_goal, complete_goal",
```

- [ ] **Step 4: Update `CLAUDE.md`:**
  - Claude Session Manager table: add row `| consolidate | Haiku | 20h | 1/day |`.
  - Cognitive Loop section: extend the valid-actions list with `set_goal, update_goal, complete_goal`; add `set_goal/update_goal/complete_goal` to the `NIGHT_ALLOWED_ACTIONS` sentence; add one bullet:
    `- **Memory consolidation**: nightly Haiku pass (02:00–06:00 Hobart, ≤2 attempts/day, state/consolidation_meta.json) distills the last 24h of thoughts into state/memories-spark.jsonl; reflection retrieves the top-3 relevant memories by keyword/tag overlap (falls back to last-3 notes while empty). Goal persistence in state/intention-spark.json (7-day expiry, one active at a time).`

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -m "not live" -q`
Expected: 0 failures (≈870+ passed — 812 existing + ~55 new; GPIO live tests excluded as usual).

- [ ] **Step 6: Commit**

```bash
git add src/pxh/spark_config.py CLAUDE.md tests/test_mind_utils.py
git commit -m "feat(spark): offer goal actions in reflection prompt; document continuity sprint"
```
