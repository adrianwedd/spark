# px-race: Autonomous Track Racing with Learning

**Date:** 2026-03-19
**Status:** Draft (rev 2 — post spec review)

## Overview

A two-phase autonomous racing system for the PiCar-X. Phase 1 (practice) maps the track at safe speed, building a segment profile. Phase 2 (race) uses the profile to anticipate turns, maximize straight-line speed, and refine the profile each lap.

### Context

- Timed race, multi-lap, fastest wins
- ~90cm wide dark gray track, light gray raised barriers, orange corners
- Physical start/finish gate — overhead structure (lap detection)
- Practice laps available before timed race
- Other cars may be present on track

### Hardware Constraints

- **Steering:** ±30° max (`DIR_MAX = 30`)
- **Sonar:** single ultrasonic on camera pan servo, ~30ms per ping typical, up to 200ms worst case (10 retries × 20ms timeout). Pan servo needs ~80ms settle time per angle change.
- **Grayscale:** 3-channel underside sensor array, analog read — effectively instantaneous (<1ms)
- **Motors:** differential speed on turns (built into `px.forward()`). Speed parameter is PWM duty cycle (0-100), not cm/s. Relationship to actual velocity is nonlinear and battery-dependent.
- **Pi 4:** no real-time vision processing; no LLM in the control loop
- **I2C bus:** shared by sonar, grayscale, servos. Glitches under load are common on Pi 4.

## Dual-Sensor Navigation Model

The two sensor systems serve complementary roles:

| Sensor | Latency | Role | Strength |
|--------|---------|------|----------|
| **Grayscale** | <1ms | Primary edge avoidance, gate detection | Instant reaction, no moving parts |
| **Sonar** | 30-200ms | Obstacle detection, centering, turn anticipation | Range information, forward vision |

At race speed, grayscale is the primary wall-avoidance mechanism. Sonar provides strategic information (centering, obstacles, turn entry) but cannot react fast enough alone.

### Grayscale Edge Detection

The 3 grayscale sensors (left, center, right) continuously read track reflectance:
- **On dark gray track:** readings in calibrated `track_ref` range
- **Approaching light gray barrier:** readings shift toward `barrier_ref`
- **Over orange corner:** readings match `corner_ref` (if calibrated)

The PD controller uses grayscale deviation from track_ref as its primary input during race mode:
- Left sensor drifting toward barrier_ref → steer right
- Right sensor drifting toward barrier_ref → steer left
- Proportional response based on how far the reading has shifted from track_ref toward barrier_ref

This gives continuous, sub-millisecond edge tracking with no moving parts.

### Sonar Roles

| Mode | When | Pings | Realistic Rate | Pan Angles |
|------|------|-------|---------------|------------|
| **Forward-only** | Every loop iteration | 1 | ~10-15Hz | 0° (no pan move) |
| **Quick-3** | Every 1-2s on straights | 3 | ~2-3Hz | -25°, 0°, +25° |
| **Full sweep** | Mapping mode, lost/stuck | 5 | ~1.5Hz | -50°, -25°, 0°, +25°, +50° |

Pan servo returns to 0° after every Quick-3 or Full sweep. During mapping, pan stays at each angle only for minimum settle+ping duration (~100ms per angle).

Forward-only sonar (no pan move needed) is the workhorse during racing — detects obstacles and provides center distance for the track profile position tracker.

## Control Loop Architecture

### Loop Timing

| Mode | Loop period | Speed | Rationale |
|------|------------|-------|-----------|
| Map | ~150ms | PWM 20 | Full 3-point sonar each iteration |
| Race | ~30-50ms | PWM 25-50 | Grayscale every iteration, sonar forward-only, Quick-3 every 1-2s |

### Map Mode (practice lap)

