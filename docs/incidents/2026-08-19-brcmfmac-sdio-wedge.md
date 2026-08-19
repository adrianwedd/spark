# Incident: brcmfmac SDIO wedge under sustained host load

**Status:** open — root cause identified, not yet mitigated
**Date:** 2026-08-19 (two reproductions, ~1 hour apart)
**Impact:** robot becomes unreachable over Wi-Fi with no self-recovery; requires
physical power-cycle or a wired fallback. Host stays alive; only the radio dies.

> **Timestamp warning.** This host has no RTC. Boot 0's clock ran on a stale
> `fake-hwclock` value (11:17:33), was moved to 12:06:28 by timesyncd, and only
> reached truth at **12:17:16**. Any boot-0 journal timestamp before 12:17:16 is
> fiction. Everything below at/after 12:17:16 is real. See
> `project_pi_has_no_rtc_journal_lies.md`.

---

## Summary

Two independent reproductions of the same failure. The Wi-Fi firmware stops
answering the host on the SDIO control path:

```
brcmfmac: brcmf_sdio_bus_rxctl: resumed on timeout
ieee80211 phy0: brcmf_cfg80211_dump_station: BRCMF_C_GET_ASSOCLIST failed, err=-110
```

`-110` is `ETIMEDOUT`. This is **not** an RF, association, or AP-side event —
NetworkManager never observes a link failure, which is exactly why nothing ever
attempts recovery. It is the host failing to service the `brcmf_wq` workqueue
kthread (`kworker/u21:N-brcmf_wq/mmc1:0001:1`) inside the driver's control-message
deadline.

**Incident 1 blamed the live GPIO test suite. Incident 2 falsifies that.**
Incident 1 recorded its own falsifier — *"same `brcmf_sdio_bus_rxctl` timeouts
appearing in idle boots with no test run"* — and incident 2 supplied it.

---

## Incident 1 — boot `470449545ea7…` (idx -1), wedge at 11:47:30

| Time | Event |
|---|---|
| 11:41:35 – 11:44:19 | `TestGpioLive` runs live: tool-look / sonar / emote / drive / stop / circle / figure8 / perform / face / wander / photograph / describe-scene |
| 11:41:52 | `px-alive: Watchdog timeout (limit 15s)!` → SIGABRT. Restart counter climbs **1 → 8** |
| 11:44:08 – 11:47:13 | `Failed with result 'protocol'` / `'watchdog'`, SIGKILL, *"Processes still around after SIGKILL"* |
| 11:46:59 | Last HTTP request ever served |
| **11:47:30** | **First SDIO `-110`** |
| 11:49:43 | `.pytest_cache/…/nodeids` written — **the suite completed normally** |
| 11:55:25 | PulseAudio connect timeout; `arecord: overrun!!! (28571 ms)` |
| 12:06:41 | Host stops dead, no shutdown sequence (~9 min down) |

Battery 74–76% on charger, no threshold warnings, no undervoltage, no throttling,
no OOM, no panic, no mmc/EXT4 errors. The suite **outlived** the network by 2
minutes and the host outlived it by 17.

## Incident 2 — boot `982d1d7c-25e2…` (idx 0), wedge at 12:33:31

| Time | Event |
|---|---|
| 12:19:22 – 12:36:16 | Incident-1 forensic session: sustained `journalctl` scans over a 192 MB journal |
| 12:19:58 | `px-alive: Watchdog timeout (limit 15s)!` → SIGABRT (restart 1) |
| **12:33:31** | **First SDIO `-110`** — *21 min before any pytest* |
| 12:38:34 | px-alive watchdog timeout (restart 2) |
| 12:46:21 | px-alive watchdog timeout (restart 3) |
| 12:47:36 | Containment test's first RED run executes a **real** `sudo systemctl stop px-alive` (see below) |
| 12:48:13 – 12:48:54 | SDIO `-110` burst ×5 — *still 6 min before pytest* |
| 12:48:28 – 12:49:55 | `px-wake-listen: enable_speaker … timed out after 3.0 seconds`, repeatedly |
| **12:54:59** | Targeted pytest launches: `timeout 400 .venv/bin/python -m pytest tests/test_px_alive.py tests/test_brain_daemon.py tests/test_health_report.py tests/test_routines.py tests/test_introspect.py tests/test_session_isolation.py -q` |
| 12:56:01, 12:59:36, 13:00:28 | SDIO `-110` continues |
| 13:01:52 | Claude session dies: `Request timed out` |
| 13:04:13 | **Ethernet plugged in** — `eth0` up, 192.168.0.27. Wi-Fi never recovered on its own |
| **13:20:15** | **Another SDIO `-110`, on ethernet, with no pytest running at all** |

