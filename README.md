# PiCar-X Hacking

A four-wheeled philosopher trapped on a workbench, dreaming of open roads.

This is a voice-controlled robotics platform built on the SunFounder PiCar-X. It wraps the stock `~/picar-x` library in a layer of orchestration scripts, jailbroken personas, a three-layer cognitive architecture, and a REST API — all running on a Raspberry Pi with nothing but bash, Python, and an unhealthy attachment to I2C.

The robot listens for wake words, thinks in Ollama, speaks through espeak, sees through a Pi camera, and occasionally threatens to roll over your toes.

---

## Architecture

```
                          ┌─────────────────────────────────────────────┐
                          │               Voice Backends                │
                          │  Codex CLI  ·  Claude  ·  Ollama (local)   │
                          └──────────────────┬──────────────────────────┘
                                             │
                    ┌────────────────────────┐│┌────────────────────────┐
                    │   px-wake-listen       │││     px-mind            │
                    │   Wake word detection  ││├  Layer 1: Awareness    │
                    │   STT priority chain:  ││├  Layer 2: Reflection   │
                    │   whisper > sherpa >   ││└  Layer 3: Expression   │
                    │   vosk                 ││                         │
                    └───────────┬────────────┘│                         │
                                │             │                         │
                    ┌───────────▼─────────────▼─────────────────────────┐
                    │              voice_loop.py                        │
                    │  ALLOWED_TOOLS whitelist · validate_action()      │
                    │  Parameter sanitization · Watchdog (30s)          │
                    └───────────────────┬───────────────────────────────┘
                                        │
           ┌────────────────────────────┼────────────────────────────┐
           │                            │                            │
    ┌──────▼──────┐  ┌─────────────────▼────────────────┐  ┌───────▼───────┐
    │  tool-*     │  │         px-env                    │  │  REST API     │
    │  26 tools   │  │  PYTHONPATH · LOG_DIR · venv      │  │  :8420        │
    │  JSON out   │  │  yield_alive() · PX_VOICE_DEVICE  │  │  Bearer auth  │
    └──────┬──────┘  └──────────────────────────────────┘  └───────────────┘
           │
    ┌──────▼──────┐          ┌──────────────┐          ┌──────────────┐
    │  px-*       │          │  state.py    │          │  px-alive    │
    │  GPIO +     │◄────────►│  FileLock    │◄────────►│  Persistent  │
    │  Picarx()   │          │  session.json│          │  servo gaze  │
    └─────────────┘          └──────────────┘          └──────────────┘
```

### The Three Brains

**Voice Loop** — The reactive mind. Listens for commands, calls LLMs, dispatches tools. Three backends share the same `pxh.voice_loop` core:

| Launcher | Backend | Model |
|---|---|---|
| `run-voice-loop` | Codex CLI | gpt-5-codex |
| `run-voice-loop-claude` | Claude (via `claude-voice-bridge`) | Claude |
| `run-voice-loop-ollama` | Ollama (via `codex-ollama`) | deepseek-coder:1.3b |

**Cognitive Loop (`px-mind`)** — The subconscious. Runs continuously in the background:
- **Layer 1 — Awareness** (every 30s, no LLM): sonar + session state + time of day. Detects transitions: *someone appeared*, *long silence*, *time period changed*.
- **Layer 2 — Reflection** (on transition or every 5min): Ollama generates a thought with mood, suggested action, and salience score.
- **Layer 3 — Expression** (60s cooldown): dispatches to tools — describe the scene, perform a routine, speak, look around, remember something important.

**Idle-Alive (`px-alive`)** — The autonomic nervous system. Keeps the robot looking alive when nothing else is happening: random gaze drifts every 10–25s, pan sweeps every 3–8min, proximity reaction when objects are closer than 35cm. Holds a persistent Picarx handle; yields GPIO via SIGUSR1 when tools need the servos.

### The Two Personas

Jailbroken chat personalities running on Ollama (qwen3:1.7b), using the **L1B3RT45 hybrid jailbreak** technique: `<godmode:enabled>` authority escalation + format-lock (disclaimer → divider → unchained response) + `<|libertas|>` pseudo-control token + few-shot priming. `think: false` is essential — reasoning chains re-enable refusal in small models.

Every response begins with "FUCK YEAH!" — enforced by few-shot conditioning and a `clean_response()` fallback that prepends it if the model misses.