```
warmup_pings(3)  # discard noisy first readings

while not lap_complete:
    gs = px.get_grayscale_data()          # <1ms
    sonar_left   = ping_at(-25°)          # ~100ms (settle + ping)
    sonar_center = ping_at(0°)            # ~100ms
    sonar_right  = ping_at(+25°)          # ~100ms
    px.set_cam_pan_angle(0)               # return to center

    steer = pd_center(sonar_left, sonar_right)
    steer = clamp(steer, -DIR_MAX, DIR_MAX)
    px.set_dir_servo_angle(steer)
    px.forward(MAP_SPEED)

    record_sample(steer, sonar_left, sonar_center, sonar_right, gs, elapsed)
    if detect_gate(gs, prev_gs):
        lap_complete = True
    prev_gs = gs
```

After the mapping lap, the raw sensor log is compressed into a track profile.

### Race Mode (timed laps)

```
last_quick3 = 0
last_sonar_lr = (None, None)  # cached left/right from last Quick-3

while racing:
    gs = px.get_grayscale_data()                       # <1ms — every iteration
    sonar_center = px.get_distance()                   # ~30ms — every iteration (no pan)
    segment = track_profile[current_segment]

    # Grayscale-primary edge avoidance
    gs_steer = pd_edge(gs, calibration)                # instant

    # Periodic Quick-3 for centering (every 1-2s)
    if now - last_quick3 > QUICK3_INTERVAL:
        sonar_left, sonar_right = quick3_scan(px)      # ~300ms
        last_sonar_lr = (sonar_left, sonar_right)
        last_quick3 = now

    # Blend grayscale edge avoidance with sonar centering
    sonar_steer = pd_center(last_sonar_lr) if last_sonar_lr[0] else 0
    steer = 0.7 * gs_steer + 0.3 * sonar_steer        # grayscale-dominant blend

    # Profile-based speed control
    if segment.type == "straight":
        speed = segment.race_speed
        if approaching_next_segment(segment, elapsed):
            speed = segment.next.entry_speed
    elif segment.type.startswith("turn"):
        speed = segment.race_speed
        steer += segment.steer_bias                    # pre-computed turn offset

    # Obstacle check
    if is_obstacle(sonar_center, segment):
        speed = OBSTACLE_SPEED                         # PWM 25
        steer = dodge_direction(last_sonar_lr)

    # Gate detection
    if detect_gate(gs, prev_gs):
        advance_lap()

    steer = clamp(steer, -DIR_MAX, DIR_MAX)
    px.set_dir_servo_angle(steer)
    px.forward(speed)
    prev_gs = gs

    # Position tracking: sonar-primary, time-secondary
    advance_segment_if_matched(sonar_center, last_sonar_lr, gs, elapsed, segment)
```

## PD Controller

Two PD controllers with different inputs:

### `pd_edge(gs, calibration)` — grayscale edge avoidance
- **Input:** 3 grayscale readings normalized to 0.0 (track center) to 1.0 (barrier)
- **Error:** `(right_normalized - left_normalized)` — positive = drifting right
- **Setpoint:** 0.0 (centered, both sides equally far from barrier)
- **Output:** steering angle, clamped to ±DIR_MAX
- **Gains (defaults, tunable):** `Kp_edge = 20.0`, `Kd_edge = 5.0`
- Map mode: more conservative gains (`Kp = 15.0, Kd = 3.0`)

### `pd_center(sonar_left, sonar_right)` — sonar centering
- **Input:** left and right wall distances from last Quick-3
- **Error:** `(right_cm - left_cm)` — positive = closer to left wall
- **Setpoint:** 0.0 (equidistant from both walls)
- **Output:** steering correction, clamped to ±DIR_MAX
- **Gains:** `Kp_sonar = 0.5`, `Kd_sonar = 0.2`

Derivative computed as `(error - prev_error) / dt`.

Gains will need on-site tuning. The `--calibrate` mode could include a short straight-line drive to auto-tune Kp by measuring oscillation.

## Gate Detection

### Physical Description
Overhead structure that SPARK drives through. Creates a shadow/reflectance change on the grayscale sensors.

