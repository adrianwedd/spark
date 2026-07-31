# Wander: ADC latch acceptance and stale cliff calibration

Date: 2026-07-31
Branch: `feat/intelligent-wander`
Status: design, approved for planning

Two changes to `src/pxh/wander.py`, treated as one design because they touch the
same function and the same moment — the first seconds after `Picarx()` — and the
second consumes the first's return value.

## Background

`wait_for_grayscale` exists to reject the robot_hat ADC power-on latch, observed
on this hardware as `[2571, 3085, 3599]` for roughly 0.75s after `Picarx()`. The
latch sits far above any calibrated cliff threshold, so a guard check taken
inside the window reports "clear" while the car sits at the edge of a step.

It proves the sensor is live by requiring a reading that differs from a baseline
sample (`wander.py:452`, `if gs != first: return gs`). Three independent reviews
converged on the same conclusion: change-detection is unreliable in **both**
directions.

### Attack A — torn read at the latch boundary (accepts fabricated data)

`gs != first` compares whole lists and returns a single bool, so **one** channel
moving accepts all three. The three channels are three separate I2C
transactions, so a read straddling the moment the ADC goes live yields e.g.
`[245, 3085, 3599]` — ch0 live, ch1/ch2 still latched. It differs from the
baseline, so it is returned as live. Two of three channels are fabricated, and
fabricated *high*, which reads as "floor present, no cliff".

This needs no hardware fault — only that the sample window lands between two I2C
transactions.

### Attack B — alternating or partially-releasing latch (accepts fabricated data)

Same root cause. Any latch that changes on one channel defeats a whole-list
inequality.

### Attack C — quiet floor (rejects a healthy sensor)

If the latch has already expired before the baseline read, a noise-free ADC over
a uniform floor returns `gs == first` for the whole `GRAYSCALE_SETTLE_S` (3.0s)
window, returns `None`, and wander refuses to move on a perfectly live sensor.
5a9aea30's failed-baseline rebaseline widened this slightly (after a failed
baseline you now need two *distinct* live readings), which makes the commit
message's claim that "a genuine floor reading after the latch is still returned"
false in the stable case. Pre-existing, not introduced.

No measurement of the real false-reject rate exists: `logs/px-wander.log`'s
grayscale series are test fixtures from `tests/test_wander.py:96`, and
`wander_calibration.json` holds a single sample, not a series. Attack C is
believed rare (calibration succeeded against a real floor on 2026-07-29 and no
false refusal has been reported) but has never been observed either way.

## Part 1 — `wait_for_grayscale`

Three defences, each proving something the others cannot.

### Defence 1 — hard wall-clock settle (primary)

Sleep `LATCH_SETTLE_MIN_S` **before the baseline read**, not before the loop.
Sleeping before the loop leaves the baseline inside the latch window and the
whole run compares against a latch value — which works, but is not what the
settle is for.

Default 1.5s, roughly 2x the observed 0.75s window. The latch window is
wall-clock bounded, so waiting past it is direct evidence rather than inference.

No plumbing from `Picarx()` construction is needed: both call sites construct
`px` immediately before calling (`wander.py:708` → `709` → `467`, and
`wander.py:728` → `741`), so a sleep at the top of the function is equivalent to
measuring from construction.

`LATCH_SETTLE_MIN_S` must be a **module attribute read at call time**, following
the existing `settle_s`/`poll_s` pattern (`wander.py:433-434`) rather than a
captured default arg. `tests/test_wander.py` has 18 call references across 34
tests; a hardcoded 1.5s sleep would add ~30s to that file alone.

This defence alone closes Attacks A and B: after the settle, no read in the
window is a latch value, so nothing fabricated can be accepted regardless of what
the comparison does.

It does **not** close Attack C on its own, because the change requirement is
retained (see below) — a quiet floor still yields `gs == first` and still
returns `None`. That is a deliberate trade, and the split log messages exist to
measure exactly how often it costs anything.

### Defence 2 — per-channel acceptance

Replace `if gs != first` with:

```python
if all(g != f for g, f in zip(gs, first)):
    return gs
```

