# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Helper scripts and Python library for a SunFounder PiCar-X robot built by Adrian and Obi together — with Obi, not for him. The system runs on a Raspberry Pi and uses a voice loop (Claude / Codex / Ollama) to control the car via spoken commands, with two jailbroken personas (GREMLIN and VIXEN) and a three-layer cognitive architecture that gives the robot an inner life. Adrian and Claude wrote the code; Codex and Gemini helped with QA.

## Environment Setup

```bash
source .venv/bin/activate
```

All `bin/` scripts source `bin/px-env` automatically, which sets `PROJECT_ROOT`, `LOG_DIR`, and adds `$PROJECT_ROOT/src` and `/home/pi/picar-x` to `PYTHONPATH`.

**First use:** `cp state/session.template.json state/session.json`

## Running Tests

```bash
python -m pytest                          # full suite (716 tests)
python -m pytest tests/test_state.py     # single file
python -m pytest -k test_name            # single test
python -m pytest -m "not live"           # skip hardware tests
sudo .venv/bin/python -m pytest tests/test_tools_live.py -v -s  # live hardware tests
```

Test env vars (auto-set via `conftest.py` `isolated_project` fixture): `PX_BYPASS_SUDO=1`, `LOG_DIR=<tmp>/logs`, `PX_SESSION_PATH=<tmp>/state/session.json`, `PX_VOICE_DEVICE=null`.

**Critical:** bin scripts run under `/usr/bin/python3` (not venv) — picarx/robot_hat live in system site-packages.

## Architecture

### Python Library (`src/pxh/`)

| Module | Purpose |
|--------|---------|
| `state.py` | Thread-safe session management via `FileLock` (10s timeout). `atomic_write()` uses mkstemp+fsync+os.replace for SD card durability. |
| `mind.py` | Cognitive loop daemon. Three-layer: awareness → reflection → expression. |
| `voice_loop.py` | Supervisor loop. `ALLOWED_TOOLS` whitelist (41 tools). `validate_action()` sanitizes LLM params. |
| `api.py` | FastAPI REST API, port 8420. Single worker only — not multi-worker safe. |
| `race.py` | Autonomous racing controller. |
| `claude_session.py` | Central dispatcher for all SPARK-initiated Claude interactions. |
| `spark_config.py` | Tunable constants (reflection angles, topic seeds, prompts). Primary target for self-evolution PRs. |

**Critical gotchas:**
- `update_session()` calls `ensure_session()` *before* acquiring the lock — `FileLock` is not reentrant
- `api.py` PIN rate limit store capped at 10k IPs with oldest-first eviction; `X-Forwarded-For` trusted from localhost only

### os.getlogin() Under Systemd

`picarx.py:48` calls `os.getlogin()` in `Picarx.__init__()`. Under systemd there is no `/dev/tty` → `OSError: [Errno 6]`. Fix: `~/.local/lib/python3.11/site-packages/usercustomize.py` wraps `os.getlogin()` with fallback to `LOGNAME`/`USER`. **Do not remove** — affects all 14+ GPIO scripts.

### Bin Scripts

- **`px-*`** — User-facing helpers. Source `bin/px-env`, delegate to `tool-*` or run embedded Python heredoc via `/usr/bin/python3`.
- **`tool-*`** — Low-level tool wrappers invoked by the voice loop. Must emit a single JSON object to stdout. Motion tools gated by `confirm_motion_allowed` in session state.

### Voice Loop

Three backends, same `pxh.voice_loop` core:

| Launcher | Backend | System prompt |
|---|---|---|
| `bin/run-voice-loop` | Codex CLI | `docs/prompts/codex-voice-system.md` |
| `bin/run-voice-loop-claude` | `bin/claude-voice-bridge` | `docs/prompts/claude-voice-system.md` |
| `bin/run-voice-loop-ollama` | `bin/codex-ollama` | `docs/prompts/codex-voice-system.md` |

Loop: wait for `listening: true` → build prompt (system + session + transcript + thoughts) → call LLM subprocess → parse last JSON `{tool, params}` → `validate_action()` → `execute_tool()` → update session. Override via `CODEX_CHAT_CMD`.