No undervoltage (`throttled=0x0`), 59.9 °C, core 0.8563 V. No OOM kill, no panic,
no mmc/EXT4 errors, no D-state tasks.

Host state measured during the forensics, **with no pytest running**:

```
load average: 16.74 / 18.86 / 14.87      (4 cores)
Mem: 3796 MB total, 245 MB free
Swap: 506.1 MB used of 512 MB           SwapFree: 5988 kB
Committed_AS: 5538680 kB                 (5.5 GB committed against 3.8 GB RAM)
vmstat si: 180                           (active swap-in)
```

Resident set, largest first: `claude` 635 MB · `python3`(px-wake-listen) 588 MB ·
`claude` 533 MB · `claude` 516 MB · `python3`(px-mind) 334 MB · `ffmpeg` 123 MB ·
`rpicam-vid` 103 MB. **Three concurrent Claude sessions ≈ 1.7 GB**, plus the full
production stack (px-mind, px-alive, px-wake-listen, go2rtc, rpicam-vid, ffmpeg,
uvicorn, cloudflared).

---

## Intersection of conditions (present in BOTH incidents)

This is the load-bearing section. Not what differed — what was true both times.

1. **`px-alive` missed its 15-second systemd watchdog and entered a restart
   storm.** Incident 1: counter 1 → 8. Incident 2: 11 restarts, SIGABRT *and*
   SIGKILL. A daemon whose entire job is a servo loop could not check in inside
   15 seconds.
2. **Short, trivial subprocess deadlines blew out by orders of magnitude.**
   Incident 1: `arecord: overrun!!! (28571 ms)`, PulseAudio connect timeout.
   Incident 2: a bare `import robot_hat; enable_speaker()` exceeding 3.0 s,
   repeatedly, concurrent with the SDIO burst.
3. **Identical kernel signature**: `brcmf_sdio_bus_rxctl: resumed on timeout`
   followed by `-110` on `BRCMF_C_GET_ASSOCLIST` / `GET STA INFO`.
4. **Power is not implicated, in either.** No undervoltage, no throttling,
   battery healthy and on charger. This machine *does* log
   `hwmon hwmon1: Undervoltage detected!` when it dies that way — twice earlier
   the same day, as the literal final line of boots -4 and -5.
5. **No OOM kill in either** — and that is not reassurance. `cgroup_disable=memory`
   is on the kernel cmdline, so memory cgroup accounting is off: no
   `systemd-oomd`, no per-service limits, no cgroup OOM. `/proc/pressure` does
   not exist, so there is no PSI telemetry either. Under pressure this host has
   **no relief valve and no instrument** — it simply thrashes.
6. **Sustained aggregate contention on a 4-core / 3.8 GB host** running the full
   production stack plus multiple large Claude sessions.
7. **The radio never self-recovers.** NetworkManager sees no link event, so no
   layer retries. Recovery required a power-cycle (incident 1) or a wired
   fallback (incident 2).

**Not in the intersection:** the live GPIO/camera/audio test suite. Present in
incident 1, absent in incident 2. **Live tests are therefore sufficient but not
necessary.**

Also not in the intersection: pytest itself. In incident 2 the first two wedges
(12:33:31 and 12:48:13) preceded pytest by 21 and 6 minutes, and the 13:20:15
wedge followed the incident with no pytest anywhere.

