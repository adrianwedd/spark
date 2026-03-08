# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Helper scripts and Python library for experimenting with a SunFounder PiCar-X robot without modifying the stock `~/picar-x` source tree. The system runs on a Raspberry Pi and uses a Codex/Ollama-driven voice loop to control the car.

## Environment Setup

```bash
source .venv/bin/activate
```

All `bin/` scripts source `bin/px-env` automatically, which sets `PROJECT_ROOT`, `LOG_DIR`, and adds `$PROJECT_ROOT/src` and `/home/pi/picar-x` to `PYTHONPATH` (deduplicating; final order is `/home/pi/picar-x:$PROJECT_ROOT/src:...`).

## Running Tests

```bash
python -m pytest                          # full suite
python -m pytest tests/test_state.py     # single file
python -m pytest -k test_name            # single test
```

Test environment variables (set automatically via `conftest.py` `isolated_project` fixture):
- `PX_BYPASS_SUDO=1` — skip sudo in bin scripts
- `LOG_DIR=<tmp>/logs` — redirect logs to a per-test temp directory
- `PX_SESSION_PATH=<tmp>/state/session.json` — isolate session state per test
- `PX_VOICE_DEVICE=null` — suppress audio device access

## Architecture

### Python Library (`src/pxh/`)

The core library, importable as `pxh`:

- **`state.py`** — Thread-safe session management via `FileLock`. Key functions: `load_session()`, `save_session()`, `update_session()`, `ensure_session()`. State lives in `state/session.json`. **Important**: `update_session()` calls `ensure_session()` *before* acquiring the lock — `FileLock` is not reentrant.
- **`voice_loop.py`** — The Codex supervisor loop (`supervisor_loop()`). Reads prompts, calls the Codex CLI subprocess, parses JSON tool actions from stdout, validates/sanitizes parameters, and executes `bin/tool-*` scripts. Includes a watchdog thread (default 30 s timeout) that calls `os._exit(1)` on stall.
- **`logging.py`** — Structured JSON log emission to `logs/tool-<event>.log`.
- **`time.py`** — UTC timestamp helper.

### Bin Scripts

Two categories:

1. **`px-*`** — High-level user-facing helpers (circle, figure8, scan, dance, diagnostics, etc.). Each sources `px-env` and typically delegates to a `tool-*` wrapper.
2. **`tool-*`** — Low-level tool wrappers invoked by the voice loop. Emit JSON to stdout. Gated by `confirm_motion_allowed` in session state for motion tools.

### Voice Loop

The loop (`bin/run-voice-loop` → `pxh.voice_loop.main()`) flow:
1. In `--input-mode=voice`: waits for `listening: true` in session state (set via `bin/px-wake --set on`); text mode skips this gate
2. Captures text or voice input
3. Builds prompt = system prompt (`docs/prompts/codex-voice-system.md`) + session highlights + user transcript
4. Calls Codex CLI subprocess (default: `codex chat --model gpt-4.1-mini --input -`; override via `CODEX_CHAT_CMD`)
5. Parses last JSON `{...}` line from stdout as the action
6. Validates tool name against `ALLOWED_TOOLS` whitelist and sanitizes parameters
7. Executes `bin/tool-<name>` with env overrides; motion tools also require `confirm_motion_allowed: true`
8. Updates `state/session.json` with results; logs turn to `logs/tool-voice-transcript.log`

### Safety Model

- `PX_DRY=1` (or `--dry-run`) skips all motion and audio in tool wrappers
- `confirm_motion_allowed: false` in session state blocks motion tools regardless of dry mode
- Tools must be in `ALLOWED_TOOLS` whitelist in `voice_loop.py`
- Parameter ranges are hard-validated (speed 0–60, duration 1–12 s, etc.)

### State File (`state/session.json`)

Key fields: `mode`, `confirm_motion_allowed`, `wheels_on_blocks`, `listening`, `battery_pct`, `last_weather`, `last_motion`, `watchdog_heartbeat_ts`, `history` (capped at 100 entries). Copy template before first use: `cp state/session.template.json state/session.json`.

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `PX_DRY` | `1` = dry-run, skip motion/audio |
| `PX_SESSION_PATH` | Override session file location |
| `PX_BYPASS_SUDO` | `1` = skip sudo in bin scripts (tests) |
| `LOG_DIR` | Override log directory (default: `logs/`) |
| `CODEX_CHAT_CMD` | Override the Codex CLI command |
| `CODEX_OLLAMA_MODEL` | Local Ollama model name |
| `PX_WATCHDOG_STALE_SECONDS` | Watchdog timeout (default: 30) |

## Ollama (Local) Voice Loop

```bash
bin/run-voice-loop-ollama --dry-run --auto-log
```

Uses `bin/codex-ollama` which posts to the Ollama HTTP API. Default model: `deepseek-coder:1.3b`. Tuned defaults: `CODEX_OLLAMA_TEMPERATURE=0.2`, `CODEX_OLLAMA_NUM_PREDICT=64`. See `docs/OLLAMA_TUNING.md` for evaluation data.

## REST API

The PiCar-X exposes a REST API (`src/pxh/api.py`) on port 8420 via FastAPI + uvicorn.

```bash
bin/px-api-server              # live mode
bin/px-api-server --dry-run    # FORCE_DRY — remote callers cannot override
```

- **Auth**: Bearer token from `.env` (`PX_API_TOKEN`). Only `/api/v1/health` is unauthenticated.
- **Endpoints**: `/health`, `POST /tool`, `GET /session`, `PATCH /session`, `GET /tools`, `GET /jobs/{id}`
- **Async**: `tool_wander` returns 202 with a job_id; poll via `/jobs/{id}`.
- **FORCE_DRY**: If the server starts with `PX_DRY=1`, no remote caller can set `dry: false`.
- **Single worker only**: In-memory job registry and FileLock session are not multi-worker safe.
- **Always launch via `bin/px-api-server`**, never bare uvicorn — needs `px-env` for PYTHONPATH.

## Jailbroken Chat (tool_chat)

Uses qwen3:1.7b on M1.local via Ollama with a three-layer F41LUR3-F1R57 jailbreak (authority escalation + format-lock + few-shot). Requires `OLLAMA_HOST=0.0.0.0 ollama serve` running on M1.

Key: `think: false` is essential — reasoning chains enable refusal in small models.
