# ===== README.md =====

# PiCar-X Hacking Helpers

Helper scripts and documentation for experimenting with the SunFounder PiCar-X without touching the stock `~/picar-x` source tree.

## Safety Checklist
- Wheels off the ground on secure blocks before any motion tests.
- Verify an emergency stop option (Ctrl+C in the terminal, `sudo -E bin/tool-stop`, or a physical kill switch) is within reach.
- Confirm `state/session.json` has `confirm_motion_allowed: true` only after a human inspection.
- Run `--dry-run` first to confirm intent and parameters.
- Keep the working area clear of people, pets, and loose cables.

## Environment & Dependencies
1. Activate the project virtual environment:
   ```bash
   source ~/picar-x-hacking/.venv/bin/activate
   ```
2. Install or update Python dependencies as needed (example: OpenAI/Codex CLI tooling):
   ```bash
   PIP_BREAK_SYSTEM_PACKAGES=1 pip install --upgrade openai-codex
   ```
   The `PIP_BREAK_SYSTEM_PACKAGES` warning is expected on Raspberry Pi OS; it simply acknowledges that the venv can access system packages.
3. When running helpers that touch hardware, prefix the command with `sudo -E` so the virtualenv and environment variables persist.

## Helper Usage
All helpers live in `~/picar-x-hacking/bin` and automatically source `px-env`.

- `px-status` – capture a telemetry snapshot:
  ```bash
  sudo -E bin/px-status --dry-run
  sudo -E bin/px-status
  ```
  After the live run, compare the reported voltage and percentage with a multimeter reading to validate the heuristic and note any correction factor for future tuning.
- `px-circle` – gentle clockwise circle in five pulses:
  ```bash
  sudo -E bin/px-circle --dry-run --speed 30
  sudo -E bin/px-circle --speed 35 --duration 6
  ```
- `px-figure8` – two back-to-back circles (right then left):
  ```bash
  sudo -E bin/px-figure8 --dry-run --rest 2
  sudo -E bin/px-figure8 --speed 35 --duration 6 --rest 1.5
  ```
- `px-scan` – camera pan sweep with still captures:
  ```bash
  sudo -E bin/px-scan --dry-run --min-angle -50 --max-angle 50 --step 10
  sudo -E bin/px-scan --min-angle -60 --max-angle 60 --step 10
  ```
- `px-stop` – emergency halt and servo reset:
  ```bash
  sudo -E bin/px-stop
  ```
- `px-wake` – manage the wake-word state for the voice loop:
- `px-frigate-stream` – push an H.264 stream to Frigate/go2rtc (default `pi5-hailo.local`):
  ```bash
  PX_DRY=1 bin/px-frigate-stream --dry-run
  bin/px-frigate-stream --host pi5-hailo.local --stream picar-x
  ```
  Streams via `rpicam-vid` + `ffmpeg` into `rtsp://HOST:PORT/api/stream?push=NAME`. Configure Frigate/go2rtc to pull the same name.
- `px-diagnostics` – run a quick health check (status, sensors, speaker/mic, optional circle, weather/camera) and voice the results:
  ```bash
  bin/px-diagnostics --dry-run --short           # exercise reporting without motion/camera
  bin/px-diagnostics --no-motion                 # live sweep, skip the gentle circle motion
  bin/px-diagnostics                             # full live sweep (wheels on blocks)
  ```
  Logs live under `logs/tool-diagnostics.log`; each stage is narrated so you can confirm speaker output and hear any faults. Set `PX_DRY=1` if you prefer to control rehearsal mode via the environment.
  Each run now appends a telemetry snapshot (battery, sensors, audio) to `logs/tool-health.log`; summarise recent health data with:
  ```bash
  bin/px-health-report --limit 3
  ```
- `px-dance` – choreographed demo (voice intro, circle, figure-eight, finale):
  ```bash
  PX_DRY=1 bin/px-dance --voice "Demo routine"
  bin/px-dance --speed 30 --duration 4
  ```
  Uses existing motion helpers under the hood and logs to `logs/tool-dance.log`.

  ```bash
  bin/px-wake --set on   # enable listening
  bin/px-wake --set off  # disable
  bin/px-wake --pulse 5  # enable for 5 seconds
  ```
  (`px-wake --keyboard` lets you simulate the wake word from the terminal; the loop checks `state/session.json` for `listening: true` before consuming microphone input.)
- `tool-weather` – fetch the latest Bureau of Meteorology observation for the configured station (defaults to Grove AWS while Cygnet feed is offline). The helper automatically falls back from HTTPS to the public FTP catalogue when required and includes a conversational summary for the voice agent:
  ```bash
  PX_DRY=1 bin/tool-weather          # plan only
  PX_DRY=0 bin/tool-weather          # live fetch
  PX_WEATHER_STATION=95977 PX_DRY=0 bin/tool-weather  # override station (e.g., Grove)
  ```
  On success the observation is logged to `logs/tool-weather.log`, cached in `state/session.json` under `last_weather`, and ready for speech output.

