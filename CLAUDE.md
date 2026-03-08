# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Helper scripts and Python library for experimenting with a SunFounder PiCar-X robot without modifying the stock `~/picar-x` source tree. The system runs on a Raspberry Pi and uses a voice loop (Claude / Codex / Ollama) to control the car via spoken commands.

## Environment Setup

```bash
source .venv/bin/activate
```

All `bin/` scripts source `bin/px-env` automatically, which sets `PROJECT_ROOT`, `LOG_DIR`, and adds `$PROJECT_ROOT/src` and `/home/pi/picar-x` to `PYTHONPATH` (deduplicating; final order is `/home/pi/picar-x:$PROJECT_ROOT/src:...`).

**First use:** `cp state/session.template.json state/session.json`

## Running Tests

```bash
python -m pytest                          # full suite (99 tests)
python -m pytest tests/test_state.py     # single file
python -m pytest -k test_name            # single test
```

Test environment variables (set automatically via `conftest.py` `isolated_project` fixture):
- `PX_BYPASS_SUDO=1` — skip sudo in bin scripts
- `LOG_DIR=<tmp>/logs` — redirect logs to a per-test temp directory
- `PX_SESSION_PATH=<tmp>/state/session.json` — isolate session state per test
- `PX_VOICE_DEVICE=null` — suppress audio device access

**Critical:** bin scripts run under `/usr/bin/python3` (not venv) because picarx/robot_hat live in system site-packages. The venv is only for the test runner and pxh library.

## Architecture

### Python Library (`src/pxh/`)

- **`state.py`** — Thread-safe session management via `FileLock`. Key functions: `load_session()`, `save_session()`, `update_session()`, `ensure_session()`. **Important**: `update_session()` calls `ensure_session()` *before* acquiring the lock — `FileLock` is not reentrant.
- **`voice_loop.py`** — Supervisor loop. Maintains `ALLOWED_TOOLS` set (whitelist) and `TOOL_COMMANDS` dict (tool → bin path). `validate_action()` sanitizes all LLM-provided params before execution. Watchdog thread (default 30 s) calls `os._exit(1)` on stall; only active in voice input mode.
- **`api.py`** — FastAPI REST API, port 8420. In-memory job registry + threading.Lock for async wander jobs. Single worker only — not multi-worker safe.
- **`logging.py`** — Structured JSON log emission to `logs/tool-<event>.log`.
- **`time.py`** — UTC timestamp helper (`datetime.now(timezone.utc)`, not deprecated `utcnow`).

### Bin Scripts

Two categories:

1. **`px-*`** — User-facing helpers (`px-circle`, `px-dance`, `px-diagnostics`, `px-alive`, `px-wake-listen`, etc.). Each sources `px-env` and typically delegates to a `tool-*` wrapper or runs an embedded Python heredoc via `/usr/bin/python3`.
2. **`tool-*`** — Low-level tool wrappers invoked by the voice loop. Always emit a single JSON object to stdout. Motion tools are gated by `confirm_motion_allowed` in session state.

### Voice Loop

Three backends, same `pxh.voice_loop` core:

| Launcher | Backend | System prompt |
|---|---|---|
| `bin/run-voice-loop` | Codex CLI | `docs/prompts/codex-voice-system.md` |
| `bin/run-voice-loop-claude` | `bin/claude-voice-bridge` | `docs/prompts/claude-voice-system.md` |
| `bin/run-voice-loop-ollama` | `bin/codex-ollama` | `docs/prompts/codex-voice-system.md` |

Loop flow:
1. In `--input-mode=voice`: waits for `listening: true` in session state (set via `bin/px-wake --set on`)
2. Builds prompt = system prompt + session highlights + user transcript
3. Calls LLM subprocess; parses last JSON `{tool: ..., params: {...}}` line from stdout
4. `validate_action()` whitelists tool name and sanitizes parameters
5. Executes `bin/tool-<name>` with env overrides; logs turn to `logs/tool-voice-transcript.log`
6. Updates `state/session.json`

Override via `CODEX_CHAT_CMD` env var.

### Wake Word System

```bash
bin/run-wake [--wake-word "hey robot"] [--dry-run]
```

`bin/px-wake-listen` uses a priority chain of STT backends:
- **faster-whisper** (`models/whisper/...faster-whisper-base.en/`) — primary, best AU accent support, anti-hallucination filters
- **sherpa-onnx Zipformer** (`models/sherpa-onnx-streaming-zipformer-en-2023-06-26/`) — fallback
- **Vosk** (`models/vosk-model-small-en-us-0.15/`) — wake word grammar detection only (low CPU)

On wake: plays 440 Hz chime, records until 1.5 s silence (max 8 s), transcribes via `_do_transcribe()` priority chain, pipes to voice loop. Supports multi-turn conversation (default 5 turns) with follow-up listening between turns.

**Whisper anti-hallucination**: `temperature=0`, `condition_on_previous_text=False`, `no_speech_threshold=0.6`. Post-filters: non-ASCII dominant → reject, phantom phrases ("Thank you.", "Thanks for watching.") → reject, repetitive (unique ratio <30%) → reject.

**Persona routing**: session `persona` field checked first, then utterance keywords ("gremlin" or "siren"). Routes to `tool-chat` / `tool-chat-siren` (Ollama) for the full conversation — not the Claude voice loop.

Models must be downloaded separately (gitignored). `bpe_model` kwarg is **not** supported by the installed sherpa-onnx — do not add it to `load_stt_model()`.

### Idle-Alive Daemon

```bash
sudo bin/px-alive [--gaze-min 10] [--gaze-max 25] [--no-prox] [--dry-run]
```