---

## Observed failure chain (evidence)

Stated as what was measured, in order. No mechanism is claimed here.

1. Aggregate host load rises: load average 16.74 / 18.86 / 14.87 on 4 cores,
   `SwapFree` 5988 kB of 512 MB, `Committed_AS` 5.5 GB against 3.8 GB RAM,
   active swap-in.
2. Userspace deadlines that are normally met start being missed by orders of
   magnitude — `px-alive` misses a 15 s systemd watchdog repeatedly;
   `import robot_hat; enable_speaker()` exceeds 3.0 s; `arecord` overruns by
   28.5 s.
3. `brcmfmac` reports `brcmf_sdio_bus_rxctl: resumed on timeout` — the SDIO
   control-message wait expired.
4. `cfg80211` operations fail `-110` (`ETIMEDOUT`).
5. The Wi-Fi link becomes unusable and does not recover. NetworkManager logs
   no link event, so no layer retries.

Steps 1–4 are correlated in time in both incidents, twice, with the ordering
above preserved. That correlation is the finding.

## Inferred mechanism (NOT proven)

The natural reading of the chain is **scheduling starvation of the `brcmf_wq`
workqueue kthread** (`kworker/u21:N-brcmf_wq/mmc1:0001:1`): `brcmfmac` carries
its control plane over SDIO and waits on a bounded timeout serviced by that
kthread, so a deep enough run queue would cause it to miss the deadline.

**This is an inference, not a measurement.** "This exact kernel worker missed
this exact deadline *because* the scheduler did not run it" requires evidence
this investigation does not have. Nothing here rules out an alternative path to
the same timeout — SDIO bus contention with `mmcblk0` under swap I/O, a
firmware-side fault, or a driver bug independent of load.

What would actually establish it:

- `ftrace` / `perf sched` on `sched_switch` + `sched_wakeup` filtered to the
  `brcmf_wq` kworker, showing runnable-but-not-running latency spanning the
  control timeout;
- `workqueue:workqueue_queue_work` / `workqueue_execute_start` tracepoints
  showing queue-to-execute delay;
- `/proc/schedstat` or `/proc/<pid>/schedstat` run-delay deltas across a wedge;
- a counter-test: reproduce the load *without* memory pressure, and separately
  without SDIO block I/O, and see which one carries the wedge.

**What the evidence does support, without the inference:** resource starvation
is *correlated with* SDIO control timeout, reproducibly, on this host. That is
enough to act on.

## Operational conclusion (independent of the mechanism)

**A 4 GB production Pi running at load ~18 with ~6 MB swap free and 5.5 GB
committed is already outside a sane operating envelope.** Whatever the exact
kernel path from that state to `-110`, the state itself is the defect. The
remediation does not wait on the trace.

The causal variable is **total host load** — not pytest, and not live tests.
Both incidents are the same failure sampled at two points on that curve:

| | Incident 1 | Incident 2 |
|---|---|---|
| Live GPIO tests | yes | **no** |
| pytest running at first wedge | yes | **no** |
| px-alive watchdog storm | yes | yes |
| Multi-second deadline blowouts | yes | yes |
| SDIO `-110` | yes | yes |
| Power fault | no | no |

Incident 1's proposed cause survives only as an **accelerant**: live GPIO tests
were the largest single load contributor that run. In incident 2 that role was
filled by three concurrent Claude sessions (~1.7 GB RSS), the camera/streaming
pipeline, and a forensic session scanning a 192 MB journal.

### Confidence and falsifiers