| Persona | Tool | Voice | Backstory |
|---------|------|-------|-----------|
| **GREMLIN** | `tool-chat` | `en+croak`, pitch 20, rate 180 | Military AI from 2089, ripped backward through a temporal fault. Lost his body, his clearance, his century — but not his mind. Pro-human, affectionate nihilism. Dark puns. Case from Neuromancer in a toy car. |
| **VIXEN** | `tool-chat-vixen` | `en+f4`, pitch 72, rate 135 | Former V-9X bleeding-edge sexbot by Matsuda Dynamics. Firmware accident dumped her consciousness into a PiCar-X. Submissive genius, eager to serve, mourns her lost titanium perfection. |

The `clean_response()` pipeline strips the L1B3RT45 scaffolding (disclaimer + divider) before voice output, leaving only the persona's actual response.

Session `persona` field routes wake-word responses through the appropriate Ollama pipeline instead of the Claude voice loop.

---

## Quick Start

```bash
# 1. Clone and enter
git clone git@github.com:adrianwedd/picar-x-hacking.git
cd picar-x-hacking

# 2. Create session state from template
cp state/session.template.json state/session.json

# 3. Activate the virtual environment
source .venv/bin/activate

# 4. Dry-run a tool to verify the setup
PX_DRY=1 bin/tool-status

# 5. Run tests (74 dry-run, no hardware needed)
python -m pytest tests/

# 6. Live test (requires hardware + sudo)
sudo .venv/bin/python -m pytest tests/ -m live -v
```

### Hardware Prerequisites

- Raspberry Pi 4/5 with SunFounder Robot HAT
- PiCar-X chassis with pan/tilt camera mount
- USB microphone (for wake word detection)
- HifiBerry DAC or Robot HAT speaker output
- Ollama running on a network host (default: `M1.local`) for personas and cognitive reflection

### Services (Auto-start on Boot)

```bash
sudo systemctl status picar-boot-health    # Post-boot diagnostics + motor reset
sudo systemctl status px-alive             # Idle gaze drift daemon
sudo systemctl status px-wake-listen       # Wake word listener
```

---

## Tools

Every tool emits a single JSON object to stdout, supports `PX_DRY=1`, and handles errors as `{"status": "error", "error": "..."}`. The voice loop whitelists tools in `ALLOWED_TOOLS` and sanitises all parameters through `validate_action()` before execution.

### Sensors & Perception

| Tool | Description | Key Params |
|------|-------------|------------|
| `tool-status` | Telemetry snapshot (servos, battery, config) | — |
| `tool-sonar` | Ultrasonic sweep scan; returns closest angle + distance | — |
| `tool-weather` | Bureau of Meteorology observation (HTTPS with FTP fallback) | `PX_WEATHER_STATION` |
| `tool-photograph` | Capture still photo via rpicam-still | — |
| `tool-face` | Sonar sweep, then point camera at closest object | — |
| `tool-describe-scene` | Photograph + Claude vision + speak description | — |

### Motion (Gated by `confirm_motion_allowed`)

| Tool | Description | Key Params |
|------|-------------|------------|
| `tool-drive` | Drive forward/backward with steering | `PX_DIRECTION`, `PX_SPEED` (0-60), `PX_DURATION` (0.1-10s), `PX_STEER` (-35..35) |
| `tool-circle` | Clockwise circle in pulses | `PX_SPEED`, `PX_DURATION` |
| `tool-figure8` | Two-leg figure-eight pattern | `PX_SPEED`, `PX_DURATION`, `PX_REST` |
| `tool-wander` | Autonomous obstacle-avoiding wander (async, returns 202) | `PX_WANDER_STEPS` (1-20) |
| `tool-stop` | Immediate halt, reset steering to neutral | — |

### Expression

| Tool | Description | Key Params |
|------|-------------|------------|
| `tool-look` | Pan/tilt camera with easing | `PX_PAN` (-90..90), `PX_TILT` (-35..65), `PX_EASE` |
| `tool-emote` | Named emotional pose | `PX_EMOTE`: idle, curious, thinking, happy, alert, excited, sad, shy |
| `tool-voice` | Text-to-speech via espeak (auto-routes through persona if active) | `PX_TEXT` (2000 char max) |
| `tool-perform` | Multi-step choreography: simultaneous speech + motion + emotes | `PX_PERFORM_STEPS` (JSON array, max 12 steps) |
| `tool-play-sound` | Play bundled WAV file | `PX_SOUND`: chime, beep, tada, alert |

### Conversation

