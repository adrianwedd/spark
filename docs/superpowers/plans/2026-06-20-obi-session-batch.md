# Obi Session Batch — Implementation Plan (#26, #33, #34, #38, #36 L1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship five GitHub-issue features in one session: Custom Sound Effects (#26), Obi's Dopamine Menu items (#33), Sleep Mode (#34), the web Settings panel (#38), and full-tool MCP exposure (#36 Layer 1).

**Architecture:** Each feature follows the established SPARK tool/loop/state patterns. New `bin/tool-*` scripts emit one JSON line and gate on `PX_DRY`; voice-loop registration goes through `ALLOWED_TOOLS`/`TOOL_COMMANDS`/`validate_action`; session flags flow through `state.default_state()` + `api.PATCHABLE_FIELDS`; the MCP server reuses `validate_action`+`execute_tool` rather than duplicating dispatch.

**Tech Stack:** Python 3.11 (bin scripts run under `/usr/bin/python3`, library under `.venv`), bash heredocs, FastAPI (`api.py`, single worker), FastMCP (`mcp==1.27.1`), pytest, espeak/aplay/arecord, FileLock.

## Global Constraints

- **TDD always.** Write the failing test, watch it fail, minimal code to pass, commit. Never write impl before a red test.
- **Every tool emits exactly one JSON object** as the last stdout line; tests parse via `parse_json` = `json.loads(output.splitlines()[-1])`.
- **Every tool supports `PX_DRY=1`** (skips motion/audio/recording) and reports `{"dry": true|false}`; errors are `{"status": "error", "error": "..."}`.
- **`FileLock` is not reentrant** — `update_session` calls `ensure_session()` *before* the lock. Don't nest locks.
- **Motion gating** is enforced inside each motion `bin/tool-*` (returns rc=2, `{"status":"blocked"}`); never bypass it.
- **Night silence** is unconditional 19:00–07:00 Hobart (`mind._is_night_silence`); Sleep Mode is *additional*, user-initiated, and works outside that window.
- **Tests use the `isolated_project` fixture** (`tests/conftest.py`) — copy `["env"]`, set `PX_DRY=1`, run via `run_tool(["bin/tool-x"], env)`.
- **New tools must be added in 6 places** (CLAUDE.md "Adding a New Tool"): `bin/tool-*`, `ALLOWED_TOOLS`, `TOOL_COMMANDS`, `validate_action`, the four prompt docs, and a dry-run test.
- **Commit after every green task.** End commit messages with `Claude-Session: https://claude.ai/code/session_01SWw4QW6bzbv3gQWphrcV8E`.
- **Staging:** never `git add -A`; stage explicit paths (untracked artifacts live in repo root).
- Run `python -m pytest -q` (Pi suite) before each commit touching library code; `m5/announce-relay` is unaffected by this plan.

## Recommended Execution Order

