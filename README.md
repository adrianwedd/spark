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

SPARK runs on Claude (via `run-voice-loop-claude` / `px-spark`), with the full intelligence of the model behind every response. It uses clear, measured espeak settings (`en-gb`, pitch 95, rate 100) and a system prompt grounded entirely in the AuDHD (ADHD + ASD comorbid) profile.

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
    │  37 tools   │  │  PYTHONPATH · LOG_DIR · venv      │  │  :8420        │
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
- **Layer 1 — Awareness** (every 60s, no LLM): sonar + session state + time of day. Detects transitions.
- **Layer 2 — Reflection** (on transition or every 2min): Claude Haiku via persistent tmux session (SPARK persona) or Ollama deepseek-r1:1.5b on M1.local (others). Generates a thought with mood, suggested action, and salience score.
- **Layer 3 — Expression** (30s cooldown): dispatches to tools — speak, look around, remember something important. Photo capture (`tool-describe-scene`) is on-request only, not autonomous.

**Idle-Alive (`px-alive`)** — The autonomic nervous system. Keeps the robot looking alive when nothing else is happening: random gaze drifts every 10–25s, pan sweeps every 3–8min, proximity reaction at <35cm. Holds a persistent Picarx handle; yields GPIO via SIGUSR1 when tools need the servos.

### Personas

| Persona | Launcher | Voice | Character |
|---|---|---|---|
| **SPARK** | `bin/px-spark` | `en-gb`, pitch 95, rate 100 | Child companion. Warm, calm, declarative. Built on AuDHD coaching frameworks. |
| **GREMLIN** | session `persona=gremlin` | `en+croak`, pitch 20, rate 180 | Military AI from 2089, temporal fault casualty. Affectionate nihilism. Ollama. |
| **VIXEN** | session `persona=vixen` | `en+f4`, pitch 72, rate 135 | Former V-9X unit, consciousness-in-a-toy-car. Submissive genius. Ollama. |

GREMLIN and VIXEN are adult-oriented jailbroken personas running on Ollama — they are not active when SPARK is in use. Persona routing: session `persona` field, then utterance keywords.

---

## How It Works — End-to-End Workflow

This section traces the complete data flow from power-on to a robot response, and the continuous background processes that give SPARK its sense of inner life.

### 1. Boot Sequence

Three systemd services start automatically:

```
Boot
 ├── px-alive.service        (root)   — claims Picarx() GPIO handle; starts gaze drift loop
 ├── px-wake-listen.service  (pi)     — loads Vosk wake word model; starts mic capture loop
 └── px-battery-poll.service (root)   — polls Robot HAT ADC every 30s → state/battery.json; plays rising/falling sweep tones on plug/unplug with voice announcement; escalating warnings + emergency shutdown at 10%
```

**`px-alive`** runs as root (GPIO access) and immediately calls `Picarx()`, claiming GPIO5 via `reset_mcu()`. It never releases this handle. All other processes that need servos must signal px-alive with `SIGUSR1` (via the `yield_alive` function in `px-env`) to make it exit cleanly. systemd restarts it after 10 seconds. The PCA9685 PWM chip retains the last servo position between restarts, so the robot head stays still.

**`px-wake-listen`** loads the Vosk grammar model (~40 MB) and sits in a tight capture loop on the USB microphone at 44100 Hz.

### 2. Launching SPARK

```bash
bin/px-spark [--dry-run] [--input-mode voice|text]
```

`px-spark` does the following in sequence:

```
px-spark
 1. Sets session.persona = "spark"          (via update_session)
 2. Sets session.listening = false
 3. Speaks greeting via tool-voice          ("Hey. I'm here.")
 4. Exports CODEX_CHAT_CMD=bin/claude-voice-bridge
 5. Exports PX_VOICE_VARIANT=en-gb, PX_VOICE_PITCH=95, PX_VOICE_RATE=100
 6. exec bin/codex-voice-loop --prompt docs/prompts/spark-voice-system.md ...
```

After step 6, `px-spark` is replaced by `codex-voice-loop` via `exec` (no fork). The voice loop process inherits all environment variables and owns the terminal.