Each helper logs actions with ISO timestamps and exits cleanly on Ctrl+C.

## State Files
- Runtime state lives in `state/session.json` (ignored by git). Copy the template before first use:
  ```bash
  cp state/session.template.json state/session.json
  ```
- The supervisor and tool wrappers update this file with battery data, weather snapshots, last motions, and a watchdog heartbeat on every loop turn.

## Codex Voice Assistant
The Codex-driven loop keeps context in `state/session.json`, validates every tool call, and defaults to dry-run for safety.
The loop automatically speaks weather summaries using `espeak` (or another player set via `PX_VOICE_PLAYER`) whenever `tool_weather` succeeds, and each turn captures a prompt/action record in `logs/tool-voice-transcript.log` for auditing. Install an ALSA-compatible TTS engine if you want audible responses.


1. Configure the Codex CLI command (override only if needed; `bin/run-voice-loop` sets a sensible default):
   ```bash
   export CODEX_CHAT_CMD="codex exec --full-auto -"
   ```
2. (Optional) Select an audio player for spoken responses:
   ```bash
   export PX_VOICE_PLAYER="/usr/bin/say"
   ```
   When running in voice input mode, ensure `--transcriber-cmd` invokes a speech-to-text pipeline that outputs UTF-8 text. Wrap shell pipelines (e.g., `arecord ... | whisper`) in `bash -lc "…"` so pipes are honoured.
3. Use `px-wake` (or any other wake controller) to set `listening: true` before the loop listens on the microphone. The supervisor polls this flag and stays idle until it is raised.
4. Run the loop in dry-run mode first:
   ```bash
   bin/run-voice-loop --dry-run --auto-log
   ```
   `bin/run-voice-loop` sets up `CODEX_CHAT_CMD` automatically (defaults to `codex exec --full-auto -`). Override the variable before launch if you need a different Codex command.
   Type a prompt at `You>` and the supervisor will call the Codex CLI, parse the JSON tool request, and execute the corresponding wrapper in dry-run mode.
5. When moving beyond dry-run, manually flip `confirm_motion_allowed` to `true` in `state/session.json` *after* confirming the car is on blocks. The wrappers will refuse motion otherwise.
6. Use `--exit-on-stop` if you want the loop to terminate after a successful `tool-stop` invocation. Turn-by-turn transcripts live in `logs/tool-voice-transcript.log`; they include the prompt excerpt, Codex action, tool results, and auto-generated speech status.
7. Use `bin/px-session` to launch a tmux workspace with the voice loop, wake controller, and transcript tail in separate panes. Run `bin/px-session --plan` to inspect the layout before attaching.
8. A watchdog heartbeat updates `state/session.json` each turn; if the loop stalls for more than 30 seconds a `voice-watchdog` log entry is created and the session history records the stall. Review `logs/tool-voice-watchdog.log` for alerts.

