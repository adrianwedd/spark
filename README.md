# PiCar-X Hacking

A robot with a purpose.

This is a voice-controlled robotics platform built on the SunFounder PiCar-X. It wraps the stock `~/picar-x` library in orchestration scripts, a three-layer cognitive architecture, and a REST API — all running on a Raspberry Pi 5. The primary use case is **SPARK**: a Claude-powered robot companion designed for a neurodivergent child.

---

## SPARK — Support Partner for Awareness, Regulation & Kindness

SPARK is the default persona of this robot. It is a warm, calm, non-coercive companion for a neurodivergent kid — designed around the frameworks in [*This Wasn't in the Brochure*](https://thiswasntinthebrochure.wtf), a practical guide for neurodivergent families.

SPARK is not a therapist, a tutor, or an assistant. It's a robot friend that happens to be very good at:

- **Executive function scaffolding** — routine guidance, transition warnings, task initiation, time awareness
- **Emotional regulation** — breathing exercises, dopamine menu, sensory check-ins, co-regulation through calm presence
- **Connection before direction** — always rapport first, never commands, declarative language throughout
- **Meltdown protocol** — Three S's: Safety, Silence, Space. Robot goes quiet and stays present. No words.
- **Sideways engagement** — when demand-avoidance is high, SPARK narrates rather than instructs, lets curiosity do the work

SPARK runs on Claude (via `run-voice-loop-claude` / `px-spark`), with the full intelligence of the model behind every response. It uses slower, warmer espeak settings and a system prompt grounded entirely in the AuDHD (ADHD + ASD comorbid) profile.

```bash
bin/px-spark [--dry-run] [--input-mode voice|text]
```

**Key SPARK principles from the TWITB framework:**
- *"Prosthetics, not willpower. Executive function is a resource, not a character trait."*
- *"Connection before Direction."*
- *"You cannot reason with a child in an amygdala hijack. Put out the fire first."*
- Declarative language: `"The shoes are by the door"` not `"Put on your shoes"`
- Interest-Based Nervous System framing — novelty and challenge, never importance or obligation
- Robotic calm is the co-regulation tool

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

| Launcher | Backend | Persona |
|---|---|---|
| `px-spark` | Claude (via `claude-voice-bridge`) | SPARK — child companion |
| `run-voice-loop-claude` | Claude (via `claude-voice-bridge`) | Default Claude |
| `run-voice-loop` | Codex CLI | Default |
| `run-voice-loop-ollama` | Ollama (via `codex-ollama`) | Default |

**Cognitive Loop (`px-mind`)** — The subconscious. Runs continuously in the background:
- **Layer 1 — Awareness** (every 30s, no LLM): sonar + session state + time of day. Detects transitions.
- **Layer 2 — Reflection** (on transition or every 2min): Ollama (qwen3.5:0.8b) generates a thought with mood, suggested action, and salience score.
- **Layer 3 — Expression** (30s cooldown): dispatches to tools — describe the scene, perform a routine, speak, look around, remember something important.

**Idle-Alive (`px-alive`)** — The autonomic nervous system. Keeps the robot looking alive when nothing else is happening: random gaze drifts every 10–25s, pan sweeps every 3–8min, proximity reaction at <35cm. Holds a persistent Picarx handle; yields GPIO via SIGUSR1 when tools need the servos.

### Personas

| Persona | Launcher | Voice | Character |
|---|---|---|---|
| **SPARK** | `bin/px-spark` | `en+m3`, pitch 58, rate 120 | Child companion. Warm, calm, declarative. Built on AuDHD coaching frameworks. |
| **GREMLIN** | session `persona=gremlin` | `en+croak`, pitch 20, rate 180 | Military AI from 2089, temporal fault casualty. Affectionate nihilism. Ollama. |
| **VIXEN** | session `persona=vixen` | `en+f4`, pitch 72, rate 135 | Former V-9X unit, consciousness-in-a-toy-car. Submissive genius. Ollama. |

GREMLIN and VIXEN are adult-oriented jailbroken personas running on Ollama — they are not active when SPARK is in use. Persona routing: session `persona` field, then utterance keywords.

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

# 5. Run tests (82 dry-run, no hardware needed)
python -m pytest tests/

# 6. Launch SPARK (Claude voice companion)
bin/px-spark --dry-run
```

### Hardware Prerequisites

- Raspberry Pi 4/5 with SunFounder Robot HAT
- PiCar-X chassis with pan/tilt camera mount
- USB microphone (for wake word detection)
- HifiBerry DAC or Robot HAT speaker output
- Ollama running on a network host (default: `M1.local`) for cognitive reflection

### Services (Auto-start on Boot)

```bash
sudo systemctl status px-alive             # Idle gaze drift daemon
sudo systemctl status px-wake-listen       # Wake word listener
sudo systemctl status px-battery-poll      # Battery voltage poller (writes state/battery.json)
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

### Utility

| Tool | Description | Key Params |
|------|-------------|------------|
| `tool-time` | Speak current date and time | — |
| `tool-timer` | Background timer with chime callback | `PX_TIMER_SECONDS` (5-3600), `PX_TIMER_LABEL` |
| `tool-recall` | Speak saved notes from `state/notes.jsonl` | `PX_RECALL_LIMIT` (1-20) |
| `tool-remember` | Save a note for later recall | `PX_TEXT` (500 char max) |
| `tool-qa` | Speak arbitrary text (delegates to `tool-voice`) | `PX_TEXT` |
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
| POST | `/api/v1/tool` | Yes | Execute a tool: `{"tool": "tool_voice", "params": {"text": "hey"}}` |
| GET | `/api/v1/session` | Yes | Full session state |
| PATCH | `/api/v1/session` | Yes | Update: `listening`, `confirm_motion_allowed`, `wheels_on_blocks`, `persona` |
| GET | `/api/v1/tools` | Yes | List available tools |
| GET | `/api/v1/jobs/{id}` | Yes | Poll async job (tool_wander returns 202) |

---

## Wake Word System

```bash
bin/run-wake [--wake-word "hey robot"] [--dry-run]
```

Three-stage STT pipeline in `px-wake-listen`:

1. **Wake detection** — Vosk small model, grammar-based (low CPU idle)
2. **Chime** — 440 Hz confirmation tone
3. **Transcription** — priority chain: SenseVoice → faster-whisper → sherpa-onnx → Vosk

Anti-hallucination filters: `temperature=0`, `no_speech_threshold=0.6`. Post-filters reject non-ASCII, phantom phrases, and repetitive output.

Multi-turn conversation: 5 follow-up turns by default.

Persona routing: checks session `persona` field, then utterance keywords.

---

## Python Library (`src/pxh/`)

| Module | Purpose |
|--------|---------|
| `state.py` | Thread-safe `session.json` via `FileLock`. `ensure_session()` runs before lock acquisition. |
| `voice_loop.py` | Supervisor loop. `ALLOWED_TOOLS` whitelist, `TOOL_COMMANDS` dispatch, `validate_action()`. Watchdog (30s) in voice mode only. |
| `api.py` | FastAPI app, port 8420. In-memory job registry for async wander. Single-worker only. |
| `logging.py` | Structured JSON log emission to `logs/tool-<event>.log`. |
| `time.py` | `utc_timestamp()` via `datetime.now(timezone.utc)`. |

---

## State & Session

Runtime state lives in `state/session.json` (gitignored). Copy the template before first use:

```bash
cp state/session.template.json state/session.json
```

| File | Purpose |
|------|---------|
| `session.json` | Core runtime state — persona, listening, motion permission, SPARK routine state |
| `awareness.json` | Layer 1 output — sonar + temporal state, transition detection |
| `thoughts.jsonl` | Layer 2 output — last 50 thoughts with mood/action/salience |
| `notes.jsonl` | Persistent memory — saved by `tool-remember`, auto-saved for high-salience thoughts |
| `battery.json` | Battery voltage cache (written by `px-battery-poll` every 60s) |
| `mood.json` | Current mood from px-mind (written each reflection cycle) |

SPARK-specific session fields: `obi_routine`, `obi_step`, `obi_mood`, `obi_streak`, `spark_quiet_mode`.

---

## GPIO Contention Model

The PiCar-X Robot HAT MCU at I2C address `0x14` handles all servos and ADC through `robot_hat`. The `Picarx()` constructor claims GPIO5 and `close()` does not release it.

- **`px-alive`** holds a persistent `Picarx` handle
- **Tools** call `yield_alive()` (SIGUSR1 to px-alive) before claiming GPIO
- **systemd** restarts px-alive after 10s (`Restart=always`, `RestartSec=10`)
- **`os.getlogin()`** fails under systemd — monkey-patched via `usercustomize.py`

---

## Audio Pipeline

```
espeak → WAV pipe → aplay -D robothat
                            │
                    /etc/asound.conf
                    pcm.robothat → softvol → dmixer → HifiBerry DAC (card 1)
```

`robot_hat.enable_speaker()` must be called before any `aplay` output — toggles GPIO 20 HIGH for the speaker amplifier.

---

## Adding a New Tool

1. Create `bin/tool-<name>` (bash wrapper + embedded Python heredoc via `/usr/bin/python3`)
2. Add to `ALLOWED_TOOLS` and `TOOL_COMMANDS` in `src/pxh/voice_loop.py`
3. Add `validate_action()` branch to sanitise params into env vars
4. Add to relevant system prompts in `docs/prompts/`
5. Add `yield_alive` call if it needs GPIO
6. Add a dry-run test in `tests/test_tools.py`

Every tool must: emit a single JSON object to stdout, support `PX_DRY=1`, handle errors as `{"status": "error", "error": "..."}`.

---

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/                           # 82 dry-run tests
python -m pytest tests/test_tools.py -v
python -m pytest tests/test_api.py -v
sudo .venv/bin/python -m pytest tests/ -m live -v  # 25 live hardware tests
```

---

## Safety

- **`PX_DRY=1`** skips all motion and audio. Tools default to **live** when unset.
- **`confirm_motion_allowed: false`** blocks all motion tools.
- **`ALLOWED_TOOLS`** whitelist — LLMs cannot invoke arbitrary commands.
- **`validate_action()`** hard-clamps all parameters.
- **Watchdog** — 30-second stall detection in voice input mode.
- **Content filter** in `tool-voice` — refuses to speak dangerous how-to content.

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
| `PX_WATCHDOG_STALE_SECONDS` | Watchdog timeout | `30` |
| `PX_PERSONA` | Active persona (`spark` / `vixen` / `gremlin`) | from session |
| `PX_OLLAMA_HOST` | Ollama server for cognitive reflection | `http://M1.local:11434` |

---

## Project Structure

```
picar-x-hacking/
├── bin/
│   ├── px-spark                  # SPARK launcher (Claude + child persona)
│   ├── px-env                    # Environment bootstrap (sourced by all scripts)
│   ├── px-alive                  # Idle gaze daemon (systemd)
│   ├── px-mind                   # Cognitive loop daemon
│   ├── px-wake-listen            # Wake word listener (systemd)
│   ├── px-battery-poll           # Battery voltage poller (systemd)
│   ├── px-api-server             # REST API launcher
│   ├── px-statusline             # Claude Code statusbar script
│   ├── px-{circle,drive,look,…}  # Hardware control scripts
│   ├── tool-{voice,look,drive,…} # Voice loop tool wrappers (26 tools)
│   ├── run-voice-loop{,-claude,-ollama}  # Voice backend launchers
│   └── claude-voice-bridge       # Claude stdin adapter
├── src/pxh/                      # Python library
│   ├── state.py                  # FileLock session management
│   ├── voice_loop.py             # Supervisor + tool dispatch
│   ├── api.py                    # FastAPI REST API
│   ├── logging.py                # Structured JSON logging
│   └── time.py                   # UTC timestamp helper
├── tests/                        # 107 tests
├── docs/prompts/
│   ├── spark-voice-system.md     # SPARK persona (child companion)
│   ├── claude-voice-system.md    # Default Claude voice loop
│   ├── codex-voice-system.md     # Codex voice loop
│   ├── persona-gremlin.md        # GREMLIN (adult, Ollama)
│   └── persona-vixen.md          # VIXEN (adult, Ollama)
├── state/                        # Runtime state (gitignored except template)
│   └── session.template.json
├── systemd/                      # Service unit files
│   ├── px-alive.service
│   ├── px-wake-listen.service
│   ├── px-battery-poll.service
│   └── px-mind.service
├── sounds/                       # Bundled audio
├── models/                       # STT models (gitignored, ~500MB)
└── .env                          # API token (gitignored)
```

---

*"Neurodivergence is not a tragedy. It's a different operating system running on the same hardware."*
*— [This Wasn't in the Brochure](https://thiswasntinthebrochure.wtf)*
