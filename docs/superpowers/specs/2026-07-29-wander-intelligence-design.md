# Intelligent Wander (px-wander Option B) — Design

**Date:** 2026-07-29
**Status:** APPROVED (design review with Adrian, this date)
**Scope:** Rework `bin/px-wander` explore mode + its px-mind/voice-loop integration into an
LLM-guided explorer with hard reflex safety. Option B of the three scoped tiers (A = plumbing
fixes only, C = persistent world model); A's fixes are milestone 1 here, C grows later from
this design's observation data.

## Why

Explore mode has run twice ever (last 2026-04-05) because `roaming_allowed` is an arm-switch
nobody arms. When it did run, it was wedged at 3–5cm repeating reverse-turn until stuck-abort.
The loop is purely reactive (greedy max-sonar-clearance), its "sweep" pans the camera servo
while reading a **chassis-fixed** sonar (all five angles measure one direction), its heading
estimate treats steering angle as heading change (fiction), and its observations are evicted
from `exploration.jsonl` by its own nav telemetry (100-line cap, ~10:1 nav:observation), so
every exploration is a first exploration. No LLM touches the loop at any point. QA 2026-07-11
item 10 (cumulative spatial memory) remains open; this design lays its groundwork.

## Architecture

Mirrors the three-layer mind: reflex safety (awareness-speed, no LLM), periodic LLM directives
(reflection-speed), post-hoc narrative (expression). The robot is safe at reflex speed; the
intelligence operates at its own slower cadence — the same separation that lets px-race run
with no LLM in the control loop.

### 1. Safety layer (no LLM, vetoes everything)

**Cliff guard — surface-dependent, fail closed.**
`Picarx.get_cliff_status(gm_val_list)` returns cliff when any grayscale reading is at or below
`cliff_reference[i]`. The reference is surface-dependent (dark floorboards/rugs read low and
can false-negative against the shipped default) — race.py requires explicit calibration for
exactly this reason. Therefore:

- **Calibration step:** `px-wander --calibrate-cliff` reads grayscale on the actual floor
  (race.py `_calibrate_surface` pattern), stores `{floor_ref: [l,c,r], cliff_ref: floor*0.65,
  ts}` in `state/wander_calibration.json` (atomic write), and applies it via
  `set_cliff_reference()` at wander start.
- **Fail-closed arming:** autonomous self-dispatch (px-mind `explore`) refuses to arm if
  `wander_calibration.json` is missing/corrupt or a pre-flight grayscale read fails.
  Uncalibrated is not safe — it is the opposite. Voice/API-initiated explore under the same
  rule.
- **In-loop guard:** grayscale polled immediately before and during every forward segment
  (including probe segments, §2) at reflex speed (<1ms read, race.py `safe_grayscale` I2C
  retry). Cliff → immediate stop + short bounded reverse + `edge_event`. A grayscale read
  failure mid-run is treated as a cliff (fail closed). 2 edge events → abort the wander.

