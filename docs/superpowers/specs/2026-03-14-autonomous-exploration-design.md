# Autonomous Exploration — Design Spec

**Date:** 2026-03-14
**Scope:** `bin/px-wander`, `bin/tool-wander`, `bin/px-mind`, `src/pxh/voice_loop.py`, `src/pxh/state.py`, `src/pxh/api.py`, `bin/tool-describe-scene`, system prompts

---

## Problem

SPARK can't explore autonomously. The current `px-wander` is a fixed-step reactive obstacle-avoidance loop (drive forward, sweep sonar, turn if blocked, repeat N times, stop). It has no awareness of what it's seeing, no memory of where it's been, no camera integration, and no connection to the cognitive architecture. px-mind cannot initiate movement — `explore` is not in its action set.

SPARK needs to be able to decide when to explore, navigate its environment, see and remember what it finds, and weave those observations into its inner life and conversations.

---

## Design

Three layers, bottom-up:

### Layer 1: Enhanced px-wander (Exploration Engine)

The existing `bin/px-wander` gains a new `explore` mode alongside the unchanged `avoid` mode.

#### New CLI arguments

px-wander receives new CLI args (not just env vars — required for passing through `sudo` in tool-wander):

```
--mode avoid|explore    (default: avoid, or from PX_WANDER_MODE)
--duration N            (seconds, explore mode only, default 180, max 300, or from PX_WANDER_DURATION_S)
```

The `avoid` mode is completely unchanged — existing callers (voice loop, API async job) work exactly as before.

#### Changes to `tool-wander`

`tool-wander` must read `PX_WANDER_MODE` and `PX_WANDER_DURATION_S` from its environment (set by `validate_action()`) and pass them through as CLI args to the `px-wander` subprocess:

```python
command.extend([str(PX_WANDER), "--steps", str(steps)])
if mode == "explore":
    command.extend(["--mode", "explore", "--duration", str(duration)])
```

Additionally, `tool-wander` must check `roaming_allowed` in session state when `mode == "explore"`, alongside the existing `confirm_motion_allowed` check. This ensures the three-gate safety model is enforced regardless of whether exploration is initiated by px-mind or the voice loop.

#### Explore mode loop

Each iteration in explore mode:

1. **Abort check** (FIRST, before any motor commands) — re-read `session.json` and `battery.json` fresh
2. **Sweep sonar** (existing, 5 angles at -50°, -25°, 0°, +25°, +50°) → navigate
3. **Query Frigate** → fast object detection of current camera view (COCO labels, scores)
4. **Curiosity trigger** — photograph with `tool-describe-scene` if any of:
   - Frigate detects a label not yet seen this session (first time seeing "cat" or "person")
   - Sonar detects a reliable (non-999) object <100cm in a direction SPARK hasn't photographed yet
   - SPARK has turned >60° from its last photo heading
   - Rate limit: max 1 photo per 30 seconds (failed vision calls do NOT count toward rate limit)
   - Daily cap: max 50 Claude vision calls per day (tracked in `exploration_meta.json`; when exceeded, skip photos and continue sonar+Frigate only)
5. **Log to `exploration.jsonl`** — navigation entries batched in memory, flushed every 10 steps or on abort (reduces SD card wear). Observation entries written immediately.
6. **Narrate** — speak brief observations ("Oh, there's something on the shelf" / "I can see Obi over there")

#### Per-step timeout

If a single iteration (sonar + Frigate + optional vision) takes longer than 30 seconds, the step is abandoned and the loop continues to the next iteration. This prevents a hung Frigate query or Claude vision call from blocking the entire exploration.

#### Abort checks (polled at the TOP of every iteration, before driving)

- `roaming_allowed` set to false
- `confirm_motion_allowed` set to false
- `wheels_on_blocks` set to true
- `listening` goes true (someone is talking to SPARK)
- Battery charging (from `battery.json`, read directly — not cached)
- Battery ≤20%
- Battery data stale (battery.json older than 60 seconds) — fail-safe, do not explore without confirmed battery reading
- Stuck detection: 3 consecutive all-blocked sweeps
- Sonar failure: all 5 sweep angles return 999.0 — this is sensor failure, not "room is clear"
- Time limit exceeded

On abort: stop motors, reset servos, speak brief reason ("Battery's getting low"), log abort reason in exploration.jsonl, write an `exploring: false` marker to `state/exploring.json`.

#### Sonar 999.0 handling