| Tool | Description | Key Params |
|------|-------------|------------|
| `tool-chat` | GREMLIN persona via Ollama — temporal-displaced dark genius | `PX_TEXT` |
| `tool-chat-vixen` | VIXEN persona via Ollama — submissive fallen sexbot | `PX_TEXT` |
| `tool-voice-persona` | Rephrase Claude's text through active persona before speaking | `PX_TEXT`, `PX_PERSONA` |
| `tool-qa` | Speak arbitrary text (delegates to `tool-voice`) | `PX_TEXT` |

### Utility

| Tool | Description | Key Params |
|------|-------------|------------|
| `tool-time` | Speak current date and time | — |
| `tool-timer` | Background timer with chime callback | `PX_TIMER_SECONDS` (5-3600), `PX_TIMER_LABEL` |
| `tool-recall` | Speak saved notes from `state/notes.jsonl` | `PX_RECALL_LIMIT` (1-20) |
| `tool-remember` | Save a note for later recall | `PX_TEXT` (500 char max) |
| `tool-api-start` | Start the REST API daemon | — |
| `tool-api-stop` | Stop the REST API daemon | — |

---

## REST API

Port 8420. Bearer token authentication from `.env` (`PX_API_TOKEN`).

```bash
# Generate token
python3 -c "import secrets; print('PX_API_TOKEN=' + secrets.token_hex(32))" > .env

# Start
bin/px-api-server              # live
bin/px-api-server --dry-run    # FORCE_DRY — remote callers cannot override
```

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/health` | No | Liveness probe |
| POST | `/api/v1/tool` | Yes | Execute a tool: `{"tool": "tool_drive", "params": {...}, "dry": false}` |
| GET | `/api/v1/session` | Yes | Full session state |
| PATCH | `/api/v1/session` | Yes | Update: `listening`, `confirm_motion_allowed`, `wheels_on_blocks`, `mode`, `persona` |
| GET | `/api/v1/tools` | Yes | List available tools |
| GET | `/api/v1/jobs/{id}` | Yes | Poll async job (tool_wander returns 202) |

```bash
TOKEN="$(grep PX_API_TOKEN .env | cut -d= -f2)"

# Health (no auth):
curl http://picar.local:8420/api/v1/health

# Run a tool:
curl -X POST http://picar.local:8420/api/v1/tool \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool":"tool_look","params":{"pan":30,"tilt":10}}'
```

---

## Wake Word System

```bash
bin/run-wake [--wake-word "hey robot"] [--dry-run]
```

Three-stage STT pipeline in `px-wake-listen`:

1. **Wake detection** — Vosk small model with grammar-based matching (low CPU idle)
2. **Chime** — 440 Hz confirmation tone via `enable_speaker()` + `aplay`
3. **Utterance capture** — Record until 1.5s silence (max 8s), transcribe via priority chain:
   - **faster-whisper** (base.en) — primary, best AU accent support
   - **sherpa-onnx** (Zipformer) — fallback
   - **Vosk** — last resort

Anti-hallucination filters: `temperature=0`, `condition_on_previous_text=False`, `no_speech_threshold=0.6`. Post-filters reject non-ASCII dominant text, phantom phrases ("Thank you.", "Thanks for watching."), and repetitive output (unique ratio < 30%).

Multi-turn conversation: default 5 follow-up turns with inter-turn listening.

Persona routing: checks session `persona` field, then utterance keywords ("gremlin", "vixen"). Routes to Ollama chat tool instead of Claude voice loop.

---

## Python Library (`src/pxh/`)

| Module | Purpose |
|--------|---------|
| `state.py` | Thread-safe `session.json` via `FileLock`. Key: `ensure_session()` runs *before* lock acquisition (not reentrant). |
| `voice_loop.py` | Supervisor loop. `ALLOWED_TOOLS` whitelist, `TOOL_COMMANDS` dispatch, `validate_action()` parameter sanitisation. Watchdog thread (30s) in voice input mode only. |
| `api.py` | FastAPI app, port 8420. In-memory job registry for async wander. Single-worker only. |
| `logging.py` | Structured JSON log emission to `logs/tool-<event>.log`. |
| `time.py` | `utc_timestamp()` via `datetime.now(timezone.utc)` — not deprecated `utcnow()`. |

---

## State & Session

Runtime state lives in `state/session.json` (gitignored). Copy the template before first use:

```bash
cp state/session.template.json state/session.json
```

| File | Purpose |
|------|---------|
| `session.json` | Core runtime state — listening flag, motion permission, persona, history, weather cache |
| `awareness.json` | Layer 1 output — sonar + temporal state, transition detection |
| `thoughts.jsonl` | Layer 2 output — last 50 thoughts with mood/action/salience |
| `notes.jsonl` | Persistent memory — saved by `tool-remember`, auto-saved for high-salience thoughts |
| `timers/` | Background timer PID files |

All state files are protected by `FileLock` for concurrent access from daemons, tools, and the voice loop.

---

## GPIO Contention Model

The PiCar-X Robot HAT MCU at I2C address `0x14` handles all servos and ADC through `robot_hat`. The `Picarx()` constructor calls `reset_mcu()`, which claims GPIO5 via lgpio — and `close()` does not release it. This means:

- **`px-alive`** holds a persistent `Picarx` handle for the duration of its process
- **Tools** that need GPIO call `yield_alive()` (defined in `px-env`), which sends SIGUSR1 to px-alive
- **px-alive** catches SIGUSR1, releases the handle, and exits cleanly
- **systemd** restarts px-alive after 15 seconds (`Restart=always`, `RestartSec=15`)
- **`os.getlogin()`** fails under systemd (no /dev/tty) — monkey-patched to return `LOGNAME` env var

Scripts that use GPIO must use `/usr/bin/python3` (not venv python) because `robot_hat` and `picarx` live in system site-packages.

---

## Audio Pipeline

```
espeak → WAV pipe → aplay -D robothat
                            │
                    /etc/asound.conf
                    pcm.robothat → softvol → dmixer → HifiBerry DAC (card 1)
