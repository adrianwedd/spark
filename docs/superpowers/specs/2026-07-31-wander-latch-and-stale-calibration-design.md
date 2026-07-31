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
the existing `settle_s`/`poll_s` pattern (`wander.py:434-435`) rather than a
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

Residual it does **not** close: a tear *within* a single channel's read. If the
ADC goes live between the two 8-bit halves of one channel's 16-bit transaction,
that channel returns a hybrid (latched MSB + live LSB) which differs from its
latched baseline, as do the two fully-live channels that follow — so `all()`
passes on a partly fabricated vector, and the hybrid reads high. Only defence 1
closes this. Whether robot_hat reads channels this way is unverified (the read
path is only inspectable on the Pi), which is a further reason the settle, not
the comparison, is the primary defence.

**Defence 2 widens Attack C.** `all(g != f ...)` is strictly stricter than
`gs != first`: the old rule accepted a floor where *any* channel showed LSB
noise, the new one needs all three. The change being shipped therefore increases
the false-reject rate that the exit condition below is instrumented to measure,
and the measurement baseline starts *after* that increase. This is not an
argument against defence 2 — it is still the right call — but a first firing is
evidence that this change tightened the rule, not evidence that the floor
changed. The exit condition's ladder is built accordingly.

### Defence 3 — exact signature rejection

Never accept a reading equal to the recorded latch `[2571, 3085, 3599]`, at any
point in the function.

**Not** a general arithmetic-progression test. `d=0` is an AP, so a uniform floor
with matched sensor gains reading `[700, 700, 700]` would be rejected forever on
that floor — a floor-dependent hard outage. If a shape test beyond the exact
constant is wanted later, restrict it to the observed gap `d=514`.

Defence 3 is hardware-specific by construction. A different robot_hat revision
may latch at a different value, at which point it silently stops firing. The same
is true of environmental drift on *this* hardware: if the latch is
`[2571, 3085, 3600]` on a cold day, exact equality stops matching. Both are
acceptable precisely because defence 3 is not load-bearing — defences 1 and 2
carry the design — but it means defence 3 must never be treated as the reason the
others can be relaxed. This is also why defence 2 is retained rather than
replaced.

#### Defence 3 must also live in `CliffGuard.check`

The startup framing of this whole design hid a hole in the driving loop. The
per-slice guard does not call `wait_for_grayscale` — it calls
`CliffGuard.check`, which tests only
`any(gs[i] <= self.cliff_ref[i] for i in range(3))` (`wander.py:537`) and
otherwise returns `"clear"` (`wander.py:539`). The latch reads *high*, so if the
ADC re-latches mid-wander (I2C reset, brownout, a HAT power glitch) the guard
returns "clear" on fabricated data and the robot keeps driving.

`CliffGuard.check` must reject the exact signature and return `"fail"`, which
callers already treat identically to "cliff" (contract documented at
`wander.py:531-532`; the existing `"fail"` return is at `wander.py:534-536`).
This is
independent of everything else in part 1 and is the only change here that
protects a wander already in motion.

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

**The strongest argument for keeping it is not about the latch at all.** The
change requirement is the only defence that catches a sensor stuck at *any*
value. Defence 1 is a timer and defence 3 matches one specific constant, so an
ADC that fails returning a plausible constant — `[400, 400, 400]`, or all zeros —
passes both and is caught only by the comparison. Dropping the change requirement
therefore does not merely trade Attack C for a rarer latch case; it removes
stuck-sensor detection from the design entirely, and nothing else here replaces
it. Any future decision to drop it must name what detects a stuck sensor instead
(variance over time, not a first-reading comparison).

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

A firing is *not* a reason to revisit the settle duration. Attack C is a live
sensor on a quiet floor; no settle length makes a floor noisier. The ladder is:

- **Fires once** → step defence 2 down from `all(g != f ...)` to the whole-list
  `gs != first`. Defences 1 and 3 already close Attacks A and B; defence 2's
  marginal value is only for hardware whose latch window exceeds
  `LATCH_SETTLE_MIN_S` or whose latch value differs from the recorded one.
  Stepping down keeps a generic check at half the strictness rather than
  trading it for nothing.
- **Fires often** → drop the change requirement early, but only together with a
  replacement generic check, never on its own.
- **Zero firings** across the three conditions above → drop it, as written.

#### Measured 2026-07-31 — gate passed, proceed as designed

Run on the live robot (`pi@192.168.0.236`), reading ADC `A0/A1/A2` directly so
no Picarx handle was taken from px-alive. 50 samples at 50ms, 5 discarded as
warm-up:

