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
| `px-race` | Autonomous track racing. Two-phase system: `--calibrate` samples grayscale surfaces + gate threshold + battery voltage, `--map` does a practice lap to build track profile, `--race --laps N` races with per-lap learning. `--status` prints profile summary. `--dry-run` skips motors. `--max-speed N` caps PWM (default 50, hard cap 60). Uses dual PD controllers (grayscale edge + sonar centering), 8-layer safety, and live telemetry to `state/race_live.json`. |
| `px-frigate-stream` | Streams the camera to Frigate/go2rtc using `rpicam-vid` + `ffmpeg` (RTSP push). |
| `tool-weather` | Fetches the latest Bureau of Meteorology observation for the configured product/station (default Grove AWS), falling back from HTTPS to FTP when required and producing a conversational summary for Codex/voice playback. Override with `PX_WEATHER_PRODUCT`, `PX_WEATHER_STATION`, or `PX_WEATHER_URL`. |
| `run-voice-loop` | Convenience launcher that exports `CODEX_CHAT_CMD` (default `codex exec --full-auto -`) and executes `codex-voice-loop` with supplied flags. |
| `run-voice-loop-ollama` | Wrapper that pins `CODEX_CHAT_CMD` to `bin/codex-ollama`, defaults `CODEX_OLLAMA_MODEL` to `deepseek-coder:1.3b`, and applies the tuned env overrides (`CODEX_OLLAMA_TEMPERATURE=0.2`, `CODEX_OLLAMA_NUM_PREDICT=64`). |
| `codex-ollama` | Reads a Codex prompt from stdin, posts it to the local Ollama HTTP API, normalises tool JSON, and honours `CODEX_OLLAMA_MODEL`, `CODEX_OLLAMA_TEMPERATURE`, and `CODEX_OLLAMA_NUM_PREDICT`. |
| `px-voice-report` | Summarises `logs/tool-voice-transcript.log` (tool counts, voice success/failure, battery warnings) in text or JSON form. |
| `px-health-report` | Rolls up the latest entries from `logs/tool-health.log` to highlight battery, sensor, and audio status. Supports `--json`. |
| `px-session` | Creates a tmux workspace with the voice loop, wake controller, and log tail panes; supports `--plan` to print the layout without launching tmux. |
| `codex-voice-loop` | Supervisor that pipes transcripts through the Codex CLI, parses JSON tool requests, enforces allowlists/ranges, executes wrappers, and records a watchdog heartbeat in `state/session.json`. |

| `tool-chat` | Jailbroken conversational AI via Ollama (gemma4:e4b on M5.local). Sends user text through a F41LUR3-F1R57 format-lock jailbreak prompt, cleans the response, and speaks it aloud. Logs full prompt/response to `logs/tool-chat.log`. Env: `PX_TEXT` (required), `PX_OLLAMA_HOST`, `PX_CHAT_MODEL`, `PX_CHAT_TEMPERATURE`, `PX_CHAT_MAX_TOKENS`. |
| `px-api-server` | Launches the REST API (FastAPI + uvicorn) on port 8420. Sources `px-env` and `.env` (for `PX_API_TOKEN`). Supports `--dry-run`, `--port`, `--host`. Must always be used instead of bare uvicorn. |
| `tool-api-start` | Daemonises `px-api-server` in the background; writes PID to `logs/px-api-server.pid`. Respects `PX_DRY`. |
| `tool-api-stop` | Sends SIGTERM to the API server via PID file; waits for clean shutdown. |
| `run-voice-loop-claude` | Wrapper that pins `CODEX_CHAT_CMD` to `bin/claude-voice-bridge` and uses `docs/prompts/claude-voice-system.md` as the system prompt. |
| `claude-voice-bridge` | Thin adapter that pipes the voice loop prompt into `claude -p` with no tools and plain-text output, allowing Claude Code to serve as the LLM backend for the voice loop. |

All motion-capable helpers include `--dry-run` (or honour `PX_DRY`) so you can review planned actions before spinning the wheels. Always confirm the car is on blocks prior to running live motion. Use `sudo -E bin/<script>` to ensure the virtualenv and path configuration remain intact under sudo.