The existing sentinel value 999.0 means both "nothing detected" and "sensor failure." For exploration, these must be distinguished:

- `read_dist()` returns `None` on exception/null/negative, `999.0` only for genuine far readings
- Navigation entries include `"sonar_reliable": false` when any angle returned `None`
- The curiosity trigger ("sonar detects an object <100cm") explicitly excludes `None` readings
- All-`None` sweep = sensor failure abort (not "all clear")

#### Exploring state file

On explore start, write `state/exploring.json`:
```json
{"active": true, "pid": 12345, "started": "2026-03-14T10:30:00+11:00"}
```
On explore end (normal or abort), update to `{"active": false, ...}`.

px-alive checks this file on startup — if `active: true` and the PID is still running, px-alive exits cleanly (exit 0) to avoid GPIO contention and systemd restart storm. This prevents systemd's `StartLimitBurst` from killing px-alive permanently during long explorations.

#### Motor safety on crash

The `finally` block in px-wander must:
1. Attempt `px.stop()` via the Picarx handle
2. If that fails (I2C error), attempt a direct I2C write to PCA9685 register 0xFD (ALL_LED_OFF) as a fallback
3. Update `state/exploring.json` to `active: false`
4. Remove any PID file

Additionally, px-wander explore mode handles SIGTERM gracefully: a signal handler sets a flag, the main loop checks it, and motors are stopped before exit. This covers the case where px-mind's subprocess.run times out and kills the child.

#### Sensing stack

| Layer | Source | Speed | Purpose |
|-------|--------|-------|---------|
| Navigation | Sonar (ultrasonic) | Instant | Obstacle distance, every step |
| Detection | Frigate (Hailo AI on pi5) | ~instant | Object labels in camera view (COCO: person, cat, dog, etc.) |
| Understanding | Claude vision (`tool-describe-scene`) | 3-5s + API cost | Rich scene description, selective trigger only |

Frigate is the fast trigger ("there's a person over there"), Claude vision is the deep look ("Obi is sitting on the floor reading a book").

#### Frigate integration in px-wander

A simplified Frigate query function in px-wander (self-contained, matching the existing bin-script pattern). It only needs labels and scores, not the full presence analysis that px-mind's `_fetch_frigate_presence()` provides.

When Frigate is unreachable:
- The **first** failure in the session is logged and spoken: "My object detection is offline — I'll explore by sonar"
- Subsequent failures are logged at debug level (no repeated speech)
- Exploration continues with sonar-only navigation
- The exploration result JSON includes `"frigate_available": false` so px-mind knows the exploration was degraded

#### Vision failure handling

When `tool-describe-scene` returns its fallback description ("I couldn't see anything right now"):
- The observation entry is marked `"vision_failed": true`
- The entry is NOT promoted to notes.jsonl
- The failed call does NOT count toward the 30-second rate limit
- After 3 consecutive vision failures, the explore loop stops attempting photos for the rest of the session and logs a warning

### Layer 2: Fuzzy Mental Map & Exploration Log

#### `state/exploration.jsonl`

Append-only log, one JSON object per line, trimmed to last 100 entries. Uses `atomic_write()` (temp file + `os.rename`) for the trim step and `FileLock` on `exploration.jsonl.lock` for concurrent access. Two entry types:

**Navigation entry** (batched in memory, flushed every 10 steps or on abort):
```json
{
  "ts": "2026-03-14T10:32:15+11:00",
  "type": "nav",
  "explore_id": "e-20260314-103200",
  "heading_estimate": "left",
  "sonar_readings": {"-50": 120, "-25": 85, "0": 45, "25": 200, "50": null},
  "sonar_reliable": false,
  "action": "turned_right",
  "steps_from_start": 3,
  "frigate_labels": ["cat"]
}
```

Note: sonar readings use `null` (not 999) for sensor failures. The `sonar_reliable` field is `false` if any angle returned `null`.

**Observation entry** (written immediately on photo):
```json
{
  "ts": "2026-03-14T10:32:45+11:00",
  "type": "observation",
  "explore_id": "e-20260314-103200",
  "heading_estimate": "ahead-right",
  "sonar_cm": 45,
  "frigate_labels": ["cat", "person"],
  "description": "A ginger cat sitting next to a small boy reading a book on the floor",
  "landmark": "Obi reading with cat",
  "interesting": true,
  "vision_failed": false,
  "steps_from_start": 3
}
```

The `explore_id` (session-unique, derived from start timestamp) correlates all entries from one exploration for log analysis.