Part A (#26) → Part B (#33) → Part C (#34) → Part D (#38) → Part E (#36 L1). A/B are independent quick wins; C adds the `spark_sleep_mode` flag that D's Settings panel surfaces; D's session-voice persistence and C's amplitude flag both touch `tool-voice` (do C's amplitude first); E reuses the registry the earlier parts extend, so it runs last and picks up the new tools for free.

---

## File Structure

**Part A — #26 Custom Sound Effects**
- Modify: `bin/tool-play-sound` (dynamic allowlist from `sounds/`, `PX_SOUNDS_DIR` test seam)
- Create: `bin/tool-record-sound` (record N s → `sounds/<name>.wav`)
- Modify: `src/pxh/voice_loop.py` (register `tool_record_sound`; ensure `tool_play_sound` validate branch passes name)
- Modify: `docs/prompts/{claude,codex}-voice-system.md`, `docs/prompts/persona-{gremlin,vixen}.md`
- Test: `tests/test_tools.py`

**Part B — #33 Dopamine Menu items**
- Modify: `bin/tool-dopamine-menu` (`add` action writes tagged note; suggest path merges Obi items)
- Modify: `src/pxh/voice_loop.py` (`validate_action` branch for `tool_dopamine_menu` params)
- Modify: prompt docs
- Test: `tests/test_tools.py`

**Part C — #34 Sleep Mode**
- Modify: `src/pxh/state.py` (`default_state` adds `spark_sleep_mode`), `state/session.template.json`
- Create: `bin/tool-sleep` (start|check|end)
- Modify: `bin/tool-voice` (`PX_VOICE_AMPLITUDE` → espeak `-a`; echo in dry payload)
- Modify: `src/pxh/mind.py` (`expression()` sleep suppression + amplitude injection)
- Modify: `bin/px-wake-listen` (whisper onset via `_effective_onset_rms` helper + env overrides)
- Modify: `src/pxh/api.py` (`SessionPatch` + `PATCHABLE_FIELDS` add `spark_sleep_mode`)
- Modify: `src/pxh/voice_loop.py` + prompt docs (register `tool_sleep`)
- Test: `tests/test_tools.py`, `tests/test_mind.py`, `tests/test_wake_listen.py` (new), `tests/test_api.py`

**Part D — #38 Settings Panel**
- Modify: `src/pxh/voice_loop.py` (`execute_tool` applies session voice fields as base)
- Modify: `src/pxh/api.py` (`PATCH /api/v1/voice`, `POST /api/v1/voice/preview`, `GET /api/v1/config/backup`, `POST /api/v1/config/import`, `PATCH /api/v1/config`; dashboard Settings sub-tab)
- Create: `src/pxh/runtime_config.py` (read/write `state/runtime_config.json` overrides)
- Modify: `src/pxh/mind.py` (read runtime_config backend/model override at reflection start)
- Test: `tests/test_api.py`, `tests/test_voice_loop.py`, `tests/test_runtime_config.py` (new)

**Part E — #36 Layer 1 MCP expansion**
- Create: `src/pxh/schemas.py` (`TOOL_SCHEMAS` declarative param spec)
- Modify: `src/pxh/mcp_server.py` (`spark_list_tools`, `spark_run_tool` reusing validate/execute; `spark://` resources)
- Modify: `requirements.txt` (add `mcp>=1.27`)
- Test: `tests/test_schemas.py` (new), `tests/test_mcp_server.py`

---

# PART A — #26 Custom Sound Effects

### Task A1: Dynamic sound allowlist in `tool-play-sound`

**Files:**
- Modify: `bin/tool-play-sound:20-23` (SOUNDS_DIR + ALLOWED_SOUNDS)
- Modify: `bin/tool-play-sound:36-43` (validation against dynamic set)
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces: `tool-play-sound` accepts any `PX_SOUND` whose `<name>.wav` exists in the sounds dir (builtins ∪ recorded). Honors `PX_SOUNDS_DIR` override (defaults to `$PROJECT_ROOT/sounds`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools.py
def test_tool_play_sound_allows_recorded_file(isolated_project, tmp_path):
    sounds = tmp_path / "sounds"
    sounds.mkdir()
    (sounds / "obi-laugh.wav").write_bytes(b"RIFF0000WAVEfmt ")
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_SOUNDS_DIR"] = str(sounds)
    env["PX_SOUND"] = "obi-laugh"
    payload = parse_json(run_tool(["bin/tool-play-sound"], env))
    assert payload["status"] == "ok"
    assert payload["sound"] == "obi-laugh"


def test_tool_play_sound_rejects_unknown(isolated_project, tmp_path):
    sounds = tmp_path / "sounds"; sounds.mkdir()
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"; env["PX_SOUNDS_DIR"] = str(sounds); env["PX_SOUND"] = "nope"
    payload = parse_json(run_tool(["bin/tool-play-sound"], env))
    assert payload["status"] == "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tools.py -k play_sound_allows_recorded -v`
Expected: FAIL — `obi-laugh` not in the hardcoded `{"chime","beep","tada","alert"}`.

- [ ] **Step 3: Write minimal implementation**

In `bin/tool-play-sound`, replace lines 20-23:

```python
PROJECT_ROOT = Path(os.environ["PROJECT_ROOT"])
SOUNDS_DIR   = Path(os.environ.get("PX_SOUNDS_DIR", PROJECT_ROOT / "sounds"))

# Built-in effects always allowed; recorded sounds are discovered from SOUNDS_DIR.
BUILTIN_SOUNDS = {"chime", "beep", "tada", "alert"}


def allowed_sounds() -> set[str]:
    found = set()
    if SOUNDS_DIR.exists():
        found = {p.stem.lower() for p in SOUNDS_DIR.glob("*.wav")}
    return BUILTIN_SOUNDS | found
```

Then replace the membership check (lines 36-43):

```python
    if name not in allowed_sounds():
        payload = {
            "status": "error",
            "error": f"unknown sound '{name}'; allowed: {sorted(allowed_sounds())}",
        }
        log_event("play_sound", payload)
        print(json.dumps(payload))
        return 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tools.py -k play_sound -v`
Expected: PASS (both new tests).

- [ ] **Step 5: Commit**

```bash
git add bin/tool-play-sound tests/test_tools.py
git commit -m "feat(sound): tool-play-sound discovers recorded sounds dynamically"
```

### Task A2: `bin/tool-record-sound`

**Files:**
- Create: `bin/tool-record-sound`
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `PX_RECORD_NAME` (slug), `PX_RECORD_SECONDS` (1-15, default 5), `PX_SOUNDS_DIR`.
- Produces: writes `<name>.wav` via `arecord`; dry-run skips recording. JSON `{status, name, seconds, path, dry}`. Name sanitised to `[a-z0-9-]` (lowercase, spaces→`-`); rejects empty/invalid.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools.py
def test_tool_record_sound_dry_run(isolated_project, tmp_path):
    sounds = tmp_path / "sounds"; sounds.mkdir()
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"; env["PX_SOUNDS_DIR"] = str(sounds)
    env["PX_RECORD_NAME"] = "Obi Laugh!"; env["PX_RECORD_SECONDS"] = "3"
    payload = parse_json(run_tool(["bin/tool-record-sound"], env))
    assert payload["status"] == "ok"
    assert payload["name"] == "obi-laugh"   # sanitised slug
    assert payload["seconds"] == 3
    assert payload["dry"] is True
    # dry-run must NOT create a file
    assert not (sounds / "obi-laugh.wav").exists()


def test_tool_record_sound_requires_name(isolated_project, tmp_path):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"; env["PX_SOUNDS_DIR"] = str(tmp_path / "sounds")
    env["PX_RECORD_NAME"] = "   "
    payload = parse_json(run_tool(["bin/tool-record-sound"], env))
    assert payload["status"] == "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tools.py -k record_sound -v`
Expected: FAIL — `bin/tool-record-sound` does not exist.

- [ ] **Step 3: Write minimal implementation**

Create `bin/tool-record-sound` (chmod +x):

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/px-env"

python - "$@" <<'PY'
"""Tool: record a short sound from the USB mic, save as a named effect."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from pxh.logging import log_event
from pxh.state import update_session

PROJECT_ROOT = Path(os.environ["PROJECT_ROOT"])
SOUNDS_DIR   = Path(os.environ.get("PX_SOUNDS_DIR", PROJECT_ROOT / "sounds"))


def slugify(raw: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    return s[:40]


def main() -> int:
    dry = os.environ.get("PX_DRY", "0") != "0"
    name = slugify(os.environ.get("PX_RECORD_NAME", ""))
    try:
        seconds = int(os.environ.get("PX_RECORD_SECONDS", "5"))
    except ValueError:
        seconds = 5
    seconds = max(1, min(15, seconds))

    if not name:
        payload = {"status": "error", "error": "PX_RECORD_NAME is required"}
        log_event("record_sound", payload); print(json.dumps(payload)); return 1
    if name in {"chime", "beep", "tada", "alert"}:
        payload = {"status": "error", "error": f"'{name}' is a built-in sound name"}
        log_event("record_sound", payload); print(json.dumps(payload)); return 1

    out_path = SOUNDS_DIR / f"{name}.wav"

    if dry:
        payload = {"status": "ok", "name": name, "seconds": seconds,
                   "path": str(out_path), "dry": True}
        log_event("record_sound", payload); print(json.dumps(payload)); return 0

    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    device = os.environ.get("PX_RECORD_DEVICE", "")
    cmd = ["arecord", "-q", "-f", "cd", "-d", str(seconds)]
    if device:
        cmd.extend(["-D", device])
    cmd.append(str(out_path))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:
        payload = {"status": "error", "error": f"arecord failed: {exc}"}
        log_event("record_sound", payload); print(json.dumps(payload)); return 1
    if result.returncode != 0:
        payload = {"status": "error", "error": "arecord exited non-zero",
                   "rc": result.returncode, "stderr": result.stderr[-512:]}
        log_event("record_sound", payload); print(json.dumps(payload)); return 1

    update_session(
        fields={"last_action": "tool_record_sound"},
        history_entry={"event": "record_sound", "name": name, "seconds": seconds},
    )
    payload = {"status": "ok", "name": name, "seconds": seconds,
               "path": str(out_path), "dry": False}
    log_event("record_sound", payload); print(json.dumps(payload)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY
```

- [ ] **Step 4: Run test to verify it passes**

Run: `chmod +x bin/tool-record-sound && python -m pytest tests/test_tools.py -k record_sound -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/tool-record-sound tests/test_tools.py
git commit -m "feat(sound): add tool-record-sound (record named effect from mic)"
```

### Task A3: Register `tool_record_sound` in the voice loop + prompts

**Files:**
- Modify: `src/pxh/voice_loop.py` (`ALLOWED_TOOLS`, `TOOL_COMMANDS`, `validate_action`)
- Modify: `docs/prompts/{claude,codex}-voice-system.md`, `docs/prompts/persona-{gremlin,vixen}.md`
- Test: `tests/test_voice_loop.py`

**Interfaces:**
- Consumes: `validate_action({"tool":"tool_record_sound","params":{"name":..,"seconds":..}})` → `("tool_record_sound", {"PX_RECORD_NAME":..,"PX_RECORD_SECONDS":..})`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_voice_loop.py
def test_validate_record_sound():
    from pxh.voice_loop import validate_action, ALLOWED_TOOLS
    assert "tool_record_sound" in ALLOWED_TOOLS
    tool, env = validate_action({"tool": "tool_record_sound",
                                 "params": {"name": "Obi Laugh", "seconds": 3}})
    assert tool == "tool_record_sound"
    assert env["PX_RECORD_NAME"] == "Obi Laugh"
    assert env["PX_RECORD_SECONDS"] == "3"


def test_validate_record_sound_clamps_seconds():
    from pxh.voice_loop import validate_action
    _, env = validate_action({"tool": "tool_record_sound",
                              "params": {"name": "x", "seconds": 99}})
    assert env["PX_RECORD_SECONDS"] == "15"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_voice_loop.py -k record_sound -v`
Expected: FAIL — `tool_record_sound` not in `ALLOWED_TOOLS`.

- [ ] **Step 3: Write minimal implementation**

In `src/pxh/voice_loop.py` add `"tool_record_sound",` to `ALLOWED_TOOLS` (after `"tool_play_sound",`, line ~41) and to `TOOL_COMMANDS`:

```python
    "tool_record_sound":   BIN_DIR / "tool-record-sound",
```

Add a `validate_action` branch (near the `tool_play_sound` branch):

```python
    elif tool == "tool_record_sound":
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise VoiceLoopError("tool_record_sound requires a non-empty name")
        sanitized["PX_RECORD_NAME"] = name.strip()[:60]
        seconds = int(clamp(_num(params.get("seconds", 5), "seconds"), 1, 15))
        sanitized["PX_RECORD_SECONDS"] = str(seconds)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_voice_loop.py -k record_sound -v`
Expected: PASS.

- [ ] **Step 5: Update prompt docs**

Add to `docs/prompts/claude-voice-system.md` and `codex-voice-system.md` (tool list) and both persona files, matching the existing bullet style:

```markdown
- tool_record_sound → Record a short sound from the mic and save it with a name Obi picks (params: name, seconds 1-15). Then play it with tool_play_sound.
```

Also update the `tool_play_sound` line in all four docs to note recorded sounds are allowed: `params: name — chime|beep|tada|alert, or any recorded sound`.

- [ ] **Step 6: Commit**

```bash
git add src/pxh/voice_loop.py tests/test_voice_loop.py docs/prompts/
git commit -m "feat(sound): register tool_record_sound in voice loop + prompts"
```

---

# PART B — #33 Obi's Dopamine Menu items

### Task B1: `add` action writes a tagged note; suggest path merges Obi items

**Files:**
- Modify: `bin/tool-dopamine-menu` (add `PX_DOPAMINE_ACTION`, tagged-note read/write)
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `PX_DOPAMINE_ACTION` = `suggest` (default) | `add`; `PX_DOPAMINE_ITEM` (text, for add); existing `PX_DOPAMINE_ENERGY`, `PX_DOPAMINE_CONTEXT`.
- Produces: on `add`, appends a note `"[dopamine-item:<energy>:<context>] <text>"` to the persona-scoped notes file; on `suggest`, the pick pool = builtin MENU[energy][context] ∪ Obi items tagged for that energy/context.
- Tag format: `[dopamine-item:<energy>:<context>]` prefix on the note's `note` field (matches the `[mind]` prefix convention).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools.py
def test_dopamine_add_then_suggest_includes_item(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_DOPAMINE_ACTION"] = "add"
    env["PX_DOPAMINE_ITEM"] = "Watching a volcano video"
    env["PX_DOPAMINE_ENERGY"] = "low"
    env["PX_DOPAMINE_CONTEXT"] = "free"
    add = parse_json(run_tool(["bin/tool-dopamine-menu"], env))
    assert add["status"] == "ok"
    assert add["action"] == "add"

    # the note file lives under the isolated state dir, shared persona scope
    notes = (isolated_project["state_dir"] / "notes.jsonl").read_text()
    assert "[dopamine-item:low:free] Watching a volcano video" in notes

    env2 = isolated_project["env"].copy()
    env2["PX_DRY"] = "1"
    env2["PX_DOPAMINE_ENERGY"] = "low"; env2["PX_DOPAMINE_CONTEXT"] = "free"
    # force the Obi item into the pick set by making it the only candidate path:
    env2["PX_DOPAMINE_PICK_OBI_ONLY"] = "1"   # test seam
    sug = parse_json(run_tool(["bin/tool-dopamine-menu"], env2))
    assert "Watching a volcano video" in sug["picks"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tools.py -k dopamine_add -v`
Expected: FAIL — no `add` action; no tagged note written.

- [ ] **Step 3: Write minimal implementation**

In `bin/tool-dopamine-menu`, add imports and a notes path helper near the top of the heredoc:

```python
from pxh.state import load_session, update_session
from pxh.time import utc_timestamp
from filelock import FileLock

STATE_DIR = Path(os.environ.get("PX_STATE_DIR", PROJECT_ROOT / "state"))


def notes_file_for_persona(persona: str) -> Path:
    return STATE_DIR / (f"notes-{persona}.jsonl" if persona else "notes.jsonl")


def add_item(item: str, energy: str, context: str, persona: str) -> None:
    notes_file = notes_file_for_persona(persona)
    notes_file.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": utc_timestamp(),
              "note": f"[dopamine-item:{energy}:{context}] {item[:200]}"}
    with FileLock(str(notes_file) + ".lock", timeout=10):
        with notes_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def obi_items(energy: str, context: str, persona: str) -> list[str]:
    notes_file = notes_file_for_persona(persona)
    if not notes_file.exists():
        return []
    prefix = f"[dopamine-item:{energy}:{context}]"
    out = []
    for line in notes_file.read_text(encoding="utf-8").strip().splitlines():
        try:
            note = json.loads(line).get("note", "")
        except json.JSONDecodeError:
            continue
        if note.startswith(prefix):
            out.append(note[len(prefix):].strip())
    return out
```

Then in `main()`, branch on the action before the existing suggest logic:

```python
    action = os.environ.get("PX_DOPAMINE_ACTION", "suggest").lower()
    persona = (load_session().get("persona") or "").lower().strip()

    if action == "add":
        item = os.environ.get("PX_DOPAMINE_ITEM", "").strip()
        if not item:
            payload = {"status": "error", "error": "PX_DOPAMINE_ITEM is required"}
            log_event("dopamine_menu", payload); print(json.dumps(payload)); return 1
        if not dry:
            add_item(item, energy, context, persona)
        speak(f"Added that to your menu. {energy} energy.", dry)
        payload = {"status": "ok", "action": "add", "item": item,
                   "energy": energy, "context": context, "dry": dry}
        log_event("dopamine_menu", payload); print(json.dumps(payload)); return 0
```

And in the suggest path, merge Obi items into the pool before `random.sample`:

```python
    pool = list(MENU[energy][context])
    extra = obi_items(energy, context, persona)
    if os.environ.get("PX_DOPAMINE_PICK_OBI_ONLY") == "1" and extra:
        pool = extra
    else:
        pool = pool + extra
    picks = random.sample(pool, min(2, len(pool)))
```

(Note `energy`/`context` are validated against `MENU` *before* `add` so tags use canonical values — keep the existing fallback lines 112-115 above this block.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tools.py -k dopamine -v`
Expected: PASS (new + existing dopamine tests).

- [ ] **Step 5: Commit**

```bash
git add bin/tool-dopamine-menu tests/test_tools.py
git commit -m "feat(dopamine): Obi can add his own menu items via tagged notes"
```

### Task B2: `validate_action` for dopamine add + prompts

**Files:**
- Modify: `src/pxh/voice_loop.py` (`validate_action` branch for `tool_dopamine_menu`)
- Modify: prompt docs
- Test: `tests/test_voice_loop.py`

**Interfaces:**
- Consumes: `validate_action({"tool":"tool_dopamine_menu","params":{"action":"add","item":..,"energy":..,"context":..}})`. `tool_dopamine_menu` is already in `ALLOWED_TOOLS`/`TOOL_COMMANDS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_voice_loop.py
def test_validate_dopamine_add():
    from pxh.voice_loop import validate_action
    tool, env = validate_action({"tool": "tool_dopamine_menu",
        "params": {"action": "add", "item": "magnetic tiles",
                   "energy": "high", "context": "free"}})
    assert tool == "tool_dopamine_menu"
    assert env["PX_DOPAMINE_ACTION"] == "add"
    assert env["PX_DOPAMINE_ITEM"] == "magnetic tiles"
    assert env["PX_DOPAMINE_ENERGY"] == "high"
    assert env["PX_DOPAMINE_CONTEXT"] == "free"


def test_validate_dopamine_add_requires_item():
    import pytest
    from pxh.voice_loop import validate_action, VoiceLoopError
    with pytest.raises(VoiceLoopError):
        validate_action({"tool": "tool_dopamine_menu",
                         "params": {"action": "add"}})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_voice_loop.py -k dopamine -v`
Expected: FAIL (no branch sets these env vars / no item check). If a permissive branch already exists, the `requires_item` test fails first.

- [ ] **Step 3: Write minimal implementation**

Add/replace the `tool_dopamine_menu` branch in `validate_action`:

```python
    elif tool == "tool_dopamine_menu":
        action = str(params.get("action", "suggest")).lower()
        if action not in ("suggest", "add"):
            raise VoiceLoopError("tool_dopamine_menu action must be suggest|add")
        sanitized["PX_DOPAMINE_ACTION"] = action
        energy = str(params.get("energy", "medium")).lower()
        context = str(params.get("context", "free")).lower()
        if energy not in ("high", "medium", "low"):
            raise VoiceLoopError(f"invalid energy '{energy}'")
        if context not in ("free", "focus", "wind-down"):
            raise VoiceLoopError(f"invalid context '{context}'")
        sanitized["PX_DOPAMINE_ENERGY"] = energy
        sanitized["PX_DOPAMINE_CONTEXT"] = context
        if action == "add":
            item = params.get("item")
            if not isinstance(item, str) or not item.strip():
                raise VoiceLoopError("tool_dopamine_menu add requires an item")
            sanitized["PX_DOPAMINE_ITEM"] = item.strip()[:200]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_voice_loop.py -k dopamine -v`
Expected: PASS.

- [ ] **Step 5: Update prompts + commit**

Update the `tool_dopamine_menu` entry in all four prompt docs to mention `action: suggest|add, item (for add), energy: high|medium|low, context: free|focus|wind-down`.

```bash
git add src/pxh/voice_loop.py tests/test_voice_loop.py docs/prompts/
git commit -m "feat(dopamine): validate add action params + document"
```

---

# PART C — #34 Sleep Mode

### Task C1: `spark_sleep_mode` session field

**Files:**
- Modify: `src/pxh/state.py` (`default_state`, near the `spark_quiet_mode` line ~180)
- Modify: `state/session.template.json`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces: every session has `spark_sleep_mode: bool` (default `False`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state.py
def test_default_state_has_sleep_mode():
    from pxh.state import default_state
    assert default_state()["spark_sleep_mode"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_state.py -k sleep_mode -v`
Expected: FAIL — KeyError.

- [ ] **Step 3: Write minimal implementation**

In `src/pxh/state.py` `default_state()`, add after the `spark_quiet_mode` entry:

```python
        "spark_sleep_mode": False,
```

In `state/session.template.json`, add `"spark_sleep_mode": false,` next to `"spark_quiet_mode"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_state.py -k sleep_mode -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/state.py state/session.template.json tests/test_state.py
git commit -m "feat(sleep): add spark_sleep_mode session field"
```

### Task C2: `PX_VOICE_AMPLITUDE` in `tool-voice`

**Files:**
- Modify: `bin/tool-voice` (espeak invocation ~line 220; dry payload)
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `PX_VOICE_AMPLITUDE` (0-200, default 100); clamped.
- Produces: espeak called with `-a <amp>`; dry payload includes `"amplitude": <int>`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools.py
def test_tool_voice_amplitude_in_dry_payload(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"; env["PX_TEXT"] = "hello"; env["PX_VOICE_AMPLITUDE"] = "50"
    payload = parse_json(run_tool(["bin/tool-voice"], env))
    assert payload["status"] == "ok"
    assert payload["amplitude"] == 50


def test_tool_voice_amplitude_clamped(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"; env["PX_TEXT"] = "hi"; env["PX_VOICE_AMPLITUDE"] = "999"
    payload = parse_json(run_tool(["bin/tool-voice"], env))
    assert payload["amplitude"] == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tools.py -k amplitude -v`
Expected: FAIL — `amplitude` key absent (KeyError in test).

- [ ] **Step 3: Write minimal implementation**

In `bin/tool-voice`, where env voice params are read, add:

```python
    try:
        amplitude = int(os.environ.get("PX_VOICE_AMPLITUDE", "100"))
    except ValueError:
        amplitude = 100
    amplitude = max(0, min(200, amplitude))
```

Add `-a <amplitude>` to the espeak arg list:

```python
        [DEFAULT_PLAYER, "-v", voice_variant, "-p", pitch, "-s", rate,
         "-a", str(amplitude), "--stdout", spoken],
```

Add `"amplitude": amplitude` to BOTH the dry-run payload and the normal success payload (find where the dry payload dict is built and include the key).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tools.py -k amplitude -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/tool-voice tests/test_tools.py
git commit -m "feat(voice): PX_VOICE_AMPLITUDE controls espeak amplitude"
```

### Task C3: `bin/tool-sleep` (start|check|end)

**Files:**
- Create: `bin/tool-sleep`
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `PX_SLEEP_ACTION` = `start` | `check` | `end` (default `start`).
- Produces: `start` → sets `spark_sleep_mode: True`, speaks one quiet fact at low amplitude, emote `idle`; `end` → clears flag, normal voice; `check` → reports state. JSON `{status, action, sleep_mode, dry}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools.py
import json as _json2

def test_tool_sleep_start_sets_flag(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"; env["PX_SLEEP_ACTION"] = "start"
    payload = parse_json(run_tool(["bin/tool-sleep"], env))
    assert payload["status"] == "ok"
    assert payload["sleep_mode"] is True
    session = _json2.loads(isolated_project["session_path"].read_text())
    assert session["spark_sleep_mode"] is True


def test_tool_sleep_end_clears_flag(isolated_project):
    env = isolated_project["env"].copy(); env["PX_DRY"] = "1"
    run_tool(["bin/tool-sleep"], {**env, "PX_SLEEP_ACTION": "start"})
    payload = parse_json(run_tool(["bin/tool-sleep"], {**env, "PX_SLEEP_ACTION": "end"}))
    assert payload["sleep_mode"] is False
    session = _json2.loads(isolated_project["session_path"].read_text())
    assert session["spark_sleep_mode"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tools.py -k tool_sleep -v`
Expected: FAIL — `bin/tool-sleep` missing.

- [ ] **Step 3: Write minimal implementation**

Create `bin/tool-sleep` (chmod +x), modelled on `bin/tool-quiet`:

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/px-env"

python - "$@" <<'PY'
"""Tool: SPARK bedtime / sleep mode. start|check|end."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from pxh.logging import log_event
from pxh.state import load_session, update_session
from pxh.time import utc_timestamp

PROJECT_ROOT = Path(os.environ["PROJECT_ROOT"])
TOOL_VOICE = PROJECT_ROOT / "bin" / "tool-voice"
TOOL_EMOTE = PROJECT_ROOT / "bin" / "tool-emote"

QUIET_FACTS = [
    "Did you know octopuses have three hearts? Goodnight.",
    "The moon is moving away from us, very slowly. Sleep well.",
    "Honey never goes off. Rest now.",
]


def speak(text: str, dry: bool, amplitude: str = "100") -> None:
    env = os.environ.copy()
    env["PX_TEXT"] = text[:2000]
    env["PX_VOICE_AMPLITUDE"] = amplitude
    if dry:
        env["PX_DRY"] = "1"
    subprocess.run([str(TOOL_VOICE)], env=env, capture_output=True, check=False)


def emote(name: str, dry: bool) -> None:
    env = os.environ.copy(); env["PX_EMOTE"] = name
    if dry:
        env["PX_DRY"] = "1"
    subprocess.run([str(TOOL_EMOTE)], env=env, capture_output=True, check=False)


def main() -> int:
    dry = os.environ.get("PX_DRY", "0") != "0"
    action = os.environ.get("PX_SLEEP_ACTION", "start").lower()
    session = load_session()

    if action == "start":
        emote("idle", dry)
        import random
        speak(random.choice(QUIET_FACTS), dry, amplitude="45")
        update_session(
            fields={"spark_sleep_mode": True, "last_action": "tool_sleep"},
            history_entry={"event": "sleep_start", "ts": utc_timestamp()},
        )
        payload = {"status": "ok", "action": "start", "sleep_mode": True, "dry": dry}
    elif action == "check":
        payload = {"status": "ok", "action": "check",
                   "sleep_mode": bool(session.get("spark_sleep_mode", False)), "dry": dry}
    elif action == "end":
        emote("curious", dry)
        speak("Good morning. I'm awake.", dry)
        update_session(
            fields={"spark_sleep_mode": False, "last_action": "tool_sleep"},
            history_entry={"event": "sleep_end", "ts": utc_timestamp()},
        )
        payload = {"status": "ok", "action": "end", "sleep_mode": False, "dry": dry}
    else:
        payload = {"status": "error", "error": f"unknown action '{action}'"}
        log_event("sleep", payload); print(json.dumps(payload)); return 1

    log_event("sleep", payload); print(json.dumps(payload)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY
```

- [ ] **Step 4: Run test to verify it passes**

Run: `chmod +x bin/tool-sleep && python -m pytest tests/test_tools.py -k tool_sleep -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/tool-sleep tests/test_tools.py
git commit -m "feat(sleep): add tool-sleep (start|check|end)"
```

### Task C4: `expression()` suppresses + softens speech while asleep

**Files:**
- Modify: `src/pxh/mind.py` (`expression()` ~2928; add `SLEEP_GATED_ACTIONS` near line 450)
- Test: `tests/test_mind.py`

**Interfaces:**
- Consumes: `load_session()["spark_sleep_mode"]`.
- Produces: when asleep, all actions except `wait`/`remember` are suppressed (logged); speech that does fire gets `PX_VOICE_AMPLITUDE=45`. Mirrors the bedtime gate.

- [ ] **Step 1: Write the failing test**

Mirror the existing reflection-redaction test seam (monkeypatch `load_session` + capture). Add to `tests/test_mind.py`:

```python
def test_expression_suppressed_while_sleep_mode(monkeypatch):
    from pxh import mind
    calls = []
    monkeypatch.setattr(mind, "load_session", lambda: {"spark_sleep_mode": True})
    # any voice dispatch would call subprocess.run / _run_voice — capture it
    monkeypatch.setattr(mind, "_run_voice", lambda *a, **k: calls.append(("voice", a, k)))
    # force daytime so night-silence is NOT the thing suppressing
    monkeypatch.setattr(mind, "_is_night_silence", lambda *a, **k: False)
    mind.expression({"thought": "hi", "mood": "content", "action": "comment",
                     "salience": 0.9, "text": "hello there"}, dry=True, awareness={})
    assert calls == []   # comment suppressed while asleep
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mind.py -k sleep_mode -v`
Expected: FAIL — comment is dispatched (voice captured).

- [ ] **Step 3: Write minimal implementation**

Near line 450 (alongside `ABSENT_GATED_ACTIONS`) add:

```python
# Sleep mode suppresses everything except passive wait/remember.
SLEEP_ALLOWED_ACTIONS = {"wait", "remember"}
```

In `expression()`, immediately after `session = load_session()` (insert the load if the function doesn't already read it early), and after the night-silence gate, add:

```python
    if session.get("spark_sleep_mode", False) and action not in SLEEP_ALLOWED_ACTIONS:
        log(f"expression: suppressed {action} — sleep mode")
        return
```

If `expression()` builds a voice env elsewhere, set `env["PX_VOICE_AMPLITUDE"] = "45"` when `session.get("spark_sleep_mode")` is true (defensive — passive paths shouldn't speak, but keeps amplitude consistent).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mind.py -k sleep_mode -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/mind.py tests/test_mind.py
git commit -m "feat(sleep): px-mind Layer-3 suppresses expression while asleep"
```

### Task C5: Whisper-sensitive wake onset

**Files:**
- Modify: `bin/px-wake-listen` (extract `_effective_onset_rms`; read `spark_sleep_mode`; env overrides `PX_RMS_SILENCE`, `PX_SPEECH_ONSET_RMS`, `PX_WHISPER_ONSET_RMS`)
- Test: `tests/test_wake_listen.py` (new)

**Interfaces:**
- Produces: pure helper `effective_onset_rms(sleep_mode: bool) -> int` returning the whisper threshold (`PX_WHISPER_ONSET_RMS`, default 450) when asleep, else the normal `SPEECH_ONSET_RMS` (default 800). Both overridable by env.

**Note:** `bin/px-wake-listen` is a bash+heredoc daemon and hard to import. Extract the pure helper into `src/pxh/wake_utils.py` so it's unit-testable, and call it from the daemon.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wake_listen.py
def test_effective_onset_rms_whisper_when_asleep(monkeypatch):
    from pxh.wake_utils import effective_onset_rms
    monkeypatch.delenv("PX_SPEECH_ONSET_RMS", raising=False)
    monkeypatch.delenv("PX_WHISPER_ONSET_RMS", raising=False)
    assert effective_onset_rms(False) == 800
    assert effective_onset_rms(True) == 450


def test_effective_onset_rms_env_override(monkeypatch):
    from pxh.wake_utils import effective_onset_rms
    monkeypatch.setenv("PX_WHISPER_ONSET_RMS", "300")
    assert effective_onset_rms(True) == 300
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_wake_listen.py -v`
Expected: FAIL — `pxh.wake_utils` missing.

- [ ] **Step 3: Write minimal implementation**

Create `src/pxh/wake_utils.py`:

```python
"""Pure helpers for the wake-word listener (unit-testable without audio)."""
import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def effective_onset_rms(sleep_mode: bool) -> int:
    """Speech-onset RMS threshold; lower (more sensitive) when asleep for whisper wake."""
    if sleep_mode:
        return _int_env("PX_WHISPER_ONSET_RMS", 450)
    return _int_env("PX_SPEECH_ONSET_RMS", 800)
```

In `bin/px-wake-listen`, import and use it where `SPEECH_ONSET_RMS` is currently the literal threshold (read `spark_sleep_mode` from the session it already loads in the loop ~line 1074):

```python
from pxh.wake_utils import effective_onset_rms
# ... inside the loop, after loading session:
onset = effective_onset_rms(bool(_sess.get("spark_sleep_mode", False)))
# replace `>= SPEECH_ONSET_RMS` comparisons with `>= onset`
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_wake_listen.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/wake_utils.py bin/px-wake-listen tests/test_wake_listen.py
git commit -m "feat(sleep): whisper-sensitive wake onset while asleep"
```

### Task C6: Register `tool_sleep`; patchable `spark_sleep_mode`; prompts

**Files:**
- Modify: `src/pxh/voice_loop.py` (`ALLOWED_TOOLS`, `TOOL_COMMANDS`, `validate_action`)
- Modify: `src/pxh/api.py` (`SessionPatch` field + `PATCHABLE_FIELDS`)
- Modify: prompt docs
- Test: `tests/test_voice_loop.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: `validate_action({"tool":"tool_sleep","params":{"action":"start|check|end"}})`; `PATCH /api/v1/session {"spark_sleep_mode": true}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_voice_loop.py
def test_validate_sleep():
    from pxh.voice_loop import validate_action, ALLOWED_TOOLS
    assert "tool_sleep" in ALLOWED_TOOLS
    tool, env = validate_action({"tool": "tool_sleep", "params": {"action": "start"}})
    assert tool == "tool_sleep"
    assert env["PX_SLEEP_ACTION"] == "start"
```

```python
# tests/test_api.py  (follow the reload-after-env pattern used elsewhere)
def test_patch_session_sleep_mode(isolated_project, monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    monkeypatch.setenv("PX_SESSION_PATH", str(isolated_project["session_path"]))
    import importlib, pxh.api as _api
    importlib.reload(_api)
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as client:
        r = client.patch("/api/v1/session", json={"spark_sleep_mode": True},
                         headers={"Authorization": "Bearer testtoken"})
    assert r.status_code == 200
    assert r.json()["spark_sleep_mode"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_voice_loop.py -k validate_sleep tests/test_api.py -k sleep_mode -v`
Expected: FAIL — tool not registered; field not patchable.

- [ ] **Step 3: Write minimal implementation**

`voice_loop.py`: add `"tool_sleep",` to `ALLOWED_TOOLS`, `"tool_sleep": BIN_DIR / "tool-sleep",` to `TOOL_COMMANDS`, and a branch:

```python
    elif tool == "tool_sleep":
        act = str(params.get("action", "start")).lower()
        if act not in ("start", "check", "end"):
            raise VoiceLoopError("tool_sleep action must be start|check|end")
        sanitized["PX_SLEEP_ACTION"] = act
```

`api.py`: add `spark_sleep_mode: Optional[bool] = None` to `SessionPatch`, and `"spark_sleep_mode"` to `PATCHABLE_FIELDS`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_voice_loop.py -k validate_sleep tests/test_api.py -k sleep_mode -v`
Expected: PASS.

- [ ] **Step 5: Update prompts + commit**

Add to all four prompt docs:

```markdown
- tool_sleep → Bedtime mode (params: action — start|check|end). Start: one quiet fact, then silent for the night, whisper to wake. End: back to normal.
```

```bash
git add src/pxh/voice_loop.py src/pxh/api.py tests/test_voice_loop.py tests/test_api.py docs/prompts/
git commit -m "feat(sleep): register tool_sleep + patchable spark_sleep_mode + prompts"
```

---

# PART D — #38 Settings Panel

### Task D1: `execute_tool` applies session voice fields as base

**Files:**
- Modify: `src/pxh/voice_loop.py` (`execute_tool`, persona-voice injection block ~878)
- Test: `tests/test_voice_loop.py`

**Interfaces:**
- Produces: session keys `voice_variant`, `voice_pitch`, `voice_rate` become `PX_VOICE_VARIANT/PITCH/RATE` for every tool, applied **before** `PERSONA_VOICE_ENV` so an active persona still wins (persona characters are preserved; the base SPARK voice is tunable).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_voice_loop.py
def test_execute_tool_applies_session_voice(monkeypatch, tmp_path):
    import pxh.voice_loop as vl
    captured = {}
    monkeypatch.setattr(vl, "load_session", lambda: {
        "voice_variant": "en+m1", "voice_pitch": "60", "voice_rate": "120"})

    class _R:  returncode, stdout, stderr = 0, "{}", ""
    def _fake_run(cmd, **kw):
        captured.update(kw.get("env", {}))
        return _R()
    monkeypatch.setattr(vl.subprocess, "run", _fake_run)
    monkeypatch.setitem(vl.TOOL_COMMANDS, "tool_status", vl.BIN_DIR / "tool-status")
    vl.execute_tool("tool_status", {}, dry_mode=True)
    assert captured["PX_VOICE_VARIANT"] == "en+m1"
    assert captured["PX_VOICE_PITCH"] == "60"
    assert captured["PX_VOICE_RATE"] == "120"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_voice_loop.py -k session_voice -v`
Expected: FAIL — keys absent.

- [ ] **Step 3: Write minimal implementation**

In `execute_tool`, *before* the persona-voice injection block, add:

```python
    _sess = load_session()
    _voice_map = {"voice_variant": "PX_VOICE_VARIANT",
                  "voice_pitch": "PX_VOICE_PITCH",
                  "voice_rate": "PX_VOICE_RATE"}
    for skey, envkey in _voice_map.items():
        val = _sess.get(skey)
        if val:
            env[envkey] = str(val)
    # persona voice (if any) still overrides the tuned base voice:
    session_persona = _sess.get("persona") or ""
    if session_persona and session_persona in PERSONA_VOICE_ENV:
        for k, v in PERSONA_VOICE_ENV[session_persona].items():
            env[k] = v
```

Remove the now-duplicated original persona block (the one that calls `load_session()` again) to avoid a second read.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_voice_loop.py -k "session_voice or persona" -v`
Expected: PASS (and existing persona tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/pxh/voice_loop.py tests/test_voice_loop.py
git commit -m "feat(settings): session voice fields tune the base voice"
```

### Task D2: `PATCH /api/v1/voice`

**Files:**
- Modify: `src/pxh/api.py` (new model + endpoint)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `{variant?: str, pitch?: int 0-99, rate?: int 80-200}` → writes `voice_variant/voice_pitch/voice_rate` to session. Auth required.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api.py
def test_patch_voice_writes_session(isolated_project, monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    monkeypatch.setenv("PX_SESSION_PATH", str(isolated_project["session_path"]))
    import importlib, pxh.api as _api; importlib.reload(_api)
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as client:
        r = client.patch("/api/v1/voice", json={"pitch": 60, "rate": 120, "variant": "en+m1"},
                         headers={"Authorization": "Bearer testtoken"})
    assert r.status_code == 200
    from pxh.state import load_session
    s = load_session()
    assert s["voice_pitch"] == 60 and s["voice_rate"] == 120 and s["voice_variant"] == "en+m1"


def test_patch_voice_rejects_bad_range(isolated_project, monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    monkeypatch.setenv("PX_SESSION_PATH", str(isolated_project["session_path"]))
    import importlib, pxh.api as _api; importlib.reload(_api)
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as client:
        r = client.patch("/api/v1/voice", json={"pitch": 999},
                         headers={"Authorization": "Bearer testtoken"})
    assert r.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api.py -k patch_voice -v`
Expected: FAIL — 404 (no endpoint).

- [ ] **Step 3: Write minimal implementation**

In `api.py`, near the other models/endpoints:

```python
class VoicePatch(BaseModel):
    variant: Optional[str] = None
    pitch: Optional[int] = None
    rate: Optional[int] = None

VALID_VOICE_VARIANTS = {"en-gb", "en-us", "en+m1", "en+m3", "en+f3", "en+f4", "en+croak"}

@app.patch("/api/v1/voice", dependencies=[Depends(_verify_token)])
async def patch_voice(body: VoicePatch) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    if body.variant is not None:
        if body.variant not in VALID_VOICE_VARIANTS:
            raise HTTPException(status_code=400, detail=f"invalid variant: {body.variant!r}")
        fields["voice_variant"] = body.variant
    if body.pitch is not None:
        if not (0 <= body.pitch <= 99):
            raise HTTPException(status_code=400, detail="pitch must be 0-99")
        fields["voice_pitch"] = body.pitch
    if body.rate is not None:
        if not (80 <= body.rate <= 200):
            raise HTTPException(status_code=400, detail="rate must be 80-200")
        fields["voice_rate"] = body.rate
    if not fields:
        raise HTTPException(status_code=400, detail="no voice fields provided")
    return update_session(fields=fields)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_api.py -k patch_voice -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/api.py tests/test_api.py
git commit -m "feat(settings): PATCH /api/v1/voice tunes the base voice"
```

### Task D3: `POST /api/v1/voice/preview`

**Files:**
- Modify: `src/pxh/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: optional `{variant,pitch,rate}` (defaults to current session). Runs `tool_voice` with those overrides + fixed preview text via `execute_tool`. Returns `{status, returncode}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api.py
def test_voice_preview_invokes_tool_voice(isolated_project, monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    monkeypatch.setenv("PX_SESSION_PATH", str(isolated_project["session_path"]))
    import importlib, pxh.api as _api; importlib.reload(_api)
    seen = {}
    def _fake_exec(tool, env, dry, timeout=None):
        seen["tool"] = tool; seen["env"] = env; seen["dry"] = dry
        return 0, "{}", ""
    monkeypatch.setattr(_api, "execute_tool", _fake_exec)
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as client:
        r = client.post("/api/v1/voice/preview", json={"pitch": 50},
                        headers={"Authorization": "Bearer testtoken"})
    assert r.status_code == 200
    assert seen["tool"] == "tool_voice"
    assert seen["env"]["PX_VOICE_PITCH"] == "50"
    assert "PX_TEXT" in seen["env"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api.py -k voice_preview -v`
Expected: FAIL — 404.

- [ ] **Step 3: Write minimal implementation**

```python
@app.post("/api/v1/voice/preview", dependencies=[Depends(_verify_token)])
async def voice_preview(body: VoicePatch) -> JSONResponse:
    s = load_session()
    env = {"PX_TEXT": "Hello, I'm SPARK. This is how I sound."}
    variant = body.variant or s.get("voice_variant")
    pitch = body.pitch if body.pitch is not None else s.get("voice_pitch")
    rate = body.rate if body.rate is not None else s.get("voice_rate")
    if variant:
        env["PX_VOICE_VARIANT"] = str(variant)
    if pitch is not None:
        env["PX_VOICE_PITCH"] = str(pitch)
    if rate is not None:
        env["PX_VOICE_RATE"] = str(rate)
    dry = _resolve_dry(None)
    loop = asyncio.get_running_loop()
    rc, out, err = await loop.run_in_executor(
        None, execute_tool, "tool_voice", env, dry, SYNC_TIMEOUT_DEFAULT)
    return JSONResponse(status_code=200 if rc == 0 else 500,
                        content={"status": "ok" if rc == 0 else "error", "returncode": rc})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_api.py -k voice_preview -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/api.py tests/test_api.py
git commit -m "feat(settings): POST /api/v1/voice/preview plays a sample"
```

### Task D4: Config backup/export + import + runtime config

**Files:**
- Create: `src/pxh/runtime_config.py`
- Modify: `src/pxh/api.py` (`GET /api/v1/config/backup`, `POST /api/v1/config/import`, `PATCH /api/v1/config`)
- Modify: `src/pxh/mind.py` (read runtime backend/model override at reflection start)
- Test: `tests/test_runtime_config.py`, `tests/test_api.py`

**Interfaces:**
- `runtime_config.load() -> dict` reads `state/runtime_config.json` (`{}` if absent); `runtime_config.update(fields: dict) -> dict` merges + atomic-writes. Allowed keys: `mind_backend` (`claude|ollama`), `mind_claude_model` (str), `awareness_interval` (int).
- `mind.py` at the top of `reflection()` applies `mind_backend` override if present.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runtime_config.py
def test_runtime_config_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    import importlib, pxh.runtime_config as rc; importlib.reload(rc)
    assert rc.load() == {}
    rc.update({"mind_backend": "ollama"})
    assert rc.load()["mind_backend"] == "ollama"


def test_runtime_config_rejects_unknown_key(monkeypatch, tmp_path):
    import pytest
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    import importlib, pxh.runtime_config as rc; importlib.reload(rc)
    with pytest.raises(ValueError):
        rc.update({"evil": "x"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_runtime_config.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

Create `src/pxh/runtime_config.py`:

```python
"""Operator-tunable runtime overrides persisted to state/runtime_config.json.

Deliberately NOT .env (which is on the evolve blacklist and needs a restart) —
this file is read live by mind.py each reflection cycle.
"""
import json
import os
from pathlib import Path

from filelock import FileLock

from pxh.state import atomic_write

ALLOWED_KEYS = {"mind_backend", "mind_claude_model", "awareness_interval"}


def _path() -> Path:
    state = Path(os.environ.get("PX_STATE_DIR",
                 Path(__file__).resolve().parent.parent.parent / "state"))
    return state / "runtime_config.json"


def load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def update(fields: dict) -> dict:
    bad = set(fields) - ALLOWED_KEYS
    if bad:
        raise ValueError(f"unknown config keys: {sorted(bad)}")
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(p) + ".lock", timeout=10):
        data = load()
        data.update(fields)
        atomic_write(p, json.dumps(data, indent=2) + "\n")
    return data
```

Add API endpoints in `api.py`:

```python
class ConfigPatch(BaseModel):
    mind_backend: Optional[str] = None
    mind_claude_model: Optional[str] = None
    awareness_interval: Optional[int] = None

@app.patch("/api/v1/config", dependencies=[Depends(_verify_token)])
async def patch_config(body: ConfigPatch) -> Dict[str, Any]:
    import pxh.runtime_config as rc
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="no config fields provided")
    if "mind_backend" in fields and fields["mind_backend"] not in ("claude", "ollama"):
        raise HTTPException(status_code=400, detail="mind_backend must be claude|ollama")
    try:
        return rc.update(fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/api/v1/config/backup", dependencies=[Depends(_verify_token)])
async def config_backup():
    import pxh.runtime_config as rc, tempfile
    export = {"exported_at": utc_timestamp(), "session": load_session(), "runtime_config": rc.load()}
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(export, f, indent=2); f.close()
    return FileResponse(f.name, media_type="application/json",
                        filename="spark-config-backup.json",
                        headers={"Content-Disposition": "attachment"})

class ConfigImport(BaseModel):
    session: Optional[Dict[str, Any]] = None
    runtime_config: Optional[Dict[str, Any]] = None

@app.post("/api/v1/config/import", dependencies=[Depends(_verify_token)])
async def config_import(body: ConfigImport) -> Dict[str, Any]:
    import pxh.runtime_config as rc
    applied = {}
    if body.session:
        fields = {k: v for k, v in body.session.items() if k in PATCHABLE_FIELDS}
        if fields:
            update_session(fields=fields); applied["session"] = sorted(fields)
    if body.runtime_config:
        safe = {k: v for k, v in body.runtime_config.items() if k in rc.ALLOWED_KEYS}
        if safe:
            rc.update(safe); applied["runtime_config"] = sorted(safe)
    return {"status": "ok", "applied": applied}
```

In `mind.py` `reflection()`, near the top (after `session = load_session()`):

```python
    import pxh.runtime_config as _rc
    _override = _rc.load()
    backend = _override.get("mind_backend") or os.environ.get("PX_MIND_BACKEND", "auto")
    # use `backend` where PX_MIND_BACKEND was previously read for this cycle
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_runtime_config.py tests/test_api.py -k "config" -v`
Expected: PASS. Add an API test for `/api/v1/config/backup` returning 200 + JSON attachment.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/runtime_config.py src/pxh/api.py src/pxh/mind.py tests/test_runtime_config.py tests/test_api.py
git commit -m "feat(settings): runtime config overrides + backup/import endpoints"
```

### Task D5: Dashboard Settings sub-tab

**Files:**
- Modify: `src/pxh/api.py` (dashboard HTML/JS — admin sub-tab bar ~2416, panels, JS ~2523)
- Test: `tests/test_api.py` (markup smoke test)

**Interfaces:**
- Adds an `at-settings` admin sub-tab with: voice sliders (pitch/rate) + variant select + Preview button (→ `POST /api/v1/voice/preview`, save → `PATCH /api/v1/voice`); a Sleep-mode toggle (→ `PATCH /api/v1/session {spark_sleep_mode}`); Backup (link to `/api/v1/config/backup`) and Mind backend select (→ `PATCH /api/v1/config`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api.py
def test_dashboard_has_settings_tab(monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    import importlib, pxh.api as _api; importlib.reload(_api)
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as client:
        html = client.get("/").text
    assert 'id="at-settings"' in html
    assert "voice/preview" in html
    assert "spark_sleep_mode" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api.py -k settings_tab -v`
Expected: FAIL — markup absent.

- [ ] **Step 3: Write minimal implementation**

Add the sub-tab button in the admin sub-tab bar (after `at-parental`):

```html
<button class="atab-btn" id="at-settings" onclick="swA('settings')">⚙️ Settings</button>
```

Add the panel (after the parental `apanel` div):

```html
<div id="ap-settings" class="apanel" style="padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:12px">
  <div class="sec-hdr">Voice</div>
  <label>Pitch <input type="range" id="set-pitch" min="0" max="99"></label>
  <label>Rate <input type="range" id="set-rate" min="80" max="200"></label>
  <select id="set-variant">
    <option value="en-gb">en-gb</option><option value="en+m1">en+m1</option>
    <option value="en+f3">en+f3</option><option value="en+f4">en+f4</option>
  </select>
  <button class="btn btn-muted" onclick="voicePreview()">▶ Preview</button>
  <button class="btn btn-spark" onclick="voiceSave()">Save voice</button>
  <div class="sec-hdr">Sleep mode</div>
  <button class="btn btn-muted" id="btn-sleep" onclick="toggleSleep()">Loading…</button>
  <div class="sec-hdr">Mind backend</div>
  <select id="set-backend" onchange="setBackend(this.value)">
    <option value="claude">claude</option><option value="ollama">ollama</option>
  </select>
  <div class="sec-hdr">Backup</div>
  <a class="btn btn-muted" href="/api/v1/config/backup" download>⬇ Export config</a>
</div>
```

Add JS (near the other control functions):

```javascript
async function voicePreview(){await api('/api/v1/voice/preview',{method:'POST',body:JSON.stringify({pitch:+document.getElementById('set-pitch').value,rate:+document.getElementById('set-rate').value,variant:document.getElementById('set-variant').value})});}
async function voiceSave(){await api('/api/v1/voice',{method:'PATCH',body:JSON.stringify({pitch:+document.getElementById('set-pitch').value,rate:+document.getElementById('set-rate').value,variant:document.getElementById('set-variant').value})});}
async function toggleSleep(){try{const s=await api('/api/v1/session');await api('/api/v1/session',{method:'PATCH',body:JSON.stringify({spark_sleep_mode:!s.spark_sleep_mode})});}catch(e){}loadParental();}
async function setBackend(v){await api('/api/v1/config',{method:'PATCH',body:JSON.stringify({mind_backend:v})});}
```

Ensure `swA('settings')` is handled by the existing `swA` switcher (it toggles `.apanel.active` by id `ap-<name>` — the new panel id matches). Also extend `loadParental()` (or add `loadSettings()`) to set the `btn-sleep` label from session.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_api.py -k settings_tab -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/api.py tests/test_api.py
git commit -m "feat(settings): dashboard Settings sub-tab (voice, sleep, backend, backup)"
```

---

# PART E — #36 Layer 1 MCP full-tool exposure

### Task E1: `src/pxh/schemas.py` declarative tool schemas

**Files:**
- Create: `src/pxh/schemas.py`
- Test: `tests/test_schemas.py`

**Interfaces:**
- Produces: `TOOL_SCHEMAS: dict[str, dict]` mapping each tool name to `{"description": str, "params": {name: {"type","required","range"/"enum"/"max"}}}`. Every `ALLOWED_TOOLS` entry has an entry.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schemas.py
def test_every_allowed_tool_has_schema():
    from pxh.voice_loop import ALLOWED_TOOLS
    from pxh.schemas import TOOL_SCHEMAS
    missing = ALLOWED_TOOLS - set(TOOL_SCHEMAS)
    assert not missing, f"tools missing schemas: {sorted(missing)}"


def test_schema_shape():
    from pxh.schemas import TOOL_SCHEMAS
    for name, spec in TOOL_SCHEMAS.items():
        assert "description" in spec
        assert "params" in spec and isinstance(spec["params"], dict)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schemas.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

Create `src/pxh/schemas.py` with an entry per tool (authored from `validate_action`). Example shape (fill in all 45 incl. the new `tool_record_sound`, `tool_sleep`):

```python
"""Declarative parameter schemas for SPARK tools, mirrored from validate_action.

Hand-maintained alongside voice_loop.validate_action — test_schemas.py guards
that every ALLOWED_TOOLS entry is covered.
"""

TOOL_SCHEMAS = {
    "tool_status": {"description": "Report robot status", "params": {}},
    "tool_stop": {"description": "Stop all motion", "params": {}},
    "tool_circle": {"description": "Drive in a circle", "params": {
        "speed": {"type": "int", "range": [0, 60], "required": False},
        "duration": {"type": "float", "range": [1, 12], "required": False}}},
    "tool_drive": {"description": "Drive forward/backward", "params": {
        "direction": {"type": "str", "enum": ["forward", "backward"], "required": False},
        "speed": {"type": "int", "range": [0, 60], "required": False},
        "duration": {"type": "float", "range": [0.1, 10.0], "required": False},
        "steer": {"type": "int", "range": [-35, 35], "required": False}}},
    "tool_voice": {"description": "Speak text aloud", "params": {
        "text": {"type": "str", "max": 2000, "required": True}}},
    "tool_emote": {"description": "Play an emotion animation", "params": {
        "name": {"type": "str", "enum": ["idle","curious","thinking","happy","alert","excited","sad","shy"], "required": False}}},
    "tool_play_sound": {"description": "Play a sound effect", "params": {
        "name": {"type": "str", "required": True}}},
    "tool_record_sound": {"description": "Record a named sound from the mic", "params": {
        "name": {"type": "str", "required": True},
        "seconds": {"type": "int", "range": [1, 15], "required": False}}},
    "tool_sleep": {"description": "Bedtime/sleep mode", "params": {
        "action": {"type": "str", "enum": ["start", "check", "end"], "required": False}}},
    "tool_dopamine_menu": {"description": "Offer/append dopamine-menu activities", "params": {
        "action": {"type": "str", "enum": ["suggest", "add"], "required": False},
        "item": {"type": "str", "required": False},
        "energy": {"type": "str", "enum": ["high","medium","low"], "required": False},
        "context": {"type": "str", "enum": ["free","focus","wind-down"], "required": False}}},
    "tool_announce": {"description": "Announce via Nest speakers", "params": {
        "text": {"type": "str", "required": True},
        "targets": {"type": "list", "required": False}}},
    # ... one entry for EVERY remaining tool in ALLOWED_TOOLS ...
}
```

(The implementer fills every remaining tool; `test_schemas.py` red-guards completeness — iterate until green.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_schemas.py -v`
Expected: PASS once all tools are covered.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/schemas.py tests/test_schemas.py
git commit -m "feat(mcp): declarative TOOL_SCHEMAS covering all tools"
```

### Task E2: MCP `spark_list_tools` + `spark_run_tool` (dry-default, motion-gated)

**Files:**
- Modify: `src/pxh/mcp_server.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- `spark_list_tools()` → `TOOL_SCHEMAS`. `spark_run_tool(tool: str, params: dict = {}, dry: bool = True)` → validates via `validate_action`, executes via `execute_tool`, returns `{returncode, stdout, stderr, dry}`. Defaults `dry=True` for safety; motion still blocked by `confirm_motion_allowed` (rc=2 surfaces as `status: blocked`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_server.py
def test_spark_list_tools_covers_all():
    from pxh import mcp_server
    from pxh.voice_loop import ALLOWED_TOOLS
    listed = mcp_server.spark_list_tools()
    assert set(listed) >= ALLOWED_TOOLS


def test_spark_run_tool_dry(monkeypatch):
    from pxh import mcp_server
    captured = {}
    monkeypatch.setattr(mcp_server, "execute_tool",
        lambda tool, env, dry, timeout=None: captured.update(
            {"tool": tool, "dry": dry}) or (0, '{"status":"ok"}', ""))
    out = mcp_server.spark_run_tool("tool_status", {})
    assert captured["tool"] == "tool_status"
    assert captured["dry"] is True   # safe default
    assert out["returncode"] == 0


def test_spark_run_tool_rejects_unknown(monkeypatch):
    from pxh import mcp_server
    out = mcp_server.spark_run_tool("tool_evil", {})
    assert out["status"] == "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_server.py -k "list_tools or run_tool" -v`
Expected: FAIL — functions don't exist.

- [ ] **Step 3: Write minimal implementation**

In `src/pxh/mcp_server.py`, add imports and two new `@mcp.tool()` functions:

```python
from pxh.voice_loop import validate_action, execute_tool, VoiceLoopError
from pxh.schemas import TOOL_SCHEMAS


@mcp.tool()
def spark_list_tools() -> dict:
    """List every SPARK tool and its parameter schema."""
    return TOOL_SCHEMAS


@mcp.tool()
def spark_run_tool(tool: str, params: dict | None = None, dry: bool = True) -> dict:
    """Run a SPARK tool. dry=True (default) simulates; set dry=False to actuate.
    Motion tools still require confirm_motion_allowed in session state."""
    try:
        validated_tool, env = validate_action({"tool": tool, "params": params or {}})
    except VoiceLoopError as exc:
        return {"status": "error", "error": str(exc)}
    rc, out, err = execute_tool(validated_tool, env, dry_mode=dry)
    status = "ok" if rc == 0 else ("blocked" if rc == 2 else "error")
    return {"status": status, "returncode": rc,
            "stdout": out[-4096:], "stderr": err[-2048:], "dry": dry}
```

Update the FastMCP `instructions=` string to note tools are now actionable (dry by default) and motion is gated.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_server.py -v`
Expected: PASS (new + existing 5-tool tests).

- [ ] **Step 5: Commit**

```bash
git add src/pxh/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): expose all SPARK tools via spark_run_tool (dry-default, gated)"
```

### Task E3: `spark://` resources (session, thoughts, notes)

**Files:**
- Modify: `src/pxh/mcp_server.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Resources `spark://session`, `spark://thoughts`, `spark://notes` return the current JSON via FastMCP `@mcp.resource(...)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_server.py
def test_resource_session_reads_state(monkeypatch, tmp_path):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    (tmp_path / "session.json").write_text('{"persona":"spark"}')
    import importlib, pxh.mcp_server as m; importlib.reload(m)
    assert '"persona"' in m.resource_session()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_server.py -k resource_session -v`
Expected: FAIL — function missing.

- [ ] **Step 3: Write minimal implementation**

```python
@mcp.resource("spark://session")
def resource_session() -> str:
    """Current session.json."""
    return json.dumps(_read_json(STATE_DIR / "session.json") or {}, indent=2)


@mcp.resource("spark://thoughts")
def resource_thoughts() -> str:
    """Recent SPARK thoughts."""
    return json.dumps(_read_jsonl_tail(STATE_DIR / "thoughts-spark.jsonl", 20), indent=2)


@mcp.resource("spark://notes")
def resource_notes() -> str:
    """SPARK long-term notes."""
    return json.dumps(_read_jsonl_tail(STATE_DIR / "notes-spark.jsonl", 20), indent=2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_server.py -k resource -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): add spark:// session/thoughts/notes resources"
```

### Task E4: Pin `mcp` dependency + docs

**Files:**
- Modify: `requirements.txt`
- Modify: `CLAUDE.md` (MCP Server section — update tool count/capabilities)
- Test: n/a (dependency + docs)

- [ ] **Step 1: Add dependency**

Append to `requirements.txt`:

```
mcp>=1.27
```

- [ ] **Step 2: Verify install + full suite**

Run: `pip install -r requirements.txt && python -m pytest -q`
Expected: full suite green.

- [ ] **Step 3: Update CLAUDE.md**

In the "MCP Server" subsection, change the description from "5 read-only tools" to note it now also exposes `spark_list_tools` + `spark_run_tool` (all tools, dry-by-default, motion-gated) and `spark://` resources.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt CLAUDE.md
git commit -m "chore(mcp): pin mcp dependency; document expanded MCP server"
```

---

## Out of Scope (this session)

- **#162** (Layer 3 autonomous coding agent) — highest-risk, deferred; blocked on the #25 persona being proven. Not in this batch.
- **#31 Face Follow**, **#32 Python Lesson Mode**, **#35 Obstacle Course** — net-new hardware/loop features, each a session of its own; not selected for this batch.
- **Live hardware validation** (mic record for #26, whisper-RMS tuning for #34, espeak amplitude audibility) — needs the Pi + mic; do after merge, like the announce-pipeline Phase-0 gates.

## Self-Review

**Spec coverage:**
- #26 → A1 (dynamic allowlist), A2 (record tool), A3 (registration). ✓
- #33 → B1 (add+merge), B2 (validate+prompts). ✓
- #34 → C1 (flag), C2 (amplitude), C3 (tool-sleep), C4 (mind suppression), C5 (whisper wake), C6 (register+patchable). ✓ (the issue's "lower espeak amplitude" = C2/C4; "whisper wake" = C5; "L3 skip" = C4; "tool-sleep start|check|end" = C3.)
- #38 → D1 (voice base), D2 (PATCH voice), D3 (preview), D4 (backup/import/config), D5 (dashboard). Session & Persona section reuses existing PATCH /session; Mind & Model persisted via runtime_config. ✓
- #36 L1 → E1 (schemas), E2 (run/list), E3 (resources), E4 (dep/docs). L2 already shipped (noted). ✓

**Placeholder scan:** E1 deliberately leaves "fill remaining tools" — guarded red by `test_schemas.py` (completeness is the test, not a guess). All other steps carry runnable code. No TBD/TODO.

**Type consistency:** `validate_action(action_dict) -> (tool, env)` and `execute_tool(tool, env, dry_mode, timeout) -> (rc, out, err)` used identically in D3, E2. Session voice keys `voice_variant/voice_pitch/voice_rate` consistent across D1/D2/D3. `spark_sleep_mode` consistent across C1/C4/C6/D5. `effective_onset_rms(bool)` consistent C5.

**Risks called out:** D1 changes voice for every tool — persona override ordering preserved + covered by test. C4 inserts a gate into the hot `expression()` path — test asserts suppression; keep the existing gates intact. E2 defaults `dry=True` so MCP can't accidentally actuate the robot.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-20-obi-session-batch.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