Every channel must have moved. Defence in depth for a settle that proves too
short on a cold boot or different hardware. Closes Attacks A and B on its own.

Residual: an ADC alternating on all three channels simultaneously — the
irreducible limit of any change-detector.

### Defence 3 — exact signature rejection

Never accept a reading equal to the recorded latch `[2571, 3085, 3599]`, at any
point in the function.

**Not** a general arithmetic-progression test. `d=0` is an AP, so a uniform floor
with matched sensor gains reading `[700, 700, 700]` would be rejected forever on
that floor — a floor-dependent hard outage. If a shape test beyond the exact
constant is wanted later, restrict it to the observed gap `d=514`.

Defence 3 is hardware-specific by construction. A different robot_hat revision
may latch at a different value, at which point it silently stops firing. This is
why defence 2 is retained rather than replaced.

### Retained

- 5a9aea30's failed-baseline rebaseline (adopt the first successful read as the
  new baseline rather than returning it).
- Fail-closed `None`; callers treat it identically to "cliff".

### The change requirement is kept, and made observable

A stable, non-signature, plausible reading is still rejected. The decision to
keep it rests on **observability asymmetry**, not on which failure is likelier:

- Keeping it fails as `None` → "refusing to move" logged → `status: blocked`.
  Loud, self-announcing, and in the safe direction.
- Dropping it fails as accepting a stuck non-signature reading. Silent, and in
  the direction that drives.

Defence 2 is also the only hardware-agnostic check in the stack; defences 1 and 3
are both calibrated to this specific robot.

Because Attack C has never been measured, the two `None` paths must be
distinguishable in the log — today both emit "never left its power-on latch",
which is false in the stable case and sends a reader down the wrong path:

- never left the known signature → current message, fail closed
- stable, non-signature, plausible reading for the full window → its own message
  naming the value, fail closed

#### Exit condition (this is a decision rule, not "we'll see")

Keeping the change requirement is a deliberate, instrumented trade, not an
unfortunate residual. Anyone who hits the stable-reading log line and "fixes" it
by dropping the requirement is deleting the only hardware-agnostic defence in the
stack without knowing that is what they are doing.

Drop the change requirement only when **all** of these hold:

1. At least 30 days of live wandering have elapsed since this design shipped.
2. The stable-non-signature log line has fired zero times in that period.
3. There were at least 20 distinct wander sessions across at least two floor
   surfaces (the failure is floor-dependent — a single carpet is not evidence
   about hardwood).

If the line fires even once, the false-reject is real: keep defence 2 and
revisit the settle duration instead.

If it fires *often*, that is the signal to drop the requirement early — but only
together with a replacement generic check, never on its own.

## Part 2 — stale cliff calibration

Today staleness is a warning only and the stale calibration is still returned
(`wander.py:498`, `514-517`). A calibration that no longer matches the floor is a
silently incorrect cliff guard — the same failure class as the latch. But a hard
refusal grounds an autonomous wander at 02:00 with no human present to run
`--calibrate-cliff`.

### Revalidate, read-only

On a stale calibration, compare it against a live reading:

- Every channel must exceed its `cliff_ref` (otherwise the current floor already
  reads as a cliff).
- Every channel must sit within tolerance of `floor_ref`.

Pass → proceed with the existing calibration for **this wander only**.
Fail → refuse, with a reason (see remediation below).

**No timestamp refresh, no write.** A live check samples one spot of wherever the
robot happens to be sitting; letting that reset `ts` would mint another
`CALIBRATION_STALE_S` (30 days) of authority for what is a deliberate
all-sensors-on-the-surface procedure. Read-only also avoids flipping
`wander_calibration.json` between root and pi ownership — `--calibrate-cliff`
self-elevates (`bin/px-wander:20-27`) and writes root-owned 0644
(`wander.py:481-482`), while normal wander runs as pi.

### Reuse the reading `main()` already has

`main()` calls `wait_for_grayscale(px)` at `wander.py:741` and currently discards
its return value. Revalidation consumes that reading instead of taking its own:
one I2C read, and it is guaranteed to be the post-settle one.

