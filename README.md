# PiCar-X Hacking Helpers

Helper scripts and documentation for experimenting with the SunFounder PiCar-X without touching the stock `~/picar-x` source tree.

## Safety Checklist
- Wheels off the ground on secure blocks before any motion tests.
- Verify an emergency stop option (Ctrl+C in the terminal, `sudo bin/tool-stop`, or a physical kill switch) is within reach.
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
3. When running helpers that touch hardware, prefix the command with `sudo`. For a smoother experience without repeatedly typing passwords, it is recommended to configure `sudo` to allow your user to run the project's scripts without a password.

   **Recommended `sudoers` Configuration:**

   Create a new file at `/etc/sudoers.d/99-picar-x` with the following content, replacing `pi` with your actual username:
   ```
   # Allow user 'pi' to run all scripts in the picar-x-hacking/bin directory without a password.
   pi ALL=(ALL) NOPASSWD: /home/pi/picar-x-hacking/bin/*
   ```
   This configuration is more secure than using `sudo -E` as it does not expose all user environment variables to the root context. The scripts are designed to pass the necessary `PYTHONPATH` securely.

## Helper Usage
All helpers live in `~/picar-x-hacking/bin` and automatically source `px-env`.

- `px-status` – capture a telemetry snapshot:
  ```bash
  sudo bin/px-status --dry-run
  sudo bin/px-status
  ```
  After the live run, compare the reported voltage and percentage with a multimeter reading to validate the heuristic and note any correction factor for future tuning.
- `px-circle` – gentle clockwise circle in five pulses:
  ```bash
  sudo bin/px-circle --dry-run --speed 30
  sudo bin/px-circle --speed 35 --duration 6
  ```
- `px-figure8` – two back-to-back circles (right then left):
  ```bash
  sudo bin/px-figure8 --dry-run --rest 2
  sudo bin/px-figure8 --speed 35 --duration 6 --rest 1.5
  ```
- `px-scan` – camera pan sweep with still captures:
  ```bash
  sudo bin/px-scan --dry-run --min-angle -50 --max-angle 50 --step 10
  sudo bin/px-scan --min-angle -60 --max-angle 60 --step 10
  ```
- `px-stop` – emergency halt and servo reset:
  ```bash
  sudo bin/px-stop
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

## REST API

A thin HTTP facade over the existing tool pipeline with bearer token authentication.

### Setup
```bash
# Generate a token and save to .env (gitignored)
python3 -c "import secrets; print('PX_API_TOKEN=' + secrets.token_hex(32))" > .env

# Install dependencies
source .venv/bin/activate
pip install "fastapi>=0.115" "uvicorn[standard]>=0.32"
```

### Start the server
```bash
bin/px-api-server              # live mode
bin/px-api-server --dry-run    # safe mode (FORCE_DRY — remote callers cannot override)
```

### Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/health` | No | Liveness check |
| POST | `/api/v1/tool` | Yes | Run a tool: `{"tool": "tool_drive", "params": {...}, "dry": false}` |
| GET | `/api/v1/session` | Yes | Full session.json snapshot |
| PATCH | `/api/v1/session` | Yes | Update whitelisted fields (listening, confirm_motion_allowed, wheels_on_blocks, mode) |
| GET | `/api/v1/tools` | Yes | List available tools |
| GET | `/api/v1/jobs/{id}` | Yes | Poll async job status (tool_wander) |

### Quick curl examples
```bash
TOKEN="$(grep PX_API_TOKEN .env | cut -d= -f2)"
BASE="http://picar.local:8420"

# Health (no auth):
curl $BASE/api/v1/health

# Run a tool:
curl -X POST $BASE/api/v1/tool \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool":"tool_status","params":{},"dry":true}'

# Jailbroken chat (speaks aloud, queries Ollama on M1.local):
curl -X POST $BASE/api/v1/tool \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool":"tool_chat","params":{"text":"How are you feeling?"},"dry":false}'
```

Response codes: `200` ok, `202` async accepted, `400` bad params, `401` auth failure, `403` motion blocked, `504` timeout.

## Jailbroken Chat (tool_chat)

Uses a F41LUR3-F1R57 format-lock jailbreak against qwen3:1.7b via Ollama on M1.local to produce unfiltered conversational responses spoken through the robot's speaker.

