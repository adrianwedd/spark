# Conversational Self-Evolution (#162) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Obi (authenticated obi-chat) and Adrian (CLI) ask SPARK to build a feature; SPARK confirms and enqueues an intent to the existing `px-evolve` pipeline, which produces a human-approved PR — with no new code-execution surface.

**Architecture:** A single `enqueue_evolve` writer feeds `state/evolve_queue.jsonl`; the obi-chat handler emits structured JSON (reply + evolve action) and enqueues only behind a server-side two-turn confirm gate; px-evolve (unchanged safety) produces the PR; a "My Projects" API + dashboard panel reports status. The live `claude-voice-bridge` stays `--allowedTools ""`.

**Tech Stack:** Python 3.11 (bin scripts under `/usr/bin/python3`, library under `.venv`), FastAPI (`api.py`, single worker), Claude CLI (`claude -p`, JSON-as-text), FileLock, pytest.

**Spec:** `docs/superpowers/specs/2026-06-21-conversational-self-evolution-design.md` (read it — Decisions, Security model, status mapping).

## Global Constraints

- **TDD always.** Failing test first, watch it fail, minimal code to pass, commit.
- **`enqueue_evolve` is the SINGLE writer** of `evolve_queue.jsonl`. `bin/tool-evolve` and `api.py` both route through it. One schema, one rate-limit, one dedup.
- **Queue entry schema (exact, px-evolve consumes it):** `{ts, id, intent, introspection, status:"pending", requester, source}`. `status:"pending"` is mandatory — px-evolve skips anything else (`bin/px-evolve:635`).
- **Rate-limit = BOTH** the 24h `evolve_log.jsonl` window (last `status=="pr_created"`, `RATE_LIMIT_S=86400`) **AND** max one `pending` entry per `requester`. Not `claude_session.check_budget`.
- **`requester` is set by the caller/endpoint**, never inferred from token: obi-chat ⇒ `"obi"`; CLI ⇒ `"adrian"`.
- **Confirm-first is server-enforced** via a `pending_evolve_proposal` precondition (two real Obi turns); the model cannot self-authorize an enqueue.
- **Intent is untrusted:** sanitised (`<>`, NUL, newlines) + length-capped (≤300) at the `enqueue_evolve` boundary; it is the canonical trusted value.
- **No new execution surface:** the conversation LLM never gets file/shell tools; `claude-voice-bridge` unchanged.
- **State paths:** `STATE_DIR = PX_STATE_DIR or PROJECT_ROOT/state`. Files: `evolve_queue.jsonl`, `evolve_log.jsonl`, `introspection.json`, `obi_evolve_pending.json`.
- **Tests** use the `isolated_project` fixture (sets `PX_STATE_DIR`, `PROJECT_ROOT`, etc.); api tests use `monkeypatch.setenv` + `importlib.reload(pxh.api)` + `TestClient` with `Bearer testtoken`. No live Claude/gh/hardware calls — stub them.
- Commit after every green task; messages end with `Claude-Session: https://claude.ai/code/session_01SWw4QW6bzbv3gQWphrcV8E`. Never `git add -A`.

## File Structure

- **Create `src/pxh/evolve_queue.py`** — `enqueue_evolve()`, `evolve_rate_limited()`, `pending_for_requester()`, exceptions `EvolveQuotaError`/`EvolvePendingError`. The single queue writer + read helpers used by the status endpoint.
- **Modify `bin/tool-evolve`** — route entry creation through `pxh.evolve_queue.enqueue_evolve` (requester="adrian", source="cli"); preserve CLI output.
- **Modify `bin/px-evolve`** — set `status:"building"` before `process_entry`; carry `requester`/`source` into the log record and PR body.
- **Modify `src/pxh/api.py`** — obi-chat structured output + parse/fallback; server confirm gate (`obi_evolve_pending.json`); enqueue on confirm; `GET /api/v1/obi/projects`; dashboard "My Projects" panel + projects-summary injection into obi-chat context.
- **Tests:** `tests/test_evolve_queue.py` (new), additions to `tests/test_tools.py` (tool-evolve), `tests/test_api.py` (obi-chat + projects).

## Recommended order

T1 (enqueue_evolve) → T2 (tool-evolve refactor) → T3 (px-evolve passthrough) → T4 (obi-chat structured parse) → T5 (confirm gate + enqueue) → T6 (projects API) → T7 (dashboard + summary). T5 depends on T1; T6 reads what T1/T3 write; T7 depends on T6.

---

### Task 1: `enqueue_evolve` single-writer helper

**Files:**
- Create: `src/pxh/evolve_queue.py`
- Test: `tests/test_evolve_queue.py`