```
transitions: 49
identical consecutive (all three channels): 0
identical consecutive per channel A0/A1/A2:  [12, 12, 6]
distinct vectors: 40    min/max: [(220,225), (383,393), (209,216)]
```

Individual channels repeat 12–25% of the time, but all three never repeated
together in 49 transitions. Per transition the chance all three differ is ~50%;
across a 3s window at `GRAYSCALE_POLL_S` (~60 samples) the probability of never
accepting is negligible. **Defence 2 ships as specified.**

Caveat: one floor, one lighting condition, one session. The per-channel repeat
rates are high enough that a quieter surface could raise the all-three rate, so
the exit condition below remains the instrument for floors not yet measured.

#### Read path verified 2026-07-31 — Attack A is real

`Grayscale_Module.read()` is `[self.pins[i].read() for i in range(3)]`
(`robot_hat/modules.py:329`), and each `ADC.read()` issues its own
`write([chn, 0, 0])` then `read(2)` (`robot_hat/adc.py:47-49`). Three separate
I2C transactions per grayscale read, as this design assumed — the attack is not
hypothetical and defence 2 is not dead code.

The same source narrows the within-channel tear noted under defence 2: MSB and
LSB come from a *single* `super().read(2)`, one transaction, not two 8-bit
reads. A tear would have to occur inside one bus read. Keep the note, downgrade
the concern.

#### The measurement procedure, for other floors

To repeat the measurement on a new surface (this is what the exit condition asks
for), read the ADC channels directly rather than via Picarx — it needs no GPIO
and does not disturb px-alive:

```python
from robot_hat import ADC
pins = [ADC(p) for p in ("A0", "A1", "A2")]   # picarx.py:38 grayscale_pins
```

Sample ~50 times at 50ms after discarding a few warm-up reads, and count
consecutive identical readings per channel and across all three. All-three
identical is the number that matters: it is the rate at which defence 2 would
falsely refuse.

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

Note `px.set_cliff_reference(cal["cliff_ref"])` runs at `wander.py:729`, before
the settle — i.e. picarx's internal cliff reference is configured from a
calibration that has not yet been revalidated. Verified harmless rather than
assumed: `set_cliff_reference` is called exactly once in `wander.py:729` and
`px.get_cliff_status` is never called anywhere in the module, so the guard path
does not consult it. `CliffGuard.check` performs its own `safe_grayscale` read
and compares against its own `cliff_ref` (`wander.py:533-539`).

The ordering is therefore unchanged. If a future change starts using picarx's
built-in cliff status, this ordering becomes wrong and must move after
revalidation.

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

A third case fits neither pattern and is the most dangerous of the three: the
robot parked square to a drop with **all three** sensors over the void. All three
read near zero — which matches "all three drifted together" exactly, and would
therefore be told to recalibrate, over the void, producing the near-zero
`floor_ref` this section exists to prevent.

Pattern alone cannot separate that from a surface change. The rule needs an
absolute test as well: a changed surface still reads a *plausible floor value*,
while a void reads near zero. So:

1. All three within a plausible floor range but away from `floor_ref` → surface
   changed → recalibrate.
2. One or two below `cliff_ref`, others near `floor_ref` → parked at an edge →
   move the robot, then retry.
3. All three near zero → sensors are over a drop or not over a surface at all →
   move the robot, then retry. **Never** recalibrate.
4. **Anything else** — e.g. one channel 30% above `floor_ref` while the other two
   sit within tolerance, which is neither a shared drift nor a below-`cliff_ref`
   edge. Refuse, name the channel and the value, and advise recalibration *after*
   confirming the robot is on a normal surface. A catch-all is mandatory: a
   three-case rule with an unhandled fourth shape either crashes or silently
   falls through to "proceed", and "proceed" here means driving on a reference
   that just failed its own check.

All four must produce different reasons in both the log and the
`status: blocked` JSON.

### Revalidation must not fail on a single noisy sample

A single spike on one channel is normal ADC behaviour, and revalidation that
grounds the robot on one sample converts routine noise into a refusal to move —
the same class of failure as Attack C, in the part of the design added to
*prevent* a silent wrong answer.

Take N samples (N=5 is enough) and use the per-channel median, or require the
failure to persist across consecutive samples. A genuine surface change and a
genuine edge both persist; a spike does not.

### `load_cliff_calibration` must validate `floor_ref`

The loader today validates only `cliff_ref`:
`isinstance(ref, list) and len(ref) == 3` (`wander.py:508-510`). `floor_ref` is
written by `calibrate_cliff` (`wander.py:471`) but never checked on load.