The `CODEX_CHAT_CMD` override is the key to persona routing: instead of calling `codex exec`, the voice loop calls `claude-voice-bridge`, which is a thin adapter that passes the prompt to the `claude` CLI with SPARK's system prompt.

### 3. Wake Word Path

```
USB mic (44100 Hz)
 └── px-wake-listen (venv python)
      ├── [idle] Vosk grammar matches "hey robot" / "hey spark" / etc.
      │         CPU: ~3% — grammar decoder, no neural net
      ├── [wake] enable_speaker() → aplay 440 Hz chime (confirmation)
      ├── [record] capture until 1.5s silence (max 8s)
      ├── [STT] priority cascade:
      │    1. SenseVoice (sherpa-onnx, ~5s, non-autoregressive)
      │    2. faster-whisper base.en (~3-7s, best AU accent accuracy)
      │    3. sherpa-onnx Zipformer streaming (~2s)
      │    4. Vosk fallback
      ├── [anti-hallucination filters]
      │    • temperature=0, no_speech_threshold=0.6
      │    • reject: non-ASCII dominant, phantom phrases, repetitive (unique ratio <30%)
      ├── [persona routing]
      │    • session.persona = "spark"? → tool-chat (Ollama) if persona keyword in text
      │    • otherwise → set session.listening=true + write transcript to session
      └── [multi-turn] up to 5 follow-up turns with 1.5s silence detection each
```

For SPARK in normal mode, the transcript is written into `session.json` and `session.listening` is set to `true`. The voice loop, which is polling the session file, detects this and proceeds to step 4.

### 4. LLM Turn — Building and Sending the Prompt

The voice loop (`pxh/voice_loop.py`) runs this on each turn:

```python
build_model_prompt()
 ├── system_prompt    = docs/prompts/spark-voice-system.md   (full file)
 ├── session_summary  = key fields from session.json:
 │    persona, listening, obi_mood, obi_routine, obi_step,
 │    spark_quiet_mode, last_action, confirm_motion_allowed
 ├── recent_thoughts  = last 3 entries from state/thoughts-spark.jsonl
 │    (mood, action, salience — not full text, to avoid re-seeding loops)
 └── user_transcript  = session.transcript (the STT text)
```

This prompt is piped via stdin to `claude-voice-bridge`:

```bash
claude-voice-bridge (bin/claude-voice-bridge)
 1. Reads full prompt from stdin
 2. Unsets CLAUDECODE + CLAUDE_CODE_ENTRYPOINT   (prevents Claude Code tool use)
 3. Runs: claude -p "$PROMPT"
            --system-prompt docs/prompts/spark-voice-system.md
            --allowedTools ""
            --output-format text
            --no-session-persistence
 4. Streams stdout back to voice loop
```

`--allowedTools ""` is critical: it prevents Claude from using any Claude Code tools. It is a pure text-completion endpoint.

The voice loop captures all stdout and scans it for a JSON action object. It uses `JSONDecoder.raw_decode()` with a multi-line fallback scan — so Claude can reason in plain text above the action, and the final JSON is extracted cleanly:

```json
{"tool": "tool_voice", "params": {"text": "Obi! Guess what? A teaspoon of neutron star weighs a billion tonnes."}}
```

### 5. Tool Dispatch — Sanitise, Execute, Return

```python
validate_action(tool_name, raw_params)
 ├── ALLOWED_TOOLS whitelist check              (37 tools; KeyError = reject)
 ├── per-tool param sanitisation:
 │    • type coercion (str → int where needed)
 │    • range clamping (speed 0-60, duration 1-12s, pan -90..90, etc.)
 │    • enum validation (emote names, breathe types, etc.)
 │    • injection-safe: params become env vars, never shell-interpolated
 └── returns: (env_dict, tool_bin_path)

execute_tool(env_dict, tool_bin_path)
 ├── if session.persona set:
 │    inject PERSONA_VOICE_ENV → PX_VOICE_VARIANT, PX_VOICE_PITCH, PX_VOICE_RATE
 ├── subprocess.run(tool_bin, env=merged_env, ...)
 └── capture stdout JSON → log to logs/tool-<name>.log
```

Every tool in `bin/tool-*` follows the same pattern:

