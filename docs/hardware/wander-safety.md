# Wander Safety

**Owns:** the cliff guard and the autonomous exploration engine.
`src/pxh/wander.py`, `bin/px-wander`, `bin/tool-wander`.

---

## Invariant

### The engine is a module, not a script

`bin/px-wander` is a thin bash wrapper — privilege self-elevation, calibration
guard, `exploring.json` write, `yield_alive` — around `src/pxh/wander.py`. The
engine is importable so the cliff guard can be regression-tested in-process.

Do not replace the launcher with a direct Python invocation; it does work the
engine relies on.

### Calibrate before wandering on a new floor

Place all grayscale sensors over the surface and run
`bin/px-wander --calibrate-cliff` (`--accumulate` keeps the darkest floor
across spots).

The ADC power-on latch is **rejected**, including a partially latched read, so
calibration fails closed until live sensor values appear. See
[hardware/gpio-and-alive](gpio-and-alive.md).

### The cliff guard is layered, and every layer earned its place

Motor noise tripped every early live run. Do not simplify any one of these
away:

1. **Median-of-3 sampling** — a single read is noise.
2. **Confirmation by persistence**, not by one stationary read.
3. **A stationary re-read** to confirm an in-motion trip.
4. **Sonar echo-timeout retries** (`SONAR_RETRIES`) before counting a sensor
   failure.
5. **Board-gap vs. drop discrimination by *width*, not depth** — a floorboard
   gap and a table edge look identical on depth.

`EDGE_ABORT_COUNT` and `SENSOR_FAIL_ABORT_COUNT` end the run rather than
letting it degrade.

### GPIO protection must be refreshed, not written once

Every live wander writes `state/exploring.json` **before** constructing
`Picarx`, and runs a 20s `_ExploringRefresher` thread for the whole run.
px-alive ignores the file once its mtime is older than 60s, so a single
start-of-run write protects only the first minute.

`wander.py` acquires a `GpioLeaseGuard` and **exports `PX_GPIO_LEASE_ID`** so
`tool-describe-scene` and `tool-announce` can borrow the lease.

### Probe-turn recovery reverses with the SAME steer angle

Bicycle model: mirroring the steer angle *doubles* the heading change instead
of undoing it. This is counter-intuitive and has been got wrong before.

### Vision timeouts are a strict ordering, not three independent numbers

`wander.DESCRIBE_SCENE_TIMEOUT` (165s) must outlive `tool-describe-scene`'s
entire run: `vision.CLAUDE_TIMEOUT` plus photo capture (including an 8s stream
pause) plus its **bounded** 60s `tool-voice` step.

That bound on the speech step is what stops wander killing the tool mid-run:
`tool-voice` blocks indefinitely when another process holds the audio device.

The relationship is pinned by
`test_describe_scene_timeout_has_margin_over_claude`, which reads the tool's
real constant rather than a literal **and** pins the surplus. A bare floor
check would stay green right up to the moment there was no margin left, which
is the failure it exists to catch.

---

## Related: autonomous racing

`bin/px-race` / `src/pxh/race.py` is the *other* autonomous motion system, and
it is documented in full in
[docs/SCRIPTS.md § bin/px-race](../SCRIPTS.md) — PD gains, the safety-layer
priority order, and per-lap learning. Not duplicated here.

Two things worth knowing before you touch it:

- **`pd_edge` uses a negative `Kp` (−20.0) on purpose.** Positive error (drift
  right) must produce a negative steer (left correction) for the error
  convention it uses. Unit tests use a generic `kp=20.0`; that is fine and not
  a contradiction.
- **The race loop makes no LLM, network, or audio calls.** It must stay that
  way — every one of those has an unbounded tail, and the loop's safety layers
  assume they run every cycle.
- **The grayscale power-on latch has not been verified against `race.py`.**
  `wander.wait_for_grayscale()` handles it; whether race does is unconfirmed.

---

## Why it looks like this

*History, not rule.*

Each cliff-guard layer was added after a specific live failure, which is why
the list reads as over-engineered and is not. The width-not-depth
discrimination came from SPARK reversing away from floorboard gaps in the
hallway until the run aborted.

`bin/tool-wander` runs `px-wander` under `sudo -n`, and for a long time that
environment flowed straight down into `tool-describe-scene`'s `claude` call, so
vision ran as root with root's `HOME` and silently returned
`FALLBACK_DESCRIPTION` on every real wander. Fixed (#202) by dropping privilege
with `runuser -u pi` for the CLI and passing `HOME` through. `HOME` alone was
the wrong fix — it littered root-owned files in pi's `~/.claude`. The cold
start that exposed it also forced `CLAUDE_TIMEOUT` 45→60 and
`DESCRIBE_SCENE_TIMEOUT` 150→165 **together**, which is why they must be
changed together.