**Existing gates unchanged:** charging → no motion (this was the working proxy for "don't
drive off the desk"), battery >20%, night silence, school/quiet calendar suppression,
`listening`, `wheels_on_blocks`, stuck-abort, SIGTERM cleanup, exploring.json/px-alive
handshake.

**Roaming gate becomes a kill switch.**
`roaming_allowed` flips to default `true` (live session + `state/session.template.json`); it
remains PATCHable (safety-critical, `confirm: true`) as an emergency off.

*What this loses, explicitly:* with `roaming_allowed` defaulted true, the only remaining
independent motion switch is `confirm_motion_allowed` — which also gates every
voice-commanded motion tool. The state "voice motion OK, autonomous roaming off" stops being
a default and becomes something you must remember to set. Accepted as kill-switch semantics
because the **arm condition is not the gate booleans alone**: self-dispatch additionally
requires a calibrated cliff reference (above). A fresh install ships `roaming_allowed: true`
but cannot roam until someone runs calibration — the flip is safe on fresh installs.

### 2. Honest sensing

- **Delete the camera-pan sonar sweep** (both copies: `sweep_distances`/`read_dist` and
  `_sweep_sonar`/`_read_sonar`). Sonar is chassis-fixed; the sweep measured noise.
- **Probe-turn scanning** when forward is blocked (<30cm): bounded reverse, steer ±30°, creep
  forward ~0.4s, read sonar; compare sides; commit to the better one.
  - *Probes are forward segments*: every probe creep runs under the cliff guard.
  - *Reverse is blind* (sonar faces forward): hard-bounded ≤0.3s at low speed; a bump/stall
    during reverse counts as an edge-event equivalent (feeds the same 2-strike abort).
- **Pan servo returns to its real job:** aiming the camera at the curiosity target before
  `tool-describe-scene` shots.
- **Drop the fake compass:** `turn_accumulator`/`_heading_label` deleted. Nav entries carry an
  honest event trail (`action` sequence: forward/probe_left/probe_right/reverse/edge_event)
  instead of fictional headings. The narrative layer doesn't need degrees; pretending we have
  them poisons memory.

### 3. LLM in the loop (Ollama on M5, no Claude budget)

- **Intention:** px-mind's `explore` dispatch passes the triggering reflection thought as
  `PX_WANDER_INTENT`. Voice-initiated explore may pass the transcript; absent → generic
  "explore and see what you find".
- **Directives:** every ~5 steps or after each observation, one short Ollama call
  (`http://M5:11434`, small model, prompt ≈ intent + last observations + sonar/edge summary,
  ~200 tokens) returns `{"directive": "continue|turn_left|turn_right|investigate|photograph|
  done", "reason": "..."}`.
  - **Wheels stopped while awaiting** the call (5s timeout) — never drive on a stale decision.
  - Any failure/timeout/garbage → reactive fallback until the next directive window; the
    wander never stalls on the network.
  - Safety layer vetoes any directive; the duration time-limit check in `_check_abort` runs
    **above** directive handling, and total directive calls are capped
    (`duration // DIRECTIVE_INTERVAL_S` + 2) so a continue-looping LLM cannot extend the run.
  - Test seam: `PX_WANDER_LLM_CMD` overrides the directive backend (same pattern as
    `CODEX_CHAT_CMD`).
- **Synthesis:** post-wander, one LLM call turns the observation list + intent into a
  first-person narrative, appended as a thought (salience scaled by discoveries, e.g. 0.4
  barren → 0.8 with interesting observations). Flows naturally to px-post (a good exploration
  can become a Bluesky post) and to notes for future reflections. Replaces the current
  hardcoded "I found N things" post-thought in mind.py.

### 4. Memory that accumulates (QA item 10 groundwork)

- **File split:** observations → `state/observations.jsonl` (cap 1000, FileLock, atomic
  rewrite on trim); nav telemetry stays in `state/exploration.jsonl` (cap 100, as now).
  Consumers move to the observations file: mind.py awareness (`recent_exploration`), mind.py
  explore hints, voice_loop.py context injection. Observation windows can no longer be flooded
  by odometry.
- **Frigate freshness:** `_query_frigate()` gains `after=<wander start epoch>` so curiosity
  triggers only on events from *this* wander, not this morning's cat.

### 5. Cleanup riding along

- Single writer for `last_explore_ts`: px-wander already writes it at explore **start**
  (crash-safe — a mid-run crash still leaves the cooldown); mind.py's duplicate pre-launch
  write (`mind.py` ~3150-3156) is deleted. *Residual accepted:* if px-wander dies before its
  start-of-run write (launch failure), px-mind may re-dispatch next cycle; those paths are
  quick-exit, motionless, and logged.
- Deduplicate the copy-pasted sonar helpers (one set survives, minus the sweep).
- `speak()` during wander becomes non-blocking (no `ap.wait()` in the drive path).
- Fix the test-suite `LOG_DIR` leak that writes pytest noise (synthetic thoughts, "explore
  injection: action enum not found") into the real `logs/px-mind.log`.

## File changes

| File | Change |
|---|---|
| `bin/px-wander` | Explore-mode rework: cliff guard, calibration mode, probe-turn, directive loop, synthesis call, compass removal, helper dedupe, non-blocking speak |
| `bin/tool-wander` | Pass through `PX_WANDER_INTENT`; keep the explore-mode `roaming_allowed` pre-check as a fast-fail (avoids a pointless sudo spawn) — px-wander's own check remains authoritative |
| `src/pxh/mind.py` | Explore dispatch: calibration-armed check, intent pass-through, delete duplicate meta write, synthesis replaces hardcoded post-thought; consumers → observations.jsonl |
| `src/pxh/voice_loop.py` | Context injection reads observations.jsonl; `tool_wander` validation passes intent |
| `src/pxh/spark_config.py` | Directive/synthesis prompts + tunables (DIRECTIVE_INTERVAL_S, caps) |
| `state/session.template.json` | `roaming_allowed: true` |
| `tests/` | Cliff guard (fake grayscale incl. read-failure fail-closed), calibration gating, directive parsing/fallback/cap, reverse bound, file split, LOG_DIR leak fix |
| `CLAUDE.md` | Wander section: kill-switch semantics, calibration requirement |

## Testing

Dry-run paths for all new logic under `isolated_project`: injected grayscale sequences
exercise cliff guard and fail-closed arming; `PX_WANDER_LLM_CMD` stub exercises directive
parse/fallback/cap and synthesis; probe-turn and reverse bounds unit-tested on the
state-machine level (no hardware). Live validation: run `--calibrate-cliff` on the actual
floor, then a supervised `--duration 60` floor run before enabling self-dispatch.

## Rollout order

1. Safety + sensing (cliff guard, calibration, probe-turn, compass removal) — mergeable alone
2. Memory split + Frigate freshness + cleanup — mergeable alone
3. LLM directives + synthesis + intent plumbing
4. Gate flip (`roaming_allowed: true` + calibration-armed self-dispatch) — last, after 1–3
   proven on-floor