#### Heading estimation

Without encoders or a compass, SPARK tracks heading as a fuzzy relative direction from its starting position. A running `turn_accumulator` (degrees) is updated each drive step:

```python
delta_heading = servo_angle * (drive_time / TURN_S) * K
turn_accumulator += delta_heading
turn_accumulator = ((turn_accumulator + 180) % 360) - 180  # wrap to [-180, 180]
```

Where `K = 1.0` is a tuning constant (will need field calibration). The accumulator wraps at ±180° to prevent unbounded growth.

The accumulator maps to fuzzy labels:

| Accumulated turn | Label |
|-----------------|-------|
| -180 to -135 | "behind-left" |
| -135 to -45 | "left" |
| -45 to 45 | "ahead" |
| 45 to 135 | "right" |
| 135 to 180 | "behind-right" |

The accumulator resets at the start of each exploration session. This drifts over time — that's fine. It's a vibes-based map, not a SLAM map.

"Hasn't photographed yet in this direction" means no photo taken while heading_estimate matched the same label.

#### Landmark extraction

When Claude vision returns a scene description, px-wander extracts a short landmark label (3-6 words) by truncating the description to its first noun phrase. This is done locally in px-wander — no extra API call. A simple heuristic: take the first sentence, strip to 6 words, remove leading articles.

If vision fails (fallback description), no landmark is extracted.

#### Interesting flag

An observation is marked interesting if:
- Frigate detected a person (always interesting)
- The scene description mentions something SPARK hasn't seen before this session (compared against previous observation descriptions in the current exploration)

No numeric rating from Claude — keep it deterministic and free. The Frigate person-detection and "new content" heuristics are sufficient.

Interesting observations are promoted to `notes.jsonl` via `auto_remember` (existing mechanism), making them available to the voice loop context and recallable via `tool-recall`. Vision-failed observations are never promoted.

#### Feeding back into the cognitive loop

- px-mind's awareness layer reads the last 5 observation entries from `exploration.jsonl` (same pattern as `thoughts.jsonl` reading)
- The reflection prompt can reference what SPARK found: "I found a red mug on a shelf to my left earlier"
- `build_model_prompt()` in voice_loop.py includes recent landmarks so SPARK can mention them in conversation: "Recent exploration: [landmark] to my [direction], [landmark] ahead"

### Layer 3: px-mind Integration (Cognitive Exploration)

#### New action: `explore`

Added to the expression layer's valid action set alongside `wait, greet, comment, remember, look_at, weather_comment, scan`.

The expression function's `elif` chain must include an `explore` branch. Additionally, a final `else` clause must be added to log any unhandled action: `log(f"expression: unhandled action: {action}")`. This prevents silent no-ops if the LLM produces an action that is in `VALID_ACTIONS` but has no corresponding branch.

#### Gate function

```python
def _can_explore(session: dict, awareness: dict) -> bool:
    if not session.get("roaming_allowed", False):
        return False
    if not session.get("confirm_motion_allowed", False):
        return False
    if session.get("wheels_on_blocks", False):
        return False
    if session.get("listening", False):
        return False
    battery = awareness.get("battery") or {}
    if battery.get("charging", False):
        return False
    if battery.get("pct") is None:
        return False  # unknown battery = do not explore
    if battery["pct"] <= 20:
        return False
    # Cooldown: 20 minutes between self-initiated explorations
    meta_path = STATE_DIR / "exploration_meta.json"
    try:
        meta = json.loads(meta_path.read_text())
        last = dt.datetime.fromisoformat(meta["last_explore_ts"])
        if (dt.datetime.now(dt.timezone.utc) - last).total_seconds() < 1200:
            return False
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        pass  # no meta file or corrupt = no cooldown (first exploration)
    return True
```

Key change from initial design: `battery.get("pct") is None` returns `False` — never explore without a confirmed battery reading. No `or 100` default.

When `_can_explore()` is true, `explore` appears in the reflection prompt's action set:
```
- "explore" — go for a wander and see what's around. You haven't explored in a while.
```

When false, `explore` is simply not in the action list — the LLM never sees it as an option.

#### Cooldown tracking: `state/exploration_meta.json`

```json
{
  "last_explore_ts": "2026-03-14T10:32:00+11:00",
  "total_explorations": 7,
  "total_observations": 23,
  "daily_vision_calls": 12,
  "daily_vision_date": "2026-03-14"
}
```