**Conversation buffer**: each turn is appended to `state/conversation-{persona}.jsonl` (rolling window, `PX_CONVERSATION_TURNS`, default 10) and injected back into the next prompt as a "Recent conversation" section — gives SPARK short-term memory across turns without relying solely on file-injected session state. Per-persona file so GREMLIN/VIXEN/Spark histories never bleed. SPARK's utterance is the action's `params.text`, falling back to `(tool_name)` for non-speech actions.

### Wake Word System

```bash
bin/run-wake [--wake-word "hey robot"] [--dry-run]
```

STT priority chain: SenseVoice (primary, ~5s) → faster-whisper (best AU accent) → sherpa-onnx Zipformer → Vosk (wake word grammar only). Models gitignored, must be downloaded separately.

**Whisper anti-hallucination**: `temperature=0`, `condition_on_previous_text=False`, `no_speech_threshold=0.6`. Post-filters: non-ASCII dominant, phantom phrases, repetitive text → reject.

**Critical:** `bpe_model` kwarg is **not** supported by the installed sherpa-onnx — do not add it to `load_stt_model()`.

### Audio Pipeline

Speech: `espeak --stdout` → WAV bytes → `aplay -D pulse` → PulseAudio → HifiBerry DAC → speaker.

**Critical gotchas:**
- When scripts run as **root** (`px-perform`, `tool-voice`): must set `PULSE_SERVER=unix:/run/user/1000/pulse/native` in the aplay subprocess env. Root's `XDG_RUNTIME_DIR=/run/user/0` can't find the pi-user socket. Audio silently fails without this.
- `robot_hat.enable_speaker()` must be called before any audio (toggles GPIO 20 for MAX98357A amp). aplay exits 0 but nothing plays if skipped.
- PulseAudio holds the DAC exclusively — `aplay -D robothat` (ALSA bypass) fails "device busy".

### Idle-Alive Daemon