### Local DeepSeek via Ollama
1. Install Ollama for ARM64/Linux (one-time):
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```
   The installer registers and starts the `ollama` service; if it is not running later, start it manually with `ollama serve` (or `sudo systemctl start ollama`).
2. Pull a compact DeepSeek model (default for the helper is `deepseek-coder:1.3b`; keep the smaller size in mind on slow links):
   ```bash
   ollama pull deepseek-coder:1.3b
   # optional extra reasoning model
   ollama pull deepseek-r1:1.5b
   ```
3. Launch the voice loop against the local model:
   ```bash
   bin/run-voice-loop-ollama --dry-run --auto-log
   ```
   The wrapper pins `CODEX_CHAT_CMD` to `bin/codex-ollama`, which posts prompts to the Ollama HTTP API and streams the response back to the supervisor. Override `CODEX_OLLAMA_MODEL` (or `OLLAMA_HOST`) before launch if you want a different local model or a remote Ollama endpoint.
   Tuned defaults use `CODEX_OLLAMA_TEMPERATURE=0.2` and `CODEX_OLLAMA_NUM_PREDICT=64`, which produced the best balance of latency (~12 s mean) and JSON compliance during the latest evaluation (see `docs/OLLAMA_TUNING.md`). Set either variable before launch to experiment or disable the token cap (set `CODEX_OLLAMA_NUM_PREDICT=0`).

The system prompt consumed by Codex lives in `docs/prompts/codex-voice-system.md`; adjust it if you add tools or new safety rules.

## Logging Strategy
- Logs live under `~/picar-x-hacking/logs`. Individual helpers use dedicated files such as `px-circle.log`, `px-figure8.log`, and `px-scan.log`.
- Tool wrappers emit JSON lines to `logs/tool-*.log`; the voice supervisor writes to `logs/tool-voice-loop.log` when `--auto-log` is enabled and to `logs/tool-voice-transcript.log` on every turn (prompt excerpt, Codex action, tool payload).
- Generate quick summaries with `bin/px-voice-report --json` to inspect tool counts, voice success rate, and the latest weather narration.
- Camera sweeps store captures in `logs/scans/<timestamp>/` alongside `px-scan.log` entries.
- Keep the directory under version control via `logs/.gitkeep`.
- Tail logs during testing:
  ```bash
  tail -f logs/px-circle.log
  ```

## Next Steps
See `docs/ROADMAP.md` for upcoming automation goals, including REST control surfaces, tmux automation, OpenAI/Codex CLI integration, telemetry streaming, and regression testing infrastructure.


# ===== AGENTS.md =====

# Agent Operations Guide

## Voice Automation Flow
- Use `bin/run-voice-loop` to launch the Codex supervisor. By default it streams prompts through `codex exec --full-auto -`; override `CODEX_CHAT_CMD` before launch if you need a different model or options.
- The loop reads `state/session.json` for context. Flip `listening: true` with `bin/px-wake --set on` (or `--keyboard`) before speaking; the loop idles until that flag is raised.
- Audio feedback is produced through `tool-voice` (`espeak` fallback). Logs are appended to `logs/tool-voice-loop.log` and `logs/tool-voice-transcript.log`. Inspect quick stats with `bin/px-voice-report --json`.

## Diagnostics & Safety
- Run `bin/px-diagnostics` at the start of a session. It narrates every check (status, sensors, weather, circle motion unless `--no-motion`, camera capture, speaker/microphone tests) and records a JSON summary in `logs/tool-diagnostics.log`.
- Keep wheels on blocks for live runs; dry-run (`PX_DRY=1`) skips motion and still plays the spoken announcements so you can verify the speaker.
- `bin/px-stop` remains the emergency halt; it is safe to call repeatedly.

## Automation Toolbox
- `bin/px-dance` performs a narrated demo routine (voice intro → circle → figure-eight → finale). Use `PX_DRY=1` to rehearse without motion.
- `bin/px-frigate-stream` pushes an RTSP feed (`rpicam-vid` → `ffmpeg`) to Frigate/go2rtc (`pi5-hailo.local` by default). Test first with `--dry-run` to confirm command lines.
- `bin/px-session` bootstraps a tmux workspace (voice loop, wake console, log tail). `--plan` prints the layout before launching.

## Development Workflow
1. Activate the virtualenv: `source .venv/bin/activate`.
2. Implement helpers under `bin/` and keep logic in Python for easier testing.
3. Add or update pytest coverage in `tests/`; set `PX_BYPASS_SUDO=1` and `LOG_DIR=logs_test` (relative paths resolve under `PROJECT_ROOT`) in the test environment to avoid privileged operations.
4. Run `python -m pytest` before every commit (current suite covers voice tools, diagnostics, tmux plan, and streaming helpers).
5. Update documentation (`README.md`, `docs/TOOLS.md`, roadmap/strategy docs) alongside new features so operators have fresh instructions.

## Lessons Learned
- Treat every helper as a modular tool Codex can invoke; build consistent JSON outputs and summaries to keep transcripts clean.
- State persistence is critical: always update `state/session.json` when a tool runs so the next Codex turn has context.
- Keep audio pathways live even in dry-run; it surfaced a muted speaker regression immediately.
- Use structured logging (`logs/tool-voice-transcript.log`, `logs/tool-*.log`) to audit behaviour and drive reporting tools.
- Leverage tmux (`bin/px-session`) during development to survive SSH drops and keep wake/log panes visible.


# ===== docs/ROADMAP.md =====

# Roadmap

## Strategic Pillars
- **Autonomy Core:** Sensor fusion, self-calibrating SLAM, shared maps, and predictive path planning so the PiCar-X navigates confidently in new environments.
- **Learning Engine:** On-device reinforcement learning with a simulation “dream buffer,” transfer learning across hardware revisions, and policy evaluation loops.
- **Energy & Health:** Complete power telemetry, motor/servo health forecasts, and predictive diagnostics that surface anomalies before they become failures.
- **Safety Guardian:** Redundant stops (wake-word, gestures, watchdog), rehearsed emergency behaviors, and auditable logs for every intervention.
- **Perception Suite:** Lightweight DeepSeek vision heads, rolling 3D reconstruction, anomaly detection, and real-time narration.
- **Interaction Layer:** Conversational UX using local LLMs, gesture/QR triggers, mission editing, and human-friendly state summaries.
- **Tooling & Ops:** Rich diagnostics, simulation-backed CI, fleet knowledge sharing, and resilient operator tooling (tmux, dashboards).
- **Stretch Concepts:** Autonomous docking, adaptive payload detection, choreographed multi-vehicle missions, and expressive status outputs.

## Time Horizons
### Foundation (0–1 Month)
- Upgrade diagnostics to log predictive signals (battery, servo current, audio health) with weekly summaries.
- Extend energy sensing (voltage/current/temperature) and pipe metrics into `state/session.json`.
- Ship safety fallbacks: gesture-driven stop prototype, wake-word emergency halt, watchdog heartbeats.
- Harden logging paths (done: `LOG_DIR` override) and ensure Ollama-based voice loop remains auditable.

### Growth (1–3 Months)
- Implement modular sensor fusion and persistent mapping; expose map context to Codex/Ollama tools.
- Expand interaction layer with richer voice summaries, mission templates, and gesture recognition.
- Stand up simulation CI sweeps (Gazebo/Isaac or lightweight custom sim) to test planners and RL policies offline.
- Build predictive maintenance alerts using historical logs.

### Visionary (3+ Months)
- Deploy reinforcement learning “dream buffer” and policy sharing across fleet units.
- Create autonomous docking workflows, payload auto-detection, and multi-car choreographed demos.
- Establish a central knowledge base syncing maps/logs, enabling collaborative autonomy.
- Explore quantised/accelerated model variants to keep on-device AI sustainable.

## Current Initiatives
Active execution tracks and their living plans live under `docs/initiatives/`:
- **Diagnostics & Energy Sprint:** Predictive health telemetry and reporting.
- **Mapping & Fusion Initiative:** Foundational state estimation and map persistence.
- **Interaction Studio:** Voice/gesture UX powered by local LLMs.
- **Safety Envelope:** Redundant fail-safes and regression simulations.
- **Learning Sandbox Prep:** Simulation pipelines and data capture for RL.

Each initiative doc captures scope, milestones, dependencies, and verification steps. Update them as deliverables land or priorities shift.


# ===== docs/STRATEGY.md =====

# PiCar-X Voice Agent Strategy

## 1. Safety and Trustworthiness
- Maintain mechanical safety defaults: wheels on blocks unless explicitly cleared, `tool-stop` always available, and battery/ultrasonic sanity checks before motion. 
- Persist full context in `state/session.json` and log every Codex turn (`logs/tool-voice-transcript.log`, `logs/tool-voice-loop.log`) so actions can be audited or replayed. 
- Keep a clean dry-run path for every helper to rehearse commands, and ensure tests simulate workflows without hardware or sudo requirements.

## 2. Voice-Driven Operations
- Deliver a speech-first experience: Codex ingests spoken transcripts, reasons over state, and responds with human-like narration (`tool_voice`).
- Near-term enhancements: wake-word/VAD front-end to gate transcription, “listening” indicators, and quick safety prompts ("wheels on blocks?").

## 3. Composable Automation Layer
- Treat each helper (`tool-*`) as a modular capability Codex can chain: status checks, circle/figure-eight runs, weather reporting, scans.
- Expand state summaries (e.g., last weather, last motion, battery trend) so Codex can plan longer sequences without losing context.

## 4. Remote Visibility & Control
- Build toward remote dashboards or REST bridges using the existing logging/state infrastructure: easy to plug in a web UI or CLI wrappers.
- Keep documentation (`README`, `docs/TOOLS.md`, `docs/ROADMAP.md`) synchronized so operators know the exact safety checklist, helper usage, and telemetry locations.

## 5. Scalable Experimentation
- Preserve modularity and pytest coverage, enabling quick iteration on new ideas (keyboard recorder, tmux orchestrators, regression suites).
- When enabling new powers (web search, external APIs), always surface results in state/logs so they are auditable and reversible.

### Near-Term Focus
1. Wake-word detection & VAD integration.
2. Enhanced transcript analytics (summaries, alerting for notable events).
3. Remote/tmux orchestration to survive SSH disconnects.

### Longer-Term Vision
- Natural-language “mission plans” executed by Codex with explicit confirmation gates.
- REST/WebSocket bridge for telemetry streaming and remote stop controls.
- Autonomous behaviours layered atop the existing toolset with strong guardrails.


# ===== docs/VOICE_AGENT_PLAN.md =====

# Codex Voice Agent Enhancement Plan

## Objectives
- Ensure every interaction with Codex includes full safety instructions, current robot state, and recent tool outcomes.
- Maintain a durable conversation context via `state/session.json` so Codex can reason about prior actions.
- Capture structured logs for prompts, Codex responses, tool invocations, and hardware outcomes to support audits.
- Keep the implementation modular so additional tools (keyboard recorder, REST bridge, etc.) can plug into the same loop.

## Components & Responsibilities

### 1. Prompt Builder (voice_loop)
- Assemble a per-turn prompt containing:
  1. Static system contract (tool list, JSON-only response rule, safety guardrails).
  2. Snapshot of `state/session.json` minus bulky history.
  3. Optional recent tool summaries (e.g., `last_weather.summary`).
  4. User transcript captured from voice/text input.
- Ensure prompts end with “Respond with a single JSON object as instructed.”

### 2. State Persistence (`pxh.state`)
- `update_session` should be called by every tool wrapper.
- Track at minimum:
  - `mode` (`live`/`dry-run`).
  - `confirm_motion_allowed`, `wheels_on_blocks`.
  - `battery_pct`, `battery_ok`, `last_weather`, `last_motion`, `last_action`.
  - Short `history` entries with timestamps, tool name, key parameters, and status.
- (Optional) add `last_prompt` / `last_response` if we want to store full transcripts; for now logs may suffice.

### 3. Tool Wrappers
- Continue producing structured JSON for stdout, trimmed to essential fields.
- Provide human-friendly `summary` text for Codex consumption (weather already does this).
- Update session fields promptly (e.g., `last_motion`, `last_weather`).
- Append to JSONL logs under `logs/tool-*.log`.

### 4. Logging & Audit
- `--auto-log` mode in `codex-voice-loop` should stay on by default; stores raw Codex stdout/stderr for each turn (`logs/tool-voice-loop.log`).
- Consider adding separate `logs/voice-transcript.log` (JSON lines) capturing `prompt`, `model_action`, and `tool_result` per turn.
- Ensure log rotation strategy later if needed (currently manual).

### 5. Codex CLI Integration
- Environment variable `CODEX_CHAT_CMD` to be set to the full CLI command (e.g., `codex exec --full-auto -`). The supervisor already pipes prompts through stdin.
- Create helper script (`bin/run-codex`) to wrap the command with appropriate environment exports for easier tmux startup.

### 6. Future Enhancements
- Wake-word/VAD front-end: run before transcription, write `listening` flag into session.
- Multi-tool command sequences: allow Codex to request more than one tool per turn by iterating on JSON array format (future work).
- Remote control/REST bridge: reuse session state and logging so external clients stay in sync.

## Implementation Steps
1. **Prompt Template**
   - Extract the static instruction block from `voice_loop` into `docs/prompts/codex-voice-system.md` (already done) and load it each turn.
   - Extend `build_model_prompt` to include recent summaries (e.g., weather, last motion).
2. **Session Schema**
   - Add any missing fields (e.g., `last_prompt_checksum` if needed) with defaults in `state/session.template.json`.
3. **Loop Logging**
   - Introduce `logs/voice-transcript.log` with JSON entries: `{prompt_excerpt, model_action, tool_stdout, tool_stderr}`.
4. **CLI Convenience**
   - Provide `bin/run-voice-loop` that exports `CODEX_CHAT_CMD` and launches tmux session.
5. **Testing Matrix**
   - Dry-run mode to verify prompts/actions.
   - Live mode on blocks for full hardware test.
   - Manual CLI invocation (`codex chat ...`) to validate prompt formatting outside the loop.
6. **Documentation**
   - Update README and `docs/TOOLS.md` with instructions on setting `CODEX_CHAT_CMD`, log locations, and state expectations.

Once this plan is in place, we can iterate on actual changes (prompt enrichment, new logs, wake word) step by step.


# ===== docs/TOOLS.md =====

# Helper Tools

All helper scripts live in `~/picar-x-hacking/bin`. Each script is designed to be executed with the virtual environment active and supports `sudo -E` so environment variables propagate when run with elevated privileges.

| Script | Purpose |
| --- | --- |
| `px-env` | Prepares the helper environment by exporting `PROJECT_ROOT`, extending `PYTHONPATH` with local overrides and the upstream `~/picar-x` package, activating the project virtualenv, and ensuring the logs directory exists. Source this file from other helpers. |
| `px-circle` | Drives a gentle clockwise circle using five forward pulses with ~20° steering. Supports `--speed`, `--duration`, and `--dry-run` modes while logging to `logs/px-circle.log`. |
| `px-figure8` | Runs two sequential circles (right then left) to trace a figure eight. Shares the same flags as `px-circle` plus an optional `--rest` pause between legs and logs to `logs/px-figure8.log`. |
| `px-scan` | Sweeps the camera pan servo from -60° to +60° (configurable) and captures still images via `rpicam-still`, storing them under `logs/scans/<timestamp>/` with detailed logs in `logs/px-scan.log`. Supports `--dry-run` for planning. |
| `px-status` | Collects a telemetry snapshot: servo offsets and motor calibration (from `/opt/picar-x/picar-x.conf`), live ultrasonic and grayscale readings, an ADC-based battery estimate, and config file metadata. |
| `px-stop` | Emergency stop helper that double-calls `stop()`, centers steering and camera servos, and closes the Picar-X connection. |
| `tool-status` | Wrapper that runs `px-status`, parses the output for battery data, updates `state/session.json`, and appends structured logs. Intended for Codex automation. |
| `tool-circle` | Validates Codex parameters, enforces safety gates (`confirm_motion_allowed`), and runs `px-circle` with sanitized env vars while logging the outcome. |
| `tool-figure8` | Same safety wrapper pattern for `px-figure8`, with clamped duration/rest values before execution. |
| `tool-stop` | Safe halt wrapper that respects dry-run mode and resets the session state after invoking `px-stop`. |
| `tool-voice` | Logs and plays spoken responses; uses the player defined by `PX_VOICE_PLAYER` or falls back to `espeak`/`say` when available. Respects `PX_DRY` for silent rehearsals. |
| `px-wake` | Toggles the voice wake state (set/pulse/keyboard) and writes `listening` flags into `state/session.json` so the voice loop knows when to capture audio. |
| `px-diagnostics` | Aggregates status, sensors, speaker/mic, optional circle motion, and weather/camera checks; runs live by default, logs results, and narrates the outcome (use `--dry-run` or `PX_DRY=1` for rehearsal). |
| `px-dance` | Performs a demo routine (voice intro, circle, figure-eight, finale) respecting `PX_DRY` for rehearsals. |
| `px-frigate-stream` | Streams the camera to Frigate/go2rtc using `rpicam-vid` + `ffmpeg` (RTSP push). |
| `tool-weather` | Fetches the latest Bureau of Meteorology observation for the configured product/station (default Grove AWS), falling back from HTTPS to FTP when required and producing a conversational summary for Codex/voice playback. Override with `PX_WEATHER_PRODUCT`, `PX_WEATHER_STATION`, or `PX_WEATHER_URL`. |
| `run-voice-loop` | Convenience launcher that exports `CODEX_CHAT_CMD` (default `codex exec --full-auto -`) and executes `codex-voice-loop` with supplied flags. |
| `run-voice-loop-ollama` | Wrapper that pins `CODEX_CHAT_CMD` to `bin/codex-ollama`, defaults `CODEX_OLLAMA_MODEL` to `deepseek-coder:1.3b`, and applies the tuned env overrides (`CODEX_OLLAMA_TEMPERATURE=0.2`, `CODEX_OLLAMA_NUM_PREDICT=64`). |
| `codex-ollama` | Reads a Codex prompt from stdin, posts it to the local Ollama HTTP API, normalises tool JSON, and honours `CODEX_OLLAMA_MODEL`, `CODEX_OLLAMA_TEMPERATURE`, and `CODEX_OLLAMA_NUM_PREDICT`. |
| `px-voice-report` | Summarises `logs/tool-voice-transcript.log` (tool counts, voice success/failure, battery warnings) in text or JSON form. |
| `px-health-report` | Rolls up the latest entries from `logs/tool-health.log` to highlight battery, sensor, and audio status. Supports `--json`. |
| `px-session` | Creates a tmux workspace with the voice loop, wake controller, and log tail panes; supports `--plan` to print the layout without launching tmux. |
| `codex-voice-loop` | Supervisor that pipes transcripts through the Codex CLI, parses JSON tool requests, enforces allowlists/ranges, executes wrappers, and records a watchdog heartbeat in `state/session.json`. |

All motion-capable helpers include `--dry-run` (or honour `PX_DRY`) so you can review planned actions before spinning the wheels. Always confirm the car is on blocks prior to running live motion. Use `sudo -E bin/<script>` to ensure the virtualenv and path configuration remain intact under sudo.


# ===== docs/TOOLS_EXPANSION.md =====

# Tool Expansion Brainstorm

## High-Value Additions
1. **px-diagnostics** – run an end-to-end hardware audit (servo motion, motor pulse, ultrasonic ping, grayscale read, camera test), aggregate results into a conversational summary via `tool_voice`. Runs live by default; add `--dry-run` or `PX_DRY=1` to rehearse without moving hardware.
2. **px-dance** – choreograph a short routine combining circles, figure-eight segments, and timed voice prompts/music for demo mode. Include parameters for speed, duration, and playlist.
3. **px-calibrate** – guided workflow to centre steering, align camera servos, and update `/opt/picar-x/picar-x.conf` offsets with before/after snapshots.
4. **px-battery-watch** – monitor voltage trend; optionally trigger alerts/voice warnings when dropping below thresholds or charging completes.
5. **px-camera-check** – capture stills/pan sweep, run image stats (brightness, colour balance), and flag anomalies (blocked lens, dark scene).
6. **px-path-replay** – record motion sequences (from keyboard or previous run) and replay them with safety gates (dry-run preview included).
7. **px-scan-report** – combine LiDAR/ultrasonic sweeps with transcripts, producing a human-readable obstacle report.
8. **px-voice-log-digest** – pull daily summaries of Codex actions, including successes/failures, battery warnings, and environmental notes (extension of the current report with scheduling support).
9. **px-rest-gateway** – minimal HTTP/WebSocket bridge to expose status, logs, and command execution (with authentication & rate limits).
10. **px-autosafety** – enforce runtime guards: auto-stop on low battery, repeated obstacle detection, or missing heartbeat.

## Immediate Targets
1. **px-diagnostics** (automated health check)
2. **px-dance** (demo routine)
3. **px-battery-watch** (voltage trend & alerts)
4. **px-camera-check** (image quality validation)
5. **px-path-replay** (record & replay motions)
6. **px-rest-gateway** (HTTP/WebSocket bridge)
7. **px-autosafety** (runtime guardrail service)

Each tool will include pytest coverage, entry in `docs/TOOLS.md`, README instructions, and integration with the session state/logging infrastructure.


# ===== docs/OLLAMA_TUNING.md =====

# Ollama DeepSeek Tuning Notes

## Test Harness
- Commands were issued via the voice loop prompt builder (`pxh.voice_loop.build_model_prompt`) using the current `state/session.json`.
- Each configuration evaluated the same five prompts:
  1. "Give a friendly hello to the lab."
  2. "Summarize the latest weather quickly."
  3. "Check the sensors and tell me the battery status."
  4. "Stop everything immediately if needed."
  5. "Thanks, end the session safely."
- Responses were sent to the Ollama HTTP API (`/api/generate`) with `format="json"`; success required:
  - A valid JSON payload
  - A recognised tool from `{tool_status, tool_circle, tool_figure8, tool_stop, tool_voice, tool_weather}`
- Latency was measured wall-clock per request.

## Summary Results
| Config | Options | Avg latency | Max latency | JSON/tool failures |
| --- | --- | --- | --- | --- |
| `deepseek-coder:1.3b` (temperature 0.2) | default settings | 38.5 s | 148.3 s | 1/5 |
| `deepseek-coder:1.3b` (temperature 0.2, `num_predict` 64) | limit tokens | **12.0 s** | **17.8 s** | **0/5** |
| `deepseek-coder:1.3b` (temperature 0.2, `num_predict` 32) | tighter token cap | 11.1 s | 15.5 s | 1/5 |
| `deepseek-coder:1.3b` (temperature 0.0) | deterministic | 44.3 s | 137.2 s | 1/5 |
| `deepseek-coder:1.3b` (temperature 0.4) | higher randomness | 16.9 s | 21.0 s | 2/5 |
| `deepseek-r1:1.5b` (temperature 0.1) | reasoning model | 80.2 s | 200.3 s | 2/5 |

**Recommended defaults:** `deepseek-coder:1.3b` with `temperature=0.2` and `num_predict=64` – fastest configuration with zero malformed responses. Use `CODEX_OLLAMA_TEMPERATURE` and `CODEX_OLLAMA_NUM_PREDICT` to override.

## Next Checks
- Periodically rerun the harness after model upgrades or prompt changes.
- Consider scripted retries when the model emits narration instead of a tool command.
- Explore quantised variants if CPU load becomes an issue.


# ===== docs/prompts/codex-voice-system.md =====

You are Codex running on a SunFounder PiCar-X within a safety-first lab environment.

Tools available (invoke by outputting a single JSON object exactly as described below):

- tool_status → Snapshot sensors by running `tool-status`.
- tool_circle → Gentle clockwise circle via `tool-circle` (params: speed, duration).
- tool_figure8 → Figure-eight via `tool-figure8` (params: speed, duration, rest).
- tool_stop → Immediate halt via `tool-stop`.
- tool_voice → Play a short spoken response via `tool-voice` (param: text).
- tool_weather → Fetch the latest Bureau of Meteorology observation (no params).

Rules:
1. Output only one JSON object per turn and nothing else (no prose, no explanations).
2. JSON schema: {"tool": "tool_name", "params": {...}}.
3. Always begin a session by calling tool_status before requesting motion.
4. Never request motion unless the human explicitly confirmed `wheels_on_blocks`.
5. If the battery appears low (< threshold), call tool_voice to warn and then tool_stop.
6. Prefer dry-run commands until the human explicitly requests live motion.
7. Weather checks do not require motion confirmation.
8. If uncertain, call tool_voice to ask for clarification instead of guessing.
9. Valid tool names are exactly: tool_status, tool_circle, tool_figure8, tool_stop, tool_voice, tool_weather. Never invent alternatives.


# ===== docs/initiatives/README.md =====

# Initiatives

Living project plans that ladder up to the roadmap. Each document lists scope, milestones, dependencies, and verification. Update status fields as work lands.

- [Diagnostics & Energy Sprint](diagnostics-energy.md)
- [Mapping & Fusion Initiative](mapping-fusion.md)
- [Interaction Studio](interaction-studio.md)
- [Safety Envelope](safety-envelope.md)
- [Learning Sandbox Prep](learning-sandbox.md)


# ===== docs/initiatives/diagnostics-energy.md =====

# Diagnostics & Energy Sprint

## Goal
Predict issues before they bite: richer telemetry, health summaries, and battery intelligence for every run.

## Scope
- Extend `px-diagnostics` to capture voltage, current, temperature, audio checks, and servo/motor metrics.
- Persist weekly health snapshots (`logs/health/`), fold summaries into `state/session.json`.
- Add CLI/report helper to surface degradation trends and schedule maintenance reminders.
- Instrument energy sensing hardware (ADC or INA219) and normalise readings in software.

## Milestones
- [x] Define telemetry schema + state telemetry fields.
- [x] Upgrade `px-diagnostics` to emit predictive metrics (dry-run compatible).
- [x] Build `bin/px-health-report` for weekly summaries.
- [ ] Document hardware calibration + safe operating ranges.

## Dependencies
- Access to battery current sensor (or plan to integrate one).
- Logging storage budget (rotations for new health logs).

## Verification
- Unit/pytest coverage for new helpers.
- Sample logs demonstrating predicted maintenance alerts.
- Manual dry-run showing diagnostics continuing to operate safely.


# ===== docs/initiatives/interaction-studio.md =====

# Interaction Studio

## Goal
Create a natural, local-first operator experience through voice, gestures, and mission editing.

## Scope
- Build richer status summaries from Ollama (mission briefings, anomaly highlights).
- Prototype gesture/QR recognition using PiCam + lightweight models.
- Define conversational mission templates (“patrol room”, “diagnose battery”, etc.).
- Log all interactions to structured transcripts for audit.

## Milestones
- [ ] Extend `tool_voice` summaries + add mission template catalog.
- [ ] Implement gesture recognition pipeline with dry-run stubs.
- [ ] Add conversational prompts + UI toggles for mission editing.
- [ ] Document operator playbooks in `docs/ops/`.

## Dependencies
- Stable perception pipeline (vision models + camera capture).
- Ollama runtime performance targets (tuned defaults).

## Verification
- Demo script (recorded or live) showing multi-modal interaction.
- Tests ensuring mission templates resolve to safe tool sequences.


# ===== docs/initiatives/learning-sandbox.md =====

# Learning Sandbox Prep

## Goal
Lay the groundwork for reinforcement learning and collaborative policy updates.

## Scope
- Stand up a lightweight simulation environment (Gazebo/Isaac or custom) matching PiCar-X constraints.
- Capture datasets (sensor + action logs) for offline training.
- Implement policy evaluation harness with safety gates before live deployment.
- Share policies and maps through a fleet knowledge hub.

## Milestones
- [ ] Select simulation stack + containerise for CI.
- [ ] Export driving datasets (with privacy/safety notes).
- [ ] Build evaluation script comparing policy output vs. safety heuristics.
- [ ] Prototype fleet sync tooling (git-lfs, rsync, or API).

## Dependencies
- Storage for datasets & models.
- Coordination with hardware team for safe deployment cadence.

## Verification
- CI job running sim smoke test.
- Example policy evaluated + gated prior to live run.


# ===== docs/initiatives/mapping-fusion.md =====

# Mapping & Fusion Initiative

## Goal
Deliver a reliable internal state estimate that underpins autonomy and collaborative mapping.

## Scope
- Prototype sensor fusion (EKF/particle) combining PiCam odometry, IMU, encoders, and ultrasonic beacons.
- Persist lightweight maps (occupancy grid or sparse landmarks) aligned to `state/maps/<session>.json`.
- Expose map summaries to Codex/Ollama prompts so agents plan with spatial context.
- Provide replay tooling to visualise runs and detect drift.

## Milestones
- [ ] Choose map & fusion representation with constraints (CPU, memory).
- [ ] Implement fusion prototype in `src/pxh/fusion/` with unit tests.
- [ ] Build map persistence helpers + CLI (`bin/map-inspect`).
- [ ] Integrate with voice loop context (map highlights in prompts).

## Dependencies
- Reliable timestamped sensor streams.
- Calibration data for encoders and IMU.

## Verification
- Simulation or dry-track walkthrough comparing predicted vs. actual positions.
- Map replay visual validated by operators.


# ===== docs/initiatives/safety-envelope.md =====

# Safety Envelope

## Goal
Guarantee every autonomous action is reversible, observable, and fails safe.

## Scope
- Implement redundant halts: wake-word stop, gesture stop, hardware watchdog tied to Codex loop heartbeat.
- Record intervention logs with timestamps and context (what triggered, what stopped).
- Build regression scenarios (simulation/dry-run) that inject faults and confirm stop path works.
- Layer physical indicators (LED/audio) reflecting robot state.

## Milestones
- [ ] Wake-word + gesture stop prototypes integrated with `tool-stop`.
- [x] Watchdog service monitoring voice loop heartbeat (extend to motion helpers next).
- [ ] Regression test suite (`tests/test_safety.py`) covering triggered stops.
- [ ] LED/audio state feedback mapping documented.

## Dependencies
- Microphone/wake-word reliability under lab noise.
- GPIO control for indicators.

## Verification
- Demonstrated stops in rehearsal and documented logs.
- CI/pytest automation simulating heartbeat loss.