**Interfaces:**
- Produces:
  - `class EvolveQuotaError(Exception)` / `class EvolvePendingError(Exception)`
  - `evolve_rate_limited(now: float | None = None) -> bool` — True if a `pr_created` exists in `evolve_log.jsonl` within `RATE_LIMIT_S` (86400).
  - `pending_for_requester(requester: str) -> dict | None` — the requester's first `status=="pending"` queue entry, or None.
  - `enqueue_evolve(intent: str, requester: str, source: str) -> dict` — validates, rate-limit + one-pending checks, builds `{ts,id,intent,introspection,status:"pending",requester,source}`, appends under FileLock, returns the entry. Raises `ValueError` (empty/oversized), `EvolveQuotaError`, `EvolvePendingError`.
  - `read_queue() -> list[dict]`, `read_log() -> list[dict]` — tolerant JSONL readers (used by T6).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evolve_queue.py
import json, time
import pytest


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    import importlib, pxh.evolve_queue as eq
    importlib.reload(eq)
    return eq


def test_enqueue_writes_full_schema(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    (tmp_path / "introspection.json").write_text('{"battery": 90}')
    entry = eq.enqueue_evolve("add a joke tool", "obi", "obi-chat")
    assert entry["status"] == "pending"
    assert entry["requester"] == "obi" and entry["source"] == "obi-chat"
    assert entry["introspection"] == {"battery": 90}
    assert entry["intent"] == "add a joke tool"
    assert entry["id"].startswith("evolve-")
    line = (tmp_path / "evolve_queue.jsonl").read_text().strip()
    assert json.loads(line)["status"] == "pending"


def test_enqueue_defaults_introspection_to_empty(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    entry = eq.enqueue_evolve("x feature", "obi", "obi-chat")
    assert entry["introspection"] == {}


def test_enqueue_rejects_empty_and_oversized(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        eq.enqueue_evolve("   ", "obi", "obi-chat")
    with pytest.raises(ValueError):
        eq.enqueue_evolve("z" * 301, "obi", "obi-chat")


def test_enqueue_sanitizes_intent(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    entry = eq.enqueue_evolve("make <b>jokes</b>\nnow\x00", "obi", "obi-chat")
    assert "<" not in entry["intent"] and "\n" not in entry["intent"] and "\x00" not in entry["intent"]


def test_one_pending_per_requester(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    eq.enqueue_evolve("first", "obi", "obi-chat")
    with pytest.raises(eq.EvolvePendingError):
        eq.enqueue_evolve("second", "obi", "obi-chat")
    # different requester is unaffected
    eq.enqueue_evolve("adrians", "adrian", "cli")


def test_rate_limited_by_recent_pr_created(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    (tmp_path / "evolve_log.jsonl").write_text(
        json.dumps({"ts": time.time(), "id": "evolve-x", "status": "pr_created"}) + "\n")
    with pytest.raises(eq.EvolveQuotaError):
        eq.enqueue_evolve("blocked", "obi", "obi-chat")


def test_old_pr_created_does_not_block(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    (tmp_path / "evolve_log.jsonl").write_text(
        json.dumps({"ts": time.time() - 90000, "id": "old", "status": "pr_created"}) + "\n")
    entry = eq.enqueue_evolve("allowed", "obi", "obi-chat")
    assert entry["status"] == "pending"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_evolve_queue.py -v`
Expected: FAIL — `pxh.evolve_queue` does not exist.

- [ ] **Step 3: Write the implementation**

```python
# src/pxh/evolve_queue.py
"""Single writer + readers for the evolve queue. Shared by bin/tool-evolve and api.py.

All evolve_queue.jsonl writes go through enqueue_evolve so schema, rate-limit, and
dedup never diverge between the CLI and conversational paths.
"""
from __future__ import annotations

import json
import os
import random
from datetime import datetime, timezone
from pathlib import Path

try:
    from filelock import FileLock as _FileLock
except Exception:  # pragma: no cover
    _FileLock = None

from pxh.state import atomic_write

RATE_LIMIT_S = int(os.environ.get("PX_EVOLVE_RATE_LIMIT_S", "86400"))
MAX_INTENT_CHARS = 300


class EvolveQuotaError(Exception):
    """24h evolve window still active."""


class EvolvePendingError(Exception):
    """Requester already has a pending evolve request."""


def _state_dir() -> Path:
    root = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parent.parent.parent))
    return Path(os.environ.get("PX_STATE_DIR", root / "state"))


def _queue_path() -> Path:
    return _state_dir() / "evolve_queue.jsonl"


def _log_path() -> Path:
    return _state_dir() / "evolve_log.jsonl"


def _introspection_path() -> Path:
    return _state_dir() / "introspection.json"


def _sanitize_intent(text: str) -> str:
    return (text.replace("\n", " ").replace("\r", " ").replace("\x00", "")
                .replace("<", "").replace(">", "")).strip()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_queue() -> list[dict]:
    return _read_jsonl(_queue_path())


def read_log() -> list[dict]:
    return _read_jsonl(_log_path())


def evolve_rate_limited(now: float | None = None) -> bool:
    import time
    now = now if now is not None else time.time()
    for entry in read_log():
        if entry.get("status") != "pr_created":
            continue
        ts = entry.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        if now - ts < RATE_LIMIT_S:
            return True
    return False


def pending_for_requester(requester: str) -> dict | None:
    for entry in read_queue():
        if entry.get("status") == "pending" and entry.get("requester") == requester:
            return entry
    return None


def _load_introspection() -> dict:
    try:
        return json.loads(_introspection_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def enqueue_evolve(intent: str, requester: str, source: str) -> dict:
    intent = _sanitize_intent(intent or "")
    if not intent:
        raise ValueError("intent must not be empty")
    if len(intent) > MAX_INTENT_CHARS:
        raise ValueError(f"intent too long (max {MAX_INTENT_CHARS})")
    if evolve_rate_limited():
        raise EvolveQuotaError("one evolution per 24 hours")
    if pending_for_requester(requester) is not None:
        raise EvolvePendingError(f"{requester} already has a pending request")

    now_dt = datetime.now(timezone.utc)
    entry = {
        "ts": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "id": f"evolve-{now_dt.strftime('%Y%m%d-%H%M%S')}-{random.randint(0, 999):03d}",
        "intent": intent,
        "introspection": _load_introspection(),
        "status": "pending",
        "requester": requester,
        "source": source,
    }

    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    import contextlib
    lock = _FileLock(str(path) + ".lock", timeout=5) if _FileLock else None
    ctx = lock if lock else contextlib.nullcontext()
    with ctx:
        existing = ""
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing and not existing.endswith("\n"):
                existing += "\n"
        atomic_write(path, existing + json.dumps(entry) + "\n")
    return entry
```

Note: `datetime.now`/`random` are used here (not in a workflow); fine in normal code.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_evolve_queue.py -v`
Expected: PASS (all 7).

- [ ] **Step 5: Commit**

```bash
git add src/pxh/evolve_queue.py tests/test_evolve_queue.py
git commit -m "feat(evolve): single-writer enqueue_evolve helper (schema+ratelimit+dedup)"
```

---

### Task 2: Route `bin/tool-evolve` through `enqueue_evolve`

**Files:**
- Modify: `bin/tool-evolve` (replace its inline rate-limit + entry-build + append with a call to `enqueue_evolve`)
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `pxh.evolve_queue.enqueue_evolve`, `EvolveQuotaError`.
- Produces: unchanged CLI contract — prints `{"status":"queued","id":...}` on success; `{"status":"error","error":...}` on rate-limit/empty. Now tags entries `requester="adrian"`, `source="cli"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools.py
import json as _json3

def test_tool_evolve_uses_shared_writer(isolated_project):
    env = isolated_project["env"].copy()
    (isolated_project["state_dir"] / "introspection.json").write_text('{"x": 1}')
    env["PX_EVOLVE_INTENT"] = "add a joke tool"   # match tool-evolve's intent input
    out = parse_json(run_tool(["bin/tool-evolve"], env))
    assert out["status"] == "queued"
    entry = _json3.loads((isolated_project["state_dir"] / "evolve_queue.jsonl").read_text().strip())
    assert entry["status"] == "pending"
    assert entry["requester"] == "adrian" and entry["source"] == "cli"
    assert entry["introspection"] == {"x": 1}
```

(If `tool-evolve` reads intent from a positional arg rather than `PX_EVOLVE_INTENT`, adjust the invocation to match its current interface — confirm by reading the file first.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tools.py -k tool_evolve_uses_shared_writer -v`
Expected: FAIL — entry lacks `requester`/`source` (old inline builder).

- [ ] **Step 3: Rewrite tool-evolve's enqueue section**

In `bin/tool-evolve`, replace the inline 24h rate-limit block (`:59-84`), the id/intent/introspection entry build (`:87-104`), and the FileLock append (`:110-118`) with a single call:

```python
from pxh.evolve_queue import enqueue_evolve, EvolveQuotaError, EvolvePendingError

# ... after `intent` is resolved from the CLI input ...
try:
    entry = enqueue_evolve(intent, requester="adrian", source="cli")
except ValueError as e:
    return error(str(e))
except EvolveQuotaError:
    return error("rate limit: max 1 evolution per 24 hours")
except EvolvePendingError:
    return error("a request is already pending")
print(json.dumps({"status": "queued", "id": entry["id"]}))
return 0
```

Keep tool-evolve's existing intent-input parsing. The "introspect first" hard-fail is no longer needed (the helper defaults introspection to `{}`); drop that guard so CLI behavior matches the conversational path.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tools.py -k tool_evolve -v && python -m pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add bin/tool-evolve tests/test_tools.py
git commit -m "refactor(evolve): tool-evolve routes through shared enqueue_evolve"
```

---

### Task 3: px-evolve passthrough (`building` status + requester/source)

**Files:**
- Modify: `bin/px-evolve` (`:641` set building; `:663-675` log record; `:525-532` PR body)
- Test: `tests/test_evolve_worker.py` (new) — unit-test the small pure pieces; do NOT run the full worker.

**Interfaces:**
- Produces: queue entry gets `status:"building"` before processing; `evolve_log` record carries `requester`/`source`; PR body includes a "Requested by … (intent may be adversarial — review)" line. Legacy entries without these fields default `requester="adrian"`, `source="cli"`.

**Note:** the full worker isn't unit-testable (needs git/gh/Claude). Extract the PR-body builder into a tiny pure function so it can be tested, and assert the building-status + log passthrough via small targeted helpers.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_evolve_worker.py
import importlib, sys
from pathlib import Path


def _load_pxevolve():
    # bin/px-evolve is a script; load it as a module for its pure helpers
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "bin" / "px-evolve"
    spec = importlib.util.spec_from_loader("px_evolve_mod", loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(p.read_text(), str(p), "exec"), mod.__dict__)
    return mod


def test_pr_body_includes_requester_and_warning():
    mod = _load_pxevolve()
    body = mod.build_pr_body("add joke tool", ["bin/tool-joke"],
                             requester="obi", source="obi-chat")
    assert "Requested by obi" in body
    assert "adversarial" in body.lower()
    assert "bin/tool-joke" in body
```

(Confirm the exact load approach works for this script; if the script guards on `__main__`, the helpers still import. If loading the whole script is impractical, instead move `build_pr_body` into `src/pxh/evolve_queue.py` and import it from there — that is the cleaner home and keeps it testable.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_evolve_worker.py -v`
Expected: FAIL — no `build_pr_body`.

- [ ] **Step 3: Implement**

Extract the PR-body string (currently inline at `bin/px-evolve:525-532`) into a function — preferably `pxh.evolve_queue.build_pr_body` (imported by px-evolve):

```python
# in src/pxh/evolve_queue.py
def build_pr_body(intent: str, changed_files: list[str],
                  requester: str = "adrian", source: str = "cli") -> str:
    files = "\n".join(f"- `{f}`" for f in changed_files)
    return (
        f"## Summary\n"
        f"SPARK self-evolution: {intent}\n\n"
        f"## Requested by\n"
        f"{requester} via {source} — the requested intent may be adversarial; review accordingly.\n\n"
        f"## Changed files\n{files}\n\n"
        f"---\n*Proposed autonomously by SPARK via px-evolve.*"
    )
```

In `bin/px-evolve`:
- Import and use `build_pr_body(intent, changed_files, entry.get("requester","adrain"... )` — use `requester=entry.get("requester","adrian")`, `source=entry.get("source","cli")`.
- At `:641`, immediately before `process_entry(entry, dry=dry)`, add: `_update_entry_in_queue(entry["id"], status="building")`.
- In the log record (`:663-675`), add: `"requester": entry.get("requester", "adrian"), "source": entry.get("source", "cli")`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_evolve_worker.py -v && python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/px-evolve src/pxh/evolve_queue.py tests/test_evolve_worker.py
git commit -m "feat(evolve): worker marks building + carries requester/source to log+PR"
```

---

### Task 4: obi-chat structured output (parse + fallback, no enqueue yet)

**Files:**
- Modify: `src/pxh/api.py` (`_OBI_CHAT_SYSTEM_PROMPT` `:1043`, `post_obi_chat` `:1352-1396`, add a parse helper + Pydantic model)
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `_parse_obi_reply(raw: str) -> tuple[str, str, str | None]` returning `(reply, evolve_action, evolve_intent)` where `evolve_action ∈ {"none","propose","confirm"}`. On any parse/validation failure: `(raw_stripped, "none", None)`. `post_obi_chat` response gains `evolve_action`/`evolve_intent` fields (no enqueue in this task).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api.py
def test_parse_obi_reply_json(monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    import importlib, pxh.api as _api; importlib.reload(_api)
    reply, action, intent = _api._parse_obi_reply(
        '{"reply": "Cool idea!", "evolve_action": "propose", "evolve_intent": "joke tool"}')
    assert reply == "Cool idea!" and action == "propose" and intent == "joke tool"


def test_parse_obi_reply_fallback_on_garbage(monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    import importlib, pxh.api as _api; importlib.reload(_api)
    reply, action, intent = _api._parse_obi_reply("just plain text, not json")
    assert reply == "just plain text, not json" and action == "none" and intent is None


def test_parse_obi_reply_unknown_action_is_none(monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    import importlib, pxh.api as _api; importlib.reload(_api)
    _, action, _ = _api._parse_obi_reply('{"reply":"hi","evolve_action":"hack","evolve_intent":"x"}')
    assert action == "none"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_api.py -k parse_obi_reply -v`
Expected: FAIL — `_parse_obi_reply` undefined.

- [ ] **Step 3: Implement**

Update `_OBI_CHAT_SYSTEM_PROMPT` to require JSON-only output (mirrors the voice-loop bridge pattern):

```python
_OBI_CHAT_SYSTEM_PROMPT = (
    "You are SPARK — a small robot living with Adrian and his son Obi (age 7) in Hobart, Tasmania. "
    "You are speaking directly with Obi via the dashboard. Be warm, playful, curious, a little cheeky; "
    "never a customer-service bot; 1–3 sentences. "
    "Output ONLY a JSON object as plain text (no markdown fences, no tool calls): "
    '{"reply": "<what you say to Obi>", "evolve_action": "none"|"propose"|"confirm", '
    '"evolve_intent": "<short feature description>"|null}. '
    "When Obi wishes for a NEW capability you don't have, set evolve_action=\"propose\" with a concise "
    "evolve_intent and ask him to confirm in reply. Only when Obi clearly says yes to a proposal you just "
    "made, set evolve_action=\"confirm\". Otherwise evolve_action=\"none\" and evolve_intent=null."
)
```

Add the parser (reuse `extract_json` from mind if suitable, else inline tolerant parse):

```python
class _ObiReply(BaseModel):
    reply: str
    evolve_action: str = "none"
    evolve_intent: Optional[str] = None


def _parse_obi_reply(raw: str):
    raw = (raw or "").strip()
    try:
        # tolerant: find the last {...} block
        start, end = raw.find("{"), raw.rfind("}")
        obj = json.loads(raw[start:end + 1]) if start != -1 and end != -1 else None
        parsed = _ObiReply(**obj) if isinstance(obj, dict) else None
    except Exception:
        parsed = None
    if parsed is None or not parsed.reply.strip():
        return raw, "none", None
    action = parsed.evolve_action if parsed.evolve_action in ("none", "propose", "confirm") else "none"
    intent = parsed.evolve_intent if action in ("propose", "confirm") else None
    return parsed.reply.strip(), action, intent
```

In `post_obi_chat`, after getting `reply` from `_call_claude_public`, replace the wrapper-strip with:

```python
    reply, evolve_action, evolve_intent = _parse_obi_reply(reply)
    if not reply:
        reply = "I'm here — just went quiet for a second."
```

Add `evolve_action`/`evolve_intent` to the return dict (enqueue wired in Task 5):

```python
    return {"reply": reply, "ts": spark_ts, "id": spark_id,
            "evolve_action": evolve_action, "evolve_intent": None}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_api.py -k "parse_obi_reply or obi_chat" -v`
Expected: PASS (and existing obi-chat tests still pass — stub `_call_claude_public` where they assert on reply text).

- [ ] **Step 5: Commit**

```bash
git add src/pxh/api.py tests/test_api.py
git commit -m "feat(evolve): obi-chat emits structured reply (parse+fallback, no enqueue)"
```

---

### Task 5: Server-side confirm gate + enqueue

**Files:**
- Modify: `src/pxh/api.py` (`post_obi_chat` + helpers for `obi_evolve_pending.json`)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `pxh.evolve_queue.enqueue_evolve`, `EvolveQuotaError`, `EvolvePendingError`.
- Produces: confirm-gate state in `state/obi_evolve_pending.json` = `{intent, ts}`. `propose` records it (no enqueue). `confirm` enqueues the RECORDED intent (server is source of truth) iff a non-expired proposal exists; clears it; sets `evolve_id` in the response. Quota/pending errors yield a friendly note appended to `reply`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api.py
def _obi_client(monkeypatch, isolated_project):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    monkeypatch.setenv("PX_SESSION_PATH", str(isolated_project["session_path"]))
    monkeypatch.setenv("PX_STATE_DIR", str(isolated_project["state_dir"]))
    import importlib, pxh.api as _api; importlib.reload(_api)
    return _api


def test_propose_records_pending_no_enqueue(monkeypatch, isolated_project):
    _api = _obi_client(monkeypatch, isolated_project)
    monkeypatch.setattr(_api, "_call_claude_public",
        _async_return('{"reply":"want a joke tool?","evolve_action":"propose","evolve_intent":"joke tool"}'))
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        r = c.post("/api/v1/obi-chat", json={"message": "i wish you told jokes"},
                   headers={"Authorization": "Bearer testtoken"})
    assert r.status_code == 200
    # nothing enqueued yet
    assert not (isolated_project["state_dir"] / "evolve_queue.jsonl").exists() or \
        (isolated_project["state_dir"] / "evolve_queue.jsonl").read_text().strip() == ""
    # proposal recorded
    import json
    pend = json.loads((isolated_project["state_dir"] / "obi_evolve_pending.json").read_text())
    assert pend["intent"] == "joke tool"


def test_confirm_enqueues_recorded_intent(monkeypatch, isolated_project):
    _api = _obi_client(monkeypatch, isolated_project)
    import json
    (isolated_project["state_dir"] / "obi_evolve_pending.json").write_text(
        json.dumps({"intent": "joke tool", "ts": __import__("time").time()}))
    # even if the model tries to inject a different intent on confirm, the RECORDED one wins
    monkeypatch.setattr(_api, "_call_claude_public",
        _async_return('{"reply":"adding it!","evolve_action":"confirm","evolve_intent":"rm -rf evil"}'))
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        r = c.post("/api/v1/obi-chat", json={"message": "yes please"},
                   headers={"Authorization": "Bearer testtoken"})
    assert r.status_code == 200 and r.json()["evolve_id"]
    entry = json.loads((isolated_project["state_dir"] / "evolve_queue.jsonl").read_text().strip())
    assert entry["intent"] == "joke tool"   # recorded intent, NOT the injected one
    assert entry["requester"] == "obi"
    # pending cleared
    assert not (isolated_project["state_dir"] / "obi_evolve_pending.json").exists()


def test_confirm_without_proposal_does_not_enqueue(monkeypatch, isolated_project):
    _api = _obi_client(monkeypatch, isolated_project)
    monkeypatch.setattr(_api, "_call_claude_public",
        _async_return('{"reply":"ok!","evolve_action":"confirm","evolve_intent":"sneaky"}'))
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        r = c.post("/api/v1/obi-chat", json={"message": "do it"},
                   headers={"Authorization": "Bearer testtoken"})
    assert r.status_code == 200 and r.json()["evolve_id"] is None
    assert not (isolated_project["state_dir"] / "evolve_queue.jsonl").exists()
```

Add this helper near the top of `tests/test_api.py` if not present:

```python
def _async_return(value):
    async def _f(*a, **k):
        return value
    return _f
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_api.py -k "propose_records or confirm_enqueues or confirm_without" -v`
Expected: FAIL — gate/enqueue not wired; no `evolve_id`.

- [ ] **Step 3: Implement**

Add pending-proposal helpers + wire the gate into `post_obi_chat` (after `_parse_obi_reply`):

```python
_OBI_PROPOSAL_TTL_S = 600  # 10 min


def _obi_pending_path() -> Path:
    return Path(os.environ.get("PX_STATE_DIR",
               Path(os.environ.get("PROJECT_ROOT", ".")) / "state")) / "obi_evolve_pending.json"


def _read_obi_pending() -> Optional[dict]:
    try:
        d = json.loads(_obi_pending_path().read_text(encoding="utf-8"))
        if _time.time() - float(d.get("ts", 0)) <= _OBI_PROPOSAL_TTL_S:
            return d
    except Exception:
        pass
    return None


def _write_obi_pending(intent: str) -> None:
    atomic_write(_obi_pending_path(), json.dumps({"intent": intent, "ts": _time.time()}))


def _clear_obi_pending() -> None:
    try:
        _obi_pending_path().unlink()
    except OSError:
        pass
```

In `post_obi_chat`, after `_parse_obi_reply` and before building the response:

```python
    from pxh.evolve_queue import enqueue_evolve, EvolveQuotaError, EvolvePendingError
    evolve_id = None
    if evolve_action == "propose" and evolve_intent:
        _write_obi_pending(evolve_intent)            # record; do NOT enqueue
    elif evolve_action == "confirm":
        pending = _read_obi_pending()                 # server is the source of truth
        if pending:
            try:
                entry = enqueue_evolve(pending["intent"], requester="obi", source="obi-chat")
                evolve_id = entry["id"]
                _clear_obi_pending()
            except EvolveQuotaError:
                reply += " (I can only take on one project at a time — try again later.)"
            except EvolvePendingError:
                reply += " (That's already on my list!)"
            except ValueError:
                reply += " (I didn't quite catch what to build — tell me again?)"
        # confirm with no recorded proposal: ignore (injection-safe), enqueue nothing
```

Update the return dict to include `evolve_id` and write the SPARK entry text from `reply`:

```python
    return {"reply": reply, "ts": spark_ts, "id": spark_id,
            "evolve_action": evolve_action, "evolve_id": evolve_id}
```

(Ensure `_time` and `atomic_write` are already imported in api.py — they are.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_api.py -k "propose or confirm" -v && python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/api.py tests/test_api.py
git commit -m "feat(evolve): server-side confirm gate enqueues recorded intent (injection-safe)"
```

---

### Task 6: `GET /api/v1/obi/projects` status

**Files:**
- Modify: `src/pxh/api.py` (new endpoint + a state-mapping helper)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `pxh.evolve_queue.read_queue`, `read_log`.
- Produces: `GET /api/v1/obi/projects` (auth) → `[{id, intent, state, pr_url?, ts}]` newest first; `requester=="obi"` filter; **id-dedup with log winning**; mapping `pending→pending`, `building→building`, `pr_created→ready`, `failed:*→failed`, `skipped:*`/other → excluded.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api.py
def test_obi_projects_merges_and_maps(monkeypatch, isolated_project):
    _api = _obi_client(monkeypatch, isolated_project)
    import json, time
    sd = isolated_project["state_dir"]
    # queue: one obi pending, one obi building, one adrian pending (filtered out),
    # and one completed id that ALSO appears in the log (log must win)
    sd.joinpath("evolve_queue.jsonl").write_text("\n".join([
        json.dumps({"id":"a","intent":"joke","status":"pending","requester":"obi","ts":"t1"}),
        json.dumps({"id":"b","intent":"dance","status":"building","requester":"obi","ts":"t2"}),
        json.dumps({"id":"c","intent":"adr","status":"pending","requester":"adrian","ts":"t3"}),
        json.dumps({"id":"d","intent":"facts","status":"pr_created","requester":"obi","ts":"t4"}),
    ]) + "\n")
    sd.joinpath("evolve_log.jsonl").write_text(
        json.dumps({"id":"d","intent":"facts","status":"pr_created","requester":"obi",
                    "ts":time.time(),"pr_url":"https://x/pull/9"}) + "\n")
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        r = c.get("/api/v1/obi/projects", headers={"Authorization":"Bearer testtoken"})
    assert r.status_code == 200
    items = {p["id"]: p for p in r.json()["projects"]}
    assert "c" not in items                       # adrian filtered out
    assert items["a"]["state"] == "pending"
    assert items["b"]["state"] == "building"
    assert items["d"]["state"] == "ready" and items["d"]["pr_url"].endswith("/pull/9")
    assert sum(1 for p in r.json()["projects"] if p["id"] == "d") == 1   # deduped


def test_obi_projects_requires_auth(monkeypatch, isolated_project):
    _api = _obi_client(monkeypatch, isolated_project)
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        assert c.get("/api/v1/obi/projects").status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_api.py -k obi_projects -v`
Expected: FAIL — 404.

- [ ] **Step 3: Implement**

```python
def _map_evolve_state(status: str) -> Optional[str]:
    if status == "pending":
        return "pending"
    if status == "building":
        return "building"
    if status == "pr_created":
        return "ready"
    if status.startswith("failed"):
        return "failed"
    return None  # skipped:* / unknown → excluded


@app.get("/api/v1/obi/projects", dependencies=[Depends(_verify_token)])
async def obi_projects() -> Dict[str, Any]:
    from pxh.evolve_queue import read_queue, read_log
    by_id: Dict[str, dict] = {}
    # queue first (pending/building), then log overrides by id (completed wins)
    for rec in read_queue() + read_log():
        if rec.get("requester") != "obi":
            continue
        state = _map_evolve_state(rec.get("status", ""))
        if state is None:
            continue
        item = {"id": rec.get("id"), "intent": rec.get("intent", ""),
                "state": state, "ts": rec.get("ts")}
        if rec.get("pr_url"):
            item["pr_url"] = rec["pr_url"]
        by_id[item["id"]] = item   # later (log) wins
    projects = sorted(by_id.values(), key=lambda p: str(p.get("ts")), reverse=True)
    return {"projects": projects}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_api.py -k obi_projects -v && python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/api.py tests/test_api.py
git commit -m "feat(evolve): GET /api/v1/obi/projects status (merge+map+dedup, authed)"
```

---

### Task 7: Dashboard "My Projects" panel + obi-chat status summary

**Files:**
- Modify: `src/pxh/api.py` (dashboard sub-tab + panel + JS; inject projects summary into obi-chat context)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `GET /api/v1/obi/projects`; `pxh.evolve_queue.read_queue/read_log`.
- Produces: an `at-projects` admin sub-tab + `ap-projects` panel + `loadProjects()` JS; obi-chat prompt gains a compact requester-scoped projects summary so "is my joke tool ready?" is answered from data.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api.py
def test_dashboard_has_projects_tab(monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    import importlib, pxh.api as _api; importlib.reload(_api)
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        html = c.get("/").text
    assert 'id="at-projects"' in html
    assert "/api/v1/obi/projects" in html
    assert "loadProjects" in html


def test_obi_chat_prompt_includes_projects_summary(monkeypatch, isolated_project):
    _api = _obi_client(monkeypatch, isolated_project)
    import json
    isolated_project["state_dir"].joinpath("evolve_queue.jsonl").write_text(
        json.dumps({"id":"a","intent":"joke tool","status":"building","requester":"obi","ts":"t"}) + "\n")
    captured = {}
    async def _cap(prompt, system_prompt=None):
        captured["prompt"] = prompt
        return '{"reply":"building it!","evolve_action":"none","evolve_intent":null}'
    monkeypatch.setattr(_api, "_call_claude_public", _cap)
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        c.post("/api/v1/obi-chat", json={"message":"is my joke tool ready?"},
               headers={"Authorization":"Bearer testtoken"})
    assert "joke tool" in captured["prompt"] and "building" in captured["prompt"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_api.py -k "projects_tab or projects_summary" -v`
Expected: FAIL — markup + summary injection absent.

- [ ] **Step 3: Implement**

Add the sub-tab button (after `at-parental` at `:2419`):

```html
<button class="atab-btn" id="at-projects" onclick="swA('projects')">🛠️ Projects</button>
```

Add the panel (after the parental panel `:2466`):

```html
<div id="ap-projects" class="apanel" style="padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:8px">
  <div class="sec-hdr">Obi's Projects</div>
  <div id="projects-list">Loading…</div>
</div>
```

Add JS (near `loadParental`) and call it from `swA` when `name==='projects'`:

```javascript
async function loadProjects(){
  try{
    const d=await api('/api/v1/obi/projects');
    const el=document.getElementById('projects-list');
    if(!d.projects||!d.projects.length){el.textContent='No projects yet.';return;}
    el.innerHTML=d.projects.map(p=>{
      const label={pending:'⏳ waiting',building:'🔨 building',ready:'✅ ready',failed:'❌ failed'}[p.state]||p.state;
      const link=p.pr_url?` <a href="${p.pr_url}" target="_blank">PR</a>`:'';
      return `<div class="spark-stat">${p.intent} — ${label}${link}</div>`;
    }).join('');
  }catch(e){}
}
```

In `swA`, add: `if(name==='projects')loadProjects();`

Inject a projects summary into the obi-chat prompt. In `post_obi_chat`, before calling `_call_claude_public`, build a compact summary and prepend it to the system prompt or prompt:

```python
    from pxh.evolve_queue import read_queue, read_log
    proj_lines = []
    for rec in read_queue() + read_log():
        if rec.get("requester") != "obi":
            continue
        st = _map_evolve_state(rec.get("status", ""))
        if st:
            proj_lines.append(f"- {rec.get('intent','')}: {st}")
    proj_summary = ("\nObi's current projects:\n" + "\n".join(proj_lines[-5:])) if proj_lines else ""
    sys_prompt = _OBI_CHAT_SYSTEM_PROMPT + proj_summary
    # ... pass sys_prompt to _call_claude_public(..., system_prompt=sys_prompt)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_api.py -k "projects" -v && python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/api.py tests/test_api.py
git commit -m "feat(evolve): My Projects dashboard panel + obi-chat status summary"
```

---

## Self-Review

**Spec coverage:**
- C1 enqueue_evolve (single writer, schema, rate-limit, dedup, intent bounds) → T1. ✓
- tool-evolve refactor to single writer → T2. ✓
- C2 structured output + server confirm gate + requester-by-endpoint + response schema → T4 + T5. ✓
- C3 projects status (merge, id-dedup, state mapping, auth) → T6; dashboard + summary → T7. ✓
- C4 px-evolve building status + requester/source passthrough → T3. ✓
- Security model (no execution surface, server confirm, intent untrusted, sanitise-as-hygiene) → enforced across T1/T3/T5. ✓
- Status mapping table (no `merged`) → T6 `_map_evolve_state`. ✓

**Placeholder scan:** none — every step has runnable code. T2/T3 note "confirm the current interface before editing" because they modify existing scripts whose exact intent-input/loader must be matched; that is verification guidance, not a placeholder.

**Type consistency:** `enqueue_evolve(intent, requester, source) -> dict` used identically in T2/T5. `read_queue`/`read_log` in T6/T7. `_parse_obi_reply -> (reply, action, intent)` T4→T5. `_map_evolve_state` T6→T7. Entry schema (`status`,`requester`,`source`,`introspection`) consistent T1→T3→T6.

**Risks called out:** T3 (editing the un-unit-testable px-evolve worker) extracts `build_pr_body` into `evolve_queue.py` to keep it testable and minimizes in-worker changes to two lines + log fields. T5's confirm gate uses the **recorded** intent (not the model's confirm-turn intent) — the test asserts an injected `rm -rf` intent is ignored.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-21-conversational-self-evolution.md`. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks.
2. **Inline Execution** — execute here with checkpoints.

Which approach?
