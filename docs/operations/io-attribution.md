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

**Update 2026-09-17 20:05 — it may not be a writer at all.** The residual caught
live is a *wedge*: io PSI `full` 58 % with `mmcblk0` at 0/0 `inflight`, 2-7 %
busy, and a durable 169-byte write completing in 7-52 ms *during* the stall. See
[the 19:35-20:05 section](#2026-09-17-1935-2005--the-residual-stall-is-not-storage-work-device-idle-while-everything-waited).
Read that before running another writer hunt; `device_inflight_pre` is the field
that distinguishes the two shapes.

## What it does, and what it deliberately does not

A **trigger-on-demand** observer, not a poller:

1. every second, reads two cheap things — `/proc/pressure/io` (`some avg10`) and
   `px-alive`'s heartbeat file;
2. when **io PSI `some avg10` ≥ 40 %** *or* **heartbeat age ≥ 7 s while px-alive
   is alive** (15 s is its watchdog), it takes **one** bounded snapshot;
3. the snapshot is a window (`--window`, default 3 s): process and device
   counters before, **the window sampled every `--sample-interval` (0.5 s)**
   rather than slept through, counters after;
4. it writes one record to `logs/tool-io-attrib.log`, prints one summary line to
   the journal, and stays quiet for `--cooldown` (default 60 s).

An observer that sampled `/proc/*/io` for all ~150 processes all day would be a
candidate for the very list it produces, on a host whose remaining defect is a
transient storage stall. That is why the heavy part is bounded and rare, why the
unit runs `Nice=10` + `IOSchedulingClass=idle`, and why every record carries
`observer_write_bytes` — the instrument accounting for itself.

The heartbeat trigger is gated on aliveness (the pid in `--px-alive-pid-file`,
default `LOG_DIR/px-alive.pid`, exists) so a *stopped* daemon's stale heartbeat
file cannot trigger forever and bury the real stalls.

That default is derived from `LOG_DIR`, which makes the gate **silently
disarmable** — measured 2026-09-18: a bounded verify instance run with
`LOG_DIR=/tmp/...` had the heartbeat ageing 0.26-0.32 s at **20 of 20** samples
and fired **zero** `heartbeat_age` captures, because it was looking for
`px-alive.pid` under the scratch log dir. Nothing in its output said the trigger
could not fire; the only symptom would have been records that never carry
`heartbeat_age`, which reads as "the heartbeat was healthy" rather than "nobody
was watching it". So the observer states it three ways: `heartbeat_gate=armed|
disarmed` plus `px_alive_pid_file=<path>` on the startup line, an explicit
`heartbeat trigger DISARMED: ...` line when it starts with no live pid (re-checked
every poll, so a later px-alive start arms it), and `trigger.px_alive_gate` in
**every** record.

## Two channels, and the privilege asymmetry between them

| channel | source | needs root? |
|---|---|---|
| **writer** | `/proc/<pid>/io` deltas — `write_bytes`, `read_bytes`, `syscw`, plus `wchar` for context | **yes** for the complete list: as `pi`, `/proc/1/io` is `EACCES`, so px-alive and journald are invisible. It is still *attempted* unprivileged — what is readable is reported, with the number that refused beside it |
| **file** | a bounded *watchlist* (`default_growth_patterns`: the system journal, `logs/*`, `logs/*/*`, `state/*`, `state/health/*.json`, `state/brain/*`) read two ways in one stat walk: **size** deltas (`file_growth`) and **mtime** movement (`file_touched`) | no — and it names *files*, which is attribution: `logs/px-wake-listen.log` growing by 8 KB during the window names px-wake-listen even when its `/proc/<pid>/io` was refused, and the system journal moving is journald by another name. The mtime half exists because the size half cannot see either likeliest writer — see the two-shapes section below |
| **stall** | `/proc/<pid>/stat` state, `schedstat` run delay, `wchan`, plus per-thread D state; `/proc/diskstats` write-queue time; `/proc/vmstat`; PSI; `/sys/block/*/inflight`; `/sys/fs/ext4/*` | no — with **one exception measured on 2026-09-18: `wchan` is withheld for root-owned processes** (see the next section). `state`, `schedstat` and the rest are readable for every process |
| **kernel** | `journalctl -k` over the window, filtered to what can explain a **wedge** — `mmc`, `sdio`, `brcmf`, `ext4`, `jbd2`, `blk_`, I/O errors, timeouts, undervoltage, thermal, hung-task | no on hosts where the operator is in `adm` — measured on `picar` 2026-09-18 (`pi` is in `adm`, so `journalctl -k` and `dmesg` both work) |
| **units** | `journalctl` lifecycle lines over a **5 min** lookback (`unit_log`), and watchlist files modified in the same lookback ranked by size (`file_recent`) | no on hosts where the operator is in `adm` | 
| **window** | the same state sampled *across* the window (`sample_window` → `blocked_in_window`), plus per-thread D state for a rotating quarter of the process set | no — and this is the channel that still **names a root-owned writer**, because `state` is readable where `wchan` and `/proc/<pid>/io` are not |
| **filesystem** | `ext4` counters: `session_write_kbytes` (bytes written *through* this filesystem, metadata and journal included), `lifetime_write_kbytes` (card wear), `delayed_allocation_blocks`, `errors_count`, `journal_task` | no — it cross-checks the device's bytes against the filesystem's, which is how you rule out a raw writer outside the filesystem and how you measure **write amplification**. It does *not* separate file data from metadata — see the calibration below |

**Pid files, two different roles until 2026-09-17:** `--pid-file` (default
`logs/px-io-attrib.pid`) is where the observer publishes *its own* pid while
running; `--px-alive-pid-file` (default `logs/px-alive.pid`) is the one it
*reads* to gate the heartbeat trigger. They were a single flag, and the runbook's
discovery step wrote root's `px-battery-poll` pid into the observer's file —
`kill $(cat …)` would have killed a daemon that was doing its job.

The file channel is a **watchlist, not a filesystem scan**: a bounded set of stat calls, opt-in per run (`--growth-pattern`, repeatable; `--no-file-growth` to skip). It was widened on 2026-09-18 (see `default_growth_patterns`) after a record with **112 KB written to the device and both file channels empty** — the old set matched `*.log` and `*.json` while this host churns `state/*.jsonl`, `state/*.lock`, `logs/*.jsonl`, `logs/*.out`, `logs/*.rotlock` and files one level down. Pattern shape matters more than pattern count: an extension whitelist is a guess about a filesystem you can just list. `file_growth_watched` records how many paths it covered, because growth *outside* the watchlist is invisible and silence there is not evidence of not writing.

An unprivileged record is therefore **not** a record that found no writer:
it keeps the stall channel, reports the writer list it *could* read, and names
the gap — `privileged: false` (the probe: this uid cannot read other users'
`/proc/<pid>/io`) plus `writers_unreadable_count` (how many processes refused
one). "The camera pipeline wrote, 152 processes were invisible" is a usable
reading; "no writers" is not. `/proc/<pid>/wchan` is what turns "someone was
blocked" into `jbd2_log_wait_commit` — the symbol the 2026-08-16 fsync
investigation found by hand — but it only does that for a process this uid may
ptrace, which is the correction in the next section.

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

## The window channel, and the one field the kernel withholds

**Measured 2026-09-18, and it changes how to read every earlier record:**
`/proc/<pid>/wchan` is *silently withheld* for any process this uid may not
ptrace. It does not fail — it returns `0`, which is the same string the kernel
returns for a task that is not blocked at all. Eight consecutive passes over
`/proc`, classifying every process in `S`/`D` state by owner:

| owner | sleeping processes | wchan returned a symbol | returned `0` |
|---|---|---|---|
| root | 65 | **0** | 65 |
| `pi` | 23-24 | 21-22 | 2-3 |

So `wchan` names a writer only when `/proc/<pid>/io` was *already* readable for
it, and on an unprivileged observer it can never name journald, px-alive or the
`jbd2` kthread. Consequences, all of them now in the record rather than in a
reader's head:

* `wchan_withheld_count` and `wchan_unavailable_reason` — derived from the io
  denial actually measured for each pid, counted over the whole walk and the
  whole window rather than over the `top`-truncated rows;
* per-row `wchan_withheld` on `blocked_in_window[]` and `stalled[]`, so
  `wchan: null` can no longer be misread as "not blocked in anything";
* the remedy is named in the reason itself: run as root.

What still works unprivileged for a root-owned writer is **state** — and, once
`kernel.task_delayacct=1`, `delayacct_blkio_ticks`. That is the whole basis of
the window channel.

**The units channel is how a *timer* gets named, and it exists because of what
happened on 2026-09-18.** The largest writer on this host is not a SPARK daemon.
`apt-daily.service` ran 05:25:38-05:26:14, rewrote the 137 MB `/var/lib/apt/lists`
tree and a **60 MB** `/var/cache/apt/pkgcache.bin`, and **exited** — and the card
was still absorbing those pages minutes later:

```
05:26:16  record: nr_dirty 22,238 pages (~87 MB)
05:27:52  record: io PSI some 93.61 %, 46,976 KB written to ext4 in 36 s,
          mmcblk0 ms_io 36,324 ms (queue 100 % occupied), pgpgout 14,988 pages,
          write_bytes_total 37 KB   ->  unattributed_write_share ~0.999
```

Every window-scoped channel was empty, and *correctly* so: the bytes were in the
page cache and then on the card, charged to `kblockd`/`flush-179:0`, and the
process that asked for them had already exited. Page-cache writeback is not
attributable to a process by any `/proc` channel, privileged or not.

So two channels that look *before* the window:

* **`unit_log`** — systemd's own `Starting`/`Finished`/`Deactivated` lines over a
  5-minute lookback, because the writer has exited by the time the stall is
  visible (measured delay: ~2 min). The lifecycle verbs are matched anywhere
  after the `systemd[1]:` prefix, because systemd writes both
  `Starting x.service ...` and `x.service: Deactivated successfully.`.
* **`file_recent`** — watchlist files modified in the same lookback, ranked by
  **size** (the watchlist contains gauges rewritten every few seconds, and
  ordering by recency would fill the list with them). On 2026-09-18 this field
  names `cache/apt/pkgcache.bin`, 60 MB, mtime 138 s before the record.

Neither would have been needed if the writer were a daemon. Both are needed
because it is a timer. The remedy is a config decision, not a code hunt:
`IOWeight`/`IOSchedulingClass` on `apt-daily`, moving the APT trees off the card,
or scheduling around it — see **#402**, which carries the evidence and the
options. The watchlist now covers `/var/lib/apt/lists/*`, `/var/lib/apt/periodic/*`
and `/var/cache/apt/*.bin` (739 paths, up from 694) for the same reason.

**The kernel channel (`kernel_log`) is the only one that can name a *mechanism*
when there is no writer to name.** The 20:20 wedge had `mmcblk0` at 0/0 inflight,
2-7 % busy, and the whole task set blocked: nothing in any `/proc` or sysfs
channel can explain that, and if anything in software can, it is a kernel
message. Measured on the robot with a 14 h window:

```json
{"source": "journalctl -k", "available": true, "window_s": 50400,
 "lines_total": 559, "lines_scanned": 559, "matched_total": 24,
 "filter": "mmc|sdio|brcmf|ext4|jbd2|blk_|i/o error|timeout|undervolt|voltage|..."}
  2026-09-17T16:55:55+1000 picar kernel: hwmon hwmon1: Undervoltage detected!
  2026-09-17T16:56:16+1000 picar kernel: hwmon hwmon1: Voltage normalised
  2026-09-17T17:30:12+1000 picar kernel: hwmon hwmon1: Undervoltage detected!
  2026-09-17T17:30:20+1000 picar kernel: hwmon hwmon1: Voltage normalised
```

That is this boot's kernel side of the "power" candidate #247 has been carrying
on `vcgencmd get_throttled = 0x50000` alone: **two undervoltage events, one
lasting 21 s and one 8 s**, plus the SDIO WiFi's power-save transitions. Every
future record will carry the ones that fall inside its window, with timestamps.

Cost, and how to read it: **one** `journalctl` call per capture, after the window
(never inside it), bounded by `KERNEL_LOG_MAX_LINES`, a 20-line result cap and a
5 s timeout — the journal lives on the same card as everything else here, so this
channel buys its evidence with the resource it is measuring. `child_io()` is
differenced around it because `/proc/self/io` cannot see a child's reads: the 14 h
call above reported **0 block-read bytes and 4096 block-write bytes**. Read
`kernel_log_child_read_bytes` as a *lower bound* — journald serves recent entries
from memory and reads its journal through an `mmap`, and page-fault-driven reads
are not what `ru_inblock` counts.

`available: false` is a first-class answer (no `journalctl`, no permission, no
journal). It is never "the kernel was quiet": that is an empty `lines` with
`lines_total` and the `filter` beside it.

**The window channel (`blocked_in_window`) replaces the window's idle `sleep`.**
A burst that ends before t1 is invisible to a two-ended walk, so the window is
sampled every `--sample-interval` (default 0.5 s): `/proc/<pid>/stat` for every
pid (state + delayacct), `wchan` for any task seen in D, and a per-thread D walk
for a rotating quarter of the process set — rotating because a sample costs
~40 ms of /proc reads on the robot and a full thread walk every sample would be
the observer becoming the writer it is hunting. The pid set is the t0 walk, so a
process that *starts* inside the window is caught by the closing walk only.

| field | meaning |
|---|---|
| `blocked_in_window[]` | ranked `{pid, comm, unit, d_samples, thread_d_samples, samples, d_share, d_at, thread_d_at, wchan, wchan_withheld}`. Ranked on the **worse** of the two sample counts, so a wedged thread behind a healthy leader (#287's shape) cannot sort below a process that flickered into D once. `d_at`/`thread_d_at` are the sample *indices*, not just counts — see `blocked_at_samples` |
| `blocked_at_samples` / `co_blocked_samples` | who was blocked **in each sampled instant**, and how many instants had more than one. The count above cannot tell "two processes, one after another" from "two processes at the same moment", and the second is the evidence for a *shared* resource — on this host one `mmc` bus carries both the SD card (mmcblk0) and the SDIO WiFi (mmc1), and one ext4 journal serves everything. Built from the whole per-pid map, not the `top`-truncated rows, and a blocked *thread* counts as blocked |
| `blocked_in_window_count` | how many processes were blocked at all during the window |
| `window_samples` / `window_sample_interval_s` / `window_span_s` | how many samples, how far apart, and the span actually covered (`≈ (samples-1) × interval` plus one sample's work — the window's two ends are the walks). `d_share` is a fraction of *samples*; check it against this span, not against `--window` |
| `window_thread_coverage_s` | how long a full per-thread rotation takes. A thread block shorter than this can fall between two visits to the same process — the sampler's stated resolution, not a hidden limit |
| `kernel_log` | `{source, available, window_s, filter, lines_total, lines_scanned, matched_total, lines[]}`. **Read `available` before `lines`**: `false` means nobody looked (no journalctl, no permission), `[]` means the kernel said nothing matching `filter` — which is quoted so that stays checkable. `window_s` is the measured window plus a 5 s pad, because the trigger fires on a 10 s PSI average and the cause can predate t0 |
| `kernel_log_child_read_bytes` / `_write_bytes` | what the one child process cost in block IO, from `RUSAGE_CHILDREN`. A *lower bound* (see the kernel-channel section) |
| `unit_log` | `{source, available, window_s, filter, lines_total, lines_scanned, matched_total, lines[]}` over a **5 min** lookback — what systemd started, finished, failed or restarted. The only channel that names a timer-driven writer that exited before its bytes landed. Read `available` before `lines`, as with `kernel_log` |
| `unit_log_child_read_bytes` / `_write_bytes` | the unit channel's own block IO, costed separately so the record says which journal query paid |
| `file_recent[]` | watchlist files modified in the last 5 minutes, `{path, age_s, size_bytes}` ranked by size. `size_bytes` is the file's size, **not** how much it grew; weigh it against `file_growth` for the same path |
| `delayacct` | `{enabled, note}`. **The field exists in `/proc/<pid>/stat` and reads 0 for every process while `kernel.task_delayacct=0`**, so a record that emitted the delta anyway would print a measured "waited 0 ms" for everything — the #306 failure mode. `blkio_wait_ms` therefore appears on a row *only* when the sysctl says 1, and a zero there is then a real zero |

Measured cost of the change on the robot (197 pids, 6 samples): the sampler
covered 2.54 s of a 3 s window, `observer_read_bytes` **0**, `observer_write_bytes`
**0** — /proc reads are memory, not card traffic, which is the point.

**First reading from the window channel (2026-09-18 04:33 AEST, robot
`b9df4527` + this change):**

```
blocked_in_window = [{"pid": 217, "comm": "jbd2/mmcblk0p2-8", "d_samples": 1,
  "samples": 6, "d_share": 0.17, "wchan": null, "wchan_withheld": true,
  "unit": null}]
wchan_withheld_count = 1
```

Unprivileged, on an unremarkable window, this now says the thing the whole
investigation wanted: **the ext4 journal thread itself — the writer that has no
file and no process — was in uninterruptible sleep for one of six samples**,
while `writers_unreadable_count` sits at 175 and every byte-level channel is
blind to it. `unit: null` is correct and not a gap: `jbd2` is a kernel thread
with no cgroup unit to blame.

**Second reading (2026-09-18 04:48 AEST, deployed `d58af50f`) — and it is the
first time two blocked processes have been caught in the *same* sample:**

```json
"blocked_at_samples": [[], [{"pid": 92, "comm": "kworker/u21:0+brcmf_wq/mmc1:0001:1"},
                            {"pid": 217, "comm": "jbd2/mmcblk0p2-8"}], [], [], [], []],
"co_blocked_samples": 1
```

`brcmf_wq/mmc1:0001:1` is the **brcmfmac SDIO WiFi workqueue on mmc1**. An
unremarkable window, no PSI, no overrun — and the ext4 journal thread and the
WiFi SDIO workqueue were both in uninterruptible sleep in the same half-second.
That is the shared-`mmc`-subsystem hypothesis this file already flagged as
invisible to `/sys/block` (`mmc1` has no block device), now visible as a
timestamped overlap rather than an inference, and it is the first evidence for
#217's brcmfmac wedge and #247's residual coming from one channel.

One sample is not a correlation, and the field is presented as an overlap for
that reason: `co_blocked_samples` counts instants, not duration, and the sampler
reports its own resolution (`window_thread_coverage_s`) beside it.

## Install (root, one command block)

```bash
ssh pi@picar
cd /home/pi/picar-x-hacking && git pull --ff-only

# Unprivileged smoke test first — prints one record, writes nothing:
bin/px-io-attrib --dry-run --window 3

# Second lever, now measured rather than hoped for (2026-09-18): the counter is
# real and switched off. /proc/<pid>/stat field 42 (delayacct_blkio_ticks) exists
# and reads 0 for every process on the robot while kernel.task_delayacct=0, and
# the field alignment was verified there (indices 36/37/38 = processor 3,
# rt_priority 0, policy 0).
#   sudo sysctl -w kernel.task_delayacct=1
#   echo kernel.task_delayacct=1 | sudo tee /etc/sysctl.d/99-spark-delayacct.conf
#   sudo systemctl restart px-alive px-wake-listen px-mind   # see next paragraph
# It is a *global* switch and only tasks forked afterwards are accounted, so
# without the restart the daemons keep reporting 0 and the channel looks broken.
# Then every blocked_in_window row carries blkio_wait_ms — per-process block-IO
# wait time, which is readable for root-owned processes. That is the only
# *quantitative* channel an unprivileged observer has for them.

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

### Interim: keeping it running *without* root

The root unit is still the ask, because only root can read the root-owned
writers' bytes. But the observer is an instrument that exists only while it is
running, and as a hand-started process it is silently fragile: on 2026-09-18 it
stopped at **03:57** and stayed stopped for an hour, and nothing said so —
exactly the failure shape it exists to catch, one level up.

`bin/px-io-attrib-ensure` is the interim. It is idempotent, and on a healthy host
it prints nothing and writes nothing:

```bash
# pi crontab — one line, no root, reversible
* * * * * cd /home/pi/picar-x-hacking && bin/px-io-attrib-ensure >> logs/cron-io-attrib.log 2>&1
```

It decides "running" from `/proc/<pid>/stat` **plus** a `--io-threshold` check on
`/proc/<pid>/cmdline`, because a pid file alone would let a stale — or reused —
pid keep the observer *down* while reporting everything fine. A root-owned
observer is still recognised (its argv is unreadable, so it is assumed alive),
which is why the cron line must be removed rather than left in place once the
unit is installed.

Revert: `crontab -l | grep -v px-io-attrib-ensure | crontab -`


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

**Caveat (added 2026-09-17 20:20) — `ms_writing` on this host does not mean what
it looks like it means.** That worked example quotes "`ms_writing` 21-419 s
inside 3.2 s windows — per-request service times in the seconds" as the device
grinding. Direct measurement contradicts it: a 169-byte `fsync`+`replace` costs
p50 7 ms (max 25 ms over 15 samples), a 512 KiB sequential `fsync` write runs at
~575 KB/s, and a quiet record taken 2026-09-17T20:11 shows
`writes_completed 81, sectors_written 720 (360 KB), ms_writing 6286, ms_io 432`
— which asks the device to have ~100 4 KB writes in flight at once, on an
`mmc` queue that is one or two deep. The two counters are irreconcilable;
`ms_io` (queue-occupied time, 19 % here) agrees with the direct measurements and
`ms_writing` does not. **Do not read `ms_writing` as device service time on this
kernel (6.12)** — in the 2026-08-16 fsync investigation it was the corroborating
evidence for a real defect that `/proc/<pid>/wchan: jbd2_log_wait_commit`
already proved on its own, so the conclusion stands on that, not on this field.
`device_inflight_pre` and `ms_io` are the fields to reason from.

That record predates the file channel, so a current run also carries
`file_growth` / `file_growth_total_bytes` / `file_growth_groups` /
`file_growth_watched` (see the field table below).

Calibration from the same window: px-alive's normal heartbeat gap max sits at
~2.2-2.6 s, so the 7 s trigger has ~3× margin and the first record used the io-PSI
trigger alone (`heartbeat age 0.04s`). Idle io PSI is under 5 %, with bursts past
40 %; one catch in the first ten minutes is about the expected rate.

## 2026-09-17 19:35-20:20 — two stall shapes, and the bytes are invisible to every channel here

Measured live on `picar` with a bounded ad-hoc sampler (1 s cadence; no unit, no
root), chasing the residual that #247/#283/#287 share.

**The deepest stall caught: io PSI `some` 64.3 %, `full` 58.4 % — and the SD card
idle.**

```
t= 90.9  some=30.5 full=27.1 inflight=0/2  rd=0 wr=6 72KB  busy=8ms   FSYNC DURING STALL: 8855 33 16 ms
t= 90.9  some=62.2 full=55.9 inflight=0/0  rd=0 wr=89 760KB busy=9000ms FSYNC DURING STALL: 8 11 8 ms
t= 92.0  some=64.3 full=58.4 inflight=0/0  rd=0 wr=9  144KB busy=24ms  FSYNC DURING STALL: 11 7 11 ms
t= 96.1  some=43.3 full=39.3 inflight=0/0  rd=0 wr=12 168KB busy=68ms  FSYNC DURING STALL: 33 12 10 ms
t=101.1  some=29.2 full=26.4 inflight=0/0  rd=0 wr=13 228KB busy=36ms  FSYNC DURING STALL: 12 8 50 ms
```

- `full` tracks `some` within a few points: **when it stalls, it stalls
  everything** — not one slow daemon.
- `inflight` is **0/0** and the device is **2-7 % busy** for the whole episode,
  doing 144-228 KB/s of writes: it has essentially nothing to do.
- A 169-byte `mkstemp`+`fsync`+`os.replace` *during* the stall completed in
  **7-52 ms**. The 8.8 s fsync in the first line was the tail of the wedge
  *before* sampling began.

Independent calibration that the storage path itself is healthy when not wedged:

```
169B fsync+replace x15 (idle): min 5.5 p50 7.1 p90 13.0 max 24.8 ms
512KiB sequential fsync write: 0.89 s -> ~575 KB/s
120 s continuous sampling: mmcblk0 read 0 bytes in every sample; every readable
  process read ~0 and wrote ~180 KB total; io PSI some still reached 38 %
```

### Shape 2, from the first four records the `inflight` field produced (20:12-20:18)

The records immediately after the field went live (observer pid 12516,
`3dedb851`) show a *different* shape from the one above, and it is the one to
reason from until told otherwise:

```
ts                  psi_some  inflight_pre  inflight_post  ms_io  writes sectors  window
2026-09-17T10:12:14Z   47.81   0/2           0/2           3760ms   28    156KB   3.19s
2026-09-17T10:14:36Z   41.77   0/2           0/0           1892ms   10    128KB   3.23s
2026-09-17T10:16:36Z   44.71   0/2           0/0           2612ms   57    524KB   3.21s
2026-09-17T10:18:17Z   40.27   0/2           0/0           1004ms   40    492KB   3.28s
```

Here the device is **not** idle: two writes are in flight at every
`inflight_pre`, the queue is 31-117 % occupied (i.e. essentially saturated), and
it is delivering **40-160 KB/s** — 4-14× below its own measured sequential rate
of ~575 KB/s — while `psi_io` `full` tracks `some` within a couple of points.
The D-state roster in those windows is ext4 metadata work, not a daemon:
`jbd2/mmcblk0p2-8`, `kworker/*+kblockd`, `kworker/u19:1+ext4-rsv-conversion`,
plus a `python3` in `folio_wait_bit_common` in the first one (`nr_writeback 16`
there, 0 in the others).

And **none of it is attributable with the channels this instrument has**:
`write_bytes_total` is 0-8192 B, the top "writers" are `go2rtc`/`ffmpeg`/
`cloudflared`/`rpicam-vid` with `write_bytes: 0` (pipe traffic), `privileged:
false` with ~177 processes refusing, and — the part that matters —
`file_growth_groups` is `{}` with `file_growth_watched: 115`. Nothing in
`state/` or `logs/` grew by a byte while the device wrote 128-524 KB.

**This is a limitation of the size channel, not evidence of no writes.** The two
likeliest writers in that window have no size delta to see: journald appends
into an 8 MB preallocated, mmap'd journal file (`system.journal` sits at exactly
8388608 bytes), and ext4 journal/metadata writes change no file size at all.

**Half of that is now closed** (`#361`): the same watchlist is read through
mtime as well, as `file_touched` / `file_touched_count`, with zero-byte rows
sorted first. An ad-hoc mtime watcher found journal mtimes advancing in 35 of 91
high-PSI samples where the size channel saw nothing, so the first of those two
writers is now attributable without root. The second — ext4 metadata with no
file-level signature — still needs the root channel.

**What this changes.** Two consequences, and one corrected claim:

1. `IOWeight`/`IOSchedulingClass=` is still **closed**: whatever is driving these
   stalls, the device is saturated at 40-160 KB/s with ~2 requests in flight, so
   there is no *competing* writer to deprioritise — the requests that are there
   are the ones blocking everyone.
2. The observer's empty writer lists are still **correct for what it can read**,
   and now demonstrably incomplete for a reason that is not "nobody wrote":
   metadata writes and journald are outside both the `/proc/<pid>/io` view
   (privilege) and the size-delta view (no size change). Install as root, or
   watch mtime, or both.
3. **Corrected:** the "device idle while everything waited" reading below was one
   episode and is *not* the general shape. It is still a real shape (inflight
   0/0, 2-7 % busy, a 169 B fsync in 7-52 ms during a 64 % `full` stall), and
   still unexplained, but shape 2 is what the observer keeps catching.

**Where it points instead.** Candidates, in the order the evidence favours:
the `mmc` host/block path blocking *before* dispatch (`blk_mq_get_tag` waits set
`in_iowait` and are counted by io PSI while `inflight` stays 0), the SDIO WiFi
(`mmc1`, same `mmc` subsystem, no `/sys/block` entry, `brcmf_wq/mmc1:0001:1` was
in D state in two earlier records), and power. On power, the same boot reports

```
$ vcgencmd get_throttled
throttled=0x50000          # bit 16 (under-voltage has occurred), bit 18 (throttled)
$ journalctl -b -k | grep -i undervoltage
hwmon hwmon1: Undervoltage detected!    # 16:55:55 and 17:30:12
```

and the previous boot ended with
`user-1000.journal corrupted or uncleanly shut down`. An under-volting Pi is a
live explanation for a host controller that stops completing requests without
logging an error, and it is testable from the privileged side.

## 2026-09-17 21:50 — measured: fsync'd small files cost ~43 KB of card writes each (and what that does *not* explain)

Controlled, bounded, in-vitro run on `picar` (8 s windows, cleaned up after), comparing
the device's bytes against the filesystem's:

```
window                    dev KB   ext4 KB   ratio   ms_io   write reqs   psi_some
baseline (idle)              208       208    1.00     44ms            27        0.0
2 MiB sequential + fsync    2672      2672    1.00    304ms            34        0.2
300 x 40 B files + fsync   13056     13056    1.00   2780ms          1000        5.5
idle again                   428       428    1.00    120ms            52        2.7
```

Two results, one of which corrects the doc's earlier claim.

**1. `session_write_kbytes` tracks the device exactly (ratio 1.00 in every regime), so
it does not separate file data from metadata.** The earlier claim in this file that it
does was wrong — it is a *cross-check*, and a valuable one: it rules out a raw writer
outside the filesystem, because every device byte is accounted for by the filesystem.

**2. Write amplification is enormous for small durable files: ~1088×.** 300 files of
40 bytes (12 KB of data) cost **13 MB** of card writes — ~43 KB per file, ~1000 write
requests in 8 s, queue 35 % occupied. This is exactly the `atomic_write()` shape every
daemon uses (`mkstemp` + write + `fsync` + `os.replace`): one JSON state file is three
to four journal-committed metadata writes plus a flush.

The arithmetic lines up with the stall records: observed state-file rewrite *clusters*
are 3-8 files every ~60-90 s (mtime channel), at ~43 KB each that is **129-344 KB per
cluster** — against **128-524 KB** of device writes in the four `io_psi` records, on
the same ~1/minute cadence.

**What this does *not* explain — and the honest boundary.** Device work alone does not
account for the blocking ratio. Those four records show io PSI `some` 40-48 % while the
device was only 31-118 % *queue-occupied* with 3-18 completions/s, and the controlled
burst above reached 35 % occupancy with **5.5 %** PSI (0.0 % at idle). So the
amplification is real, is filesystem-authored, and is the right thing to reduce — but
something still converts a modest amount of device work into near-system-wide blocking,
and that is the part the root channel exists to see (kernel threads, per-task `wchan`,
the root-owned writers).

## 2026-09-17 21:35 — the stall reproduced in vitro, and the effect size of one `fsync`

Three 160-operation workloads on `picar`, 8 concurrent processes each doing the
`atomic_write` shape (`mkstemp` + write + `fsync` + `os.replace`) into a scratch
directory, then cleaned up. Identical logical writes in every variant; only the
`fsync` and the directory layout differ.

| variant | wall | p50 | p95 | max | card writes | card KB | ms_io | children caught in `jbd2_log_wait_commit` |
|---|---|---|---|---|---|---|---|---|
| V1 `fsync`, one shared dir | 0.8 s | 28.2 ms | 71.7 ms | 83.9 ms | 241 | 2972 | 416 ms | 7/16 samples (44 %) |
| V2 **no `fsync`**, one shared dir | 0.3 s | 7.4 ms | 19.0 ms | 29.3 ms | 131 | **524** | 112 ms | **0** |
| V3 `fsync`, own dir per process | 1.8 s | 32.8 ms | 99.4 ms | **832 ms** | 294 | 3644 | 1220 ms | 28/48 (58 %) |

1. **The `fsync` is the cost, and this is the production signature reproduced on
   demand.** Dropping it cuts card bytes ~5.7× for identical logical writes
   (524 KB vs 2972-3644 KB — ~3.3 KB per file instead of ~19-23 KB), cuts p50
   latency ~4× and p95 ~4-5×, and removes `jbd2_log_wait_commit` from the samples
   entirely. That symbol is what 20 of the 53 production stall records were
   sitting in (see the tallies above), and those were user-space daemons —
   `px-wake-listen` 14, `px-mind` 4.
2. **Directory layout is not the lever.** V3 is slightly *worse* than V1, so the
   serialisation is filesystem/journal-wide rather than per-directory: spreading
   writers across directories buys nothing, fewer `fsync`s buys everything. Do
   not propose a directory-layout change for this.
3. **The tail is visible even at this scale**: a single 40-byte write taking
   **832 ms**. Production adds concurrency, 7297 KB/min of idle background churn,
   and the microphone capture loop on top — which is how a handful of `fsync`s
   becomes a seconds-long stall.

**Caveat on the PSI columns**: `psi_io_*` is a 10-second average and V1/V2 lasted
under a second, so their peaks understate the burst (V3, at 1.8 s, reached
8.7 / 7.2 %). The robust columns here are bytes, latencies, and the
`jbd2_log_wait_commit` sample rate.

**The fix this measures** (`#367`): the health store — a liveness record every
daemon rewrites on its next tick, written by `px-wake-listen` *inside the capture
loop* — now uses `atomic_write(..., durable=False)`. Verified on the deployed
artifact: `fsync` calls during a real `record_success` = **0**, record written,
no temps left. The device-rate comparison before/after that change (7297 → 2433
KB/min, 604 → 301 requests/min) is **suggestive, not proof**: the "after" window
also contained an io-PSI episode peaking at 33 %, so the two windows are not
load-matched. The load-matched evidence is the table above.

**Reading a record's threshold**: the hand-run observer on `picar` was restarted
at 21:50 with `--io-threshold 25` (from the 40 % default) on purpose, because
episodes at 25-40 % were being missed and the open question — do
`px-wake-listen`/`px-mind` still appear in `jbd2_log_wait_commit` after `#367` —
is answered by *record count*, not by peak height.

### Two ways a record can mislead (both observed 2026-09-17)

- **A deploy is itself a workload.** The 11:56:34Z record contains `git` in D
  state with `wchan: do_get_write_access` — that was *my own* `git fetch &&
  merge --ff-only` on the host, blocked in a jbd2 metadata wait. Deploying to
  `picar` rewrites many files at once, so a record taken within a minute of a
  deploy is contaminated by it. Wait before drawing conclusions from one.
- **The threshold decides what exists.** Episodes at 25-40 % were invisible to
  the 40 % default, and a question like "does this unit still appear in
  `jbd2_log_wait_commit`?" is answered by *record count*, not peak height. That
  is why the hand-run observer triggers at 25 % now — a measurement choice, not
  a change in what counts as a stall.

## 2026-09-17 23:30 — where the remaining card traffic comes from, and why no channel here can name it

A 60 s decomposition on `picar`, at idle:

```
device wrote          2596 KB   (43 KB/s)
ext4 session delta    2596 KB   (ratio 1.00 — every byte is filesystem traffic)
readable processes      92 KB   <- 3.5 % of it
journal files          size delta 0 B, mtime moved on both
```

And a 90 s scan of `logs/*` (111 files) found **exactly one** file written at all:
`px-mind.log`, 66 B/min. So the 2.4 MB/min of background writes are **not** app
logs and **not** attributable to any process this uid can read. They are journal
and metadata — charged to kernel threads, which no `/proc/<pid>/io` channel can
report under any privilege.

That is the shape of the remaining residual, and it is why the instrument now
carries `unattributed_write_share`: the reading "the device wrote megabytes with
`write_bytes_total` in the kilobytes" is a *measurement of the journal/metadata
share*, and it is the honest answer to "who wrote this" when the answer is
"nobody with a name".

**The levers this leaves are all root-only**, which is the concrete form of the
open ask: `vm.dirty_*` and `dirty_writeback_centisecs` (the 5 s flusher that
`flush-179:0` serves), the ext4 `commit=` interval, and journald's
`Storage=`/`SyncIntervalSec`. Nothing in user space changes how many commits the
kernel chooses to write.

## 2026-09-18 02:00 — how to read an io-PSI number on *this* box

PSI io is a per-task aggregate, and this host runs ~200 processes with very few
runnable at any instant. 582 samples at 0.5 s, 300 s:

```
                    median   max
procs_running            1      6
procs_blocked            0      3
D-state tasks            0      5

worst intervals (accrued task-io-stall per 0.5 s sample):
   482 ms  running=2 blocked=2 D=4
   468 ms  running=1 blocked=3 D=5
   459 ms  running=1 blocked=1 D=2
```

**So `psi_io some ≈ full` at 25-45 % here means "the one or two runnable tasks
were waiting on IO", not "the system froze".** Real waits are visible (an
interval can accrue ~480 ms of a task's stall out of 500 ms), but they involve a
handful of tasks, and after the `#367`/`#370`/`#373`/`#376`/`#377` fixes the
tasks doing the waiting are `jbd2`, `flush-179:0`, `kblockd` and
`systemd-journal` — kernel threads and journald, which no `/proc` channel this
side of root can name.

Every record therefore carries `procs_running_*` and `procs_blocked_*`: without
them a 40 % reading invites the wrong story, and this issue's own history shows
where that goes.

## 2026-09-18 04:00 — the card itself, measured (identity, wear, TRIM)

When the question becomes "is it the card?", these are the facts, so the next
reader does not have to re-derive them:

```
name EB1QT   serial 0xfb8c6775   manfid 0x1b   date 08/2019   29.8 GiB SDHC
ext4 lifetime_write_kbytes  1,105,803,377  (1.03 TB = 35 full-card writes)
ext4 session  (since mount)     2,451.8 MB  (10.5 h boot -> ~5.6 GB/day)
ext4 errors_count               0
queue/discard_max_bytes         170 GB, granularity 4 MB
fstrim.timer                    enabled, weekly (last 2026-09-14, next 09-21)
mount                           rw,noatime   (no `discard` — TRIM is the timer's job)
```

Two readings from that, and one of them closes a hypothesis:

1. **Wear-out is not indicated.** 35 full-card writes is modest against microSD
   endurance (hundreds to thousands of P/E cycles). The card is *old* — made
   08/2019 — but SD keeps no life-time register to read actual wear (that is an
   eMMC feature), so age alone is not evidence about the residual. A card swap
   would be an experiment, not a remedy with a rationale behind it.
2. **The traffic is still real after every fix in this arc**: ~5.6 GB/day, against
   the 96.5 % of it that is journal/metadata with no process name (see the
   decomposition above). That is the residual, and it is intrinsic to a
   filesystem doing small durable writes on a card whose per-write tail reaches
   seconds.

The mount options are already tuned in the direction this investigation would
recommend (`noatime`, TRIM left to the weekly timer rather than `discard`), so
there is nothing to change there.

## Reading a record

```bash
jq -c '{ts, reason, writers: [.writers[0:3][] | {comm, unit, write_bytes}],
        devices, d_state_count, stalled: [.stalled[0:3][] | {comm, state, wchan}]}' \
  logs/tool-io-attrib.log | tail -5
```

| field | meaning |
|---|---|
| `reason` | `io_psi`, `heartbeat_age`, both (`io_psi+heartbeat_age`), or `manual` |
| `trigger.px_alive_gate` | `armed` / `disarmed` — whether the watchdog half of the trigger was actually watching. `disarmed` means this record could only have come from io PSI, and that a late heartbeat would not have been captured at all. `trigger.px_alive_pid_file` names the path it looked in |
| `writer_channel_attempted` | whether the writer channel was tried at all (false only under `--no-proc-io`) |
| `privileged` | the probe's answer: can this uid read *other users'* `/proc/<pid>/io`? (It asks about pid 1 — reading our own io always succeeds and would prove nothing.) |
| `writers_unreadable_count` / `writers_unavailable_reason` | the measurement: how many processes actually refused `/proc/<pid>/io` in this snapshot, and one sentence saying which of the three gaps applies — not attempted, unprivileged, or refused-despite-privilege (a non-dumpable process) |
| `file_growth[]` | files that grew during the window, ranked by bytes, paths shortened to their last three components |
| `file_growth_total_bytes` / `file_growth_groups` | the total, and the same bytes grouped as `journal` / `logs` / `state` / `health` |
| `file_growth_watched` | how many paths were covered — the honesty field for this channel, and it covers `file_touched` too (same walk) |
| `file_touched[]` / `file_touched_count` | files whose **mtime** moved, `{path, bytes}`. **Zero-byte rows sort first**: those are the writes the size channel cannot see (journald appending into its 8 MB preallocated mmap'd journal, and any fixed-size rewrite). A row means *someone wrote this file in this window* — mtime moves when the write lands in page cache, so it is a writer to attribute, not proof that bytes reached the device |
| `writers[]` | ranked by `write_bytes` (disk) across the window, with `unit` from `/proc/<pid>/cgroup` — the process *and* the thing to change |
| `writers_with_activity` / `write_bytes_total` | how many processes moved anything at all, and how much of it reached the disk |
| `stalled[]` | processes whose group leader *or* a secondary thread is in D state (uninterruptible sleep), and/or with the largest run delay, with `wchan`, `unit` and `blocked_threads[]`. `wchan: null` with `wchan_withheld: true` means the kernel hid the symbol from this uid — not that the process was blocked in nothing |
| `blocked_in_window[]` | who was blocked *during* the window, sampled (see the window-channel section). This is the list to read when `writers_unreadable_count` is high: it is the only channel that can name a root-owned writer |
| `d_state_count` / `blocked_thread_count` | stalled processes, and how many of the blocked tasks are non-leader threads |
| `devices` | per-device deltas. `ms_io` (queue-occupied time) is the trustworthy one; `ms_writing` is reported but over-counts on this kernel — see the caveat above |
| `ext4` | `{fs, write_kbytes_delta, session_write_kbytes, lifetime_write_kbytes, delayed_allocation_blocks, errors_count, journal_task}`. Compare `write_kbytes_delta` against `devices.<dev>.sectors_written`: a ratio near 1.00 means the device's bytes *are* filesystem writes (no rogue writer), and the interesting number is then amplification — device bytes per byte of file data, which the file channels approximate. `journal_task` is a **tid** on this kernel (217 = `jbd2/mmcblk0p2-8`), so it cross-references `stalled[].pid`. `{}` means unwatched, never zero. `lifetime_write_kbytes` is the wear figure for the card |
| `device_write_bytes` / `unattributed_write_bytes` / `unattributed_write_share` | the busiest real device's bytes, and how much of that has **no process's name on it**. Measured 60 s window: `mmcblk0` 2596 KB, every readable process 92 KB — share **0.965**. Journal and metadata are charged to kernel threads (`jbd2`, `kblockd`, `flush-179:0`), so *no* `/proc` channel can name them, privileged or not. Caveat that keeps it honest: `write_bytes` charges page-cache writes to the dirtying task, so a high share means "mostly journal/metadata", never "nobody wrote" |
| `device_inflight_pre` / `device_inflight_post` | `/sys/block/<dev>/inflight` at each end of the window — requests *currently* dispatched. Zero is reported, not dropped: `0/0` during a stall is the finding, not a missing sample. `pre` is inside the stall (the caller triggered because PSI is high now), `post` shows recovery |
| `vmstat` / `vmstat_end` | swap-in/out and direct-reclaim deltas; `nr_dirty`/`nr_writeback` gauges |
| `procs_running_pre` / `procs_blocked_pre` / `_post_` | the context for reading PSI at all. `some` is "≥1 task stalled" and `full` is "every non-idle task stalled", so with one or two runnable tasks both read 25-45 % while almost nothing is happening. Measured on `picar` 2026-09-18 (582 samples @ 0.5 s): `procs_running` median **1**, max 6; `procs_blocked` median **0**, max 3; `D`-state median **0**, max 5 — including in the intervals with the most accrued stall time, where 1-2 tasks were runnable. `full` tracking `some` on this box is **not** evidence of a system-wide freeze |
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
| `file_growth` empty **and `file_touched` names the journal** | journald wrote in this window without moving any file size — the shape the size channel was blind to | journal-side tuning (`SyncIntervalSec`, rates, `Storage=`) is a real candidate now; install as root to get its bytes |
| device bytes ≈ `ext4.write_kbytes_delta` (ratio ~1.00) | the device's bytes are filesystem writes — no raw or outside-the-filesystem writer exists, which retires that whole class of suspect | stop looking for a rogue writer; measure amplification (below) and count fsyncs instead |
| `ext4.lifetime_write_kbytes` is in the hundreds of GB to TB (1.03 TB on `picar`, 2026-09-17) | the card has been written a great deal; a wear-related latency tail is a live hypothesis, not a theory | treat card health as a candidate alongside the kernel/host path, and say so when reporting |
| both file channels empty with the device saturated | metadata/journal work with no file-level signature at all (ext4 `-rsv-conversion`, `kblockd`, `jbd2` in the D-state roster) | the root channel is the only way in; do not read the empty channels as "nobody wrote" |
| `privileged: true` yet processes refused | non-dumpable processes; they are invisible under any uid | note them by pid/unit and reason about them separately |
| device wrote megabytes, `write_bytes_total` is kilobytes, `unattributed_write_share` ≈ 1, and `file_growth`/`file_touched` are empty | **page-cache writeback of a writer that has already exited.** Not a broken measurement — the shape. Read `file_recent` (what mass moved just before) and `unit_log` (which timer did it) | if a timer is named, the fix is I/O priority or placement for it (#402); if nothing is named, widen the watchlist to the tree the bytes went to |
| `kernel_log.matched_total` > 0 with `Undervoltage detected!` in the window | a power event, and the only channel here that dates it. Matches #217's and #247's power candidate (`vcgencmd get_throttled = 0x50000`, one boot-long sticky flag, no timestamps) | correlate it with the record's timestamps before blaming the card; a *currently* undervolted Pi is a different fix (supply/cable) from a slow card |
| `kernel_log` names `mmc1`/`brcmfmac`/SDIO errors or timeouts | the SDIO WiFi shares the `mmc` subsystem and has no `/sys/block` entry, so this is the only place it appears | cross-read it with `co_blocked_samples`: a shared block means the bus, not a daemon |
| `kernel_log.available: false` | nobody read the kernel log (no `journalctl`, or not in `adm`) — **not** a quiet kernel | fix the access (`usermod -aG adm pi` needs root) or run as root; do not read this as evidence of anything |
| a log with `io_psi` records but **no** `heartbeat_age` record ever, on a host whose px-alive has been parked | check `trigger.px_alive_gate` before concluding the heartbeat is healthy | pass `--px-alive-pid-file` explicitly, or stop overriding `LOG_DIR`; the gate is /proc-based and a missing pid file disarms it |
| `blocked_in_window[0]` is a root-owned daemon or a kernel thread with `wchan_withheld: true`, `writers_unreadable_count` high | the unprivileged reading of "the writer is one of the processes we cannot measure". A `jbd2/mmcblk0p2-8` row is ext4 journal work; the journal thread *is* the writer, and it has no file and no unit | the unit-level fix is journal-side tuning (`SyncIntervalSec`, `Storage=`, rates) or taking fsyncs out of the suspect daemon — not process weighting; install as root for the symbol and the bytes |
| `blocked_in_window` names a *user-space* daemon with `thread_d_samples` at or near `samples` while `d_samples` is 0 | a thread of that daemon is wedged behind a healthy leader (#287) — the shape a leader-only view reports as "fine" | that daemon's write path, not its park logic |
| `co_blocked_samples` > 0, especially with `brcmf_wq/mmc1:*` and `jbd2/mmcblk0p2-8` in the same instant | the block is **shared**, not a writer: the `mmc` bus carries the card and the SDIO WiFi, and nothing here is competing for it | stop looking for a writer; this is #217/#247's "wedge, not writer" territory — kernel/driver/power, and `mmc1` has no `/sys/block` entry to consult |
| **`device_inflight_pre` is `0/0` while `d_state_count` is high and `devices` shows queue time** | **nothing was in flight: the queue is wedged, not busy — there is no writer in this record to find, and a longer search for one is the wrong search** | take the question to the *waiters*: `stalled[].wchan` and (as root) the kernel threads, and to the other bus users — `mmc1`'s SDIO WiFi shares the `mmc` subsystem and cannot be seen from `/sys/block` |

## Host tuning: the two apt timers, and why the lever is worth pulling (#402)

The largest writers on this host are not SPARK daemons. Measured 2026-09-18, both
`apt` timers produced a stall within an hour of each other, with *different*
shapes on the same device:

| run | when | bytes | queue | io PSI peak | shape |
|---|---|---|---|---|---|
| `apt-daily.service` (update) | 05:25:38-05:26:14 | **48 MB** in a 36 s window, `pgpgout` 61 MB, 87 MB dirty backlog | 100 % occupied | 93.6 % | **mass** |
| `apt-daily-upgrade.service` (upgrade + clean) | 06:00:44-06:03:31 (167 s, 38.5 s CPU) | 44 KB - 1.8 MB per window, `pkgcache.bin` 57.2 MB rewritten | 100 % occupied | 70.2 % | **latency** |

The second row is the one that changes the argument. It moves *kilobytes* and
still saturates the queue for whole windows, because this card's write service
time is ~86 ms (p90 207 ms — see the hardware issue, #405). A job that cannot be
reordered away from the device is exactly where weighting has leverage, and both
rows are *scheduled*, so they will recur at randomised times each day.

The instrument found both without anyone reading a timer table: `unit_log`
carried the lifecycle lines into the records and `file_recent` named
`/var/cache/apt/pkgcache.bin` 25-260 s before each one.

**The lever (root, one drop-in, covers both units):**

```bash
sudo mkdir -p /etc/systemd/system/apt-daily.service.d
sudo tee /etc/systemd/system/apt-daily.service.d/10-io-priority.conf >/dev/null <<'CONF'
# Keep apt off the SD card's critical path (#402). apt-daily and apt-daily-upgrade
# both write tens of megabytes and hold this card's queue for whole windows
# (measured 2026-09-18: 93.6 % and 70.2 % io PSI), and neither can be rescheduled
# away from the device -- so lower its priority instead of its volume.
[Service]
IOSchedulingClass=idle
IOWeight=1
Nice=19
CONF
sudo cp -r /etc/systemd/system/apt-daily.service.d /etc/systemd/system/apt-daily-upgrade.service.d
sudo systemctl daemon-reload
```

Alternatives, if weighting proves insufficient: move `/var/lib/apt/lists` and
`/var/cache/apt` onto zram with a persistent overlay (removes ~253 MB of
recurring churn from the card), or accept it and keep the small-write pressure
low elsewhere. All three are decisions, not code — the evidence lives in #402,
and the card question in #405.

## Deliberately not done

- **No health record.** `health.STALE_AFTER_S` entries are "expected to exist"
  components; adding one that reports `missing` on any host that never installed
  the unit is a permanent false alarm — the same shape as the retired-service
  residue CLAUDE.md documents. Silence from this observer is visible in
  `logs/tool-io-attrib.log` and in the absence of the unit, not on the board.
- **No `IOSchedulingClass`/`IOWeight` change on the suspect services.** That
  lever is now *closed* rather than deferred: the 2026-09-17 wedge shows the
  stall happens with the device idle and no competing writer, so there is
  nothing to deprioritise.
- **`task_delayacct` is off, and that is now a *measured ask* rather than an
  unknown.** Corrected 2026-09-18: the earlier note reasoned about
  `/proc/<pid>/delayacct`, which does not exist on this kernel and never will —
  the counter moved into `/proc/<pid>/stat` field 42. That field **exists and
  reads 0 for every process while the sysctl is 0**, and its position was
  verified against the robot's own `/proc/1/stat` (indices 36/37/38 read
  processor 3, `rt_priority` 0, `policy` 0). So the channel is real, readable
  for root-owned processes, and simply switched off; turning it on needs root,
  which is why it lives in the install block above rather than in a code path.
  The record emits `blkio_wait_ms` **only** when the sysctl reads 1, so a
  switched-off kernel can never produce a per-process "waited 0 ms".

  What does work there without root, and is already in every record:
  `/proc/<pid>/schedstat` run delay (the observer's `max_run_delay_ms`) —
  measured 2.8 ms for `px-alive` on an idle box. Run delay is CPU, not IO.