Written atomically (temp + rename). Updated at exploration **start** (sets `last_explore_ts` to establish cooldown even if exploration crashes) and again at completion (updates counts). On corruption, defaults to "cooldown active" — `_can_explore()` returns `False` until the file is fixed or 20 minutes pass.

The `daily_vision_calls` counter resets when `daily_vision_date` differs from today. The daily cap (50 calls) is checked by px-wander before each vision call.

#### Exploration triggers (hints in reflection prompt)

The reflection prompt already receives awareness context. When `_can_explore()` is true, additional hints bias toward exploring:

- If `obi_mode` just transitioned to "present": "Obi might be nearby — you could go find him"
- If idle for >30 min: "You haven't moved in a while"
- If last exploration found something interesting: "Last time you explored you found [landmark] — you could check if it's still there"

These are hints, not forced actions. The LLM decides — SPARK might choose to comment or wait instead.

#### Execution in expression layer

When the LLM picks `explore`:

1. Re-read session state fresh (must be <2 seconds old) — verify gates still pass
2. `yield_alive` (existing — signal px-alive to release GPIO)
3. Wait for px-alive PID to disappear (timeout 5 seconds). If px-alive is still running after timeout, log error and abort — do NOT start exploration with GPIO contention
4. Update `exploration_meta.json` with `last_explore_ts` (establishes cooldown even if exploration crashes)
5. Run `tool-wander` with `PX_WANDER_MODE=explore`, `PX_WANDER_DURATION_S=180`, subprocess timeout = `duration + 60`
6. Parse the result JSON
7. Generate a post-exploration thought: feed the exploration summary back into a quick reflection ("I just explored and found...")
8. Update `exploration_meta.json` with final counts
9. Verify px-alive is running (check `systemctl is-active px-alive`). If not running after 15 seconds, log warning and attempt `systemctl start px-alive`

#### Awareness layer: `charging` field

`read_battery()` currently returns `{pct, volts}` but not `charging`. The `battery.json` file already contains a `charging` field written by `px-battery-poll`. The change:

```python
return {"pct": int(data["pct"]), "volts": float(data["volts"]),
        "charging": bool(data.get("charging", False))}
```

The awareness layer must propagate `charging` into the awareness dict's battery section so `_can_explore()` can see it. This is a prerequisite — exploration must not be enabled until this is deployed.

### Layer 4: Session, API, and Voice Loop Wiring

#### New session field: `roaming_allowed`

- Added to `default_state()` in `state.py` (default: `false`)
- Added to `PATCHABLE_FIELDS` in `api.py`
- Togglable via dashboard, API, or voice command
- Included in voice loop context (`build_model_prompt`)

#### `validate_action()` in voice_loop.py

The existing `tool_wander` branch passes `PX_WANDER_STEPS`. Add `PX_WANDER_MODE` and `PX_WANDER_DURATION_S` support:

```python
elif tool == "tool_wander":
    steps = int(clamp(_num(params.get("steps", 5), "steps"), 1, 20))
    sanitized["PX_WANDER_STEPS"] = str(steps)
    mode = str(params.get("mode", "avoid"))
    if mode not in ("avoid", "explore"):
        mode = "avoid"
    sanitized["PX_WANDER_MODE"] = mode
    if mode == "explore":
        duration = int(clamp(_num(params.get("duration", 180), "duration"), 30, 300))
        sanitized["PX_WANDER_DURATION_S"] = str(duration)
```

This means the voice loop can trigger explore mode: "SPARK, go explore" → `{tool: "tool_wander", params: {mode: "explore"}}`. The `roaming_allowed` gate is enforced in `tool-wander` (not `validate_action`) for consistency — it's a session-state check alongside `confirm_motion_allowed`.

#### System prompts

Update `tool_wander` description in all prompt files (`claude-voice-system.md`, `codex-voice-system.md`, `spark-voice-system.md`, `persona-gremlin.md`, `persona-vixen.md`):

```
- tool_wander → Autonomous wander (params: steps 1-20, mode "avoid"|"explore", duration 30-300).
  "avoid" = obstacle avoidance only (default). "explore" = sense, photograph, build mental map.
  Explore mode requires roaming_allowed in session.
```

---

## Logging

All exploration events use `log_event()` for structured JSON logging.