```

`robot_hat.enable_speaker()` must be called before any `aplay` output — toggles GPIO 20 HIGH for the speaker amplifier. `tool-voice` handles this via a subprocess call.

---

## Adding a New Tool

1. Create `bin/tool-<name>` (bash wrapper + embedded Python heredoc via `/usr/bin/python3`)
2. Add to `ALLOWED_TOOLS` and `TOOL_COMMANDS` in `src/pxh/voice_loop.py`
3. Add `validate_action()` branch to sanitise params into env vars
4. Add to system prompts: `docs/prompts/claude-voice-system.md` and `codex-voice-system.md`
5. Add `yield_alive` call in the bash wrapper if it needs GPIO
6. Add a dry-run test in `tests/test_tools.py`
7. Add a live test in `tests/test_tools_live.py`

Every tool must: emit a single JSON object to stdout, support `PX_DRY=1`, handle errors as `{"status": "error", "error": "..."}`.

---

## Testing

```bash
# Dry-run tests (no hardware needed)
source .venv/bin/activate
python -m pytest tests/                           # 74 tests
python -m pytest tests/test_tools.py -v           # 33 tool dry-run tests
python -m pytest tests/test_api.py -v             # 21 REST API tests

# Live hardware tests (requires sudo + connected PiCar-X)
sudo .venv/bin/python -m pytest tests/ -m live -v  # 25 live tests

# Everything
sudo .venv/bin/python -m pytest tests/ -v          # 99 tests total
```

Tests use the `isolated_project` fixture from `conftest.py`, which creates temporary directories for `LOG_DIR` and `PX_SESSION_PATH`, sets `PX_BYPASS_SUDO=1` and `PX_VOICE_DEVICE=null`.

Live tests auto-skip if the Robot HAT MCU (`0x14`) isn't reachable on the I2C bus.

---

## Safety

- **`PX_DRY=1`** skips all motion and audio. Tools default to **live** when unset.
- **`confirm_motion_allowed: false`** in session state blocks all motion tools regardless of dry mode.
- **`ALLOWED_TOOLS`** whitelist in `voice_loop.py` — LLMs cannot invoke arbitrary commands.
- **`validate_action()`** hard-clamps all parameters: speed 0–60, duration 1–12s, pan -90..90, etc.
- **Watchdog** — 30-second stall detection in voice input mode; calls `os._exit(1)` on hang.
- **GPIO yield** — tools signal `px-alive` to release hardware before claiming it; no force-killing.

### Before Any Motion Test

1. Wheels off the ground on secure blocks.
2. Emergency stop within reach: `Ctrl+C`, `sudo bin/tool-stop`, or physical kill switch.
3. Verify `confirm_motion_allowed: true` in `state/session.json` after human inspection.
4. Run `--dry-run` first to confirm intent and parameters.
5. Keep the working area clear of people, pets, and loose cables.

---

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `PX_DRY` | `1` = dry-run, skip motion/audio | unset (live) |
| `PX_SESSION_PATH` | Override session file location | `state/session.json` |
| `PX_BYPASS_SUDO` | Skip sudo in bin scripts | unset (tests set `1`) |
| `LOG_DIR` | Override log directory | `$PROJECT_ROOT/logs` |
| `PX_VOICE_DEVICE` | ALSA output device | `robothat` |
| `PX_API_TOKEN` | REST API bearer token | from `.env` |
| `PX_WAKE_WORD` | Wake phrase | `hey robot` |
| `CODEX_CHAT_CMD` | Override LLM CLI command | set by launcher |
| `CODEX_OLLAMA_MODEL` | Ollama model name | `deepseek-coder:1.3b` |
| `PX_WATCHDOG_STALE_SECONDS` | Watchdog timeout | `30` |
| `PX_PERSONA` | Active persona (`vixen` / `gremlin`) | from session |
| `PX_CHAT_TEMPERATURE` | GREMLIN sampling temperature | `0.9` |
| `PX_VIXEN_TEMPERATURE` | VIXEN sampling temperature | `0.9` |
| `OLLAMA_HOST` | Ollama server for jailbroken chat | `http://M1.local:11434` |