Revalidation reads `cal["floor_ref"]`, and that access sits in `main()` —
*outside* the loader's `try/except`. On a calibration file that predates
`floor_ref`, or one hand-edited, that is an uncaught `KeyError`: a traceback with
no JSON status at all, which is strictly worse than failing closed. This design
introduces that path, so it must close it.

Extend the loader's validation to `floor_ref` (present, list, length 3, numeric)
and return `None` if it fails — which `main()` already handles as "not
calibrated → blocked" (`wander.py:717-720`). Revalidation then never sees a
malformed calibration.

### The abort message when parked at an edge is currently wrong

If the robot is parked at an edge, the sensors read the void consistently, so
`wait_for_grayscale` sees no change, returns `None`, and `main()` aborts at
`wander.py:741-745` with "grayscale sensor not live — cannot guard against
cliffs". Revalidation never runs, and the message names a sensor fault when the
truth is a parked position. This is the same root cause as Attack C and is closed
by the same split messages — the stable-reading path must name the value it saw,
so a human reading the log can tell "sensor stuck" from "I am sitting over a
drop".

### Residual: this protects "parked on", not "drives onto"

The motivating case is a calibration taken on hardwood and used on carpet 31 days
later. Revalidation samples one spot, once, at wander start — so if the robot
starts on hardwood and *drives onto* the carpet, revalidation passed and nothing
rechecks.

What part 2 actually closes is "parked on a different surface than it was
calibrated on". That is strictly better than a warning and worth shipping, but
the motivating case is only partly closed. Mid-wander recalibration is a much
larger design and is deliberately out of scope; the residual is named here so the
document is not read as closing it.

## Testing

`tests/test_wander.py`, using the existing `FakePx(grayscale=[...])` script
pattern and shortened module attributes (no real sleeps):

- torn read `[LATCH]*3 + [[245, 3085, 3599]] + [REAL_FLOOR]*5` → returns
  `REAL_FLOOR`, never the torn vector
- alternating latch → not accepted
- exact signature never accepted, at any position in the script
- `[700, 700, 700]` (d=0 AP) **is** accepted — guards against re-introducing a
  general AP test. The script must supply a *distinct* preceding baseline, e.g.
  `[[700, 701, 702]] + [[700, 700, 700]] * 3`. Written as `[[700,700,700]] * N`
  the test goes green via the change requirement without ever exercising AP
  handling — a vacuous regression guard. (`FakePx` pops scripted values per
  call, tests/test_wander.py:19-31.)
- a torn read where one channel's own bytes straddle the transition — document
  as untestable at this layer if `FakePx` cannot express it; defence 1 is what
  covers it
- `CliffGuard.check` returns `"fail"`, not `"clear"`, on the exact signature
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
- stale + all three near zero → blocked, "move the robot" reason, **not**
  "recalibrate"
- stale + one channel high and outside the band, others within → blocked via the
  catch-all, naming the channel
- a single-sample spike on one channel does **not** fail revalidation (median /
  persistence)
- calibration missing `floor_ref` → `load_cliff_calibration` returns `None` and
  `main()` blocks with the not-calibrated reason; no traceback, JSON always
  emitted

## Out of scope

Tracked separately, deliberately not in this design:

- `post_exists` list branch in `bin/px-blog` (dormant; `run_once` re-checks with
  the dict form before generating at `px-blog:993`, and again at write time
  under the lock at `px-blog:1057` and `px-blog:1118`)
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
- `bin/px-voice-test:15` — **confirmed broken on the Pi**, one-line fix. It
  enables the speaker amp via `python3 -c "from robot_hat import
  enable_speaker"`, but `px-voice-test:7` sources px-env, so that `python3`
  resolves to the venv, which has no `robot_hat`; `2>/dev/null || true` swallows
  the `ModuleNotFoundError`. The MAX98357A never gets its GPIO 20 toggle, which
  per CLAUDE.md is exactly the failure where `aplay` exits 0 and nothing plays.
  Cost is bounded — `px-voice-test` is a diagnostic tool, so it reports silence
  as success while someone debugs audio. Fix: pin `/usr/bin/python3` on that
  line.

  Provenance: verified on the Pi by the picar session (venv `python3` →
  `ModuleNotFoundError`, `/usr/bin/python3` → import succeeds); not reproducible
  from the m5 clone, where `picarx`/`robot_hat` are absent. This is also a live
  instance of PATH-resolved `python3` actually biting, which raises the priority
  of the interpreter item above.
