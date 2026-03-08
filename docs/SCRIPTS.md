# picar-x-hacking — Script Reference

Comprehensive documentation for every script in `bin/` and every module in `src/pxh/`.

---

## Table of Contents

1. [Environment and Configuration](#environment-and-configuration)
   - [bin/px-env](#binpx-env)
2. [Boot and System Health](#boot-and-system-health)
   - [bin/boot-health](#binboot-health)
   - [bin/px-diagnostics](#binpx-diagnostics)
   - [bin/px-health-report](#binpx-health-report)
   - [bin/px-status](#binpx-status)
3. [Motion — Direct Actuators](#motion--direct-actuators)
   - [bin/px-circle](#binpx-circle)
   - [bin/px-figure8](#binpx-figure8)
   - [bin/px-stop](#binpx-stop)
   - [bin/px-scan](#binpx-scan)
   - [bin/px-sonar](#binpx-sonar)
4. [Camera and Gaze](#camera-and-gaze)
   - [bin/px-look](#binpx-look)
   - [bin/px-emote](#binpx-emote)
   - [bin/px-perform](#binpx-perform)
   - [bin/px-alive](#binpx-alive)
5. [Line Following](#line-following)
   - [bin/px-line-follow](#binpx-line-follow)
   - [bin/run-line-follow](#binrun-line-follow)
6. [Speech and Audio](#speech-and-audio)
   - [bin/tool-voice](#bintool-voice)
   - [bin/transcribe-whisper](#bintranscribe-whisper)
7. [External Data Tools](#external-data-tools)
   - [bin/tool-weather](#bintool-weather)
8. [Tool Wrappers — Motion with Logging](#tool-wrappers--motion-with-logging)
   - [bin/tool-circle](#bintool-circle)
   - [bin/tool-figure8](#bintool-figure8)
   - [bin/tool-stop](#bintool-stop)
   - [bin/tool-look](#bintool-look)
   - [bin/tool-emote](#bintool-emote)
   - [bin/tool-sonar](#bintool-sonar)
   - [bin/tool-perform](#bintool-perform)
   - [bin/tool-status](#bintool-status)
9. [Voice Assistant Loop](#voice-assistant-loop)
   - [bin/codex-voice-loop](#bincodex-voice-loop)
   - [bin/run-voice-loop](#binrun-voice-loop)
   - [bin/run-voice-loop-claude](#binrun-voice-loop-claude)
   - [bin/run-voice-loop-ollama](#binrun-voice-loop-ollama)
   - [bin/claude-voice-bridge](#binclaude-voice-bridge)
   - [bin/codex-ollama](#bincodex-ollama)
10. [Wake Word and STT](#wake-word-and-stt)
    - [bin/px-wake-listen](#binpx-wake-listen)
    - [bin/run-wake](#binrun-wake)
    - [bin/px-wake](#binpx-wake)
11. [Scheduled Announcements](#scheduled-announcements)
    - [bin/px-cron-say](#binpx-cron-say)
    - [bin/px-voice-report](#binpx-voice-report)
12. [Compound Routines](#compound-routines)
    - [bin/px-dance](#binpx-dance)
13. [Streaming](#streaming)
    - [bin/px-frigate-stream](#binpx-frigate-stream)
14. [Session Management](#session-management)
    - [bin/px-session](#binpx-session)
15. [Python Library — src/pxh/](#python-library--srcpxh)
    - [src/pxh/state.py](#srcpxhstatepy)
    - [src/pxh/logging.py](#srcpxhloggingpy)
    - [src/pxh/time.py](#srcpxhtimepy)
    - [src/pxh/voice_loop.py](#srcpxhvoice_looppy)

---

## Environment and Configuration

### bin/px-env

**Purpose:** Shared environment bootstrap. Sourced by every other script to establish consistent paths and settings.

**Usage:**
```bash
source bin/px-env
```

**What it does:**
1. Resolves `PROJECT_ROOT` as the directory one level above `bin/`.
2. Sets `LOG_DIR` (default: `$PROJECT_ROOT/logs`), creating it if absent.
3. Builds `PYTHONPATH` with `$PROJECT_ROOT/src` and `/home/pi/picar-x` prepended (deduplication safe).
4. Activates the `.venv` virtualenv if present and not already active (via `VIRTUAL_ENV` test).
5. Exports `PX_VOICE_DEVICE` (default: `robothat` — the robot_hat ALSA plug device for the HifiBerry DAC).

**Key exports:**
| Variable | Default | Description |
|---|---|---|
| `PROJECT_ROOT` | directory above `bin/` | Absolute root of the project |
| `LOG_DIR` | `$PROJECT_ROOT/logs` | All log files go here |
| `PYTHONPATH` | `$PROJECT_ROOT/src:$PI_HOME/picar-x` | Python search path |
| `PX_VOICE_DEVICE` | `robothat` | ALSA device for audio output |

**Note:** Scripts that need GPIO/picarx run under `sudo`. The `sudo` call must re-export these variables explicitly since `sudo` drops the environment by default.

---

## Boot and System Health

### bin/boot-health

**Purpose:** Run at boot (e.g. via systemd or cron `@reboot`) to record hardware health and reset motors to a safe neutral state.

**Usage:**
```bash
bin/boot-health
```

**What it does:**
1. Reads the Raspberry Pi throttle register via `vcgencmd get_throttled` and decodes it into human-readable flag names (under-voltage, frequency-capped, throttled, soft-temperature-limit — both current and historical).
2. Reads core voltage, SDRAM voltage, and CPU temperature via `vcgencmd`.
3. Reads system uptime from `/proc/uptime`.
4. Logs all of the above as a structured JSON record to `logs/boot-health.log`.
5. If under-voltage flags are set, writes a WARN record and echoes a warning to stderr.
6. Runs `/usr/bin/python3` (system Python, which has `robot_hat`/`picarx`) to call `px.stop()`, `set_dir_servo_angle(0)`, `set_cam_pan_angle(0)`, `set_cam_tilt_angle(0)` — clearing any spurious PWM state left by the bootloader. Uses an `os.getlogin` monkey-patch so the picarx fileDB initializer doesn't crash under systemd (no controlling terminal).

**Log format:** JSON-lines to `$LOG_DIR/boot-health.log`. Example:
```json
{"ts":"2026-03-08T10:00:00Z","level":"INFO","msg":"Boot health check","throttled_raw":"0x0","throttled_flags":"none","v_core":"1.2000V","cpu_temp":"42.0'C","uptime_sec":12}
```

**Dependencies:** `vcgencmd` (Raspberry Pi firmware tool), `/usr/bin/python3` with `picarx`.

---

### bin/px-diagnostics

**Purpose:** Comprehensive hardware diagnostics sweep. Checks battery, sensors, camera, microphone, speaker, and optionally runs a motion test. Produces a summary spoken aloud and a JSON report logged to disk.

**Usage:**
```bash
bin/px-diagnostics [--no-motion] [--short] [--dry-run | --live]
```

**Arguments:**
| Flag | Description |
|---|---|
| `--no-motion` | Skip the circle motor test (useful when wheels can't spin freely) |
| `--short` | Skip weather fetch and camera capture (faster) |
| `--dry-run` | Force `PX_DRY=1` for all sub-tools |
| `--live` | Force `PX_DRY=0` even if env says dry |

**Sequence of checks:**
1. **Status** (`tool-status`): battery voltage/%, grayscale, ultrasonic distance.
2. **Weather** (`tool-weather`): fetches Australian BOM weather feed (skipped with `--short`).
3. **Motion** (`tool-circle --speed 25 --duration 2`): brief motor test (skipped in dry-run or with `--no-motion`).
4. **Camera** (`rpicam-still`): captures a test frame to a temp file (skipped in dry-run or `--short`).
5. **Speaker** (`tool-voice "Speaker diagnostic"`): checks espeak+aplay pipeline.
6. **Microphone** (`arecord -d 2 -f cd`): 2-second test recording.
7. **Voice summary**: reads a human-friendly summary of all results aloud via `tool-voice`.

**Output:** Prints JSON to stdout and logs events to `logs/tool-health.log` via `pxh.logging.log_event`.

**Environment variables:**
| Variable | Effect |
|---|---|
| `PX_DRY` | `1` = dry-run all sub-tools |
| `PX_BYPASS_SUDO` | `1` = skip sudo in sub-tools (for testing) |

---

### bin/px-health-report

**Purpose:** Read and display the most recent diagnostics telemetry from `logs/tool-health.log`. Useful for a quick dashboard view of the last health run.

**Usage:**
```bash
bin/px-health-report [--limit N] [--json]
```

**Arguments:**
| Flag | Description |
|---|---|
| `--limit N` | Show N most recent log entries (default: 1) |
| `--json` | Emit JSON instead of human-readable text |

**Output (text mode):**
```
[2026-03-08T10:00:00Z] status=ok dry=False battery=82% voltage=7.8 motors_ok=True speaker_ok=True
```

**Note:** This is a pure Python script with no `source px-env` — it resolves `PROJECT_ROOT` from its own file path and adds `src/` to `sys.path` directly.

---

### bin/px-status

**Purpose:** Display a telemetry snapshot of the PiCar-X hardware: servo offsets, motor calibration, ultrasonic distance, grayscale sensor readings, and battery estimate.

**Usage:**
```bash
bin/px-status [--dry-run] [--config PATH] [--battery-channel CHAN] [--samples N]
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--dry-run` | — | Skip hardware probing; only show config values |
| `--config` | `/opt/picar-x/picar-x.conf` | Config file with servo calibration |
| `--battery-channel` | `A4` | ADC channel for battery voltage |
| `--samples` | `5` | Number of ADC samples to average |

**Output (stdout, human-readable):**
```
PiCar-X Telemetry Snapshot
Generated: 2026-03-08T10:05:23
Config: /opt/picar-x/picar-x.conf (mtime ...)
Servo offsets (degrees): steering=... pan=... tilt=...
Motor direction calibration: ...
Ultrasonic distance: 42.3 cm
Grayscale readings: [850.0, 900.0, 860.0]
Battery estimate: 7.82 V (~82% full) [channel A4]
```

**Battery formula:** `voltage = ADC / 4095 * 3.3 * 2` (robot_hat divides by 2). Percent: `(voltage - 6.0) / (8.2 - 6.0) * 100`.

**Environment:**
| Variable | Default | Description |
|---|---|---|
| `PX_CONFIG` | `/opt/picar-x/picar-x.conf` | Config override |
| `PX_BATTERY_ADC` | `A4` | ADC channel override |

---

## Motion — Direct Actuators

### bin/px-circle

**Purpose:** Drive the robot in a continuous clockwise circle pattern using pulsed forward motion with a fixed steering angle.

**Usage:**
```bash
bin/px-circle [--speed N] [--duration S] [--dry-run]
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--speed` | `35` | Wheel speed 0–100% |
| `--duration` | `6.0` | Total run time in seconds |
| `--dry-run` | — | Log plan without moving |

**Motion pattern:** Divides `duration` into 5 pulses. Each pulse: steer at 20°, drive `pulse_time` seconds, stop, coast `coast_time` seconds (up to 0.5s). Resets steering to 0° in the `finally` block.

**Log file:** `logs/px-circle.log`

**Dependencies:** `picarx` (system Python via sudo).

**Exit codes:** 0 = success, 130 = keyboard interrupt, 1 = import/runtime error.

---

### bin/px-figure8

**Purpose:** Drive the robot in a figure-eight pattern: one clockwise circle leg then one counter-clockwise circle leg.

**Usage:**
```bash
bin/px-figure8 [--speed N] [--duration S] [--rest S] [--dry-run]
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--speed` | `35` | Wheel speed 0–100% |
| `--duration` | `6.0` | Duration per circle leg |
| `--rest` | `1.5` | Pause between legs |
| `--dry-run` | — | Log plan without moving |

**Motion pattern:** Right leg (steering +20°) for 5 pulses, rest pause, left leg (steering -20°) for 5 pulses. Resets steering to 0° after.

**Log file:** `logs/px-figure8.log`

---

### bin/px-stop

**Purpose:** Emergency stop. Sends two `stop()` calls to the motors and resets all servos to neutral (steering=0°, pan=0°, tilt=0°).

**Usage:**
```bash
bin/px-stop
```

**No arguments. No dry-run mode.** Always stops the hardware immediately.

**Why two `stop()` calls:** Robot HAT firmware may need a second call to fully zero the PWM duty cycle.

**Dependencies:** `picarx` (system Python).

---

### bin/px-scan

**Purpose:** Pan-and-capture photographic scan. Sweeps the camera across a range of pan angles and captures a still JPEG at each position using `rpicam-still`.

**Usage:**
```bash
bin/px-scan [--min-angle N] [--max-angle N] [--step N] [--settle S] [--dry-run]
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--min-angle` | `-60` | Leftmost pan angle (degrees) |
| `--max-angle` | `60` | Rightmost pan angle (degrees) |
| `--step` | `10` | Step size between positions |
| `--settle` | `0.4` | Settle time after pan before capture |
| `--dry-run` | — | Log plan without capturing |

**Output:** JPEG files saved to `logs/scans/YYYYMMDDTHHMMSS/frame_+NNN.jpg` (one per angle). Resets pan to 0° after scan.

**Log file:** `logs/px-scan.log`

**Dependencies:** `rpicam-still`, `picarx`.

---

### bin/px-sonar

**Purpose:** Ultrasonic sweep scan. Pans the camera servo across angles and samples the ultrasonic distance sensor at each position to produce a distance profile.

**Usage:**
```bash
bin/px-sonar [--steps N] [--pan-min F] [--pan-max F] [--tilt F] [--settle S] [--samples N] [--dry-run]
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--steps` | `7` | Number of pan positions |
| `--pan-min` | `-60` | Leftmost pan angle |
| `--pan-max` | `60` | Rightmost pan angle |
| `--tilt` | `0` | Fixed tilt angle during scan |
| `--settle` | `0.15` | Settle time per position (s) |
| `--samples` | `3` | Distance samples averaged per position |
| `--dry-run` | — | Return dummy readings |

**Output (JSON, stdout):**
```json
{"status":"ok","dry":false,"closest_angle":-30,"closest_cm":42.1,"readings":[[-60,99.0],[-40,42.1],...]}
```

**Dry-run output:** Returns `"dry":true` with `-1.0` for all distances.

**Log file:** `logs/px-sonar.log`

---

## Camera and Gaze

### bin/px-look

**Purpose:** Move the camera pan/tilt servos to a specific angle with smooth easing. Optionally hold at the target.

**Usage:**
```bash
bin/px-look [--pan F] [--tilt F] [--from-pan F] [--from-tilt F] [--ease S] [--hold S] [--dry-run]
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--pan` | `0` | Target pan angle (-90 to 90°) |
| `--tilt` | `0` | Target tilt angle (-35 to 65°) |
| `--from-pan` | `0` | Starting pan for easing |
| `--from-tilt` | `0` | Starting tilt for easing |
| `--ease` | `0.8` | Easing duration in seconds |
| `--hold` | `0.0` | Hold duration at target |
| `--dry-run` | — | Log plan without moving |

**Easing:** Linear interpolation at 20 steps/second over `ease` seconds.

**Log file:** `logs/px-look.log`

---

### bin/px-emote

**Purpose:** Move the camera to a named emotional pose with animated variations.

**Usage:**
```bash
bin/px-emote [NAME] [--from-pan F] [--from-tilt F] [--dry-run]
```

**Named poses:**
| Name | Pan | Tilt | Animation |
|---|---|---|---|
| `idle` | 0° | 0° | none |
| `curious` | 25° | 18° | slight tilt nod |
| `thinking` | -22° | -8° | none |
| `happy` | 0° | 12° | side-to-side sweep ×2 |
| `alert` | 0° | 0° | quick move |
| `excited` | 0° | 15° | rapid pan sweeps to ±35°, ±25°, 0° |
| `sad` | -10° | -20° | slow ease |
| `shy` | -40° | 5° | none |

**Log file:** `logs/px-emote.log`

---

### bin/px-perform

**Purpose:** Multi-step choreography engine. Executes a sequence of steps, each of which can simultaneously speak text and move camera servos (run in parallel threads).

**Usage:**
```bash
PX_PERFORM_STEPS='[{"speak":"Hello!","emote":"happy","pause":1.0}]' bin/px-perform [--dry-run]
```

**Step schema:**
```json
{
  "speak": "text to say",
  "emote": "happy",
  "look":  {"pan": 30, "tilt": 10},
  "pause": 1.5,
  "ease":  0.8
}
```

- `speak` and servo movement run simultaneously in two daemon threads; the step waits for both.
- `emote` and `look` are mutually exclusive (emote takes priority if both are given).
- `ease` controls servo easing duration (default 0.8s).
- `pause` adds a hold after the step completes.

**Speech engine:** `espeak --stdout | aplay`. Calls `robot_hat.enable_speaker()` first (GPIO 20 HIGH for amp).

**Input:** `PX_PERFORM_STEPS` environment variable containing a JSON array.

**Log file:** `logs/px-perform.log` (via stderr).

---

### bin/px-alive

**Purpose:** Long-running idle daemon that keeps the robot "alive" with organic camera servo motion.

**Usage:**
```bash
sudo bin/px-alive [--gaze-min S] [--gaze-max S] [--prox-cm F] [--no-prox] [--dry-run]
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--gaze-min` | `10` | Minimum seconds between gaze drift events |
| `--gaze-max` | `25` | Maximum seconds between gaze drift events |
| `--prox-cm` | `35.0` | Proximity threshold in cm to trigger reaction |
| `--no-prox` | — | Disable ultrasonic proximity sensing |
| `--dry-run` | — | Log planned moves without actuating |

**Behaviours (loop runs every 0.5s):**
1. **Gaze drift** (every 10–25s): Ease to a random (pan, tilt) within (-65°…65°, -15°…35°) over 0.8–2.2s.
2. **Slow scan** (every 3–8 min): Full pan sweep left→right→left in 15° steps at 0.6s ease each, then return to centre.
3. **Proximity reaction**: If ultrasonic < 35cm for 3 consecutive seconds, ease to forward-facing (pan=0°, tilt=5°).

**PID file:** `$LOG_DIR/px-alive.pid`. Removed on SIGTERM/SIGINT.

**Log file:** `$LOG_DIR/px-alive.log`

**Note:** Requires sudo because `picarx` needs GPIO. Uses `/usr/bin/python3` (system Python with `robot_hat`/`picarx`).

---

## Line Following

### bin/px-line-follow

**Purpose:** Autonomous boundary-detection line follower. Uses the CSI OV5647 camera and OpenCV HSV masking to detect coloured track boundaries, then steers towards the inferred track centre using a PD controller.

**Usage:**
```bash
bin/px-line-follow [--dry-run] [--debug-every N] [--debug-dir PATH] [--cam-tilt N] [--kp F] [--kd F] [--snapshot]
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--dry-run` | — | Capture and compute but don't move |
| `--debug-every N` | `30` | Save annotated JPEG every N frames (0 = off) |
| `--debug-dir` | `logs/line-debug/` | Directory for debug frames |
| `--cam-tilt` | `-15` | Camera tilt angle in degrees (negative = down) |
| `--kp` | `0.35` | Proportional gain |
| `--kd` | `0.15` | Derivative gain |
| `--snapshot` | — | Capture one frame, save debug image, then exit (for threshold tuning) |

**Track assumptions:** Grey surface, red and/or white kerbs on each side.

**Algorithm:**
1. Capture BGR frame at 640×480 from CSI camera (via `picamera2`, 15fps target).
2. Convert to HSV; mask red (two hue ranges: 0–10° and 165–179°) and white (low sat, high value).
3. For each of 5 horizontal scan rows `[380, 340, 300, 260, 220]` (bottom = near, top = far), find leftmost and rightmost boundary pixel.
4. Compute weighted average lateral error (bottom rows weighted 5×, top rows 1×).
5. PD controller: `steer = -(Kp * error + Kd * d_error/dt)`, clamped to ±35°.
6. Speed scheduling from maximum pixel shift between adjacent row centres:
   - shift < 30px → 60% (straight)
   - 30–70px → 40% (curve)
   - ≥70px → 25% (hairpin)
7. Safety: if no boundary detected for 2 seconds, stop and exit.

**Debug frames:** Annotated JPEG with green boundary mask overlay, coloured dots at boundary edges and track centres, error lines, and a text overlay showing steer and speed.

**Log file:** `$LOG_DIR/px-line-follow.log`

**Dependencies:** `picamera2`, `cv2` (OpenCV), `numpy`. Uses `/usr/bin/python3` (system Python, run under sudo via `run-line-follow`).

---

### bin/run-line-follow

**Purpose:** Launcher wrapper for `px-line-follow`. Adds sudo with the required environment variables.

**Usage:**
```bash
bin/run-line-follow [--dry-run] [--snapshot] [--kp 0.35] [--kd 0.15] [--help]
```

**What it does:**
```bash
exec sudo -n \
    PYTHONPATH="$PYTHONPATH" \
    PROJECT_ROOT="$PROJECT_ROOT" \
    LOG_DIR="$LOG_DIR" \
    PX_DRY="${PX_DRY:-0}" \
    bash "$SCRIPT_DIR/px-line-follow" "$@"
```

Pass `PX_DRY=1 bin/run-line-follow --snapshot` to test HSV thresholds without wheel motion.

---

## Speech and Audio

### bin/tool-voice

**Purpose:** AI tool interface for text-to-speech. Reads `PX_TEXT` from the environment and speaks it via `espeak+aplay`. Logs results and updates session state.

**Usage:**
```bash
PX_TEXT="Hello world" PX_DRY=0 bin/tool-voice
```

**Environment variables:**
| Variable | Required | Description |
|---|---|---|
| `PX_TEXT` | Yes | Text to speak (max 2000 chars in voice_loop) |
| `PX_DRY` | No (default `1`) | `0` = actually speak, `1` = dry-run |
| `PX_VOICE_DEVICE` | No | ALSA device (default: `robothat`) |
| `PX_VOICE_RATE` | No | espeak speaking rate in wpm (default: `150`) |
| `PX_VOICE_PLAYER` | No | Override speech engine (`espeak` or `say`) |

**Speech pipeline:**
1. Enable robot-hat speaker amp: `sudo -n python3 -c "from robot_hat import enable_speaker; enable_speaker()"`.
2. `espeak -s RATE --stdout "TEXT"` → pipe to `aplay -q [-D DEVICE]`.

**Output (JSON, stdout):**
```json
{"status":"ok","dry":false,"text":"Hello world","player":"espeak+aplay(robothat)","returncode":0}
```

**Log:** `logs/tool-voice.log` (via `pxh.logging.log_event`). Session updated with `last_action = "tool_voice"`.

---

### bin/transcribe-whisper

**Purpose:** Record audio from a microphone and transcribe it using OpenAI Whisper (tiny model). Used as a `--transcriber-cmd` for the voice loop when Whisper is available.

**Usage:**
```bash
bin/transcribe-whisper
```

**Environment variables:**
| Variable | Default | Description |
|---|---|---|
| `PX_STT_CARD` | auto-detect first card | ALSA card number |
| `PX_STT_DEVICE` | `0` | ALSA device number |
| `PX_STT_DURATION` | `4` | Recording duration in seconds |

**Process:**
1. Records `$PX_STT_DURATION` seconds via `arecord` to a temp WAV file.
2. Runs `python -m whisper` (from venv if available) with `--model tiny --language en`.
3. Prints the transcript text to stdout (spaces stripped).
4. On any failure, prints empty string and exits 1.

**Performance note:** Whisper tiny on Raspberry Pi 4 takes ~9s for a 4s clip (2× real-time). Consider `px-wake-listen` + sherpa-onnx instead for latency-sensitive use.

---

## External Data Tools

### bin/tool-weather

**Purpose:** Fetch current weather observations from the Australian Bureau of Meteorology (BOM) and produce a speech-ready summary.

**Usage:**
```bash
PX_DRY=0 bin/tool-weather
```

**Data sources (tried in order):**
1. `https://reg.bom.gov.au/fwo/{product}/{product}.{station}.json` (HTTPS primary)
2. `ftp://ftp.bom.gov.au/anon/gen/fwo/{product}.{station}.json` (FTP fallback)
3. `https://www.bom.gov.au/fwo/...` (alternate HTTPS)

**Environment variables:**
| Variable | Default | Description |
|---|---|---|
| `PX_DRY` | `1` | Skip network request if `1` |
| `PX_WEATHER_PRODUCT` | `IDT60801` | BOM product code |
| `PX_WEATHER_STATION` | `95977` | BOM station ID |
| `PX_WEATHER_TIMEOUT` | `10` | Fetch timeout in seconds |
| `PX_WEATHER_URL` | — | Override URL (disables fallback chain) |

**Output (JSON, stdout):**
```json
{
  "status": "ok",
  "station": "Hobart Airport",
  "temp_C": 18.5,
  "apparent_temp_C": 17.2,
  "wind_dir": "NW",
  "wind_kmh": 15,
  "humidity_pct": 72,
  "rain_24h_mm": 0,
  "summary": "At Hobart Airport, it's 18 degrees Celsius. Winds are NW at 15 kilometres per hour. Humidity is 72 percent."
}
```

The `summary` field is a naturally-worded string suitable for `espeak`.

**Session update:** Stores `last_weather` dict (including `summary`) in `session.json`.

**Log:** `logs/tool-weather.log`.

---

## Tool Wrappers — Motion with Logging

The `tool-*` scripts are thin wrappers that:
- Read parameters from environment variables.
- Apply safety limits (clamp speed, duration, etc.).
- Check `session.json` for `confirm_motion_allowed` before executing motion (motion tools only).
- Call the corresponding `px-*` binary under `sudo -n` (unless `PX_BYPASS_SUDO=1`).
- Log to `pxh.logging.log_event` and update `session.json`.
- Emit a JSON result to stdout.

### bin/tool-circle

Wraps `bin/px-circle`.

**Environment variables:**
| Variable | Default | Description |
|---|---|---|
| `PX_DRY` | `1` | Dry-run mode |
| `PX_SPEED` | `30` | Speed, clamped to 0–60% |
| `PX_DURATION` | `6` | Duration, clamped to 1–12s |

**Motion gate:** Blocked (returns `status: blocked`, exit 2) if `confirm_motion_allowed` is `false` in session and not in dry-run.

**Log:** `logs/tool-circle.log`.

---

### bin/tool-figure8

Wraps `bin/px-figure8`.

**Environment variables:**
| Variable | Default | Description |
|---|---|---|
| `PX_DRY` | `1` | Dry-run mode |
| `PX_SPEED` | `30` | Speed, clamped to 0–60% |
| `PX_DURATION` | `6` | Duration per leg, clamped to 1–12s |
| `PX_REST` | `1.5` | Rest between legs, clamped to 0–5s |

**Motion gate:** Same as `tool-circle`.

**Log:** `logs/tool-figure8.log`.

---

### bin/tool-stop

Wraps `bin/px-stop`.

**Environment variables:**
| Variable | Default | Description |
|---|---|---|
| `PX_DRY` | `1` | Dry-run skips the actual stop command |

**Dry-run:** Returns `{"status":"ok","dry":true,"message":"stop skipped in dry-run"}` without calling hardware.

**Session update:** Clears `last_motion` to `null`.

**Log:** `logs/tool-stop.log`.

---

### bin/tool-look

Wraps `bin/px-look`.

**Environment variables:**
| Variable | Default | Description |
|---|---|---|
| `PX_DRY` | `1` | Dry-run mode |
| `PX_PAN` | `0` | Pan angle (clamped to -90…90°) |
| `PX_TILT` | `0` | Tilt angle (clamped to -35…65°) |
| `PX_EASE` | `0.8` | Easing duration in seconds |
| `PX_HOLD` | `0.0` | Hold duration at target |

**Log:** `logs/tool-look.log`.

---

### bin/tool-emote

Wraps `bin/px-emote`.

**Environment variables:**
| Variable | Default | Description |
|---|---|---|
| `PX_DRY` | `1` | Dry-run mode |
| `PX_EMOTE` | `idle` | Named emote: `idle`, `curious`, `thinking`, `happy`, `alert`, `excited`, `sad`, `shy` |

Returns `status: error` with `valid` list if emote name is unknown.

**Log:** `logs/tool-emote.log`.

---

### bin/tool-sonar

Wraps `bin/px-sonar`.

**Environment variables:**
| Variable | Default | Description |
|---|---|---|
| `PX_DRY` | `1` | Pass `--dry-run` to px-sonar |

Parses the JSON from `px-sonar` stdout and re-emits it, also logging the `closest_cm` value.

**Log:** `logs/tool-sonar.log`.

---

### bin/tool-perform

Wraps `bin/px-perform`.

**Environment variables:**
| Variable | Required | Description |
|---|---|---|
| `PX_DRY` | No (default `1`) | Dry-run mode |
| `PX_PERFORM_STEPS` | Yes | JSON array of step objects |

**Limits:** Truncates steps list to 12 steps maximum. Each step's `speak` text is truncated to 200 chars.

**Log:** `logs/tool-perform.log`.

---

### bin/tool-status

Wraps `bin/px-status` (via `sudo -n`).

**Environment variables:**
| Variable | Default | Description |
|---|---|---|
| `PX_DRY` | `1` | Pass `--dry-run` to px-status |
| `PX_BATTERY_THRESHOLD` | `30` | Battery % below which `battery_ok=false` |

**Output:** JSON with `battery_pct`, `battery_ok`, and the full stdout from `px-status`.

**Session update:** Writes `battery_pct` and `battery_ok` to `session.json`.

**Log:** `logs/tool-status.log`.

---

## Voice Assistant Loop

### bin/codex-voice-loop

**Purpose:** Entry point for the voice assistant supervisor loop. Delegates to `python -m pxh.voice_loop` after sourcing `px-env`.

**Usage:**
```bash
bin/codex-voice-loop [all arguments forwarded to voice_loop.py]
```

This is the generic entry point; use one of the `run-voice-loop-*` wrappers which set the appropriate `CODEX_CHAT_CMD` before calling this script.

---

### bin/run-voice-loop

**Purpose:** Launch the Codex CLI voice loop. Sets `CODEX_CHAT_CMD` to `"codex exec --model gpt-5-codex --full-auto -"` if not already set in the environment.

**Usage:**
```bash
bin/run-voice-loop [--input-mode text|voice] [--dry-run] [--auto-log] [--max-turns N] [...]
```

**Environment:**
| Variable | Effect |
|---|---|
| `CODEX_CHAT_CMD` | Override the Codex invocation command |
| `PX_DRY` | Force dry-run across all tools |

---

### bin/run-voice-loop-claude

**Purpose:** Launch the voice loop using Claude Code (`claude -p`) as the LLM backend instead of Codex CLI.

**Usage:**
```bash
bin/run-voice-loop-claude [--input-mode text|voice] [--dry-run] [--max-turns N] [...]
```

**What it does:**
1. Sets `CODEX_CHAT_CMD="$SCRIPT_DIR/claude-voice-bridge"` — substitutes Claude for Codex.
2. Passes `--prompt docs/prompts/claude-voice-system.md` as the system prompt.
3. Calls `codex-voice-loop` with all remaining arguments forwarded.

**Example (single non-interactive turn):**
```bash
echo "check status" | bin/run-voice-loop-claude --dry-run --max-turns 1
```

---

### bin/run-voice-loop-ollama

**Purpose:** Launch the voice loop using a local Ollama model as the LLM backend.

**Usage:**
```bash
bin/run-voice-loop-ollama [same flags as run-voice-loop]
```

**Environment:**
| Variable | Default | Description |
|---|---|---|
| `CODEX_CHAT_CMD` | `codex-ollama` | Adapter script (set automatically) |
| `CODEX_OLLAMA_MODEL` | `deepseek-coder:1.3b` | Ollama model name |
| `CODEX_OLLAMA_TEMPERATURE` | `0.2` | Sampling temperature |
| `CODEX_OLLAMA_NUM_PREDICT` | `64` | Max tokens to generate |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama server URL |

---

### bin/claude-voice-bridge

**Purpose:** stdin→stdout adapter that lets `voice_loop.py` use Claude Code (`claude -p`) as its LLM backend.

**Usage:** Called by `run-voice-loop-claude` via `CODEX_CHAT_CMD`. Not typically invoked directly.

**What it does:**
1. Unsets `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` so that a nested `claude -p` invocation is permitted from within a Claude Code session.
2. Reads the full prompt from stdin.
3. Calls:
   ```bash
   claude -p "$PROMPT" \
     --system-prompt "You are a robot assistant controller..." \
     --allowedTools "" \
     --output-format text \
     --no-session-persistence
   ```
4. Claude's response (JSON action object) goes to stdout, which `voice_loop.py` reads.

**Why `--allowedTools ""`:** Prevents Claude from invoking file/shell tools; all robot actions go through the JSON tool dispatch system instead.

---

### bin/codex-ollama

**Purpose:** LLM adapter that reads a prompt from stdin and calls a local Ollama HTTP API, normalising the response to the tool-dispatch JSON format.

**Usage:** Called as `CODEX_CHAT_CMD` by `run-voice-loop-ollama`.

**What it does:**
1. Reads the full prompt from stdin.
2. Posts to `$OLLAMA_HOST/api/generate` with `stream: false` and `format: "json"`.
3. If the response is a valid JSON with a recognised `tool` name (one of `ALLOWED_TOOLS`), normalises it to `{"tool": "...", "params": {...}}`.
4. Writes the (possibly normalised) response to stdout.

**Allowed tools:** `tool_status`, `tool_circle`, `tool_figure8`, `tool_stop`, `tool_voice`, `tool_weather`.

**Environment variables:**
| Variable | Default | Description |
|---|---|---|
| `CODEX_OLLAMA_MODEL` | `deepseek-coder:1.3b` | Model to use |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama server |
| `CODEX_OLLAMA_TEMPERATURE` | `0.2` | Temperature |
| `CODEX_OLLAMA_NUM_PREDICT` | `64` | Max tokens |

---

## Wake Word and STT

### bin/px-wake-listen

**Purpose:** Always-on wake-word listener daemon. Uses Vosk grammar-based recognition for low-CPU wake detection, then records the user's utterance and transcribes it with sherpa-onnx (Zipformer, int8) for high-accuracy STT. Pipes the transcript to the voice loop.

**Usage:**
```bash
bin/px-wake-listen --model-dir PATH --stt-model-dir PATH --wake-word "hey robot" [--aplay-device DEV] [--dry-run]
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--model-dir` | `models/vosk-model-small-en-us-0.15/` | Vosk grammar model directory |
| `--stt-model-dir` | `models/sherpa-onnx-streaming-zipformer-en-2023-06-26/` | Sherpa-onnx Zipformer directory |
| `--wake-word` | `hey robot` | Wake phrase to listen for |
| `--aplay-device` | `$PX_VOICE_DEVICE` | ALSA device for chime playback |
| `--dry-run` | — | Detect wake word but skip voice loop pipe |

**Architecture:**

```
USB Mic (44100 Hz)
  → audioop.ratecv → 16000 Hz (for Vosk)
  → Vosk grammar recognizer (wake word detection, low CPU)
  → [wake word detected]
  → 440 Hz sine chime (aplay)
  → record until 1.5s silence (max 8s)
  → sherpa-onnx Zipformer transcribe (int8, ~RTF 0.78x)
  → transcript text
  → bin/run-voice-loop-claude --max-turns 1 (stdin pipe)
```

**Vosk model:** `vosk-model-small-en-us-0.15` (~40MB). Grammar-based `KaldiRecognizer` checks only whether the wake phrase appears — very low CPU, ~1–5ms per chunk.

**Sherpa-onnx model:** `sherpa-onnx-streaming-zipformer-en-2023-06-26` (~297MB int8). Streaming `OnlineRecognizer.from_transducer()`, num_threads=4. Processes full utterance at ~5s for a 6.6s clip (RTF ~0.78x, excellent accuracy for en-AU English).

**Audio recording:** PyAudio, USB mic (usually card 2). Records 16-bit mono at 44100 Hz (USB mic hardware constraint), resampled to 16kHz via `audioop.ratecv` for both Vosk and sherpa-onnx.

**Silence detection:** Stops recording when a 30-frame (~960ms) RMS window falls below threshold for 1.5s, or after a hard maximum of 8s.

**Python interpreter:** `$PROJECT_ROOT/.venv/bin/python3` (venv has `vosk`, `pyaudio`, `sherpa_onnx`, `numpy`).

**Log file:** `$LOG_DIR/px-wake-listen.log` (or `PX_LOG_FILE`).

---

### bin/run-wake

**Purpose:** Launcher for `px-wake-listen`. Resolves model directories and wake word from environment variables, then execs the listener.

**Usage:**
```bash
bin/run-wake [--dry-run] [--wake-word "phrase"] [additional px-wake-listen flags]
```

**Environment variables:**
| Variable | Default | Description |
|---|---|---|
| `PX_WAKE_WORD` | `hey robot` | Wake phrase |
| `PX_WAKE_MODEL` | `models/vosk-model-small-en-us-0.15` | Vosk model directory |
| `PX_STT_MODEL` | `models/sherpa-onnx-streaming-zipformer-en-2023-06-26` | Sherpa model directory |
| `PX_VOICE_DEVICE` | `robothat` | ALSA device for chime |

---

### bin/px-wake

**Purpose:** Legacy/test wake-word state manager. Manages the `listening` flag in `session.json`. Supports three modes: set on/off, timed pulse, or keyboard simulation.

**Usage:**
```bash
bin/px-wake --set on|off
bin/px-wake --pulse N           # enable for N seconds then disable
bin/px-wake --keyboard           # interactive: type wake word to trigger
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--set on\|off` | — | Directly set listening state |
| `--pulse N` | — | Enable for N seconds |
| `--keyboard` | — | Simulate wake via keyboard input |
| `--wake-word` | `hey pi` | Wake phrase for keyboard mode |
| `--duration` | `10.0` | Listening duration after keyboard wake |
| `--oneshot` | — | Exit after first wake in keyboard mode |

**Session effects:** Updates `listening`, `listening_since` fields in `session.json` and appends a history entry.

**Note:** `px-wake-listen` has superseded this for real wake-word detection. `px-wake --keyboard` is still useful for testing the voice loop without microphone hardware.

---

## Scheduled Announcements

### bin/px-cron-say

**Purpose:** Claude-driven scheduled robot announcement. Given a scene context (morning, afternoon, evening, random), asks Claude to generate a choreographed `tool_perform` response (speech + emotes), then executes it.

**Usage:**
```bash
bin/px-cron-say --scene morning [--dry-run]
bin/px-cron-say --prompt "Say something funny about being a robot."
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--scene` | `random` | Scene context: `morning`, `afternoon`, `evening`, `random` |
| `--prompt` | — | Override scene with a custom instruction |
| `--dry-run` | — | Dry-run mode for tool execution |

**Scene prompts:**
- `morning`: Warm greeting + weather from last session state, 2–3 sentences.
- `afternoon`: Friendly check-in, 1–2 sentences.
- `evening`: Calm wind-down, 1–2 sentences.
- `random`: Spontaneous robot observation, 1–2 sentences.

**LLM call:** Uses `claude -p` with `--allowedTools ""` and a system prompt instructing JSON-only `tool_perform` output (max 6 steps, max 3 sentences of speech). Unsets `CLAUDECODE`/`CLAUDE_CODE_ENTRYPOINT` for nested invocation.

**Fallback:** If Claude returns `tool_voice` instead of `tool_perform`, executes `tool-voice` directly.

**Log:** `logs/tool-cron-say.log` (via `pxh.logging.log_event`).

**Typical cron entry:**
```crontab
0 8  * * * cd /home/pi/picar-x-hacking && bin/px-cron-say --scene morning >> logs/cron.log 2>&1
0 18 * * * cd /home/pi/picar-x-hacking && bin/px-cron-say --scene evening >> logs/cron.log 2>&1
```

---

### bin/px-voice-report

**Purpose:** Summarise the voice transcript log. Reports tool call counts, voice success/failure rates, battery warnings, and the last weather summary.

**Usage:**
```bash
bin/px-voice-report [--log PATH] [--json]
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--log` | `$LOG_DIR/tool-voice-transcript.log` | Path to transcript log |
| `--json` | — | Emit JSON instead of text |

**Text output example:**
```
Entries: 42
Tools:
  tool_status: 12
  tool_voice: 30
Voice successes: 28
Voice failures: 2
Battery warnings: 0
Last weather summary: At Hobart Airport, it's 18 degrees Celsius...
```

---

## Compound Routines

### bin/px-dance

**Purpose:** Choreographed demo routine. Announces itself, drives a circle, drives a figure-eight, then says "Dance complete".

**Usage:**
```bash
PX_DRY=0 bin/px-dance [--speed N] [--duration S] [--rest S] [--voice TEXT]
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--speed` | `28` | Wheel speed % |
| `--duration` | `4.0` | Duration per circle/figure8 leg |
| `--rest` | `1.0` | Rest between figure8 legs |
| `--voice` | `"Enjoy the PiCar-X dance demo!"` | Announcement text |

**Sequence:**
1. `tool-voice`: announcement
2. `tool-circle`: clockwise circle
3. `tool-figure8`: figure-eight
4. `tool-voice`: "Dance complete"

**Output:** JSON summary with returncode for each step.

**Log:** `logs/tool-dance.log` (via `pxh.logging.log_event`). Session updated with `{"event": "dance"}`.

---

## Streaming

### bin/px-frigate-stream

**Purpose:** Stream the PiCar-X CSI camera to a Frigate NVR / go2rtc server via RTSP.

**Usage:**
```bash
bin/px-frigate-stream [--host HOST] [--port PORT] [--stream NAME] [--fps N] [--width N] [--height N] [--duration S] [--dry-run]
```

**Arguments:**
| Flag | Default | Description |
|---|---|---|
| `--host` | `pi5-hailo.local` | Frigate/go2rtc hostname |
| `--port` | `8554` | RTSP port |
| `--stream` | `picar-x` | Stream name in go2rtc |
| `--fps` | `15` | Frames per second |
| `--width` | `1280` | Frame width |
| `--height` | `720` | Frame height |
| `--duration` | `0` | Stop after N seconds (0 = run until Ctrl+C) |
| `--dry-run` | — | Print commands without executing |

**Pipeline:**
```
rpicam-vid --codec h264 --profile baseline --inline --output -
  └→ stdout pipe
      └→ ffmpeg -i pipe:0 -preset ultrafast -tune zerolatency -f rtsp tcp://host:port/api/stream?push=NAME
```

**Signal handling:** SIGINT/SIGTERM terminate both processes cleanly.

**URL format:** `rtsp://{host}:{port}/api/stream?push={stream}`

**Log:** `logs/tool-frigate-stream.log`.

---

## Session Management

### bin/px-session

**Purpose:** Launch or attach to a tmux session with the full robot assistant environment: voice loop, wake detector, and log tail.

**Usage:**
```bash
bin/px-session          # launch or attach
bin/px-session --plan   # print tmux commands without executing
```

**tmux layout:**
- Window `voice`:
  - Pane 0: `bin/run-voice-loop --auto-log` (voice loop)
  - Pane 1 (horizontal split): `bin/px-wake --keyboard` (wake simulation)
  - Pane 2 (vertical split of pane 0): `tail -f logs/tool-voice-transcript.log`
- Window `shell`: bare shell in project root

**Environment variables:**
| Variable | Default | Description |
|---|---|---|
| `PX_TMUX_SESSION` | `picar-x` | tmux session name |
| `PX_VOICE_CMD` | `bin/run-voice-loop --auto-log` | Voice loop command |
| `PX_WAKE_CMD` | `bin/px-wake --keyboard` | Wake command |
| `PX_LOG_CMD` | `tail -f logs/tool-voice-transcript.log` | Log tail command |

If the session already exists, attaches without creating a new one.

---

## Python Library — src/pxh/

### src/pxh/state.py

**Purpose:** FileLock-protected read/write access to `state/session.json`. All scripts that need to read or update robot state use this module.

**Session fields:**
| Field | Type | Description |
|---|---|---|
| `schema_version` | str | `"1.0"` |
| `mode` | str | `"dry-run"` or `"live"` |
| `last_action` | str\|null | Last tool called (e.g. `"tool_voice"`) |
| `last_motion` | str\|null | Last motion routine (e.g. `"px-circle"`) |
| `battery_pct` | int\|null | Last known battery % |
| `battery_ok` | bool\|null | `true` if battery ≥ threshold |
| `wheels_on_blocks` | bool | Safety flag: are wheels lifted? |
| `confirm_motion_allowed` | bool | Motion gate: must be `true` for tool_circle/figure8 |
| `watchdog_heartbeat_ts` | str\|null | ISO timestamp of last voice loop heartbeat |
| `last_weather` | dict\|null | Last weather payload (including `summary` string) |
| `last_prompt_excerpt` | str\|null | First 800 chars of last LLM prompt |
| `last_model_action` | dict\|null | Last action JSON from the LLM |
| `last_tool_payload` | dict\|null | Last tool result payload |
| `listening` | bool | Is the wake listener active? |
| `listening_since` | str\|null | UTC timestamp when listening started |
| `history` | list | Last 100 event history entries |

**Key functions:**

```python
def ensure_session() -> Path
```
Creates `session.json` from template (or defaults) if absent. Uses FileLock to avoid races. **Must be called before acquiring the lock** (FileLock is not reentrant).

```python
def load_session() -> Dict[str, Any]
```
Reads and returns the session dict. Falls back to defaults if JSON is corrupt.

```python
def save_session(data: Dict[str, Any]) -> None
```
Atomically overwrites `session.json` under FileLock.

```python
def update_session(
    fields: Optional[Dict] = None,
    history_entry: Optional[Dict] = None,
    history_limit: int = 100,
) -> Dict[str, Any]
```
Read-modify-write under FileLock. Merges `fields` into the session dict. If `history_entry` is given, prepends a UTC timestamp and appends to `history[]`, trimming to `history_limit` entries.

**Session path:** `state/session.json` by default. Override with `PX_SESSION_PATH` env var (used for test isolation).

**Lock file:** `state/session.json.lock` (filelock). Never commit this file.

---

### src/pxh/logging.py

**Purpose:** Structured JSON-lines logger for all tool events.

**Key function:**

```python
def log_event(name: str, payload: Mapping[str, Any]) -> None
```

Appends one JSON line to `$LOG_DIR/tool-{name}.log`:
```json
{"ts":"2026-03-08T10:00:00Z","status":"ok","dry":false,...}
```

Uses FileLock on `tool-{name}.log.lock` to prevent corruption from concurrent writes.

**Log directory:** Resolved once at import time from `LOG_DIR` environment variable, falling back to `$PROJECT_ROOT/logs`. Relative paths are resolved relative to `PROJECT_ROOT`.

**Typical log files created:**
- `logs/tool-voice.log`
- `logs/tool-status.log`
- `logs/tool-circle.log`
- `logs/tool-figure8.log`
- `logs/tool-stop.log`
- `logs/tool-weather.log`
- `logs/tool-look.log`
- `logs/tool-emote.log`
- `logs/tool-sonar.log`
- `logs/tool-perform.log`
- `logs/tool-health.log`
- `logs/tool-dance.log`
- `logs/tool-voice-loop.log`
- `logs/tool-voice-transcript.log`
- `logs/tool-frigate-stream.log`
- `logs/tool-cron-say.log`

---

### src/pxh/time.py

**Purpose:** UTC timestamp utility.

```python
def utc_timestamp() -> str
```

Returns an ISO 8601 string in UTC with second precision, e.g. `"2026-03-08T10:00:00Z"`.

Uses `datetime.now(timezone.utc)` (not the deprecated `utcnow()`).

---

### src/pxh/voice_loop.py

**Purpose:** Core voice assistant supervisor loop. Implements the turn-by-turn cycle: capture user input → build LLM prompt → call LLM → parse action → validate → execute tool → log → repeat.

**Entry point:** `python -m pxh.voice_loop` (called by `bin/codex-voice-loop`).

**Allowed tools:** `tool_status`, `tool_circle`, `tool_figure8`, `tool_stop`, `tool_voice`, `tool_weather`, `tool_look`, `tool_emote`, `tool_sonar`, `tool_perform`.

**Key arguments:**

| Flag | Default | Description |
|---|---|---|
| `--prompt PATH` | `docs/prompts/codex-voice-system.md` | System prompt file |
| `--input-mode text\|voice` | `text` | How to capture user input |
| `--transcriber-cmd CMD` | — | Command for voice STT (used with `--input-mode voice`) |
| `--codex-cmd CMD` | `$CODEX_CHAT_CMD` | LLM invocation command |
| `--max-turns N` | `50` | Max conversation turns before exit |
| `--dry-run` | — | Force `PX_DRY=1` for all tools |
| `--auto-log` | — | Log full LLM responses to `logs/tool-voice-loop.log` |
| `--exit-on-stop` | — | Exit after successful `tool_stop` |
| `--watchdog-timeout S` | `30.0` | Stale heartbeat threshold |

**Turn cycle (supervisor_loop):**
1. Push heartbeat to watchdog queue.
2. Load `session.json`; if voice mode and `listening=false`, sleep 0.5s and retry.
3. Capture input: `input("You> ")` (text) or run `--transcriber-cmd` (voice).
4. Build prompt: `system_prompt + state highlights + recent history + user_text`.
5. Run LLM: pipe full prompt to `CODEX_CHAT_CMD` subprocess stdin; capture stdout.
6. Extract action: scan stdout for last valid JSON object (reverse line scan, then multi-line fallback via `JSONDecoder.raw_decode`).
7. Validate action: check tool name, type-check and clamp all params.
8. Execute tool: run `bin/tool-*` with params as env vars.
9. Post-process: if `tool_weather`, auto-call `tool_voice` with the `summary` field.
10. Update session and log transcript entry.
11. If `--exit-on-stop` and tool was `tool_stop`, break.

**Watchdog thread:** Only started in voice mode (`--input-mode voice`). Uses a `queue.Queue` for heartbeats. If no heartbeat for `watchdog_timeout` seconds, calls `os._exit(1)` (hard exit, bypasses finally blocks). In text mode (slow typists), no watchdog.

**Security:** `capture_voice_input` rejects shell metacharacters (`|`, `;`, `&&`, `||`, `>`, `<`) in `--transcriber-cmd` to prevent injection.

**Prompt construction:** Only non-null state fields are included. Recent history is limited to 3 entries. Weather summary is extracted as a flat string.

**LLM protocol:** The LLM receives a text prompt and must return a single JSON object on the last line of stdout:
```json
{"tool": "tool_voice", "params": {"text": "Hello!"}}
```

---

## Cross-Cutting Concerns

### Dry-Run Pattern

Every motion and audio tool respects `PX_DRY`:
- `PX_DRY=1` → log the action, return `{"status":"ok","dry":true}`, skip hardware.
- `PX_DRY=0` → execute hardware actions.

The default across all scripts is `PX_DRY=1` (safe default). Live runs require explicit `PX_DRY=0`.

### sudo Pattern

Scripts that need GPIO (`picarx`, `robot_hat`) require root. The pattern used:
- `px-*` scripts are called by `tool-*` wrappers via `sudo -n env PYTHONPATH=... PX_*=... /path/to/px-*`.
- The `-n` flag means "non-interactive" — fails immediately if a password would be required. The `pi` user must have a `NOPASSWD` sudoers entry for the relevant commands.
- `PX_BYPASS_SUDO=1` skips sudo (used in tests and CI).

### Python Interpreter Split

| Use case | Interpreter |
|---|---|
| Scripts needing `robot_hat`/`picarx` | `/usr/bin/python3` (system Python) |
| Scripts needing `vosk`, `pyaudio`, `sherpa_onnx` | `$PROJECT_ROOT/.venv/bin/python3` |
| `pxh.*` library scripts | venv Python (activated by `px-env`) |

System Python has `robot_hat` and `picarx` in its site-packages. The venv has the project library (`pxh`) plus AI/audio packages. The venv explicitly excludes system site-packages.

### Log Rotation

Logs are append-only JSON-lines files. There is no built-in rotation; use `logrotate` if needed. The `history` array in `session.json` is capped at 100 entries.
