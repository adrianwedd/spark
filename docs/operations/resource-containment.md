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
| px-wake-listen | 640M | 1024M | default | High ~1.1x steady state; Max ~12% above documented 900M peak (#219) |
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

**Deliberately not contained:** the interactive/operator Claude session
(`user.slice/user-1000.slice/session-3.scope`, ~1GB observed, including the
session that produced this PR) — #217's own text flags this class of session
as a contributor ("why a third Claude operator session pushes the host to
~1.7GB"), but the goal's explicit per-service target list names only system
daemons, and applying a cgroup limit to a live interactive session risks
capping the very session doing this work. Recommended as a separate,
deliberately-scoped follow-up rather than guessed at here.

**Honesty about the ceiling, not a guarantee:** the sum of every `MemoryMax`
above (~4.2G) exceeds total host RAM (3796M). Per-cgroup hard caps are not a
reservation scheme — they bound any *single* runaway locally, which is what
#217 needs, but they do not mathematically rule out every service
simultaneously hitting its own peak at once. `MemoryHigh` (throttle, not
kill) is what actually protects against a slow multi-service squeeze, by
design. `tests/test_systemd_containment.py::test_sum_of_memory_max_is_disclosed_not_assumed_safe`
pins this fact so a future edit can't silently un-disclose it.

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