Note `px.set_cliff_reference(cal["cliff_ref"])` runs at `wander.py:731`, before
the settle. That ordering is unchanged; revalidation happens after, and refusal
returns before any motion.

### Tolerance band, asymmetric

`CLIFF_MARGIN = 0.65`, so `cliff_ref` is 65% of `floor_ref` — a 35% margin. A
symmetric ±20% band sits comfortably inside it (0.80 > 0.65).

The two sides are not equally risky. The low side is backstopped by `cliff_ref`.
The high side is backstopped by nothing, and high is the *desensitising*
direction: a floor reading brighter than `floor_ref` widens the gap to a fixed
`cliff_ref`, so shallow drops trigger later. Tighten the high side accordingly
(e.g. −20% / +10%). Not alarming in absolute terms — a real cliff reads near zero
— but free to get right.

### Remediation must distinguish two situations

"Every channel must exceed `cliff_ref`" fails in two cases that need **opposite**
human responses:

1. **The surface changed** (hardwood → carpet): all three channels drifted
   together. Correct advice: recalibrate.
2. **The robot is parked at an edge**: one or two channels below `cliff_ref`
   while the others sit near `floor_ref`, because there really is a drop.
   Correct advice: move the robot, then retry.

Emitting "run `--calibrate-cliff`" in case 2 is actively dangerous: a human who
follows that instruction correctly runs calibration with a sensor over the void,
persisting a low `floor_ref` → a low `cliff_ref` → a permanently *less* sensitive
guard, via a documented procedure followed properly. That is a worse outcome than
the stale calibration this change exists to catch.

The cases are distinguishable by the pattern above, and must produce different
reasons in both the log and the `status: blocked` JSON.

## Testing

`tests/test_wander.py`, using the existing `FakePx(grayscale=[...])` script
pattern and shortened module attributes (no real sleeps):

- torn read `[LATCH]*3 + [[245, 3085, 3599]] + [REAL_FLOOR]*5` → returns
  `REAL_FLOOR`, never the torn vector
- alternating latch → not accepted
- exact signature never accepted, at any position in the script
- `[700, 700, 700]` (d=0 AP) **is** accepted — guards against re-introducing a
  general AP test
- stable non-signature floor → `None` with the *stable* log message, not the
  latch one
- both 5a9aea30 regressions still pass
- settle is read from the module attribute at call time (tests stay fast)

Part 2:

- stale calibration + live reading within band → proceeds
- stale + all three drifted together → blocked, "recalibrate" reason
- stale + one channel below `cliff_ref`, others near `floor_ref` → blocked,
  "move the robot" reason
- revalidation never writes `wander_calibration.json` (assert mtime unchanged)
- fresh calibration → revalidation does not run

## Out of scope

Tracked separately, deliberately not in this design:

- `post_exists` list branch in `bin/px-blog` (dormant; `run_once` re-checks with
  the dict form at `px-blog:969-970`)
- interpreter pinning — reduced to fixing the misleading CLAUDE.md sentence
  ("bin scripts run under `/usr/bin/python3` (not venv)" is true only of the
  explicit-GPIO scripts), adding a test for the invariant that already holds, and
  deciding `bin/tts-glados-server` deliberately.

  **This constraint must be copied into that work item, not left here.** Any
  interpreter test must assert "sources px-env before invoking python", not
  "always runs the project venv" — the activate at `px-env:34-37` is guarded by
  `-z "${VIRTUAL_ENV:-}"`, so a developer with another venv already active gets a
  different interpreter. Under systemd `VIRTUAL_ENV` is unset, so it is
  deterministic there. A test asserting the stronger claim passes in CI and
  misleads exactly the reader it was written to protect. The reasoning is
  recorded here; the constraint belongs where the test gets written.
- `bin/px-voice-test:15` enables the speaker amp via venv `python3 -c "from
  robot_hat import enable_speaker"` with `2>/dev/null || true`. If the Pi venv
  cannot see system site-packages this silently no-ops and the amp never enables.
  Unverified; check on the Pi with `.venv/bin/python3 -c "import robot_hat"`.