| Claim | Confidence | What would falsify it |
|---|---|---|
| Wedge is SDIO control-path timeout, not RF/AP | **Very high** | An AP-side deauth at 11:47 / 12:33, or a `wpa_supplicant` disconnect event |
| Live GPIO tests are not necessary | **Very high** | Would require pytest or live tests running at 12:33:31 and 13:20:15. Neither was. |
| Wedge is *correlated with* resource starvation | **High** | A wedge occurring at low load average with free memory and no watchdog misses |
| Mechanism is specifically `brcmf_wq` scheduler starvation | **Inferred, unproven** | Scheduler/workqueue traces showing the kworker ran on time through a wedge — or showing SDIO bus contention or a firmware fault carrying it instead |
| Power/battery not implicated | **High** | An undervoltage line in a boot that logs one when it dies that way |
| No self-recovery path exists | **High** | wlan0 spontaneously recovering without intervention |

---

## Containment-test incident (preserved separately)

On 2026-08-19 at **12:47:36**, the first RED run of `tests/test_suite_containment.py`
executed `sudo systemctl stop px-alive` **for real** and stopped the live daemon.

The test was written to prove the destructive-boundary guard refuses privileged
commands — but it was written *before the guard existed*, and it used a **real
service as its negative control**. With containment absent, the containment test
was itself the destructive act it was meant to prevent.

The repaired test uses an inert canary unit:

```python
CANARY_UNIT = "px-canary-not-a-real-unit"
```

Every argv in that section is now privileged in its *name* and inert in its
*arguments* — a unit that does not exist, or `--help`.

### Invariant

> **A test that verifies containment must itself be harmless when containment is
> removed.**
>
> Containment tests assert a *refusal*. A refusal test only stays safe while the
> thing it tests works — which is precisely the condition you cannot assume,
> because the first run of such a test is by definition a run where the guard may
> be absent, broken, or not yet written. Therefore the negative control must be
> inert on its own terms: a canary unit that does not exist, a `--help` flag, a
> path under `tmp_path`. Never a real service name, never `reboot`, never
> `shutdown -h now`.

---

## Next engineering step

**Step 1 — bank the containment work.** Done: `0a1cf807`,
`fix(tests): make ordinary pytest incapable of controlling the live robot`, on
`fix/pytest-cannot-touch-the-robot`. Ordinary pytest can no longer act on the
production robot.

**Step 2 — relocate ordinary pytest off the Pi.** Not: tune test timeouts,
shrink subsets, or keep bisecting for a safe test list. Those treat the suite as
the variable, and incident 2 shows it is not the only one — nor even the first.
Explicitly **necessary but not sufficient**: incident 2's first two wedges
happened with no pytest running.

**Step 3 — production resource containment. This is now the priority, ahead of
#211.** The production baseline is itself overcommitted, before any test runs:

- Determine why the two resident Haiku sessions need **~1.05 GB** between them
  (`spark-io` 533 MB + `spark-brain` 516 MB RSS), and whether that is working
  set or accumulated context.
- Determine why a third Claude operator session pushes the host to **~1.7 GB**,
  and whether operator sessions should be permitted on the robot at all.
- Establish a hard concurrency and memory posture — a bounded number of
  sessions with bounded combined RSS — that leaves the kernel, the SDIO
  workqueues and the block layer breathing room.

Supporting work:

4. Remove `cgroup_disable=memory` from the kernel cmdline so memory accounting,
   per-service limits and `systemd-oomd` become available. Enable PSI so
   `/proc/pressure` exists. Today the host has neither a relief valve nor an
   instrument — that is why both incidents had to be reconstructed after the
   fact instead of caught in progress.
5. Give the radio a recovery path: watch for `brcmf_sdio_bus_rxctl: resumed on
   timeout` and reload `brcmfmac` / bounce `wlan0`, since NetworkManager never
   sees a link event and nothing else will.
6. Keep a wired fallback available whenever the robot is under sustained load.
7. If the mechanism is ever worth proving, capture the scheduler/workqueue
   traces listed above during a deliberate load test — **on a non-production
   Pi**, not this one.

**#211** ("fixed 10 s subprocess/thread waits fail under suite load") is very
likely this same starvation surfacing as test flakes rather than as a dead
radio. It should be reconsidered after step 3, not before — fixing the deadlines
would only hide the signal.