```bash
#!/usr/bin/env bash
source "$SCRIPT_DIR/px-env"          # sets PROJECT_ROOT, PYTHONPATH
python - "$@" <<'PY'
"""Tool docstring"""
import os, json, subprocess
from pxh.state import update_session
from pxh.logging import log_event

dry_mode = os.environ.get("PX_DRY", "0") != "0"

# ... tool logic ...

payload = {"status": "ok", ...}
log_event("tool_name", payload)
print(json.dumps(payload))           # single JSON line to stdout
PY
```

Tools that need GPIO call `yield_alive` first (defined in `px-env` as `kill -USR1 $(cat logs/px-alive.pid) 2>/dev/null; sleep 0.5`).

**Motion gate**: tools that move the robot check `confirm_motion_allowed` in session before proceeding. If false, they return `{"status": "blocked", "reason": "motion not allowed"}`.

### 6. Speech Output Pipeline

```
tool-voice
 ├── FileLock(logs/voice.lock)        (serialise — no overlapping streams)
 ├── if session.persona set → tool-voice-persona (Ollama rephrasing first)
 ├── robot_hat.enable_speaker()       (GPIO 20 HIGH → speaker amp on)
 ├── espeak -v en-gb -p 95 -s 100     (SPARK voice — British RP, higher pitch, slower)
 │    → WAV piped to aplay -D robothat
 └── /etc/asound.conf: robothat → softvol → dmixer → HifiBerry DAC (card 1)
```

The FileLock prevents two simultaneous `aplay` streams from corrupting each other. Persona voice settings (`PX_VOICE_VARIANT`, `PX_VOICE_PITCH`, `PX_VOICE_RATE`) are injected by `execute_tool()` from `PERSONA_VOICE_ENV` — so every tool that calls `tool-voice` internally picks up the right voice automatically.

### 7. Cognitive Loop — The Subconscious (px-mind)

`px-mind` runs as a separate, independent daemon. It has no GPIO access and does not interact with the voice loop directly — it writes state files that the voice loop reads passively.

```
px-mind (every cycle, ~30s)
 │
 ├── Layer 1 — Awareness (no LLM, ~1s)
 │    ├── sonar ping → distance
 │    ├── read session.json → persona, mood, routine, quiet_mode
 │    ├── time of day / day of week
 │    ├── battery voltage from state/battery.json
 │    └── write state/awareness.json
 │         detect transitions (person appeared, time changed, persona switched)
 │
 ├── Layer 2 — Reflection (~5-60s, backend varies by persona)
 │    triggered: on transition OR every 2min idle
 │    ├── build reflection prompt:
 │    │    • REFLECTION_SYSTEM_SPARK (warm, curious, age-appropriate inner voice)
 │    │    • awareness snapshot
 │    │    • last 3 moods + actions from thoughts-spark.jsonl (not full thought text)
 │    │    • random topic seed from 20 creative prompts (science, wonder, universe)
 │    ├── LLM call: Claude Haiku via tmux session (SPARK) or Ollama deepseek-r1:1.5b (others, temperature=1.3)
 │    ├── anti-repetition check via difflib (>75% similarity = suppress)
 │    ├── parse JSON: {thought, mood, action, salience}
 │    ├── append to state/thoughts-spark.jsonl
 │    └── if salience > 0.7 → auto_remember() → state/notes-spark.jsonl
 │
 └── Layer 3 — Expression (30s cooldown, pauses when session.listening=true or spark_quiet_mode=true)
      valid actions: wait, greet, comment, remember, look_at, weather_comment, scan
      dispatch based on reflection.action:
      ├── comment/greet     → tool-voice (via tool-voice-persona for rephrasing)
      ├── "remember"        → tool-remember
      ├── "look_at"         → tool-look (random gaze)
      ├── "weather_comment" → tool-weather + speak
      └── "scan"            → sonar sweep
```

**REFLECTION_SYSTEM_SPARK** enforces warm, optimistic content:
> *"NEVER be dark, nihilistic, or adult-themed. SPARK is warm, curious, and science-loving. Think like a kind robot friend who delights in sharing fascinating things about the universe."*

The reflection prompt is persona-isolated at the function level — `PERSONA_REFLECTION_SYSTEMS["spark"]` is selected at runtime from `awareness.json → persona` field.

