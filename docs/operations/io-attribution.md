# Attributing the transient IO stall (#247, #283, #287)

**Instrument:** `bin/px-io-attrib` + `src/pxh/io_attrib.py`
**Status 2026-09-17:** merged and deployed (`#337`, `#338`); **the root unit is
not installed yet** — that needs a sudo password (command below). In the
meantime an **unprivileged** instance runs by hand on the robot:
`nohup bin/px-io-attrib` (which publishes its own pid — no operator has to match
`/usr/bin/python3 -`, since four daemons on this host share that argv, including
root's `px-battery-poll`), pid in `logs/px-io-attrib.pid`, stdout in
`logs/px-io-attrib.out`. It records the stall channel plus the writer list it is
allowed to read, and it is *not* supervised and will not survive a reboot — the
unit replaces it.

## Why this exists

zram broke the *sustained* IO-pressure regime on 2026-08-26 (`/var/swap` at 0 B,
`io some avg300` 33.58 → 0.79). What is left is a burst problem of the same peak
magnitude — and three reliability symptoms that were filed separately are the
same stall wearing three faces:

| issue | symptom | what the residual evidence says |
|---|---|---|
| #247 | ~25 % sustained IO PSI on the single `mmcblk0` | sustained half fixed by zram; bursts remain, `avg10` still reaching 27 % |
| #283 | `arecord` ALSA overruns (~5/day after a 10× drop) | 100 % of instrumented overruns at `psi_io_some_avg10_at ≥ 50 %`, median 96 % |
| #287 | `px-alive` killed by `TimeoutStartSec` while parked | process killed in uninterruptible IO (`Processes still around after SIGKILL`) |

Memory PSI is 0.0 at essentially every instrumented overrun, so the original
swap-pressure story no longer explains the residual, and the 2026-08-20 sample
that cleared SPARK's daemons was taken during a *quiet* period — it measured an
idle system and therefore could not have found the writer.

**The open question is one question: which writer produces the stall.** Until it
is answered, `IOSchedulingClass=`/`IOWeight=` is a guess — if the writer is ext4
journal work or journald, process-level weighting may buy little, and if it is
the daemon heartbeat write path the fix is a design change in `bin/px-alive`,
not weighting at all.

## What it does, and what it deliberately does not

A **trigger-on-demand** observer, not a poller:

1. every second, reads two cheap things — `/proc/pressure/io` (`some avg10`) and
   `px-alive`'s heartbeat file;
2. when **io PSI `some avg10` ≥ 40 %** *or* **heartbeat age ≥ 7 s while px-alive
   is alive** (15 s is its watchdog), it takes **one** bounded snapshot;
3. the snapshot is a two-ended window (`--window`, default 3 s): process
   counters and device counters before, sleep, after;
4. it writes one record to `logs/tool-io-attrib.log`, prints one summary line to
   the journal, and stays quiet for `--cooldown` (default 60 s).

An observer that sampled `/proc/*/io` for all ~150 processes all day would be a
candidate for the very list it produces, on a host whose remaining defect is a
transient storage stall. That is why the heavy part is bounded and rare, why the
unit runs `Nice=10` + `IOSchedulingClass=idle`, and why every record carries
`observer_write_bytes` — the instrument accounting for itself.

The heartbeat trigger is gated on aliveness (the pid in `logs/px-alive.pid`
exists) so a *stopped* daemon's stale heartbeat file cannot trigger forever and
bury the real stalls.

## Two channels, and the privilege asymmetry between them

| channel | source | needs root? |
|---|---|---|
| **writer** | `/proc/<pid>/io` deltas — `write_bytes`, `read_bytes`, `syscw`, plus `wchar` for context | **yes** for the complete list: as `pi`, `/proc/1/io` is `EACCES`, so px-alive and journald are invisible. It is still *attempted* unprivileged — what is readable is reported, with the number that refused beside it |
| **file** | size deltas of a bounded *watchlist*: the system journal (`/var/log/journal/*/*.journal`), `logs/*.log`, `state/*.json`, `state/health/*.json` | no — and it names *files*, which is attribution: `logs/px-wake-listen.log` growing by 8 KB during the window names px-wake-listen even when its `/proc/<pid>/io` was refused, and the system journal growing is journald by another name |
| **stall** | `/proc/<pid>/stat` state, `schedstat` run delay, `wchan`, plus per-thread D state; `/proc/diskstats` write-queue time; `/proc/vmstat`; PSI | no — world-readable, including for root-owned processes |

**Pid files, two different roles until 2026-09-17:** `--pid-file` (default
`logs/px-io-attrib.pid`) is where the observer publishes *its own* pid while
running; `--px-alive-pid-file` (default `logs/px-alive.pid`) is the one it
*reads* to gate the heartbeat trigger. They were a single flag, and the runbook's
discovery step wrote root's `px-battery-poll` pid into the observer's file —
`kill $(cat …)` would have killed a daemon that was doing its job.

The file channel is a **watchlist, not a filesystem scan**: a bounded set of stat calls, opt-in per run (`--growth-pattern`, repeatable; `--no-file-growth` to skip). `file_growth_watched` records how many paths it covered, because growth *outside* the watchlist is invisible and silence there is not evidence of not writing.

An unprivileged record is therefore **not** a record that found no writer:
it keeps the stall channel, reports the writer list it *could* read, and names
the gap — `privileged: false` (the probe: this uid cannot read other users'
`/proc/<pid>/io`) plus `writers_unreadable_count` (how many processes refused
one). "The camera pipeline wrote, 152 processes were invisible" is a usable
reading; "no writers" is not. (`/proc/<pid>/wchan` is what
turns "someone was blocked" into `jbd2_log_wait_commit` — the symbol the
2026-08-16 fsync investigation found by hand.

**Thread-level matters here, which is why `blocked_threads` exists.**
`/proc/<pid>/stat` reports only the thread group leader, and px-alive's health
record is written by a *background* thread (`bin/px-alive`'s
`_report_health_success`) whose `mkstemp`+`fsync` into `state/health/` has been
caught in `jbd2_log_wait_commit` for seconds at a stretch. That combination —
leader fine, health thread in D state — leaves the process unkillable (SIGKILL
cannot interrupt D state, which is the `Processes still around after SIGKILL`
line in #287's journal excerpt) while the main loop keeps beating. The record
names the blocked thread and its own `wchan` so those two readings do not look
identical.

Note also that **px-alive's heartbeat is no longer an SD-card write**: since
`src/pxh/runtime_paths.py` it goes to `/run/spark` (tmpfs, confirmed: `/run` is
tmpfs on the robot). A parked px-alive's remaining SD touchpoints are the
per-iteration *read* of `state/gpio_lease.json` in `_foreign_lease()` and any
in-flight health-thread fsync — which is what makes the per-thread view the
deciding evidence for #287, not the heartbeat itself.

Ranking is on **disk** bytes (`write_bytes`), not `wchar`. #247's first sample
was misled by 2.4 MB/s of `wchar` from go2rtc/rpicam-vid that never reached the
disk; `wchar` is still reported so that reading stays available, it just does
not decide the order.

## Install (root, one command block)

```bash
ssh pi@picar
cd /home/pi/picar-x-hacking && git pull --ff-only

# Unprivileged smoke test first — prints one record, writes nothing:
bin/px-io-attrib --dry-run --window 3

# Then the real thing, as root:
sudo install -m 0644 systemd/px-io-attrib.service /etc/systemd/system/px-io-attrib.service
sudo systemctl daemon-reload
sudo systemctl enable --now px-io-attrib
systemctl status px-io-attrib --no-pager
bin/px-deploy-check
```

`sudo` is required, not preferred: without it the observer can still see *who
was blocked*, but not *who wrote* for the root-owned writers (#247 names
`bin/px-alive`'s heartbeat fsync, journald, and the camera pipeline — two of the
three are root-owned or kernel-side).

Rollback: `sudo systemctl disable --now px-io-attrib && sudo rm
/etc/systemd/system/px-io-attrib.service && sudo systemctl daemon-reload`.

## First live catch (2026-09-17T15:44:17+10:00) — a worked example

Triggered 50 seconds after the observer started, `reason=io_psi`:

```
io_psi_some_avg10_at=41.85   psi_post some avg10=57.21   heartbeat age 0.04s
privileged=false   writers_unreadable_count=179/202   writers_with_activity=7   write_bytes_total=8192 B
  python3 px-wake-listen.service  write_bytes 8192   <- the only readable disk writer
  go2rtc  px-frigate-stream       write_bytes 0, wchar 1.13 MB   <- pipe, not disk
stalled (7 in D state): python3(px-wake-listen) wchan=jbd2_log_wait_commit;
  python3(px-battery-poll); systemd-journald; jbd2/mmcblk0p2-8; kworker/2:0H+kblockd; kworker/u17:1-flush
mmcblk0: 26 writes, 320 sectors (160 KB), ms_io +2212, ms_writing +66545     vmstat: pswpin/pswpout 0, nr_dirty 166
```

How to read that: the device was pathological (`ms_writing` advanced by 66.5 s
inside a 3.2 s window — per-request service times in the seconds), memory PSI was
0.0 and swap was untouched, the ext4 journal thread and journald were themselves
in D state, and **no readable process accounts for the 152 KB that reached the
disk**. That last line is the whole reason the root channel is the next step:
"invisible to this uid" is a different statement from "nobody wrote".

That record predates the file channel, so a current run also carries
`file_growth` / `file_growth_total_bytes` / `file_growth_groups` /
`file_growth_watched` (see the field table below).

Calibration from the same window: px-alive's normal heartbeat gap max sits at
~2.2-2.6 s, so the 7 s trigger has ~3× margin and the first record used the io-PSI
trigger alone (`heartbeat age 0.04s`). Idle io PSI is under 5 %, with bursts past
40 %; one catch in the first ten minutes is about the expected rate.

## Reading a record

```bash
jq -c '{ts, reason, writers: [.writers[0:3][] | {comm, unit, write_bytes}],
        devices, d_state_count, stalled: [.stalled[0:3][] | {comm, state, wchan}]}' \
  logs/tool-io-attrib.log | tail -5
```

| field | meaning |
|---|---|
| `reason` | `io_psi`, `heartbeat_age`, both (`io_psi+heartbeat_age`), or `manual` |
| `writer_channel_attempted` | whether the writer channel was tried at all (false only under `--no-proc-io`) |
| `privileged` | the probe's answer: can this uid read *other users'* `/proc/<pid>/io`? (It asks about pid 1 — reading our own io always succeeds and would prove nothing.) |
| `writers_unreadable_count` / `writers_unavailable_reason` | the measurement: how many processes actually refused `/proc/<pid>/io` in this snapshot, and one sentence saying which of the three gaps applies — not attempted, unprivileged, or refused-despite-privilege (a non-dumpable process) |
| `file_growth[]` | files that grew during the window, ranked by bytes, paths shortened to their last three components |
| `file_growth_total_bytes` / `file_growth_groups` | the total, and the same bytes grouped as `journal` / `logs` / `state` / `health` |
| `file_growth_watched` | how many paths were covered — the honesty field for this channel |
| `writers[]` | ranked by `write_bytes` (disk) across the window, with `unit` from `/proc/<pid>/cgroup` — the process *and* the thing to change |
| `writers_with_activity` / `write_bytes_total` | how many processes moved anything at all, and how much of it reached the disk |
| `stalled[]` | processes whose group leader *or* a secondary thread is in D state (uninterruptible sleep), and/or with the largest run delay, with `wchan`, `unit` and `blocked_threads[]` |
| `d_state_count` / `blocked_thread_count` | stalled processes, and how many of the blocked tasks are non-leader threads |
| `devices` | per-device deltas: `ms_writing` (time with writes in flight) and `ms_io` (queue time) separate "busy device" from "many small writes" |
| `vmstat` / `vmstat_end` | swap-in/out and direct-reclaim deltas; `nr_dirty`/`nr_writeback` gauges |
| `psi_pre` / `psi_post` | `psi_io_some_avg10_*`, `psi_io_full_avg10_*`, memory PSI, `load1`, `swap_free_kb` on both ends |
| `observer_write_bytes` | the observer's own disk writes during the window — if it ever tops the list, the observer is the defect |
| `uptime_s` | correlates a record with boot-relative kernel logs |

### What each shape means

| the record shows | reading | next move |
|---|---|---|
| a named user-space writer + `mmcblk0.ms_io` spike | that writer's fsync/write volume is the stall | the fix is local to that writer (batching, durability policy, or weighting *now that a writer is named*) |
| `px-alive` stalled with `blocked_threads` in D state and `wchan: jbd2_log_wait_commit` | an SD-card call inside the daemon is blocking a thread (the park's lease *read*, or an in-flight health fsync) — the park logic is not the defect | take the SD touchpoint out of the park/startup path (#287); the heartbeat itself already moved to tmpfs |
| no readable writer, `privileged: true`, high `ms_io`, jbd2/kworker in D state | ext4 journal work, not a daemon | journald/ext4 tuning (`SyncIntervalSec`, rates, storage), not process weighting |
| `go2rtc`/`rpicam-vid` high `wchar`, `write_bytes: 0` | pipe traffic, not the disk | ignore; that reading is a known trap (#247) |
| a short writer list with `writers_unreadable_count` high | the list is partial by privilege, not by absence of writers | install as root (above); a `pi`-owned writer in that list is still real evidence |
| `file_growth` names a `logs/*.log` or `state/**/*.json` | that file's owner wrote those bytes during the stall, whoever it runs as | that daemon's write path is the thing to look at — no root needed to know which |
| `file_growth_groups.journal` is most of the bytes | journald is writing through the stall | journal-side tuning (`SyncIntervalSec`, rates, storage), not process weighting |
| `file_growth` empty while `mmcblk0.ms_writing` is high | the bytes went somewhere outside the watchlist (root-owned state, another tree, or kernel writeback) | widen `--growth-pattern`, or install as root |
| `privileged: true` yet processes refused | non-dumpable processes; they are invisible under any uid | note them by pid/unit and reason about them separately |

## Deliberately not done

- **No health record.** `health.STALE_AFTER_S` entries are "expected to exist"
  components; adding one that reports `missing` on any host that never installed
  the unit is a permanent false alarm — the same shape as the retired-service
  residue CLAUDE.md documents. Silence from this observer is visible in
  `logs/tool-io-attrib.log` and in the absence of the unit, not on the board.
- **No `IOSchedulingClass`/`IOWeight` change on the suspect services yet.** That
  decision waits on a named writer (this instrument is what names it).
- **No `task_delayacct`.** `/proc/sys/kernel/task_delayacct=1` would make
  per-task block-IO wait time world-readable (it is currently 0 on this host),
  which would give a second unprivileged stall channel. Reversible one-liner,
  available if the writer channel proves insufficient — not switched on
  unasked, because it is a global kernel accounting change.
