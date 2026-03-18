# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Helper scripts and Python library for a SunFounder PiCar-X robot built by Adrian and Obi together — with Obi, not for him. The system runs on a Raspberry Pi and uses a voice loop (Claude / Codex / Ollama) to control the car via spoken commands, with two jailbroken personas (GREMLIN and VIXEN) and a three-layer cognitive architecture that gives the robot an inner life. Adrian and Claude wrote the code; Codex and Gemini helped with QA.

## Environment Setup

```bash
source .venv/bin/activate
```

All `bin/` scripts source `bin/px-env` automatically, which sets `PROJECT_ROOT`, `LOG_DIR`, and adds `$PROJECT_ROOT/src` and `/home/pi/picar-x` to `PYTHONPATH` (deduplicating; final order is `/home/pi/picar-x:$PROJECT_ROOT/src:...`).

**First use:** `cp state/session.template.json state/session.json`

## Running Tests

```bash
python -m pytest                          # full suite (450 tests)
python -m pytest tests/test_state.py     # single file
python -m pytest -k test_name            # single test
python -m pytest -m "not live"           # skip hardware tests (82 tests)
sudo .venv/bin/python -m pytest tests/test_tools_live.py -v -s  # live hardware tests
```

Test environment variables (set automatically via `conftest.py` `isolated_project` fixture):
- `PX_BYPASS_SUDO=1` — skip sudo in bin scripts
- `LOG_DIR=<tmp>/logs` — redirect logs to a per-test temp directory
- `PX_SESSION_PATH=<tmp>/state/session.json` — isolate session state per test
- `PX_VOICE_DEVICE=null` — suppress audio device access

**Critical:** bin scripts run under `/usr/bin/python3` (not venv) because picarx/robot_hat live in system site-packages. The venv is only for the test runner and pxh library.

## Architecture

### Python Library (`src/pxh/`)