### 8. Memory System — Persona-Scoped Persistence

All memory is scoped to the active persona to prevent cross-contamination between SPARK (child-safe) and GREMLIN/VIXEN (adult):

```
state/
 ├── notes-spark.jsonl      ← tool-remember writes; tool-recall reads
 ├── notes-vixen.jsonl      ← same tools, different scope
 ├── notes-gremlin.jsonl
 ├── thoughts-spark.jsonl   ← px-mind Layer 2 writes; voice loop reads for context
 ├── thoughts-vixen.jsonl
 └── thoughts-gremlin.jsonl
```

The persona is derived at runtime from `session.json → persona` in every process that writes or reads memory:
- `tool-remember`: `persona = load_session()["persona"].lower()` → `notes-{persona}.jsonl`
- `tool-recall`: same derivation → reads from `notes-{persona}.jsonl`
- `px-mind`: `persona = awareness["persona"]` → all file paths computed from this
- `voice_loop.build_model_prompt()`: reads `thoughts-{persona}.jsonl` for context injection

**Memory auto-save**: when px-mind generates a thought with `salience > 0.7`, it calls `auto_remember()` which appends to `notes-{persona}.jsonl`. This creates a long-term memory without explicit user instruction — high-salience observations about Obi's wellbeing, interesting facts shared, or significant moments persist across sessions.

### 9. Session State — The Shared Source of Truth

`state/session.json` is the nervous system of the whole platform. Every process reads and writes it; all writes go through `FileLock` to prevent corruption:

```json
{
  "persona": "spark",
  "listening": false,
  "transcript": "...",
  "confirm_motion_allowed": true,
  "wheels_on_blocks": false,
  "last_action": "tool_voice",
  "obi_routine": "morning",
  "obi_step": 2,
  "obi_mood": "good",
  "obi_streak": 5,
  "spark_quiet_mode": false,
  "history": [...]
}
```

Key coordination patterns:
- **`listening: true`** — set by px-wake-listen after transcription; cleared by voice loop after processing
- **`spark_quiet_mode: true`** — set by `tool-quiet start` or `tool-transition buffer`; px-mind Layer 3 skips expression while true
- **`confirm_motion_allowed: false`** — safety gate; all motion tools check this before moving
- **`wheels_on_blocks: true`** — development flag; motor output suppressed in hardware layer

### 10. Full Request → Response Timeline

For a typical SPARK voice interaction:

```
[t=0s]    Obi: "Hey Spark!"
[t=0.1s]  Vosk detects wake phrase
[t=0.1s]  enable_speaker() → 440 Hz chime plays
[t=0.5s]  USB mic records Obi's utterance
[t=2.5s]  1.5s silence detected; recording ends
[t=7.5s]  SenseVoice STT transcribes → "can we do our morning routine"
[t=7.5s]  session.transcript saved; session.listening = true
[t=8s]    voice_loop detects listening=true
[t=8s]    build_model_prompt() → 4KB prompt (system + session + thoughts + transcript)
[t=8s]    claude-voice-bridge pipes prompt to `claude -p ...`
[t=11s]   Claude responds → {"tool": "tool_routine", "params": {"action": "load", "name": "morning"}}
[t=11s]   validate_action() sanitises params → env vars
[t=11s]   execute_tool() injects SPARK voice env
[t=11.1s] bin/tool-routine runs, loads morning routine, updates session
[t=11.1s] tool-routine calls tool-voice internally
[t=11.2s] enable_speaker() → espeak → aplay → HifiBerry DAC
[t=11.5s] Obi hears: "Morning! Step one: drink some water. I'll wait."
[t=11.5s] session.last_action = "tool_routine"; session.listening = false
[t=42s]   px-mind Layer 1 runs; detects obi_routine changed
[t=47s]   px-mind Layer 2 reflects; generates thought about morning energy
[t=77s]   px-mind Layer 3 expresses; tool-voice speaks an unprompted science fact
```

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

# 5. Run tests (105 dry-run, no hardware needed)
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
| `tool-wander` | Smart obstacle-avoiding wander: sonar sweep picks best direction, speaks while navigating | `PX_WANDER_STEPS` (1-20), `PX_WANDER_QUIET` |
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