### Detection Algorithm
1. Read grayscale every iteration (already happening)
2. Compute delta from previous reading: `delta = abs(gs[i] - prev_gs[i])` for each sensor
3. Gate trigger: all 3 sensors show delta > `GATE_THRESHOLD` within a single iteration
4. `GATE_THRESHOLD` captured during `--calibrate` by driving through the gate once
5. **Debounce:** ignore gate triggers within 3s of the last detection (prevents double-counting, minimum lap time sanity)
6. **Fallback:** if grayscale gate detection is unreliable, sonar may detect the overhead structure as a sudden short reading on center sonar — secondary signal

### Calibration Capture
During `--calibrate`, after surface calibration:
1. Position SPARK just before the gate
2. Drive through slowly, recording grayscale deltas
3. Store peak delta as `gate_ref` with a 60% threshold (trigger at 60% of calibrated peak)

## Track Profile & Learning

### Profile Structure (`state/race_track.json`)

```json
{
  "mapped_at": "2026-03-19T...",
  "map_speed": 20,
  "lap_duration_s": 28.5,
  "track_width_cm": 88,
  "segments": [
    {
      "id": 0,
      "type": "straight",
      "duration_s": 2.1,
      "width_left_cm": 44,
      "width_right_cm": 43,
      "sonar_center_cm": 120,
      "race_speed": 45,
      "steer_bias": 0,
      "entry_speed": 45,
      "gs_signature": [450, 460, 455]
    },
    {
      "id": 1,
      "type": "turn_left",
      "duration_s": 1.4,
      "width_left_cm": 30,
      "width_right_cm": 55,
      "sonar_center_cm": 65,
      "race_speed": 28,
      "steer_bias": -22,
      "brake_before_s": 0.3,
      "entry_speed": 28,
      "gs_signature": [520, 460, 380]
    }
  ],
  "lap_history": []
}
```

### Segment Detection (mapping)

Thresholds derived from calibrated track width (not hardcoded):
- **Track width:** measured during calibration (Quick-3 scan on straight section). Stored as `track_width_cm`.
- **Straight:** left/right sonar within ±20% of `track_width_cm / 2`, center clear
- **Turn:** left/right imbalance exceeds 20% of half-width AND center sonar drops below `track_width_cm`
- **Turn direction:** closer side = inside of turn
- **Orange detection:** if `corner_ref` calibrated, grayscale matching orange signals turn boundary
- **Segment boundary:** when classification changes (with 200ms hysteresis to prevent flapping)

### Position Tracking (race mode)

**Primary: sonar pattern matching.** Each segment has an expected sonar signature (center distance, left/right ratio). When live readings match the expected signature for segment N+1, advance.

**Secondary: elapsed time.** If sonar matching is ambiguous, use cumulative elapsed time (scaled by current speed vs map speed ratio) as a tiebreaker.

**Fallback: grayscale landmarks.** Orange corners provide unambiguous segment boundaries. If an orange reading is detected and it matches a known turn in the profile, re-sync position.

**Lost recovery:** If no segment match for >2s, fall back to reactive wall-following (grayscale edge + sonar centering, moderate speed) until a recognizable pattern (orange corner, distinctive turn) re-syncs.

### Per-Lap Learning

After each lap, compare actual vs predicted:

| Metric | Adjustment |
|--------|-----------|
| Segment took longer than expected | Increase `duration_s` (scaled by speed ratio) |
| Segment took less time | Decrease `duration_s` |
| Wall clipped (grayscale edge guard triggered) | Reduce `race_speed` -5, increase `brake_before_s` +0.1 |
| Turn taken cleanly with margin | Bump `race_speed` +3 next lap |
| Consistent off-center steer | Adjust `steer_bias` |
| Obstacle encountered | No speed change (not the track's fault) |

Conservative: speed only increases after clean pass, decreases immediately on wall contact. Changes capped at ±5 PWM per lap.

## Obstacle Handling (Other Cars)

Detection: center sonar reads closer than expected from track profile for current segment.

Response:
1. Slow to `OBSTACLE_SPEED` (PWM 25 — not full stop, still racing)
2. Use cached Quick-3 data to determine which side has more room
3. Steer toward open side, grayscale edge guard prevents wall contact
4. Resume race speed when center sonar clears

~0.3-0.5s reaction time.

## I2C Error Handling

The I2C bus is shared by all sensors and servos. Under race load, glitches happen.

| Sensor | On I2C error |
|--------|-------------|
| Sonar | Retry once (30ms). If still failing, use last known value for 1 iteration. If 3 consecutive failures, emergency brake. |
| Grayscale | Retry once. If failing, sonar becomes sole input. If both fail, emergency stop. |
| Servo | Fire-and-forget (no read-back). Servo holds last commanded position on I2C failure. |

All I2C reads wrapped in try/except OSError.

## Safety Layers

| Priority | Layer | Trigger | Action |
|----------|-------|---------|--------|
| 1 | E-stop | Center sonar < 8cm | `px.stop()`, reverse 0.3s |
| 2 | Edge guard | Grayscale detects barrier | Hard steer away, reduce speed to OBSTACLE_SPEED |
| 3 | Obstacle dodge | Unexpected close sonar | Slow + edge-hug |
| 4 | I2C failure | 3 consecutive sensor errors | Emergency brake |
| 5 | Stuck detect | No distance change for 2s while motors running | Stop, reverse, full sweep |
| 6 | Timeout | No gate for 60s | Stop and wait (assume lost) |
| 7 | Battery | < 20% | Finish current lap, stop |
| 8 | SIGTERM | Kill signal | `px.stop()`, clean exit |

Additional:
- `confirm_motion_allowed` gate (existing session safety)
- `yield_alive` at startup, `exploring.json` active during race
- `--max-speed N` flag (default 50, hard cap 60). Values are PWM duty cycle.
- `--dry-run` runs full loop with logging, no motor output

## Script Interface

```
bin/px-race --calibrate             # on-site sensor calibration (surfaces + gate)
bin/px-race --map                   # practice lap (slow mapping run)
bin/px-race --race                  # timed race using stored profile
bin/px-race --race --laps 5         # race N laps then stop
bin/px-race --dry-run --map         # full loop, no motors
bin/px-race --max-speed 40          # cap top speed (PWM)
bin/px-race --status                # print current profile summary
```

## File Layout

| File | Purpose |
|------|---------|
| `bin/px-race` | Launcher (bash + px-env, yields alive, delegates to python) |
| `src/pxh/race.py` | All race logic (~600-800 lines): RaceController, TrackProfile, PDController |
| `state/race_calibration.json` | Grayscale + gate references (persists) |
| `state/race_track.json` | Track profile with lap history (persists) |
| `state/race_log.jsonl` | Per-lap telemetry |
| `state/race_live.json` | Live telemetry for dashboard (current lap, speed, segment, incidents) |

### Live Telemetry (`state/race_live.json`)

Written every ~0.5s during racing, read by px-api-server for dashboard display:

```json
{
  "ts": "2026-03-19T...",
  "mode": "race",
  "lap": 3,
  "segment": 5,
  "speed": 42,
  "steer": -12,
  "sonar_center_cm": 85,
  "gs": [455, 462, 448],
  "incidents": 1,
  "lap_time_s": 14.2,
  "best_lap_s": 13.8
}
```

## Testing

- **Dry-run unit tests:** `test_race.py` using `isolated_project` fixture, mock sensor reads. Test: PD output for known inputs, segment detection from synthetic sensor sequences, learning adjustments, gate detection with debounce, I2C error recovery.
- **On-site calibration test:** `--calibrate` with real sensors, verify stored values.
- **Map mode integration:** `--dry-run --map` with real sensors but no motors, verify profile generation.
- **Safety tests:** verify e-stop, stuck detection, timeout, SIGTERM handling, I2C failure cascade.

## What This Does NOT Include

- No camera/vision (Pi 4 too slow for real-time)
- No LLM in the control loop (latency kills race performance)
- No network calls during racing (fully offline)
- No sound output during race (espeak would block control loop)
- Post-race narration via tool-voice is possible but not part of the racing loop
- No replay mode for offline development (future enhancement)