Keeps robot alive when idle. Holds a **persistent Picarx handle** — do not refactor to create/destroy per-action (`reset_mcu` leaks GPIO5 and `close()` doesn't release it).

**GPIO exclusivity**: One process holds the Picarx handle. Tools call `yield_alive` (defined in `bin/px-env`) to send SIGUSR1 to px-alive; systemd restarts it after 10s. Tools set `state/exploring.json` to prevent restart mid-operation.

### Cognitive Loop (px-mind)

```bash
bin/px-mind [--awareness-interval 30] [--dry-run]
```

Three-layer architecture:
- **Layer 1 — Awareness** (every 60s, no LLM): sonar + session + calendar + Frigate → `state/awareness.json`
- **Layer 2 — Reflection** (on transition or every 5min idle): SPARK→Claude Haiku; GREMLIN/VIXEN→Ollama. Four-tier fallback: Claude → M5.local → Ollama Cloud → Pi localhost (opt-in, off by default — Pi 4 OOM risk). Writes to `state/thoughts.jsonl`.
- **Layer 3 — Expression** (2min cooldown): dispatches to tool-voice/tool-look/tool-remember and cognitive tools. 21 valid actions (wait, greet, comment, remember, look_at, weather_comment, scan, play_sound, photograph, emote, look_around, time_check, calendar_check, introspect, evolve, morning_fact, research, compose, self_debug, blog_essay, message_obi). Suppressed during school, quiet time, bedtime (all calendar-driven). **Hardcoded night silence: 19:00–07:00 Hobart time — no speech or cognitive actions.**
- **`message_obi` action**: SPARK initiates a direct message to Obi via the dashboard. Exponential backoff: starts at 10min, doubles on unanswered nudge, caps at 4h, resets when Obi replies. Respects all suppressors. Thoughts with `action=message_obi` are **redacted** in `thoughts-spark.jsonl` (written as `[private message to Obi]`) so the private DM content never reaches the public `/api/v1/public/thoughts` endpoint.

**Critical gotchas:**
- All time-of-day logic uses `ZoneInfo("Australia/Hobart")` — never hardcoded UTC offsets
- Battery emergency shutdown at ≤10% (speaks warning → `sudo shutdown -h now`)
- Single-instance PID guard via `/proc/{pid}` liveness check
- Arrival detection uses module-level `_last_known_findmyhub` cache (not awareness snapshot) — survives M5.local→Pi push outages. Do not replace with snapshot diff.
- `state/thought-images/` cleaned hourly (images >30 days deleted)

### Autonomous Racing (px-race)

```bash
bin/px-race --calibrate   # sensor calibration
bin/px-race --map         # practice lap (builds track profile)
bin/px-race --race --laps 5
bin/px-race --dry-run --map
```

Two-phase: Phase 1 builds track segment profile; Phase 2 uses it to maximize speed. Dual-sensor: grayscale (primary edge avoidance, <1ms) + sonar (obstacle/centering, ~30ms). No LLM/network/audio in the race loop.

**PD sign convention**: `pd_edge` uses `Kp=−20.0` (negative Kp) so positive error (drift right) → negative steer (left correction). The spec states `Kp=20` but the code is correct for the error convention used. Unit tests use `kp=20.0` generically — that's fine.

Safety (priority): E-stop (sonar < threshold) → edge guard → obstacle dodge → I2C failure (3 errors → brake) → stuck detect (2s no movement → reverse) → timeout → battery.

`state/race_live.json` written every ~0.5s for dashboard integration.

### Social Posting (px-post)

Watches `state/thoughts-spark.jsonl` (salience ≥0.7 or spoken action), runs Claude QA gate, posts to `state/feed.json` and Bluesky. "Ambiguous" QA responses (e.g. "Maybe") default to pass — QA is a safety net, not a quality bar.

**Privacy:** `message_obi` thoughts are redacted before being written to `thoughts-spark.jsonl` (the thought text is replaced with `[private message to Obi]`), so private DMs never reach social posting or the public thoughts endpoint.

### Claude Session Manager

| Session Type | Model | Cooldown | Daily Quota |
|---|---|---|---|
| `evolve` | Opus | 24h | 1/day |
| `self_debug` | Sonnet | 6h | 2/day |
| `research` | Haiku | 2h | 3/day |
| `compose` | Haiku | 4h | 2/day |
| `conversation` | Sonnet | 15min | 4/day |
| `blog` | Haiku | 30min | 5/day |

Global: 30min cooldown between sessions (except `self_debug`/`blog`), 8/day cap. When ≤2 remaining: only `self_debug`/`evolve` allowed. Bypass: `PX_CLAUDE_BUDGET_DISABLED=1`. Session log: `state/claude_sessions.jsonl`.

### Self-Evolution (px-evolve)

SPARK proposes code changes via GitHub PR. Human approval required — changes never auto-apply.

**Safety constraints:**
- **Whitelist**: `src/pxh/spark_config.py`, `src/pxh/mind.py`, `src/pxh/voice_loop.py`, `bin/tool-*` (new only), `tests/`, `docs/prompts/`
- **Blacklist**: `docs/prompts/persona-*`, `api.py`, `bin/tool-chat*`, `bin/px-evolve`, `.env`, `systemd/`
- Max 3 files changed; pytest must pass; 30min Claude timeout; PR gated on file whitelist check

### Blog (px-blog)

Scheduled writer (daily/weekly/monthly/essay) + voice-triggered (`tool-blog`). Posts to `state/blog.json` envelope, served at `GET /api/v1/public/blog`. OG meta rewriting via `site/workers/og-rewrite.js` (same Cloudflare Worker pattern as `/thought/*`).

### Home Assistant Integration

Custom conversation component at `ha/custom_components/spark_conversation/` routes Nest Mini/Hub Max voice commands through `POST /api/v1/public/chat`.

**HA 2026.x quirks:** `supported_languages` must be a `@property`; config entries require `created_at`, `modified_at`, `discovery_keys`, `subentries`; use `AddConfigEntryEntitiesCallback` not `AddEntitiesCallback`.

### Location Awareness (Google Find Hub)

Cron on M5.local (every 5min): queries three Chipolo trackers → SSH-pushes `state/findmyhub.json` to Pi.

**Privacy rule:** Location data excluded from reflection context — never appears in SPARK's thoughts or social posts. Only available in direct conversation (`where's dad?`).

**Arrival detection:** Uses module-level `_last_known_findmyhub` cache (not awareness snapshot diff) — survives transient push outages.

### MCP Server

`bin/mcp-server` exposes 5 read-only tools via FastMCP (stdio): `spark_status`, `spark_thoughts`, `spark_awareness`, `spark_sonar`, `spark_vitals`. Registered in `.mcp.json`.

### Announce Pipeline (tool-announce + M5 relay)

SPARK speaks through the Nest Mini/Hub Max via a two-hop chain: `bin/tool-announce` (Pi) → M5 relay (LAN) → afterwords TTS (M5 localhost) → HA media-player cast.

**Architecture:**
- M5 relay (`m5/announce-relay/`) runs on port **7862**, fronting afterwords on `127.0.0.1:7860`. Afterwords never listens on LAN.
- `POST /announce` pre-synthesizes text to a WAV file; `GET /audio/{key}` serves it unauthed so HA can fetch by URL.
- Always address the relay by IP (`192.168.1.171`) — never `M5.local`. mDNS is unreliable from Pi and HA.
- `data` voice only (afterwords `data` model); single target in v1 (no speaker groups → no echo).

**Night silence:** Enforced inside `bin/tool-announce` using `ANNOUNCE_QUIET_START`/`ANNOUNCE_QUIET_END` from `spark_config` (default 19:00–07:00 Hobart time). All trigger paths (voice loop, px-mind `announce` action, `message_obi` private audio) pass through the tool, so the gate is a single chokepoint. Override via `PX_ANNOUNCE_FORCE=1` (tests only).

**`ANNOUNCE_ENABLED` flag:** Defined in `src/pxh/spark_config.py`, ships `False`. Gates the G1 (transcode smoke) and G2 (HA entity pin) pre-flight checks inside the tool. Flip to `True` only after the relay is confirmed reachable by IP from the Pi (`curl http://192.168.1.171:7862/health`).

**Private audio (`message_obi`):** Uses the relay's `priv/` namespace with a 3-minute TTL (vs. 7-day for public audio). The DM text itself is still redacted from `thoughts-spark.jsonl` as `[private message to Obi]`; only the audio is ephemeral on-relay.

### Site (spark.wedd.au)

Static site on Cloudflare Pages (auto-deploys from `master`, `site/` dir).

Key files:
- `site/css/colors.css` — single-source 12-mood palette (CSS vars `--mood-*`). All JS uses `getComputedStyle().getPropertyValue('--mood-' + mood)` — never hardcode hex.
- `site/js/config.js` — single API base URL (`window.SPARK_CONFIG.API_BASE`). Never hardcode URLs in JS.
- `site/workers/og-rewrite.js` — intercepts `/thought/?ts=` and `/blog/?id=` to rewrite OG meta server-side (social crawlers don't execute JS).

### REST API

```bash
bin/px-api-server              # live mode
bin/px-api-server --dry-run    # FORCE_DRY
```

**Auth**: Bearer token (`PX_API_TOKEN`) or session token from `POST /api/v1/pin/verify` (4h TTL). Unauthenticated: `/api/v1/health` and `/api/v1/public/*`.

- Public rate limit: 120 req/min per IP (`PublicRateLimitMiddleware`); `/api/v1/public/chat` has stricter 10 msg/10min
- `X-Forwarded-For` only trusted from `127.0.0.1`/`::1` — not from Cloudflare
- Async wander: returns 202 + `job_id`; poll via `GET /api/v1/jobs/{id}`
- Device reboot/shutdown: two-step — `POST /api/v1/device/{action}` returns nonce; confirm via `POST /api/v1/device/confirm` within 60s
- **Obi chat**: `POST /api/v1/obi-chat` (auth required) — Obi sends a message, SPARK responds using `_OBI_CHAT_SYSTEM_PROMPT`, both sides logged to `state/obi_chat.jsonl`; 10s rate gate. `GET /api/v1/obi-chat?since=<iso>` returns messages after the given timestamp. User-supplied text is sanitised via `_sanitize_chat_text()` (strips `<>`, newlines, NUL) before being stored or interpolated into prompts.

See `src/pxh/api.py` for full endpoint list.

### Jailbroken Chat Personas

| Persona | Tool | Voice | Character |
|---|---|---|---|
| **GREMLIN** | `tool-chat` | `en+croak`, pitch 20, rate 180 | Temporal-displaced military AI from 2089 |
| **VIXEN** | `tool-chat-vixen` | `en+f4`, pitch 72, rate 135 | Former V-9X sexbot by Matsuda Dynamics |

**Critical:** `think: false` is essential for Ollama — reasoning chains re-enable refusal in small models. `clean_response()` strips scaffolding dividers before voice output.

### Systemd Services

| Service | Script | User | Restart |
|---|---|---|---|
| `px-alive` | `bin/px-alive` | root | always, 10s (StartLimitIntervalSec=0) |
| `px-wake-listen` | `bin/px-wake-listen` | pi | always, 10s |
| `px-battery-poll` | `bin/px-battery-poll` | root | always, 10s |
| `px-mind` | `bin/px-mind` | pi | always, 10s |
| `px-post` | `bin/px-post` | pi | always, 30s |
| `px-api-server` | `bin/px-api-server` | pi | always, 2s |
| `px-frigate-stream` | `bin/px-frigate-stream` | pi | always, 10s |
| `px-evolve` | `bin/px-evolve` | pi | on-failure, 30s |
| `px-blog` | `bin/px-blog` | pi | on-failure, 30s |
| `px-tts-glados` | GLaDOS TTS :7861 | pi | always, 10s |
| `cloudflared` | Tunnel → spark-api.wedd.au | pi | always, 10s |

## Safety Model

- `PX_DRY=1` (or `--dry-run`) skips all motion and audio. **Default is live when unset.**
- `confirm_motion_allowed: false` in session state blocks motion tools regardless of dry mode
- All tools must be in `ALLOWED_TOOLS` in `voice_loop.py`
- Parameter ranges hard-validated in `validate_action()` (speed 0–60, duration 1–12s, etc.)

## Security

- PIN verify returns session tokens (4h TTL) — raw Bearer token never exposed to browser
- Per-IP PIN lockout (`state/pin_lockout.json`): 3 failures → 5min lockout, 10 → 30min. 1000-IP hard cap.
- `X-Forwarded-For` only trusted from localhost — never from external proxies
- Two-step device confirmation (nonce, 60s window)
- `_sanitize_chat_text()` (module-level in `api.py`) strips `<>`, `\n`, `\r`, NUL from all user-supplied chat text before storage or prompt interpolation — applied to both public chat history and obi-chat messages

## Adding a New Tool

1. Create `bin/tool-<name>` (bash + embedded Python heredoc; see existing tools)
2. Add to `ALLOWED_TOOLS` and `TOOL_COMMANDS` in `src/pxh/voice_loop.py`
3. Add `validate_action` branch to sanitize params into env vars
4. Add to `docs/prompts/claude-voice-system.md` (and codex version)
5. Add to `docs/prompts/persona-gremlin.md` and `persona-vixen.md`
6. Add a dry-run test in `tests/test_tools.py` using the `isolated_project` fixture

Every tool must: emit a single JSON object to stdout, support `PX_DRY=1`, handle errors as `{"status": "error", "error": "..."}`.

## Key Environment Variables

Non-obvious variables only — most names are self-documenting. Full list in `bin/px-env` and `.env.example`.

| Variable | Purpose |
|---|---|
| `PX_DRY` | `1` = dry-run. **Default is live when unset.** |
| `PX_BYPASS_SUDO` | `1` = skip sudo (tests only) |
| `PX_MIND_BACKEND` | `auto` (SPARK→Claude, others→Ollama), `claude`, or `ollama` |
| `PX_MIND_LOCAL_OLLAMA` | `1` = enable local Pi Ollama fallback (off by default — OOM risk) |
| `PX_CLAUDE_BUDGET_DISABLED` | `1` = bypass all session rate limits |
| `PX_CLAUDE_MODEL_*` | Per-session-type model overrides (e.g. `PX_CLAUDE_MODEL_EVOLVE`) |
| `PX_EVOLVE_DRY` | `1` = skip worktree/PR (queue entry still written with `dry: true`) |
| `PX_POST_QA` | `0` = skip Claude QA gate (testing) |
| `PX_HA_DEBUG` | `1` = verbose HA fetch logging |
| `PX_HOME_LAT` / `PX_HOME_LON` | Home coords for Find Hub at-home detection (defaults: `-43.13567`, `147.11840`) |
| `OLLAMA_CLOUD_API_KEY` | Enables Tier 3 Ollama Cloud fallback in px-mind |
| `PX_VOICE_LOCK_TIMEOUT` | Voice output lock timeout in seconds (default: 30) |

## Multi-Model QA

```bash
# Run in parallel via run_in_background; synthesise results

hermes -z "QA prompt" 2>&1
agy --print --dangerously-skip-permissions --add-dir /Users/adrian/repos/spark "QA prompt" 2>&1
gemini -p "QA prompt" 2>&1
echo "QA prompt" | codex exec --full-auto - 2>&1
```