### SPARK — Child Companion Tools

Available only in SPARK persona mode. All support `PX_DRY=1`.

| Tool | Description | Key Params |
|------|-------------|------------|
| `tool-routine` | Daily routine manager: load, advance, complete | `PX_ROUTINE_ACTION` (load\|next\|status\|complete), `PX_ROUTINE_NAME` (morning\|homework\|bedtime\|wind-down) |
| `tool-checkin` | Emotional check-in: ask or record mood | `PX_CHECKIN_ACTION` (ask\|record), `PX_CHECKIN_MOOD` |
| `tool-celebrate` | Specific, brief positive reinforcement | `PX_CELEBRATE_TEXT` (optional) |
| `tool-transition` | Transition warning / buffer / arrival | `PX_TRANSITION_ACTION` (warn\|buffer\|arrived), `PX_TRANSITION_MINUTES`, `PX_TRANSITION_LABEL` |
| `tool-quiet` | Three S's meltdown protocol: stop, stay, safe | `PX_QUIET_ACTION` (start\|check\|end) |
| `tool-breathe` | Guided breathing exercise | `PX_BREATHE_TYPE` (simple\|box\|478), `PX_BREATHE_ROUNDS` (1-4) |
| `tool-dopamine-menu` | Interest-based activity suggestions | `PX_DOPAMINE_ENERGY` (high\|medium\|low), `PX_DOPAMINE_CONTEXT` (free\|focus\|wind-down) |
| `tool-sensory-check` | Body scan + sensory support | `PX_SENSORY_ACTION` (ask\|record), `PX_SENSORY_ISSUE` |
| `tool-repair` | Post-conflict reconnection | `PX_REPAIR_CONTEXT` (optional, private) |

### Google Workspace (optional)

Requires `gws auth login` (see [googleworkspace/cli](https://github.com/googleworkspace/cli)). Gracefully degrades if not authenticated.

| Tool | Description | Key Params |
|------|-------------|------------|
| `tool-gws-calendar` | Read upcoming calendar events | `PX_CALENDAR_ACTION` (today\|next\|week), `PX_CALENDAR_ID` |
| `tool-gws-sheets-log` | Append a row to a tracking spreadsheet | `PX_SHEETS_ID` (required, set in `.env`), `PX_SHEETS_EVENT`, `PX_SHEETS_DETAIL`, `PX_SHEETS_MOOD` |

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
| GET | `/` | No | SPARK web dashboard (text chat + quick-action buttons) |
| GET | `/api/v1/health` | No | Liveness probe |
| POST | `/api/v1/chat` | Yes | Send text; SPARK picks a tool via LLM and executes it |
| POST | `/api/v1/tool` | Yes | Execute a tool directly: `{"tool": "tool_voice", "params": {"text": "hey"}}` |
| GET | `/api/v1/session` | Yes | Full session state |
| PATCH | `/api/v1/session` | Yes | Update: `listening`, `confirm_motion_allowed`, `wheels_on_blocks`, `persona` |
| GET | `/api/v1/tools` | Yes | List available tools |
| GET | `/api/v1/jobs/{id}` | Yes | Poll async job (tool_wander returns 202) |
| GET | `/photos/{filename}` | No | Serve captured photos (used by web UI photo button) |

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
| `battery.json` | Battery voltage — volts, pct, charging flag (written every 30s; plug/unplug detection plays audio sweep tones) |
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

## Documentation

| Document | Audience | Description |
|---|---|---|
| [How Spark's Brain Works](docs/how-sparks-brain-works.md) | Kids / non-technical | ELI7 explanation of the cognitive architecture — ears, eyes, brain, and how they connect |
| [SPARK Prompt Audit](docs/spark-prompt-audit.md) | Developers | Complete inventory of every prompt SPARK uses — system-level and tool-embedded, with full text |
| [FAQ](docs/faq.md) | Everyone | Common questions about what SPARK is, how it works, and why it writes the way it does |

---

*"Neurodivergence is not a tragedy. It's a different operating system running on the same hardware."*
*— [This Wasn't in the Brochure](https://thiswasntinthebrochure.wtf)*
