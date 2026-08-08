# Handoff — wander latch / stale calibration design

Date: 2026-07-31
Branch: `feat/intelligent-wander` @ `f2a545d4` (pushed; Pi checkout fast-forwarded to match)

## Where this stands

The design is **complete, reviewed by four independent passes, and both of its
unverified premises have now been measured on the live robot**. It is ready for
an implementation plan. No code has been written yet — every commit so far is
the spec document.

Spec: `docs/superpowers/specs/2026-07-31-wander-latch-and-stale-calibration-design.md`

### Next action

Write the implementation plan (superpowers:writing-plans) from that spec. Nothing
blocks it. Adrian has not given a final sign-off on the spec text itself; ask
before planning if that matters.

## What the design says, in one paragraph

`wait_for_grayscale` proves the grayscale ADC is live by requiring a reading that
differs from a baseline. That proxy fails in both directions, so the design adds
a hard wall-clock settle as the primary proof, tightens the comparison to
per-channel (`all(g != f ...)`), rejects the exact latch signature — including
inside `CliffGuard.check`, which the original framing missed entirely — and
converts stale cliff calibration from warn-and-use into a read-only
revalidation with four distinct remediation messages.

## Measurements taken 2026-07-31 (both gates passed)

Run against `pi@192.168.0.236`. Both are recorded in the spec with full numbers.

1. **Noise floor** — 50 samples, 50ms apart, reading ADC `A0/A1/A2` directly
   (no Picarx handle, px-alive undisturbed). All three channels never repeated
   together in 49 transitions; individual channels repeat 12–25%. Defence 2
   ships as designed. One floor, one session — the spec's exit condition remains
   the instrument for other surfaces.
2. **Read path** — `Grayscale_Module.read()` is
   `[self.pins[i].read() for i in range(3)]` (`robot_hat/modules.py:329`), each
   `ADC.read()` its own `write`+`read(2)` pair (`adc.py:47-49`). Three I2C
   transactions, so the torn-read attack is real and defence 2 is not dead code.

## Review history — who found what

Four passes, deliberately adversarial. Worth reading before reopening a decision
that looks obviously wrong; it probably already got argued.

- **Fable 5 advisor** — killed the "reject any arithmetic progression" idea
  (`d=0` is an AP; `[700,700,700]` would be rejected forever) and the
  timestamp-refresh on revalidation (single-spot check laundering 30 days of
  authority onto a deliberate procedure).
- **picar session** — the `all()` insight: `gs != first` is a whole-list compare,
  so one channel moving accepts all three. Also caught that defence 2 *widens*
  the false-reject it is instrumented to measure, and that the exit condition's
  original remedy (revisit the settle duration) could not fix the failure its
  trigger detects.
- **agy** — the `CliffGuard.check` hole (latch reads high, passes
  `<= cliff_ref`, returns "clear" mid-wander) and the all-sensors-over-void case
  in part 2's remediation.
- **hermes** — `load_cliff_calibration` never validates `floor_ref`, so part 2's
  revalidation could raise an uncaught `KeyError` in `main()`; the missing
  catch-all case; single-sample noise sensitivity; and the strongest pro-keep
  argument for the change requirement, which nobody else named — it is the only
  defence against a sensor stuck at *any* value.

### Process note that earned its place

Five confident-but-wrong claims were made this session from code fragments where
the disambiguating line was a few lines away — by agy, hermes, the picar session,
and twice by me (the "three-way interpreter split", which then propagated through
three reviewers because each trusted the last). Attach an evidence rule to QA
prompts (cite file:line, quote the line, read whole functions) and re-verify
findings before acting on them. Two models agreeing is not independent evidence.

## Robot state as of handoff

- **eth0 is DOWN**; the Pi is reachable on **wlan0, `pi@192.168.0.236`**. The
  `192.168.0.27` eth0 reservation does not ping — this is why the picar Claude
  session disconnected. Reconnect on `.236`.
- **px-alive is restart-looping** (systemd restart counter 28). A manual
  instance holds GPIO: pid 547528, parent `sudo -n bin/px-alive`, owner of
  `logs/px-alive.pid`. The systemd unit exits every 15s with "another px-alive
  already running". The PID guard is working; the orphan is the problem.
  Probably abandoned when eth0 dropped. Fix: `sudo kill 547528`, systemd
  recovers within 15s. **Not done — left as Adrian's call** (root GPIO process
  on a live robot).
- **px-post health is `failing`, correctly.** 42 consecutive failures,
  `"qa gate rejected 18 consecutive thoughts — nothing published"`, queue depth
  200, `total_posted: 0`, last success 03:29Z. The signal from `59b80cdb` working
  as designed, reporting a real backlog. Unrelated to this spec; worth its own
  look.
- All other components `ok`: px-mind, px-mind-reflection, px-api-server,
  px-battery-poll, px-blog, px-frigate-stream, px-wake-listen.
- `state/exploring.json` is `{"active": false}` — not a stale explore lock.

## Loose ends, none blocking

- **`bin/px-voice-test:15` is confirmed broken** (verified on the Pi by the picar
  session): the amp-enable runs under the venv, which has no `robot_hat`, and
  `2>/dev/null || true` swallows the error, so the MAX98357A never gets its GPIO
  20 toggle. Diagnostic tool only, so it reports silence as success while
  someone debugs audio. One-line fix: pin `/usr/bin/python3`.
- **Interpreter item** reduced to: fix the CLAUDE.md sentence "bin scripts run
  under `/usr/bin/python3` (not venv)" (true only of explicit-GPIO scripts, and
  it misled three reviewers), add a test for the invariant that already holds,
  and decide `bin/tts-glados-server` deliberately. Any such test must assert
  "sources px-env before invoking python", not "runs the project venv" —
  `px-env:34-37` is guarded by `-z "${VIRTUAL_ENV:-}"`.
- **`post_exists` list branch** in `bin/px-blog` — live but provably dormant,
  with a second backstop at `px-blog:993`. Lowest priority.
- **Uncommitted `bin/px-wake-listen` future import** is in `git stash` on the m5
  clone, not deleted. Unnecessary on both machines (m5 venv is 3.14.6, Pi venv is
  3.11.2, and the script execs the venv interpreter at line 8). Drop it when
  convenient.
