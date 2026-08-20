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
| `run-voice-loop-claude` | Launches the voice loop with Claude Code as the backend. Routes voice turns to the resident `spark-brain` session via `PX_VOICE_BACKEND=brain` (no cold `claude -p`). Uses `docs/prompts/claude-voice-system.md` as the system prompt. |


All motion-capable helpers include `--dry-run` (or honour `PX_DRY`) so you can review planned actions before spinning the wheels. Always confirm the car is on blocks prior to running live motion. Use `sudo -E bin/<script>` to ensure the virtualenv and path configuration remain intact under sudo.

## Durable perception provenance

`px-wander` treats Claude vision descriptions as `model_perception`, not direct
`observation`. A successful interesting description is promoted to a durable note
only after its exploration observation—with stable `explore_id` and
`observation_id`—has been appended successfully. The note references that event as
`exploration:<explore_id>:observation:<observation_id>` and remains bounded to the
kind's `0.65` default and `0.75` ceiling.

That reference proves which perception event grounded the note; it does not prove
the description is semantically correct. Keeping the raw JPEG is optional. Failed
capture/vision, failed event persistence, or invalid evidence prevents promotion.
`tool-recall` voices these records as model interpretations. Learned Frigate
detections must also use `model_perception` if a future writer promotes them into
durable claims.

## Lived-experience contextual preferences

`pxh.contextual_preference` is SPARK's one deliberately narrow adaptation
mechanism. System code records append-only experience events in
`state/preference-experiences-<persona>.jsonl`; each event names one person,
context, offered option, positive/negative outcome, and its existing provenance
record. The current preference is computed from the event history on demand, so
contradiction and age can revise it without rewriting or deleting experience.

Only `observation`, `report`, and `verification` records have behavioral weight.
Every record must cite a non-empty evidence reference, and repeated references
count once. `narrative`, `inference`, `unknown`, and `model_perception` have zero
weight: generated prose or a model's interpretation of a sensor cannot bootstrap
itself into relationship authority. Scoping is exact—an Obi/after-school result
cannot affect Adrian or a weekend choice—and selection is bounded to options the
caller supplied. Existing motion and expression safety gates still apply after a
choice. Supersession is equally scoped: only eligible evidence for the same
person, context, and option may discount an earlier event.

Confidence derives from signed provenance confidence with a 90-day half-life.
Two independent positive experiences and a `0.75` margin over the next option are
required to activate adaptation. Results include every contributing record ID,
kind, source, evidence reference, age, and signed weight, plus ignored-record
diagnostics.

The controlled longitudinal regression freezes clock, model/version, sensor
snapshot, randomness, reflection seed, code, and config while varying history:

```bash
PX_BYPASS_SUDO=1 LOG_DIR=logs_test .venv/bin/python -m pytest \
  tests/test_contextual_preference.py -k longitudinal -v
```

Its checked-in baseline is `tests/fixtures/lived_experience_baseline.json`.
Recall, random variation, prompt/config edits, and model upgrades do not satisfy
this development test.