Required log events:
- `explore_start` — `{explore_id, mode, duration, frigate_available}`
- `explore_step` — every 10 steps: `{explore_id, steps_completed, sonar_reliable, frigate_labels}`
- `explore_photo` — `{explore_id, landmark, interesting, vision_failed}`
- `explore_frigate_down` — first Frigate failure per session: `{explore_id, error}`
- `explore_abort` — `{explore_id, reason, steps_completed, observations_count}`
- `explore_complete` — `{explore_id, steps_completed, observations_count, interesting_count, frigate_available}`

The `explore_id` (e.g., `e-20260314-103200`) correlates all log entries from one exploration session.

---

## Testing

### `tests/test_tools.py` (dry-run, no hardware)

- `test_wander_explore_mode_dry` — explore mode accepts `--mode explore`, runs time-boxed, emits correct JSON
- `test_wander_avoid_mode_unchanged` — existing behaviour preserved with `--mode avoid`
- `test_wander_explore_abort_on_listening` — mock session with `listening: true` mid-exploration, verify clean abort
- `test_wander_explore_abort_on_charging` — mock `battery.json` with `charging: true`, verify abort
- `test_wander_explore_abort_on_roaming_disabled` — toggle `roaming_allowed: false`, verify abort
- `test_wander_explore_abort_on_stale_battery` — battery.json older than 60s, verify abort
- `test_wander_explore_abort_all_sonar_none` — all 5 sweep angles return None, verify sensor-failure abort
- `test_wander_explore_roaming_gate_in_tool` — tool-wander rejects explore mode when `roaming_allowed: false`

### `tests/test_mind_utils.py` (exec'd from heredoc, existing pattern)

- `test_can_explore_all_gates` — `_can_explore()` returns true only when all preconditions met
- `test_can_explore_rejects_charging` — charging gate
- `test_can_explore_rejects_on_blocks` — wheels_on_blocks gate
- `test_can_explore_rejects_unknown_battery` — `pct: None` returns False
- `test_can_explore_cooldown` — 20-min cooldown between explorations
- `test_can_explore_corrupt_meta_defaults_cooldown_active` — corrupt meta file = exploration blocked
- `test_explore_action_in_prompt_only_when_allowed` — `explore` only in reflection action set when `_can_explore()` is true
- `test_expression_else_logs_unhandled_action` — unhandled action produces log entry

### `tests/test_exploration.py` (new file)

- `test_exploration_log_nav_entry` — navigation entries written correctly with explore_id
- `test_exploration_log_observation_entry` — observation entries with landmark
- `test_exploration_log_trim_atomic` — trim to 100 entries uses atomic write
- `test_heading_estimate` — turn accumulator maps to correct fuzzy labels
- `test_heading_wraps_at_180` — accumulator wraps correctly at ±180°
- `test_curiosity_trigger_new_frigate_label` — new label triggers photo
- `test_curiosity_trigger_rate_limit` — max 1 photo per 30s enforced
- `test_curiosity_trigger_vision_failure_no_rate_limit` — failed vision doesn't count toward rate limit
- `test_daily_vision_cap` — photos skipped after 50 daily calls
- `test_landmark_promotion_to_notes` — interesting observations promoted to notes.jsonl
- `test_vision_failed_not_promoted` — failed vision observations NOT promoted
- `test_exploring_state_file_written` — exploring.json written on start, cleared on end
- `test_sonar_none_vs_999` — None means failure, 999 means far distance

### `tests/test_voice_loop.py`

- `test_validate_action_wander_mode` — `mode` param sanitised to `avoid`/`explore`
- `test_validate_action_wander_duration` — `duration` clamped to 30-300

All tests use `PX_DRY=1` and the `isolated_project` fixture. No hardware, no network, no API calls.

---

## Prerequisites

These must be deployed before exploration is enabled:

1. `read_battery()` must include `charging` field (Layer 3 requirement)
2. Awareness layer must propagate `charging` into awareness dict
3. `roaming_allowed` must be added to session state and API

---

## Non-goals

- No geometric SLAM or precise mapping — fuzzy landmark-based mental map only
- No path planning or return-to-start — SPARK wanders, it doesn't navigate to coordinates
- No new systemd service — exploration is dispatched by px-mind through the existing expression layer
- No changes to GREMLIN or VIXEN persona exploration (they can use avoid mode via voice command; explore mode is SPARK-only for now)
- No wheel encoder or IMU integration — design works with sonar + camera only
- No refactoring of px-mind into `src/pxh/mind.py` (tracked separately in issue #78)
- No changes to `tool-describe-scene` prompt format — landmark extraction is done locally in px-wander