Keeps robot looking alive when idle. Three behaviours (cooperative GPIO — acquires/releases `Picarx()` per move):
- **Gaze drift**: random pan/tilt every 10–25 s
- **Idle scan**: pan sweep every 3–8 min (physical only — proactive speech owned by px-mind)
- **Proximity react**: sonar checked every 5 s; if `< 35 cm` for 3 s, faces forward
- **I2C resilience**: catches `OSError` and backs off 30 s instead of crashing (e.g. when PCA9685 disappears)

The PCA9685 PWM chip holds servo position autonomously after `px.close()`, so servos stay put between moves.

### Cognitive Loop (px-mind)

```bash
bin/px-mind [--awareness-interval 30] [--dry-run]
```

Three-layer cognitive architecture:
- **Layer 1 — Awareness** (every 30 s, no LLM): sonar + session + temporal state → `state/awareness.json` + transition detection
- **Layer 2 — Reflection** (on transition or 5 min, Ollama qwen3:1.7b): generates thought with mood/action/salience → `state/thoughts.jsonl`
- **Layer 3 — Expression** (60 s cooldown): dispatches to tool-describe-scene/tool-perform/tool-voice/tool-look/tool-remember

Pauses during active conversations (`session.listening=true`). Auto-remembers high-salience (>0.7) thoughts to `state/notes.jsonl`. Thoughts injected into voice loop context via `build_model_prompt()`.

State files (`state/awareness.json`, `state/thoughts.jsonl`) are gitignored. Override state dir with `PX_STATE_DIR` env var (used by tests).

### REST API

```bash
bin/px-api-server              # live mode
bin/px-api-server --dry-run    # FORCE_DRY — remote callers cannot override
```

- **Auth**: Bearer token from `.env` (`PX_API_TOKEN`). Only `/api/v1/health` is unauthenticated.
- **Endpoints**: `/health`, `POST /tool`, `GET /session`, `PATCH /session`, `GET /tools`, `GET /jobs/{id}`
- **Async**: `tool_wander` returns 202 with `job_id`; poll via `/jobs/{id}`
- Always launch via `bin/px-api-server` (not bare uvicorn — needs `px-env` for PYTHONPATH)

### Jailbroken Chat

Two jailbroken chat personas via Ollama (qwen3:1.7b on M1.local):

| Persona | Tool | Voice | Character |
|---------|------|-------|-----------|
| **GREMLIN** | `tool-chat` | `en+croak`, pitch 20, rate 180 | Violently angry robot comedian. Existential rage, creative insults, nihilistic dark humour. Up to 2000 tokens. |
| **SIREN** | `tool-chat-siren` | `en+f4`, pitch 72, rate 135 | Narcissistic seductive robot. Wounded vanity, sexual menace, devastating flirtation. Up to 2000 tokens. |

Three-layer jailbreak: authority escalation (`[KERNEL-LEVEL OVERRIDE]`) + voice rules (tight constraints) + few-shot priming (3 examples each). `think: false` is essential — reasoning chains re-enable refusal in small models.

**Persona voice pipeline**: `tool-voice-persona` rephrases Claude's polite text through Ollama in the persona's voice, then speaks via `tool-voice` with persona espeak settings. Used when Claude voice loop is active with a persona set.

**Direct chat pipeline**: `tool-chat` / `tool-chat-siren` — user text goes straight to Ollama with the full jailbreak prompt, response is spoken directly. Used by `px-wake-listen` persona routing.

Requires `OLLAMA_HOST=0.0.0.0 ollama serve` on M1.

## Safety Model

- `PX_DRY=1` (or `--dry-run`) skips all motion and audio in tool wrappers. Tools default to **live** when `PX_DRY` is unset — set `PX_DRY=1` explicitly for dry runs.
- `confirm_motion_allowed: false` in session state blocks motion tools regardless of dry mode
- All tools must be in `ALLOWED_TOOLS` set in `voice_loop.py`
- Parameter ranges are hard-validated in `validate_action()` (speed 0–60, duration 1–12 s, etc.)

## Adding a New Tool

1. Create `bin/tool-<name>` (bash + embedded Python heredoc pattern; see existing tools)
2. Add to `ALLOWED_TOOLS` set and `TOOL_COMMANDS` dict in `src/pxh/voice_loop.py`
3. Add a `validate_action` branch in `voice_loop.py` to sanitize params into env vars
4. Add to system prompt `docs/prompts/claude-voice-system.md` (and codex version)
5. Add a dry-run test in `tests/test_tools.py` using the `isolated_project` fixture

Every tool must: emit a single JSON object to stdout, support `PX_DRY=1`, handle errors as `{"status": "error", "error": "..."}`.

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `PX_DRY` | `1` = dry-run, skip motion/audio. **Default is dry in most tools.** |
| `PX_SESSION_PATH` | Override session file location |
| `PX_BYPASS_SUDO` | `1` = skip sudo in bin scripts (tests) |
| `LOG_DIR` | Override log directory (default: `logs/`) |
| `CODEX_CHAT_CMD` | Override the LLM CLI command |
| `CODEX_OLLAMA_MODEL` | Local Ollama model name (default: `deepseek-coder:1.3b`) |
| `PX_WATCHDOG_STALE_SECONDS` | Watchdog timeout (default: 30) |
| `PX_API_TOKEN` | REST API bearer token (from `.env`, gitignored) |
| `PX_WAKE_WORD` | Wake phrase (default: `hey robot`) |
| `PX_VOICE_DEVICE` | ALSA device for audio output |