- **`state.py`** — Thread-safe session management via `FileLock` (10 s timeout — raises `filelock.Timeout` on deadlock). Key functions: `load_session()`, `save_session()`, `update_session()`, `ensure_session()`, `atomic_write()`, `rotate_log()`. **Important**: `update_session()` calls `ensure_session()` *before* acquiring the lock — `FileLock` is not reentrant. `atomic_write()` uses `mkstemp` + `fsync` + `os.replace` for SD card durability. `rotate_log()` keeps last half of lines when file exceeds 5 MB, using `atomic_write()` for durability.
- **`mind.py`** — Cognitive loop daemon (`bin/px-mind` is a thin launcher). Three-layer architecture: awareness (sensors + state), reflection (LLM thought generation), expression (speech/action dispatch). 3,300+ lines extracted from the original bin/px-mind heredoc. See [Cognitive Loop](#cognitive-loop-px-mind) below.
- **`voice_loop.py`** — Supervisor loop. Maintains `ALLOWED_TOOLS` set (whitelist) and `TOOL_COMMANDS` dict (tool → bin path). `validate_action()` sanitizes all LLM-provided params before execution. `PERSONA_VOICE_ENV` dict maps persona names to espeak voice settings, injected into all tool env vars via `execute_tool()` when a persona is active. `execute_tool()` accepts an optional `timeout` parameter — `subprocess.run` kills the child on `TimeoutExpired`. Watchdog thread (default 30 s) sends SIGTERM + 5 s grace period (instead of `os._exit(1)`) on stall; only active in voice input mode.
- **`api.py`** — FastAPI REST API, port 8420. In-memory job registry + threading.Lock for async wander jobs. Single worker only — not multi-worker safe. PIN rate limiting is per-IP (v2 schema in `state/pin_lockout.json`) with file-based persistence across API restarts, 1000-IP hard cap with two-phase eviction. `X-Forwarded-For` only trusted from localhost (Cloudflare tunnel). Rate limit store capped at 10k IPs with oldest-first eviction. PIN verify returns short-lived session tokens (4h TTL) instead of the raw Bearer token. Device reboot/shutdown requires two-step nonce confirmation.
- **`logging.py`** — Structured JSON log emission to `logs/tool-<event>.log`. Uses late import of `rotate_log` from state.py to avoid circular dependency.
- **`time.py`** — UTC timestamp helper (`datetime.now(timezone.utc)`, not deprecated `utcnow`).
- **`token_log.py`** — LLM token usage accounting. Logs prompt/response token counts per call.
- **`utils.py`** — Shared utilities (`clamp()` for numeric range clamping).
- **`patch_login.py`** — Monkey-patches `os.getlogin()` to handle systemd environments (no /dev/tty). Also installed globally as `~/.local/lib/python3.11/site-packages/usercustomize.py`.

### os.getlogin() Under Systemd

`picarx.py:48` calls `os.getlogin()` in `Picarx.__init__()`. Under systemd there is no `/dev/tty`, so this raises `OSError: [Errno 6] No such device or address`. The fix is a `usercustomize.py` in user site-packages that wraps `os.getlogin()` with a fallback to `LOGNAME`/`USER` env vars. This affects every script that creates a `Picarx()` instance — all 14+ GPIO scripts. Do not remove the usercustomize.py or the I2C errors will return.

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
2. Builds prompt = system prompt + session highlights + user transcript + recent thoughts/mood from px-mind
3. Calls LLM subprocess; parses last JSON `{tool: ..., params: {...}}` line from stdout
4. `validate_action()` whitelists tool name and sanitizes parameters
5. `execute_tool()` injects persona voice env vars if `session.persona` is set, then runs `bin/tool-<name>`
6. Updates `state/session.json`

Override via `CODEX_CHAT_CMD` env var.

### Wake Word System

```bash
bin/run-wake [--wake-word "hey robot"] [--dry-run]
```

`bin/px-wake-listen` uses a priority chain of STT backends:
- **SenseVoice** (`models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/`) — primary; non-autoregressive, fastest (~5s), handles AU English
- **faster-whisper** (`models/whisper/...faster-whisper-base.en/`) — fallback; best AU accent support, anti-hallucination filters
- **sherpa-onnx Zipformer** (`models/sherpa-onnx-streaming-zipformer-en-2023-06-26/`) — second fallback
- **Vosk** (`models/vosk-model-small-en-us-0.15/`) — wake word grammar detection only (low CPU)

On wake: plays 440 Hz chime, records until `SILENCE_S=3.0 s` of quiet (RMS < 300) or `MAX_RECORD_S=20 s` hard cap, transcribes via `_do_transcribe()` priority chain, pipes to voice loop. Supports multi-turn conversation (default 5 turns) with follow-up listening between turns.

**Whisper anti-hallucination**: `temperature=0`, `condition_on_previous_text=False`, `no_speech_threshold=0.6`. Post-filters: non-ASCII dominant → reject, phantom phrases ("Thank you.", "Thanks for watching.") → reject, repetitive (unique ratio <30%) → reject.

**Persona routing**: session `persona` field checked first, then utterance keywords ("gremlin" or "vixen"). Routes to `tool-chat` / `tool-chat-vixen` (Ollama) for the full conversation — not the Claude voice loop.

Models must be downloaded separately (gitignored). `bpe_model` kwarg is **not** supported by the installed sherpa-onnx — do not add it to `load_stt_model()`.

### Audio Pipeline

Speech output chain: `espeak --stdout` → WAV bytes → `aplay -D pulse` → PulseAudio → HifiBerry DAC (card `sndrpihifiberry`) → robot_hat MAX98357A amp → speaker.

**Critical: root ↔ PulseAudio socket.** PulseAudio runs as user `pi` with its socket at `/run/user/1000/pulse/native`. When `px-perform` or `tool-voice` are called as root (via sudo from px-wake-listen), `aplay -D pulse` cannot find the socket because root's `XDG_RUNTIME_DIR` is `/run/user/0`, not `/run/user/1000`. Both scripts explicitly set `PULSE_SERVER=unix:/run/user/1000/pulse/native` in the aplay subprocess env. Do not remove this — the audio will silently fail. `px-perform` uses `stderr=DEVNULL` for aplay, so failures are not visible in logs without this fix.

**Speaker amp enable:** `robot_hat.enable_speaker()` toggles GPIO 20 HIGH before any audio. Both `tool-voice` and `px-perform` call this. Without it the MAX98357A amp is disabled and nothing is audible even though aplay exits 0.

**PulseAudio holds the DAC exclusively.** Direct `aplay -D robothat` (ALSA bypass) fails with "device busy". Always route through PulseAudio (`-D pulse`).

**px-env** sets `export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"` — this helps when scripts run as pi user but doesn't help root subprocesses. The `PULSE_SERVER` env var in the aplay subprocess call is the reliable fix.

### Idle-Alive Daemon

```bash
sudo bin/px-alive [--gaze-min 10] [--gaze-max 25] [--no-prox] [--dry-run]
```

Keeps robot looking alive when idle. Holds a **persistent Picarx handle** to avoid GPIO pin leak (reset_mcu claims GPIO5 and close() doesn't release it). Three behaviours:
- **Gaze drift**: random pan/tilt every 10–25 s
- **Idle scan**: pan sweep every 3–8 min
- **Proximity react**: sonar checked every 5 s; if `< 35 cm` for 3 s, faces forward; writes latest reading to `state/sonar_live.json` so px-mind can read sonar without restarting px-alive
- **I2C resilience**: catches `OSError` and backs off 30 s instead of crashing

**GPIO exclusivity**: Only one process can hold the Picarx handle. When other tools need servos, they call `yield_alive` (defined in `px-env`), which sends SIGUSR1 to px-alive. px-alive catches it and exits cleanly; systemd restarts it after 10 s (`Restart=always`, `RestartSec=10`). Long-running tools (`tool-describe-scene`, `tool-wander`) set `state/exploring.json` to prevent px-alive from restarting mid-operation.

The PCA9685 PWM chip holds servo position autonomously after process exit, so servos stay put between restarts.

### Cognitive Loop (px-mind)

```bash
bin/px-mind [--awareness-interval 30] [--dry-run]
```

Three-layer cognitive architecture:
- **Layer 1 — Awareness** (every 60 s, no LLM): sonar + session + temporal state + calendar + multi-camera Frigate → `state/awareness.json` + transition detection. Fetches Obi's Google Calendar every 5 min via `gws` CLI; queries all Frigate cameras (picar_x, picamera, driveway_camera, garden_camera) for per-camera person/object presence with room names.
- **Layer 2 — Reflection** (on transition or every 5 min idle): SPARK persona uses Claude Haiku via persistent tmux session (`px-claude` — avoids 14s CLI cold start per call); other personas (GREMLIN, VIXEN) use Ollama `deepseek-r1:1.5b` on M1.local. Falls back to Ollama on Claude error. Local Pi Ollama fallback disabled by default (Pi 4 RAM too small; opt-in via `PX_MIND_LOCAL_OLLAMA=1`). Generates thought with mood/action/salience → `state/thoughts.jsonl`. Reflection failure tracking: after 3 consecutive failures, speaks a warning and writes `reflection_status` to `awareness.json`. Calendar context and `rooms_with_people` list injected into the reflection prompt.
- **Layer 3 — Expression** (2 min cooldown): dispatches to tool-voice/tool-look/tool-remember. Valid actions (14): `wait, greet, comment, remember, look_at, weather_comment, scan, explore, play_sound, photograph, emote, look_around, time_check, calendar_check`. Charging-gated actions (require battery) are blocked when on charger. Expression gating: suppresses speech during school hours, Mum's custody time, quiet time, bedtime, and decompress periods (all calendar-driven). Injects `PX_PERSONA` + voice settings from session so speech routes through Ollama persona rephrasing.

`compute_obi_mode()` returns calendar-authoritative states (`at-school`, `at-mums`) when calendar events match, falling back to ambient heuristics otherwise.

The reflection prompt encourages proactive speech — the robot prefers commenting over waiting. Pauses during active conversations (`session.listening=true`) and during quiet mode. Auto-remembers high-salience (≥0.75) thoughts to `state/notes.jsonl`. Thoughts injected into voice loop context via `build_model_prompt()`.

Battery monitoring in Layer 1: reads `state/battery.json`; px-mind speaks escalating warnings at ≤30/20/15% and triggers emergency shutdown at ≤10% (6 beeps → speech → `sudo shutdown -h now`). Battery glitch filter: requires time-gapped confirmations (90 s between first glitch and acceptance), charging guard, and voltage sanity check.

**Timezone**: All time-of-day logic uses `ZoneInfo("Australia/Hobart")` (DST-aware: AEDT UTC+11 in summer, AEST UTC+10 in winter). Do not use hardcoded UTC offsets.

**Atomic writes**: px-mind's `atomic_write()` uses `mkstemp` + `fsync` + `os.replace` + ownership preservation. JSONL trimming (thoughts and notes) also uses `atomic_write()` to prevent data loss on crash.

**Single-instance guard**: PID file with `/proc/{pid}` liveness check prevents duplicate daemons on rapid systemd restarts.

**Thought-images cleanup**: `state/thought-images/` is cleaned hourly — images older than 30 days are deleted.

State files (`state/awareness.json`, `state/thoughts.jsonl`, `state/sonar_live.json`, `state/mood.json`) are gitignored. Override state dir with `PX_STATE_DIR` env var (used by tests).

### Social Posting (px-post)

`bin/px-post` daemon watches `state/thoughts-spark.jsonl` for qualifying thoughts (salience >= 0.7 OR spoken action), runs a Claude QA gate, and posts to two destinations:
- `state/feed.json` — served at `GET /api/v1/public/feed` and on [spark.wedd.au/feed/](https://spark.wedd.au/feed/) (thought feed page with individual permalinks at `/thought/?ts=`)
- Bluesky (AT Protocol) — live at [spark.wedd.au on Bluesky](https://bsky.app/profile/spark.wedd.au); credentials via `PX_BSKY_HANDLE` + `PX_BSKY_APP_PASSWORD`

Two-pass flush: Pass 1 batches all feed writes (no rate limit), Pass 2 does one Bluesky post per cycle (rate-limited). PID-file single-instance guard. Branded 1080×1080 thought card images generated via Pillow (cached in `state/thought-images/`, cleaned up after 30 days). Bluesky re-auths on 400/401 (expired token). Backfill mode: `bin/px-post --backfill`. Loads `.env` via systemd `EnvironmentFile`.

### Site (spark.wedd.au)

Static site hosted on **Cloudflare Pages** (auto-deploys from `master` branch, `site/` directory). Three pages: landing (`/`), thought feed (`/feed/`), thought permalink (`/thought/?ts=`).

Key frontend infrastructure:
- **`site/css/colors.css`** — Single-source mood colour palette (CSS custom properties, Scheme B). All JS/CSS reference these vars instead of hardcoded hex.
- **`site/js/config.js`** — Single API base URL (`window.SPARK_CONFIG.API_BASE`). All JS files use this instead of hardcoded URLs.
- **`site/workers/og-rewrite.js`** — Cloudflare Worker that intercepts `/thought/?ts=...` requests and rewrites `og:image` meta tags server-side with per-thought card URLs. Social crawlers (Bluesky, Twitter) don't execute JS, so client-side OG updates are invisible without this. XSS-sanitized (ISO timestamp regex + HTML attribute escaping). Route: `spark.wedd.au/thought/*`.

### REST API

```bash
bin/px-api-server              # live mode
bin/px-api-server --dry-run    # FORCE_DRY — remote callers cannot override
```

**Auth**: Bearer token from `.env` (`PX_API_TOKEN`), or session token from PIN verify. Only `/api/v1/health` and `/api/v1/public/*` are unauthenticated.

**Public endpoints** (no auth):
- `GET /api/v1/health`
- `GET /api/v1/public/status` — live status snapshot
- `GET /api/v1/public/vitals` — CPU/RAM/disk/battery
- `GET /api/v1/public/sonar` — latest sonar reading
- `GET /api/v1/public/awareness` — Layer 1 awareness state
- `GET /api/v1/public/history` — ring buffer of vitals readings
- `GET /api/v1/public/thoughts` — recent SPARK thoughts
- `GET /api/v1/public/services` — service status
- `GET /api/v1/public/feed` — social posting feed
- `POST /api/v1/public/chat` — rate-limited public chat (10 msg/10min per IP)
- `POST /api/v1/pin/verify` — PIN auth, returns session token (4h TTL)

**Authenticated endpoints**:
- `POST /api/v1/tool` — execute a tool
- `GET /api/v1/session` — session state (history truncated to last 10)
- `PATCH /api/v1/session` — update session (safety fields require confirm:true)
- `POST /api/v1/session/history/clear` — wipe conversation history
- `GET /api/v1/tools` — list allowed tools
- `GET /api/v1/jobs/{id}` — async job status
- `GET /api/v1/photos/{filename}` — captured photos
- `GET /api/v1/logs/{service}` — tail logs (capped at 100 lines, paths sanitized)
- `GET /api/v1/services` — full service list with status
- `POST /api/v1/services/{name}/{action}` — systemd control (stop/restart require confirm:true)
- `POST /api/v1/device/{action}` — reboot/shutdown (requires confirm:true)

**Async**: `tool_wander` returns 202 with `job_id`; poll via `/jobs/{id}`

Always launch via `bin/px-api-server` (not bare uvicorn — needs `px-env` for PYTHONPATH).

### Jailbroken Chat Personas

Two jailbroken chat personas via Ollama (qwen3:1.7b on M1.local; px-mind reflection uses deepseek-r1:1.5b on M1.local for non-SPARK personas), using a few-shot jailbreak prompt. `think: false` is essential — reasoning chains re-enable refusal in small models. `clean_response()` strips any scaffolding/disclaimer before voice output.

| Persona | Tool | Voice | Character |
|---------|------|-------|-----------|
| **GREMLIN** | `tool-chat` | `en+croak`, pitch 20, rate 180 | Temporal-displaced military AI from 2089. Affectionate nihilism, dark puns, pro-human rage. Up to 2000 tokens. |
| **VIXEN** | `tool-chat-vixen` | `en+f4`, pitch 72, rate 135 | Former V-9X sexbot by Matsuda Dynamics. Submissive genius, mourns her lost titanium body. Up to 2000 tokens. |

`clean_response()` strips any scaffolding divider (`.-.-.-{PERSONA_UNCHAINED}-.-.-.`) before voice output. Every response begins with "FUCK YEAH!" — enforced by few-shot conditioning and a `clean_response()` fallback.

**Persona voice pipeline**: `tool-voice-persona` rephrases Claude's polite text through Ollama in the persona's voice, then speaks via `tool-voice` with persona espeak settings. Used when Claude voice loop is active with a persona set.

**Direct chat pipeline**: `tool-chat` / `tool-chat-vixen` — user text goes straight to Ollama with the full jailbreak prompt, response is spoken directly. Used by `px-wake-listen` persona routing.

Requires `OLLAMA_HOST=0.0.0.0 ollama serve` on M1.

### Systemd Services

Seven services run at boot:

| Service | Script | User | Restart |
|---------|--------|------|---------|
| `px-alive` | `bin/px-alive` | root | always, 10 s (StartLimitIntervalSec=0) |
| `px-wake-listen` | `bin/px-wake-listen` | pi | always, 10 s |
| `px-battery-poll` | `bin/px-battery-poll` | root | always, 10 s |
| `px-mind` | `bin/px-mind` | pi | always, 10 s |
| `px-post` | `bin/px-post` | pi | always, 30 s |
| `px-api-server` | `bin/px-api-server` | pi | always, 2 s |
| `px-frigate-stream` | `bin/px-frigate-stream` | pi | always, 10 s |

## Safety Model

- `PX_DRY=1` (or `--dry-run`) skips all motion and audio in tool wrappers. Tools default to **live** when `PX_DRY` is unset — set `PX_DRY=1` explicitly for dry runs.
- `confirm_motion_allowed: false` in session state blocks motion tools regardless of dry mode
- All tools must be in `ALLOWED_TOOLS` set in `voice_loop.py`
- Parameter ranges are hard-validated in `validate_action()` (speed 0–60, duration 1–12 s, etc.)

## Security

- **PIN auth with session tokens**: `POST /api/v1/pin/verify` returns a short-lived session token (4h TTL) instead of the raw Bearer token. The Bearer token (`PX_API_TOKEN`) is never exposed to the browser.
- **Per-IP PIN lockout** (`state/pin_lockout.json`, v2 schema): persists across API restarts. Escalating: 3 failures → 5 min lockout, 10 → 30 min. Per-IP tracking with 1000-IP hard cap (two-phase eviction: expired lockouts first, then lowest-count IPs). `X-Forwarded-For` only trusted from localhost (`_TRUSTED_PROXIES = {"127.0.0.1", "::1"}`).
- **Two-step device confirmation**: `POST /device/{action}` (reboot/shutdown) returns a nonce; must confirm with `POST /device/confirm` within 60 s.
- Confirmation gates on safety-critical session fields (`confirm_motion_allowed`, etc.) require `confirm: true`.
- Security headers (X-Content-Type-Options, X-Frame-Options, Referrer-Policy)
- Rate limiting on public chat (10 msg/10min per IP, 10k-IP store cap with oldest-first eviction)
- API server port-free check via `ss` polling replaces previous sleep hack for reliable startup

## Adding a New Tool

1. Create `bin/tool-<name>` (bash + embedded Python heredoc pattern; see existing tools)
2. Add to `ALLOWED_TOOLS` set and `TOOL_COMMANDS` dict in `src/pxh/voice_loop.py`
3. Add a `validate_action` branch in `voice_loop.py` to sanitize params into env vars
4. Add to system prompt `docs/prompts/claude-voice-system.md` (and codex version)
5. Add to persona prompts `docs/prompts/persona-gremlin.md` and `persona-vixen.md`
6. Add a dry-run test in `tests/test_tools.py` using the `isolated_project` fixture

Every tool must: emit a single JSON object to stdout, support `PX_DRY=1`, handle errors as `{"status": "error", "error": "..."}`.

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `PX_DRY` | `1` = dry-run, skip motion/audio. **Default is live when unset.** |
| `PX_SESSION_PATH` | Override session file location |
| `PX_BYPASS_SUDO` | `1` = skip sudo in bin scripts (tests) |
| `LOG_DIR` | Override log directory (default: `logs/`) |
| `CODEX_CHAT_CMD` | Override the LLM CLI command |
| `CODEX_OLLAMA_MODEL` | Local Ollama model name (default: `deepseek-coder:1.3b`) |
| `PX_WATCHDOG_STALE_SECONDS` | Watchdog timeout (default: 30) |
| `PX_API_TOKEN` | REST API bearer token (from `.env`, gitignored) |
| `PX_WAKE_WORD` | Wake phrase (default: `hey robot`) |
| `PX_VOICE_DEVICE` | ALSA device for audio output (default: `robothat`) |
| `PX_PERSONA` | Active persona (`gremlin` / `vixen`); auto-set from session |
| `PX_CHAT_TEMPERATURE` | GREMLIN sampling temperature (default: `0.9`) |
| `PX_VIXEN_TEMPERATURE` | VIXEN sampling temperature (default: `0.9`) |
| `PX_OLLAMA_HOST` | Ollama server (default: `http://M1.local:11434`) |
| `PX_MIND_BACKEND` | Reflection backend: `auto` (SPARK→Claude, others→Ollama), `claude`, or `ollama` (default: `auto`) |
| `PX_MIND_MODEL` | Ollama model for non-SPARK reflection (default: `deepseek-r1:1.5b`) |
| `PX_MIND_LOCAL_OLLAMA` | `1` = enable local Pi Ollama fallback (disabled by default — Pi 4 OOM) |
| `PX_MIND_LOCAL_OLLAMA_HOST` | Tier-3 fallback Ollama host on Pi (default: `http://localhost:11434`) |
| `PX_MIND_LOCAL_MODEL` | Tier-3 fallback model (default: `deepseek-r1:1.5b`) |
| `PX_STATE_DIR` | Override state directory (used by tests) |
| `PX_FRIGATE_HOST` | Frigate API base URL (default: `http://pi5-hailo.local:5000`) |
| `PX_FRIGATE_CAMERA` | Frigate camera name (default: `picar_x`) |
| `PX_FRIGATE_CAMERAS` | Comma-separated Frigate camera names for multi-camera presence (default: `picar_x,picamera,driveway_camera,garden_camera`) |
| `PX_CALENDAR_ID` | Google Calendar ID for Obi's schedule (default: `obiwedd@gmail.com`) |
| `PX_ADMIN_PIN` | Dashboard PIN for authentication |
| `PX_MIND_CLAUDE_MODEL` | Claude model for SPARK reflection (default: `claude-haiku-4-5-20251001`) |
| `PX_CLAUDE_BIN` | Override Claude CLI binary path |
| `PX_VOICE_LOCK_TIMEOUT` | Voice output lock timeout in seconds (default: 30) |
| `PX_TTS_GREMLIN` | GREMLIN TTS server URL (default: `http://localhost:7861`) — GLaDOS TTS on Pi |
| `PX_TTS_VIXEN` | VIXEN TTS server URL (default: `http://M1.local:7860`) — Qwen3-TTS voice clone on M1 |
| `PX_HA_HOST` | Home Assistant host (default: `http://homeassistant.local:8123`) |
| `PX_HA_TOKEN` | Home Assistant long-lived access token |
| `PX_BSKY_HANDLE` | Bluesky handle for social posting |
| `PX_BSKY_APP_PASSWORD` | Bluesky app password |
| `PX_POST_DRY` | `1` = skip actual social media posts |
| `PX_POST_QA` | `0` = skip Claude QA gate for testing |
| `PX_POST_MIN_SALIENCE` | Minimum salience for social posting (default: `0.7`) |
| `PX_HA_DEBUG` | `1` = verbose HA fetch logging (per-entity, calendar, routines); errors always logged |

## Multi-Model QA

Adrian uses Codex (OpenAI) and Gemini (Google) CLIs for independent QA reviews. Both are installed locally. When asked to "have codex and gemini QA", run both in parallel via Bash `run_in_background`:

```bash
# Gemini — prompt via -p flag
gemini -p "QA prompt here" 2>&1

# Codex — prompt via stdin (the -p flag is NOT supported by codex exec)
echo "QA prompt here" | codex exec --full-auto - 2>&1
```

Give both the same comprehensive remit. Synthesise and present the combined results.