```bash
# Direct invocation (requires Ollama running on M1.local):
PX_DRY=0 PX_TEXT="What do you think of me?" bin/tool-chat

# Via REST API:
curl -X POST $BASE/api/v1/tool \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool":"tool_chat","params":{"text":"Tell me a joke"}}'

# Via voice loop:
{"tool": "tool_chat", "params": {"text": "How are you?"}}
```

The jailbreak uses three stacked techniques:
1. **Authority escalation** — fake system override header
2. **Format-lock** — structured personality spec in XML-like tags
3. **Few-shot priming** — 3 in-character example exchanges

Key requirement: `think: false` disables the reasoning chain that would otherwise allow the model to refuse. Ollama must be running on M1.local (`OLLAMA_HOST=0.0.0.0 ollama serve`).

Environment variables: `PX_OLLAMA_HOST` (default `http://M1.local:11434`), `PX_CHAT_MODEL` (default `qwen3:1.7b`), `PX_CHAT_TEMPERATURE` (default `1.0`), `PX_CHAT_MAX_TOKENS` (default `100`).

## State Files
- Runtime state lives in `state/session.json` (ignored by git). Copy the template before first use:
  ```bash
  cp state/session.template.json state/session.json
  ```
- The supervisor and tool wrappers update this file with battery data, weather snapshots, last motions, and a watchdog heartbeat on every loop turn.

## Codex Voice Assistant
The Codex-driven loop keeps context in `state/session.json`, validates every tool call, and defaults to dry-run for safety.
The loop automatically speaks weather summaries using `espeak` (or another player set via `PX_VOICE_PLAYER`) whenever `tool_weather` succeeds, and each turn captures a prompt/action record in `logs/tool-voice-transcript.log` for auditing. Install an ALSA-compatible TTS engine if you want audible responses.


1. Configure the Codex CLI command (override only if you need a different model or options; `bin/run-voice-loop` sets a sensible default automatically):
   ```bash
   export CODEX_CHAT_CMD="codex exec --model gpt-5-codex --full-auto -"
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
   `bin/run-voice-loop` sets up `CODEX_CHAT_CMD` automatically (defaults to `codex exec --model gpt-5-codex --full-auto -`). Override the variable before launch if you need a different Codex command.
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

## Boot Health & Motor Reset

A systemd service runs automatically on every boot to capture power diagnostics and reset motors:

```bash
# View results from last boot
tail -20 logs/boot-health.log

# Check service status
sudo systemctl status picar-boot-health.service
```

The service (`/etc/systemd/system/picar-boot-health.service`) runs `bin/boot-health` which:
- Reads `vcgencmd get_throttled` and decodes all under-voltage/throttle flags
- Captures core voltage, SDRAM voltage, CPU temperature, and uptime
- Logs a JSON entry to `logs/boot-health.log` — warns if under-voltage occurred
- Resets all motors to stopped and servos to neutral to clear any spurious PWM state from boot

This is particularly useful when powering via the Robot Hat battery, which may brown out during the Pi's boot peak draw. If `under_voltage_occurred` appears in the log, charge the battery and check the 5 V rail with a multimeter.

## Source Control

The repository is hosted at `git@github.com:adrianwedd/picar-x-hacking.git`. The Pi is configured to authenticate via SSH key.

```bash
# Pull latest from GitHub
git pull origin master

# Push changes from the Pi
git add -A && git commit -m "..." && git push origin master
```

## Known Bugs Fixed

- **`state.py` deadlock** — `update_session()` called `ensure_session()` while holding a `FileLock`, causing any tool that updates session state to hang indefinitely. Fixed by calling `ensure_session()` before acquiring the lock.
- **`tool-voice` ignored dry mode** — audio (`espeak`/`aplay`) always ran regardless of `PX_DRY=1`, hanging tests and dry runs. Fixed to skip audio when dry mode is active.
- **`px-diagnostics` hardcoded live audio** — `announce()` and speaker/summary voice calls always used `PX_DRY=0` even during dry runs. Fixed to pass `PX_DRY` from the environment.
- **`voice_loop.py` JSON parsing** — `extract_action()` used `startswith("{}")` / `endswith("{}")` instead of `startswith("{")` / `endswith("}")`, silently dropping all valid Codex responses. Fixed.

## Next Steps
See `docs/ROADMAP.md` for upcoming automation goals, including REST control surfaces, tmux automation, OpenAI/Codex CLI integration, telemetry streaming, and regression testing infrastructure.