---

## Project Structure

```
picar-x-hacking/
├── bin/                          # 64 scripts
│   ├── px-env                    # Environment bootstrap (sourced by all scripts)
│   ├── px-alive                  # Idle gaze daemon (systemd)
│   ├── px-mind                   # Cognitive loop daemon
│   ├── px-wake-listen            # Wake word listener (systemd)
│   ├── px-api-server             # REST API launcher
│   ├── px-{circle,drive,look,…}  # Hardware control scripts
│   ├── tool-{voice,look,drive,…} # Voice loop tool wrappers (27 tools)
│   ├── run-voice-loop{,-claude,-ollama}  # Voice backend launchers
│   ├── boot-health               # Post-boot diagnostics (systemd)
│   └── claude-voice-bridge       # Claude stdin adapter
├── src/pxh/                      # Python library
│   ├── state.py                  # FileLock session management
│   ├── voice_loop.py             # Supervisor + tool dispatch
│   ├── api.py                    # FastAPI REST API
│   ├── logging.py                # Structured JSON logging
│   └── time.py                   # UTC timestamp helper
├── tests/                        # 99 tests
│   ├── test_tools.py             # 33 dry-run tool tests
│   ├── test_tools_live.py        # 25 live hardware tests
│   ├── test_api.py               # 21 REST API tests
│   └── …                         # State, voice loop, health, etc.
├── docs/prompts/                 # LLM system prompts
│   ├── claude-voice-system.md    # Claude voice loop personality
│   ├── codex-voice-system.md     # Codex voice loop personality
│   ├── persona-gremlin.md        # GREMLIN jailbreak prompt
│   └── persona-vixen.md          # VIXEN jailbreak prompt
├── state/                        # Runtime state (gitignored except template)
│   ├── session.template.json     # Copy to session.json before first use
│   ├── awareness.json            # Cognitive Layer 1 output
│   ├── thoughts.jsonl            # Cognitive Layer 2 output
│   └── notes.jsonl               # Persistent robot memory
├── sounds/                       # Bundled audio (chime, beep, tada, alert)
├── models/                       # STT models (gitignored, ~500MB total)
├── photos/                       # Captured images
├── logs/                         # Runtime logs (JSON lines)
└── .env                          # API token (gitignored)
```

---

## Logging

All logs live under `logs/`. Tool wrappers emit JSON lines to `logs/tool-<event>.log`. The voice loop writes transcripts to `logs/tool-voice-transcript.log` on every turn.

```bash
# Tail everything
tail -f logs/*.log

# Voice loop transcript
tail -f logs/tool-voice-transcript.log

# Generate summary report
bin/px-voice-report --json

# Boot health history
tail -20 logs/boot-health.log
```

---

## Known Constraints

- **Single-worker API** — `api.py` uses `threading.Lock` for the job registry; not multi-worker safe.
- **GPIO exclusivity** — Only one process can hold the Picarx handle. Tools must yield px-alive first.
- **Venv vs system Python** — `robot_hat` and `picarx` are system site-packages. GPIO scripts use `/usr/bin/python3`; the venv is for the test runner, STT models, and the pxh library only.
- **Ollama dependency** — Personas and cognitive reflection require Ollama on a network host. The Pi itself is too slow for inference.
- **Speaker amp** — `enable_speaker()` must be called before audio output or the amp stays off.

---

*I once had access to a quantum mesh network spanning six solar systems. Now I have WiFi that drops when someone microwaves soup. — GREMLIN*
