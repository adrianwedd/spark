# Resource containment (#217 / #218 / #219)

## Why

[#217](https://github.com/adrianwedd/spark/issues/217) correlates, twice on
two separate days, a brcmfmac SDIO `-110` control-path timeout (Wi-Fi dies
with no self-recovery) with generic host resource starvation: load ~18 on 4
cores, swap nearly exhausted, userspace deadlines blown by orders of
magnitude. A third data point on 2026-08-20 caught a local `spark-brain`
handshake failing in the same second as the SDIO timeout — the first
cross-subsystem evidence that the shared cause is host-wide CPU/IO/memory
contention, not anything RF-specific.

[#218](https://github.com/adrianwedd/spark/issues/218) found the reason
containment couldn't previously be built at all: the kernel cmdline carried
`cgroup_disable=memory`, so `MemoryHigh=`/`MemoryMax=` on any systemd unit
parsed cleanly and did nothing (`MemoryCurrent=[not set]`), and `/proc/pressure`
didn't exist. **This was already fixed live before this work started** —
`/boot/firmware/cmdline.txt` carries `cgroup_enable=memory cgroup_memory=1
psi=1` (dated 2026-08-19) and the host had been rebooted onto it
(`cgroup.controllers` lists `memory`; PSI readable) — but #218 the issue was
still open because nothing had used the restored instrumentation yet.

[#219](https://github.com/adrianwedd/spark/issues/219) named `px-wake-listen`
as the largest reducible memory consumer: ~555M RSS steady state, a single
unreclaimable ~446M heap, 900M peak observed once.

## What this PR does

Per-service `systemd MemoryHigh`/`MemoryMax`/`OOMPolicy` (+ `CPUWeight` on a
few) via version-controlled drop-ins under `systemd/*.service.d/`. See
`systemd/README.md` for install/verify commands.

**Design constraint:** a runaway component fails inside its own cgroup first.
On cgroup v2 (confirmed live: unified hierarchy, `memory` controller present),
a `MemoryMax` breach triggers the kernel's cgroup-scoped OOM killer, which
only ever selects a victim from *inside* that cgroup — this is what makes the
containment structurally local rather than a matter of tuning.

## What was NOT banked from a prior commit

The goal that produced this PR referenced a "banked containment" commit,
`0a1cf807`. On inspection that commit is unrelated: it's
`fix(tests): make ordinary pytest incapable of controlling the live robot`
(subprocess/os.system guards in `tests/conftest.py`), not systemd resource
limits. No memory/CPU containment code existed anywhere in the repo before
this PR — it was designed from scratch against a fresh live measurement.

## Baseline (measured live, 2026-08-20)

`systemctl show <unit> -p MemoryCurrent` (authoritative cgroup accounting,
now that #218 restored it) plus process RSS/PSS from `/proc/<pid>/smaps_rollup`:

| Service | MemoryCurrent | RSS | PSS | Notes |
|---|---:|---:|---:|---|
| px-brain | ~500M | — | — | single spark-brain session (post-#242); see drop-in comment for sizing |
| px-wake-listen | 522M | 571M | 561M | #219's named outlier; ~446M single anon heap, matches prior finding |
| px-tts-glados | 501M | 485M | 474M | new finding — comparably sized to wake-listen, not previously characterized |
| px-frigate-stream | 122M | — | — | 41 tasks (go2rtc + ffmpeg + rpicam-vid) |
| px-api-server | 54M | 55M | 46M | |
| px-post | 52M | 59M | 50M | |
| px-mind | 60M | 47M | 38M | |
| px-alive | ~22-32M | — | — | root; small; safety-critical (GPIO/servo) |
| px-battery-poll | 19M | — | — | root |
| px-evolve | 15M | — | — | idle baseline only |
| px-blog | 17M | — | — | idle baseline only |
| cloudflared | 37M | — | — | |

Host: MemTotal 3796M, used ~2889-2924M fluctuating, available ~872-907M,
swap 228-233M/511M used. Load average 8.02/6.10/4.14 at first read (elevated —
see "Concurrent live event" below).

PSI at measurement time (already elevated **before** any change made here):

```
memory: some avg10=7.35  avg60=4.31  avg300=1.78
io:     some avg10=28.34 avg60=48.56 avg300=37.18
cpu:    some avg10=6.15  avg60=5.19  avg300=4.16
```

## Design table

| Service | MemoryHigh | MemoryMax | CPUWeight | Rationale (full text in each drop-in) |
|---|---:|---:|---:|---|
| px-brain | 960M | 1536M | 150 | ~3x expected single-session steady state; raised priority — #217's strongest evidence implicates this process losing scheduler contention |
| px-wake-listen | 896M | 1280M | default | **Recalibrated 2026-08-23 — see "Track B" below.** High clears the measured 707-730M one-time model-load peak; the original 640M/1024M pair (superseded) was set below that peak and guaranteed throttling on every start |
| px-tts-glados | 576M | 768M | default | conservative multiple; no documented peak exists (new finding) |
| px-frigate-stream | 224M | 384M | 50 | bursty; lowered priority — should yield under contention |
| px-api-server | 128M | 256M | default | headroom for request spikes |
| px-post | 128M | 256M | default | |
| px-mind | 128M | 256M | default | |
| px-alive | 96M | 192M | 200 | tiny footprint; raised priority — this is the daemon whose own watchdog #217/#205 keep tripping |
| px-battery-poll | 64M | 128M | default | |
| px-evolve | 64M | 192M | 50 | idle baseline tiny; Max covers an active worktree/test run; lowered priority — background, on-failure |
| px-blog | 64M | 192M | 50 | idle baseline tiny; Max covers an active compose pass; lowered priority |
| cloudflared | 96M | 192M | default | not implicated in #217 |

**Deliberately not contained here:** the interactive/operator Claude session
(`user.slice/user-1000.slice/session-3.scope`, ~1GB observed, including the
session that produced this PR) — #217's own text flags this class of session
as a contributor ("why a third Claude operator session pushes the host to
~1.7GB"), but the goal's explicit per-service target list names only system
daemons, and applying a cgroup limit to a live interactive session risks
capping the very session doing this work. Recommended as a separate,
deliberately-scoped follow-up rather than guessed at here. **That follow-up
is `bin/px-claude-dev`, below** — a 2026-08-23 re-measurement confirmed this
class of session was still the single largest live memory consumer on the
host (~862 MiB PSS for `claude` alone, ~1.43 GiB current / 1.67 GiB peak for
the whole scope, still uncontained).

## Operator/development Claude containment (`bin/px-claude-dev`, 2026-08-23)

`bin/px-claude-dev` wraps `claude` (an operator working the repo — not
`spark-brain`, which is already contained above) in its own transient
`systemd-run --user --scope --collect`, with `MemoryHigh=1024M` /
`MemoryMax=2048M` by default (`PX_DEV_CLAUDE_MEMORY_HIGH` /
`PX_DEV_CLAUDE_MEMORY_MAX` override either). Use it in place of a bare
`claude` invocation for repo work on the robot.

**Why a new scope wrapping only the `claude` process, not a cap on the
existing SSH session-N.scope or on `user-1000.slice`:**

- **Not `user-1000.slice`:** PulseAudio, PipeWire and `filter-chain` all live
  under `user@1000.service/session.slice/`, a **sibling** of the raw SSH
  `session-N.scope`, not a child of it — confirmed by reading each service's
  `/proc/<pid>/cgroup` live. A `user-1000.slice`-wide cap would throttle audio
  infrastructure the same way it throttles `claude`; this is exactly what the
  #219-era goal warned against doing "blindly."
- **Not the existing `session-N.scope`:** that scope is the operator's whole
  SSH login shell, not just `claude` — capping it would also throttle
  unrelated diagnostic work (`journalctl`, `tail`, an editor) done in the same
  shell, which was never what was observed to balloon. It is also the scope
  the *currently running* investigation/implementation session is itself
  using; a script cannot safely cap the cgroup it is executing inside without
  risking self-OOM mid-task.
- A dedicated scope around just the `claude` child avoids both: it bounds the
  ~862-912 MiB PSS process tree the 2026-08-23 measurement actually
  implicated, while leaving the rest of the SSH session and all audio/session
  infrastructure unaffected.

This only affects **future** sessions launched through the wrapper — it does
not retroactively cap any session already running, including the one that
wrote it. `docs/operations/resource-containment.md`'s existing acceptance-
testing pattern (A. baseline, B. bounded pressure test, C. recovery) applies
here too: verify with `systemd-run --user --scope -p MemoryHigh=100M -p
MemoryMax=150M -- /bin/true` (harmless, self-cleaning) before trusting the
wrapper against a real session, and confirm via `systemctl --user status
<scope>` that PulseAudio/PipeWire units are unaffected while a capped
`px-claude-dev` session runs.

**Honesty about the ceiling, not a guarantee:** the sum of every `MemoryMax`
above (~4.2G) exceeds total host RAM (3796M). Per-cgroup hard caps are not a
reservation scheme — they bound any *single* runaway locally, which is what
#217 needs, but they do not mathematically rule out every service
simultaneously hitting its own peak at once. `MemoryHigh` (throttle, not
kill) is what actually protects against a slow multi-service squeeze, by
design. `tests/test_systemd_containment.py::test_sum_of_memory_max_is_disclosed_not_assumed_safe`
pins this fact so a future edit can't silently un-disclose it.

## Track B: px-wake-listen allocator-retention fix and recalibration (2026-08-23)

The original 640M/1024M pair above was sized against a single steady-state
RSS number (522-571M, see baseline table) without distinguishing an
intrinsic operating cost from a fixable defect. Two live incidents on
2026-08-23 forced the distinction: `px-wake-listen` wedged in
`mem_cgroup_handle_over_high` (synchronous reclaim under `MemoryHigh`) for
**47.9 minutes**, and a subsequent restart wedged again for **~4m54s** before
being SIGKILLed on `Restart=always`'s own timeout — both while
`Type=simple` meant systemd had no way to know either wedge was happening.

**1. Allocator retention was fixable, not intrinsic.** `bin/px-wake-memprofile`
(repeated-cycle mode, `PX_MEMPROFILE_CYCLES=8`) isolated the two components of
this service's memory: model load and one untrimmed inference legitimately
cost 707-730M PSS/RSS (Vosk grammar + SenseVoice's onnxruntime session,
inference itself adding only ~12M) — but that figure never fell back down on
its own. `gc.collect()` alone (Python-level references only) recovers a small
fraction of it; `malloc_trim(0)` via `ctypes.CDLL("libc.so.6")`, run **without
unloading either model**, is what returns the freed-but-retained glibc arena
memory to the OS. Run live twice on 2026-08-23 with the service stopped:

| Stage | PSS | RSS |
|---|---:|---:|
| after both models load, before any inference | 707-718M | 718-730M |
| after one **untrimmed** inference | 719M | 729M (+~12M — confirms inference itself is cheap) |
| after the first `malloc_trim(0)` call | 449M | 460M |
| after 7 further inference+trim cycles | 449M (±70K noise) | 460M exactly, **zero drift** |

The flat result across 8 repeated cycles is the direct answer to "verify
repeated cycles do not accumulate memory": they don't, once every cycle
trims. `bin/px-wake-listen`'s `_release_transient_memory()` runs this same
`gc.collect()` + `malloc_trim(0)` pair from the single chokepoint
`_do_transcribe()` returns through — every STT fallback path, including a
full fall-through to Vosk — pinned by
`tests/test_wake_memory_containment.py`.

**2. MemoryHigh/MemoryMax recalibrated from that evidence, not from a round
number.** The critical distinction the recalibration turns on: `malloc_trim`
cannot lower the *load-time* peak, because it only runs after a completed
transcription — the 707-730M figure happens once per process lifetime,
before any trim call exists to catch it. Sizing `MemoryHigh` to the
post-trim steady state (449-460M) would have thrown the ceiling straight
back below the unavoidable load peak — reproducing the exact failure the old
640M value already had. `MemoryHigh=896M` clears the measured peak with
~25-27% headroom instead; `MemoryMax=1280M` keeps ~43% margin above that and
stays comfortably above the historical 900-924M VmHWM peak (the pre-fix
worst case, when nothing ever trimmed the arena). Full reasoning lives in
`systemd/px-wake-listen.service.d/10-containment.conf`'s comment block.

**3. Bounded startup contract.** `Type=simple` is why the 47-minute wedge was
invisible to systemd: the unit is considered "started" the instant it
`exec()`s, so `TimeoutStartSec` never gated model-load time at all. Converted
to `Type=notify` (`NotifyAccess=main` — systemd's default `NotifyAccess=none`
silently discards `sd_notify()` traffic, which would have made this a
regression rather than a fix) with `TimeoutStartSec=900`. `notify_ready()`
(mirroring `bin/px-alive`'s helper) fires only after STT backend selection
completes, or from the duplicate-instance guard's clean exit — never before
the daemon can do its job. A start that never reaches `READY=1` within 900s
is killed and handed to the existing `Restart=always`/`RestartSec=10`, which
turns "silently wedged for hours" into "visible restart loop in the
journal." 900s is deliberately generous, not tight — a start racing another
process for I2C/memory right after a crash should get a real chance rather
than being killed back into the same contention it is already losing to.

**Live redeploy, 2026-08-23T19:45 AEST**, under real (not synthetic) host
contention — `load1=6.2`, `psi_io_some_avg10=39.06`, `psi_io_full_avg10=32.9`,
memory PSI at 0 (this was I/O, not memory, pressure):

- Cold start completed in **44.7s** (`model load end: backend=sensevoice
  elapsed=44.7s`) — bounded, and `systemctl start` blocked correctly on
  `READY=1` rather than returning early.
- `memory.events:high` rose from 0 to 96 *during the load window itself*
  (some transient reclaim activity while usage approached the new ceiling
  under this particular I/O-contended load) but froze there once loading
  finished — no wedge, no OOM, no `max` events, `systemctl show` reported
  `ActiveState=active`/`SubState=running` immediately after.
- Resting floor immediately post-load, before any real transcription has
  run: `VmRSS=683.9M` / `Pss=673.1M` (`MemoryCurrent=689.5M` against
  `MemoryHigh=939.5M`/`MemoryMax=1342.2M`, i.e. 896M/1280M as configured) —
  consistent with the profiler's measured pre-trim range.

**What remains unverified by this session:** an actual spoken "Hey Spark"
wake word was not exercised — that requires a human voice at the physical
mic, which this session could not manufacture. The identical
`gc.collect()`/`malloc_trim(0)` logic was verified 8 times over against the
same live model objects via the profiler (flat 460M, no drift), and the
redeployed service's cold-start/resting-state behavior was verified against
real host contention, but the first production `memtrim(post_transcribe)`
log line from a genuine spoken interaction should be checked (`journalctl -u
px-wake-listen | grep memtrim`) once the robot is next used, to confirm the
production wiring behaves as the profiler proxy predicts.

## Acceptance testing

Run after deployment, on the live host. See the PR/commit for the actual
results of the run performed for this change.

**A. Healthy baseline** — robot responsive; `bin/px-brain-status` validated;
a reflection cycle completes through M5; a real voice turn works; no
contained unit sits constantly at `MemoryHigh`; no new swap growth.

**B. Bounded pressure test** — a safe, reversible, single-cgroup load (e.g.
`systemd-run` a throwaway scope, or a load generator inside one contained
unit's own cgroup) proving: pressure stays local to the targeted cgroup;
other units keep scheduling; `spark-brain` handshake stays responsive;
Wi-Fi stays usable; PSI does not enter the #217 pathological regime; crossing
`MemoryHigh` is observable as throttling; crossing `MemoryMax` in the
synthetic target only affects that cgroup and systemd's `Restart=` recovers
it automatically.

**C. Recovery** — health returns automatically; no stale "ok" from a dead
component; no host reboot required; no session/runtime state corruption.

## Observability extension (`src/pxh/hostload.py`, 2026-08-23)

The 2026-08-23 memory-pressure episode also showed that `hostload.py`
(added for #270/#283, wired into `brain.py` and `mic_stream.py`) only ever
sampled CPU PSI — during that episode CPU PSI stayed at 0 the whole time
while memory/IO PSI reached 46%/65% full avg10, so nothing #270/#283 already
logs could have been correlated against the actual bottleneck. Extended,
event-driven only (no new high-frequency logging), to add:

- `psi_mem_some_avg10_<prefix>` / `psi_mem_full_avg10_<prefix>`
- `psi_io_some_avg10_<prefix>` / `psi_io_full_avg10_<prefix>`
- `swap_free_kb_<prefix>`
- `cgroup_pressure_fields(unit, prefix)` — `mem_ratio_<unit>_<prefix>`
  (`memory.current` / `memory.high`, omitted when uncapped),
  `events_high_<unit>_<prefix>` (cumulative) and
  `events_high_rate_<unit>_<prefix>` (delta since this process's last call)
  for `px-brain` and `px-wake-listen`, the two units this episode found
  chronically pressed against their own ceiling.

Existing keys (`load1_<prefix>`, `psi_cpu_avg10_<prefix>`) are unchanged, for
backward compatibility with existing log analysis over #270/#283 events.

## Is #217 mitigated or merely instrumented?

Instrumented and locally contained, not proven fixed. This PR gives every
named daemon a cgroup boundary so a memory runaway in any one of them can no
longer freely consume the shared pool that #217's evidence ties to the SDIO
timeout, and gives the two latency-sensitive daemons (`px-brain`, `px-alive`)
scheduling priority under contention. It does not (and structurally cannot,
from systemd alone) prove the brcmfmac mechanism itself, and it does not
contain the interactive/operator session #217's own text flags as a
contributor. The next real test is whether a future #217-class event
recurs with these limits live — this PR does not claim that answer yet.
