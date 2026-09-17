"""Attribute a transient block-IO stall to the writer that caused it.

#247 filed ~25 % *sustained* IO PSI as swap thrashing on the single mmcblk0.
zram removed that regime (avg300 33.58 → 0.79, `/var/swap` at 0 B) and left a
burst problem of the same peak magnitude behind. #283 (arecord ALSA overruns,
~5/day) and #287 (px-alive killed by TimeoutStartSec while legitimately parked
in `lease_wait`) are that same stall wearing two other faces: every instrumented
overrun sits at `psi_io_some_avg10_at >= 50 %` while memory PSI is 0.0, so the
swap-pressure story no longer explains the residual.

What the narrowed ask needs is a record taken *at* the stall. The 2026-08-20
sample in #247 took a 5-second quiet-period average, found nothing writing, and
concluded — correctly for that sample — that no SPARK daemon was the cause. This
module holds the measurement half of doing it at the right moment: sample, rank,
and state plainly what could not be read. Triggering, running and persisting
live in ``bin/px-io-attrib``.

Two channels, and the difference between them is privilege:

* **writer channel** — ``/proc/<pid>/io`` deltas. Ranked on ``write_bytes`` /
  ``read_bytes`` (disk) rather than ``wchar``/``rchar`` (which count pipe and
  socket traffic too): #247's first sample was misled by 2.4 MB/s of ``wchar``
  from go2rtc/rpicam-vid that never touched the disk. Reading it for root-owned
  processes (px-alive, journald) needs root or CAP_SYS_PTRACE — as ``pi``,
  ``/proc/1/io`` is EACCES.
* **stall channel** — ``/proc/<pid>/stat`` (state, ``delayacct_blkio_ticks``),
  ``schedstat`` (run delay), ``wchan``, plus ``/proc/diskstats`` write-queue
  time, ``/proc/vmstat`` and PSI. It is what separates "a readable process was
  writing" from "the device was busy and no readable process was writing" — the
  latter being the signature of ext4 journal work (``jbd2/*`` kthreads) or of a
  writer this process is not allowed to see.

  Readable for root-owned processes ***except `wchan`***, which is the field
  this channel was built around and which the kernel silently withholds:
  measured on `picar` 2026-09-18, 8 consecutive passes returned **0 symbols for
  65 root-owned sleeping processes and 0 errors** while the 23-24 pi-owned ones
  returned 21-22. So `wchan` names a writer only when `/proc/<pid>/io` was
  already readable for it, and every record now says which blocked processes
  were left unnamed (`wchan_withheld_count`) instead of letting `null` read as
  "not blocked in anything".
* **kernel channel** — `journalctl -k` over the window, filtered to the
  messages that can explain a wedge (`mmc`, SDIO, `brcmfmac`, `ext4`, `jbd2`,
  I/O errors, timeouts, undervoltage, thermal). World-readable on hosts where
  the operator is in `adm`; the one channel that can name a mechanism when there
  is no writer to name. Bounded to one call per capture, and its cost is
  measured (`kernel_log_child_*_bytes`) rather than assumed.
* **unit channel** — systemd lifecycle lines over the same window
  (`unit_log`). The kernel says what the hardware did; this says what the
  *scheduler* did, which is the only way to name a timer-driven writer that
  exits before its bytes reach the card — `apt-daily` on 2026-09-18 (#402).
* **window channel** — the same state sampled *across* the window
  (`sample_window`, `blocked_in_window`) instead of at its two ends. A burst
  that ends before t1 is invisible to a two-ended walk, and state is the only
  reading that survives the privilege boundary, so a 3 s window is sampled every
  0.5 s: per-pid D fractions, plus per-thread D for a rotating quarter of the
  process set (px-alive's wedged health thread sits behind a leader that stays
  ``S``, so threads are where that writer hides).
  Both views record *which* sample, not just how many, so co-blocking is
  checkable: two processes each blocked once is a different finding depending on
  whether it was the same half-second (`blocked_at_samples`, `co_blocked_samples`).

  ``/proc/<pid>/stat`` only reports the *thread group leader*, and that is not
  enough for the daemon this investigation is about: px-alive reports health
  from a background thread whose ``mkstemp``+``fsync`` into ``state/health/``
  has been caught in ``jbd2_log_wait_commit`` for seconds at a stretch, which
  leaves the process unkillable (SIGKILL cannot interrupt D state) while its
  main loop keeps beating. So blocked secondary threads are recorded per pid
  by ``blocked_threads``, with their own ``wchan``.

Everything here is read-only and never raises: a partial record taken during a
stall is worth more than an exception thrown inside it.
"""

from __future__ import annotations

import glob
import json
import os
import re
import resource
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Collection, Mapping, Sequence

from .hostload import host_load_fields

# Fields in /proc/<pid>/io we keep. `cancelled_write_bytes` is included because
# a process that writes and then discards shows up as net-zero write_bytes while
# still having driven the device.
IO_FIELDS = (
    "rchar",
    "wchar",
    "syscr",
    "syscw",
    "read_bytes",
    "write_bytes",
    "cancelled_write_bytes",
)

# Cumulative /proc/vmstat counters worth a delta across the window (swap and
# reclaim), and gauges worth a point reading (dirty/writeback pages, which is
# what tells "the device is behind" from "userspace is asking for a lot").
VMSTAT_COUNTERS = (
    "pswpin",
    "pswpout",
    "pgpgin",
    "pgpgout",
    "pgscan_direct",
    "pgsteal_direct",
    "pgscan_kswapd",
    "pgsteal_kswapd",
    "pgmajfault",
)
VMSTAT_GAUGES = ("nr_dirty", "nr_writeback", "nr_unstable")

# /proc/diskstats field names, positionally, from the first counter column.
# Kernels 5.5+ emit all of these; older ones stop after weighted_ms_io, so the
# parser keeps whatever is present rather than assuming a width.
DISKSTAT_FIELDS = (
    "reads_completed",
    "reads_merged",
    "sectors_read",
    "ms_reading",
    "writes_completed",
    "writes_merged",
    "sectors_written",
    "ms_writing",
    "ios_in_progress",
    "ms_io",
    "weighted_ms_io",
    "discards_completed",
    "discards_merged",
    "sectors_discarded",
    "ms_discarding",
    "flush_completed",
    "ms_flushing",
)

# ram*/loop*/dm-* would only pad every record: they never stall on anything,
# and a loop device's numbers restate the file behind it. zram0 is deliberately
# NOT in this list — it is the swap device since 2026-08-26, and a record that
# shows zram activity alongside a stalled mmcblk0 is what distinguishes memory
# churn from the SD-card stall this whole investigation is about.
_IGNORED_DEVICE_PREFIXES = ("ram", "loop", "dm-")
_MAX_DEVICES = 6

# /proc/<pid>/cgroup in cgroup v2 is "0::/system.slice/px-wake-listen.service".
_UNIT_PREFIXES = ("system.slice/", "user.slice/", "machine.slice/")


@dataclass(frozen=True)
class Paths:
    """Every file this module reads, so a test can point it at a fixture tree."""

    proc: Path = Path("/proc")
    pressure_io: Path = Path("/proc/pressure/io")
    diskstats: Path = Path("/proc/diskstats")
    vmstat: Path = Path("/proc/vmstat")
    proc_stat: Path = Path("/proc/stat")
    uptime: Path = Path("/proc/uptime")
    #: Per-device ``inflight`` gauges. A directory rather than a file because
    #: the interesting question is per device, and because the answer "0/0"
    #: has to be reportable — see ``sample_inflight``.
    sys_block: Path = Path("/sys/block")
    #: Per-filesystem ext4 counters (world-readable). The filesystem's own view
    #: of how many bytes it wrote, which is the only way to tell the device's
    #: 500 KB per stall from *file data* — see ``sample_ext4``.
    ext4_sysfs: Path = Path("/sys/fs/ext4")


DEFAULT_PATHS = Paths()

# How long a window may be before it stops measuring the stall and starts
# measuring the recovery. The stalls in #247/#283 are ~10 s; 3 s sits inside
# one. Not enforced — bin/px-io-attrib clamps its own flag — but named here
# because every caller of capture() inherits it.
DEFAULT_WINDOW_S = 3.0


#: How often the in-window sampler reads /proc while a stall is being captured.
#: The two-ended walk it replaces could only see a stall that happened to be
#: blocked at t0 or t1; #247's residual is a *burst*, so the window is spent
#: sampling instead of sleeping through it.
DEFAULT_SAMPLE_INTERVAL_S = 0.5

#: Kernel messages worth keeping in a record, and the reason this channel
#: exists at all: a *wedge* — a stall where the device is idle and every task is
#: blocked — has no userspace writer to name, and if it has an explanation in
#: software, that explanation is a kernel message (an `mmc` timeout, an SDIO
#: error, an undervoltage event, an ext4 complaint). Readable unprivileged on
#: `picar` (`pi` is in `adm`, so both `journalctl -k` and `dmesg` work) —
#: measured 2026-09-18, when the boot's kernel log held two
#: `hwmon hwmon1: Undervoltage detected!` events, their `Voltage normalised`
#: recoveries, and the SDIO WiFi's power-save transitions.
#:
#: The filter is reported *in* the record, alongside the totals, so an empty
#: `lines` list reads as "no kernel line matched this filter" and never as "the
#: kernel said nothing" — the same honesty rule as `file_growth_watched`.
KERNEL_LOG_PATTERN = re.compile(
    r"mmc|sdio|brcmf|ext4|jbd2|blk_|i/o error|timeout|undervolt|voltage|"
    r"thermal|throttl|hung task|blocked for more than",
    re.IGNORECASE,
)
KERNEL_LOG_LIMIT = 20
#: Of the `limit` matched lines kept, this many come from the *start* of the
#: lookback. The rest come from the end.
#:
#: Why both ends, measured 2026-09-18: the unit channel exists to name a writer
#: that dirtied and exited *before* the stall became visible, so its `Starting`
#: line sits at the front of the 5-minute lookback — and a plain tail-keep
#: dropped it. On the real episode, 22-24 matched lines against a cap of 20 meant
#: the oldest 2-4 were discarded, which is exactly where `apt-daily-upgrade`'s
#: `Starting` line was (#402). The tail still matters (continuity, failures), so
#: neither end is allowed to evict the other.
KERNEL_LOG_HEAD = 7
KERNEL_LOG_MAX_LINES = 4000
KERNEL_LOG_TIMEOUT_S = 5.0
#: Seconds of lookback added to the measured window. The trigger fires on a 10 s
#: PSI average, so the cause can predate t0; 5 s is a guess, stated as a
#: constant, and every record carries the window it actually asked for.
KERNEL_LOG_PAD_S = 5.0
#: The unit channel looks back further than the window, because the writer it
#: exists to name *has already exited* by the time the stall is visible. Measured
#: 2026-09-18: `apt-daily` ran 05:25:38-05:26:14, its 60 MB `pkgcache.bin` write
#: landed 05:25:54, and the stall it caused was recorded at 05:27:52 — so a
#: window-sized lookback would have missed the only line that names it.
UNIT_LOG_LOOKBACK_S = 300.0
#: Files modified within this long before the window, for the same reason.
RECENT_FILE_LOOKBACK_S = 300.0

#: The in-window per-thread walk covers this fraction of the process set per
#: sample, so every process is visited once per rotation instead of paying a
#: full thread walk every 0.5 s. Threads are where a block hides behind a
#: healthy leader (px-alive's health thread), so the rotation period is
#: reported as `window_thread_coverage_s` rather than left implicit.
THREAD_ROTATION_DIVISOR = 4


# --- readers (all total, none raising) -----------------------------------


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, ValueError):
        return None


def _journalctl(args: Sequence[str]) -> str:
    """Run `journalctl` and return its stdout. Raises on any failure."""
    proc = subprocess.run(
        ["journalctl", *args],
        capture_output=True,
        text=True,
        timeout=KERNEL_LOG_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"journalctl exit {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
        )
    return proc.stdout


#: systemd's own lifecycle lines. This is the channel that names a *timer*: a
#: daily job that dirties a hundred megabytes and exits before the kernel has
#: written any of it leaves no process to attribute the bytes to (measured
#: 2026-09-18: `apt-daily` exited at 05:26:14, and the record at 05:27:52 saw
#: 48 MB written with 37 KB attributable to any readable process — #402). Its
#: `Starting`/`Deactivated` lines carry the timestamps, and they are in the
#: journal, not in /proc.
#: systemd writes lifecycle lines two ways — `Starting x.service ...` and
#: `x.service: Deactivated successfully.` — so the verb is matched *anywhere*
#: after the `systemd[1]:` prefix rather than immediately after it. Getting this
#: wrong is how the 05:26 `Deactivated` line for `apt-daily` would have been
#: filtered out of the very record that needed it.
UNIT_LOG_PATTERN = re.compile(
    r"systemd\[1\]:.*\b(Starting|Started|Finished|Stopping|Stopped|Deactivating|"
    r"Deactivated|Failed|Scheduling restart|Reloading|Reloaded)\b"
)


def _journal_filtered(
    source: str,
    args: Sequence[str],
    pattern: re.Pattern[str],
    since_s: float,
    runner: Callable[[Sequence[str]], str] | None,
    limit: int,
) -> dict[str, Any]:
    """Shared body of the journal channels: run, filter, cap, never raise.

    `available: false` is a first-class answer (no journalctl, no permission, no
    journal). It is never "the log was quiet" — that is an empty `lines` with
    `lines_total` beside it — and the `filter` travels with the record so an
    empty list stays checkable years later.
    """
    since = time.time() - max(0.0, since_s)
    full_args = [*args, "--since", f"@{int(since)}"]
    run = runner or _journalctl
    try:
        text = run(full_args)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return {
            "source": source,
            "available": False,
            "window_s": round(since_s, 1),
            "reason": f"{type(exc).__name__}: {exc}"[:200],
        }
    lines = [line for line in text.splitlines() if line.strip()]
    scanned = lines[-KERNEL_LOG_MAX_LINES:]
    matched = [line for line in scanned if pattern.search(line)]
    # Keep both ends of the match set: the onset of a timer-driven writer is at
    # the front of the lookback and its recovery at the back.
    head = min(max(1, KERNEL_LOG_HEAD), limit)
    if len(matched) <= limit:
        kept = matched
    else:
        tail = max(0, limit - head)
        kept = matched[:head] + (matched[-tail:] if tail else [])
    return {
        "source": source,
        "available": True,
        "window_s": round(since_s, 1),
        "filter": pattern.pattern,
        "lines_total": len(lines),
        "lines_scanned": len(scanned),
        "matched_total": len(matched),
        # Named, not left to arithmetic on the reader's side.
        "lines_omitted": max(0, len(matched) - len(kept)),
        "lines": kept,
    }


def unit_log_window(
    since_s: float,
    *,
    runner: Callable[[Sequence[str]], str] | None = None,
    limit: int = KERNEL_LOG_LIMIT,
) -> dict[str, Any]:
    """systemd lifecycle lines from the last `since_s` seconds. Never raises.

    The complement of the kernel channel: the kernel says what the *hardware*
    did, this says what the *scheduler* did — and a timer-driven writer is
    invisible to every other channel here, because it exits before its bytes
    land. See `UNIT_LOG_PATTERN`.
    """
    return _journal_filtered(
        "journalctl (unit lifecycle)",
        ["--no-pager", "-o", "short-iso"],
        UNIT_LOG_PATTERN,
        since_s,
        runner,
        limit,
    )


def kernel_log_window(
    since_s: float,
    *,
    runner: Callable[[Sequence[str]], str] | None = None,
    limit: int = KERNEL_LOG_LIMIT,
) -> dict[str, Any]:
    """Kernel messages from the last `since_s` seconds, filtered. Never raises.

    One `journalctl -k` call per capture, with a timeout — the journal lives on
    the same card as everything else here, so this channel buys its evidence
    with the resource it is measuring. That is why the call is bounded (a pad,
    a line cap, a timeout), why it happens *after* the window rather than inside
    it, and why `capture()` differences `child_io()` around it: the observer's
    `/proc/self/io` cannot see a child's reads, so without that the instrument's
    cost would be understated exactly when the device is the thing being read.

    `available: false` is a first-class answer (no journalctl, no permission, no
    journal). It is never "the kernel was quiet" — that is `lines: []` with
    `lines_total` beside it.
    """
    return _journal_filtered(
        "journalctl -k",
        ["-k", "--no-pager", "-o", "short-iso"],
        KERNEL_LOG_PATTERN,
        since_s,
        runner,
        limit,
    )


def child_io() -> dict[str, int]:
    """Block IO performed by this process's *children* (RUSAGE_CHILDREN).

    `/proc/self/io` excludes children. This module is otherwise pure /proc reads
    — free of the device it measures — and the kernel-log channel is the one
    place that changes that, so the cost is measured rather than assumed.
    `ru_inblock`/`ru_oublock` are in 512-byte units.
    """
    try:
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    except (OSError, ValueError):  # pragma: no cover — not reachable on POSIX
        return {"read_bytes": 0, "write_bytes": 0}
    return {
        "read_bytes": int(usage.ru_inblock) * 512,
        "write_bytes": int(usage.ru_oublock) * 512,
    }


def parse_proc_io(text: str) -> dict[str, int]:
    """`{"rchar": 1, ...}` from one /proc/<pid>/io body."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key in IO_FIELDS:
            try:
                out[key] = int(value.strip())
            except ValueError:
                continue
    return out


def parse_proc_stat(text: str) -> dict[str, Any]:
    """`{"sid"?, "comm", "state"}` from one /proc/<pid>/stat body.

    `comm` is delimited by the *outermost* parentheses because it may itself
    contain spaces or parentheses (`(sd-pam)`, `(python3 /opt/x)`), which is
    why this is not a whitespace split.
    """
    open_paren = text.find("(")
    close_paren = text.rfind(")")
    if open_paren == -1 or close_paren == -1 or close_paren < open_paren:
        return {}
    fields = text[close_paren + 1 :].split()
    out: dict[str, Any] = {"comm": text[open_paren + 1 : close_paren]}
    if fields:
        out["state"] = fields[0]
    # Field 42, `delayacct_blkio_ticks` — 0-based index 39 once `pid` and
    # `comm` are off the front. It is one of the two fields here that survive
    # the privilege boundary, and unlike `state` it measures *wait* rather than
    # presence, which is what makes it the only per-process block-IO reading a
    # root-owned writer (px-alive, journald) can be charged with by an
    # unprivileged observer. Populated only when kernel.task_delayacct is 1 —
    # see `delayacct_enabled`, and do not read an absent key as zero.
    if len(fields) > 39:
        try:
            out["delayacct_blkio_ticks"] = int(fields[39])
        except ValueError:
            pass
    return out


def parse_schedstat(text: str) -> dict[str, int]:
    """`{"cpu_ns", "run_delay_ns", "timeslices"}` from /proc/<pid>/schedstat.

    `run_delay_ns` is *runnable-but-not-running* time — CPU contention, not IO
    wait. It is here because a stall that looks like IO can also be a process
    that never got a core to notice the IO was done, and those two readings
    have to be distinguishable in the same record.
    """
    parts = text.split()
    out: dict[str, int] = {}
    for name, raw in zip(("cpu_ns", "run_delay_ns", "timeslices"), parts):
        try:
            out[name] = int(raw)
        except ValueError:
            return out
    return out


def parse_diskstats(text: str) -> dict[str, dict[str, int]]:
    """`{device: {reads_completed: ..., ms_writing: ...}}` from /proc/diskstats."""
    devices: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[2]
        counters: dict[str, int] = {}
        for field_name, raw in zip(DISKSTAT_FIELDS, parts[3:]):
            try:
                counters[field_name] = int(raw)
            except ValueError:
                break
        if counters:
            devices[name] = counters
    return devices


def parse_procs_stat(text: str) -> dict[str, int]:
    """`{"running": n, "blocked": n}` from /proc/stat's procs_* lines.

    The context every io-PSI number needs and neither PSI file carries. `some`
    is "at least one task stalled" and `full` is "every non-idle task stalled",
    so on a box where only one or two tasks are runnable, both can read 25-45 %
    while almost nothing is happening — and the reverse reading ("the whole
    system froze") is the one that gets written into issues. `procs_running`
    and `procs_blocked` are what tells the two apart, and they are two lines of
    a world-readable file.
    """
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "procs_running":
            out["running"] = int(value.strip() or 0)
        elif key == "procs_blocked":
            out["blocked"] = int(value.strip() or 0)
    return out


def parse_vmstat(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    wanted = set(VMSTAT_COUNTERS) | set(VMSTAT_GAUGES)
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key in wanted:
            try:
                out[key] = int(value.strip())
            except ValueError:
                continue
    return out


def read_heartbeat_age(
    path: Path, now: float | None = None
) -> tuple[float | None, str | None]:
    """`(age_s, mode)` from px-alive's heartbeat, `(None, None)` if unreadable.

    The heartbeat is written with `os.replace`, so a torn read is not possible:
    an unreadable file is a missing file or a malformed one, and both are
    reported as absent rather than guessed at.
    """
    text = _read_text(path)
    if text is None:
        return None, None
    try:
        record = json.loads(text)
        age = (time.time() if now is None else now) - float(record["ts"])
        return age, record.get("mode")
    except (ValueError, KeyError, TypeError):
        return None, None


def process_alive(pid: int, proc_root: Path) -> bool:
    """Whether `pid` exists in `proc_root`. Gates the heartbeat trigger.

    `proc_root` is authoritative when it exists: a tree without the pid answers
    False, which is exactly what the observer needs for a pid file left behind
    by a crash. Where the tree itself is absent — a developer Mac, which has no
    `/proc` at all — fall back to a signal-0 probe, because otherwise the
    heartbeat gate can never be armed off-robot and the observer's own
    diagnostics ("is the trigger armed?") are unreadable exactly where reading
    them is cheap. On the robot the fallback is never reached, and as `pi` it
    would answer False for a root-owned pid anyway (EPERM).
    """
    try:
        if (proc_root / str(pid)).is_dir():
            return True
    except OSError:
        return False
    if not proc_root.exists():
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    return False


def write_pid_file(path: Path, pid: int | None = None) -> bool:
    """Publish this process's pid so an operator never has to guess it.

    Matching `/usr/bin/python3 -` in `/proc` is ambiguous — four daemons on this
    host share that argv, including root's `px-battery-poll` — and the first
    version of the manual runbook wrote whichever matched last. A pid file
    pointing at *another* daemon is worse than no pid file: `kill $(cat …)`
    then kills the wrong thing. Never raises.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid() if pid is None else pid))
        return True
    except OSError:
        return False


def remove_pid_file(path: Path, pid: int | None = None) -> None:
    """Remove the pid file — but only while it is still ours. Never raises."""
    mine = os.getpid() if pid is None else pid
    try:
        if read_pid_file(path) == mine:
            path.unlink()
    except OSError:
        pass


def read_pid_file(path: Path) -> int | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


# --- the file channel ----------------------------------------------------

#: Where a growth path belongs, for the record's group totals. Ordered: the
#: journal check first because journal filenames carry no directory hint.
_GROWTH_GROUPS = (
    ("apt", "/var/cache/apt"),
    ("apt", "/var/lib/apt"),
    ("journal", ".journal"),
    ("health", "/health/"),
    ("state", "/state/"),
    ("logs", "/logs/"),
)


def growth_group(path: str) -> str:
    for name, marker in _GROWTH_GROUPS:
        if marker in path:
            return name
    return "other"


def default_growth_patterns(log_dir: Path | str, state_dir: Path | str) -> list[str]:
    """The watchlist's default set: the *shapes* that actually churn here.

    Widened 2026-09-18 after a record in which the device wrote 112 KB and every
    channel came back empty (`file_growth: []`, `file_touched: []`, 115 paths
    watched). The old set was `*.log` and `*.json`, and the host's churn is not
    those extensions: `state/*.jsonl` (thoughts, conversations, debug reports),
    `state/*.lock`, `logs/*.jsonl`, `logs/*.out`, `logs/*.rotlock`, and files one
    level down (`logs/<subdir>/*`). An ad-hoc probe that globbed `state/*` and
    `logs/*` saw those files changing while the observer's narrower set saw
    nothing — which is the difference between a measurement and a claim.

    Still a watchlist, not a scan: a bounded number of globs, `limit=400` per
    pattern in `sample_file_meta`, and the caller records how many paths were
    covered so silence outside the list is never read as "nobody wrote".
    """
    log_dir, state_dir = Path(log_dir), Path(state_dir)
    return [
        "/var/log/journal/*/*.journal",
        str(log_dir / "*"),
        str(log_dir / "*" / "*"),
        str(state_dir / "*"),
        str(state_dir / "health" / "*.json"),
        str(state_dir / "brain" / "*"),
        # Not a SPARK daemon, and the largest writer on this host: measured
        # 2026-09-18, `apt-daily` rewrote `/var/cache/apt/pkgcache.bin` and the
        # 137 MB `/var/lib/apt/lists` tree and the resulting writeback stalled
        # the card for minutes (#402). The watchlist covered the journal, the
        # logs and the state dir — every tree *this repo* writes — and the
        # biggest one on the box was outside it.
        "/var/lib/apt/lists/*",
        "/var/lib/apt/periodic/*",
        "/var/cache/apt/*.bin",
    ]


def sample_file_meta(
    patterns: Sequence[str], *, limit: int = 400
) -> dict[str, dict[str, int]]:
    """`{path: {"size": n, "mtime_ns": n}}` for everything matching `patterns`.

    One stat per path — the *size* half feeds `rank_file_growth`, the *mtime*
    half feeds `rank_file_touches`. Two channels from one walk, because the
    second exists precisely for writers the first cannot see: journald appends
    into an 8 MB preallocated, mmap'd journal whose size never moves, and ext4
    journal/metadata writes change no file size at all. Measured on `picar`
    2026-09-17: four consecutive `io_psi` records with 128-524 KB written to
    mmcblk0, `file_growth_groups: {}`, and `file_growth_watched: 115`.

    Never raises: a pattern that matches nothing, a file that vanished between
    glob and stat, and a directory we may not traverse all mean "not watched",
    not "did not write" — which is why the caller records how many paths were
    watched at all.
    """
    meta: dict[str, dict[str, int]] = {}
    for pattern in patterns:
        try:
            matches = sorted(glob.glob(pattern))
        except (OSError, ValueError):
            continue
        for path in matches[:limit]:
            # Directories match a literal path glob and their st_size moves as
            # entries are added — that is not a writer, it is bookkeeping.
            if not os.path.isfile(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            meta[path] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return meta


def sample_file_sizes(patterns: Sequence[str], *, limit: int = 400) -> dict[str, int]:
    """`{path: size_bytes}` — the size half of `sample_file_meta`."""
    return {
        path: entry["size"]
        for path, entry in sample_file_meta(patterns, limit=limit).items()
    }


def _ext4_window(
    pre: Mapping[str, Any], post: Mapping[str, Any]
) -> dict[str, Any]:
    """`{fs, write_kbytes_delta, ...}` across the window; `{}` if unwatched."""
    if not post:
        return {}
    out: dict[str, Any] = {"fs": post.get("fs")}
    before = pre.get("session_write_kbytes")
    after = post.get("session_write_kbytes")
    # No pre-sample (filesystem mounted mid-window) means no delta, not a zero.
    if isinstance(before, int) and isinstance(after, int):
        out["write_kbytes_delta"] = after - before
    for field in ("session_write_kbytes", "lifetime_write_kbytes",
                  "delayed_allocation_blocks", "errors_count"):
        if field in post:
            out[field] = post[field]
    if "journal_task" in post:
        out["journal_task"] = post["journal_task"]
    return out


def recent_file_writes(
    meta: Mapping[str, Mapping[str, int]],
    *,
    now_s: float,
    lookback_s: float = RECENT_FILE_LOOKBACK_S,
    top: int = 8,
) -> list[dict[str, Any]]:
    """Watchlist files modified in the `lookback_s` before the window.

    The complement of `rank_file_growth`, and the channel that would have named
    the 2026-09-18 writer: `apt-daily` rewrote a **60 MB** `pkgcache.bin` at
    05:25:54 and exited, and by the time the stall was visible at 05:27:52 every
    window-scoped channel was empty — the bytes were in the page cache, then on
    the card, and the process that asked for them was gone.

    Ranked by **size**, not by age, because the question is "what mass moved
    recently": this watchlist contains gauges that are rewritten every few
    seconds (health records, locks), and ordering by recency would fill the list
    with them. `size_bytes` is the file's size, not how much it grew — a big file
    whose mtime just moved is a candidate, and it is the reader's job to weigh it
    against `file_growth` for the same path.
    """
    rows: list[dict[str, Any]] = []
    for path, entry in meta.items():
        mtime_s = entry.get("mtime_ns", 0) / 1_000_000_000
        if not mtime_s:
            continue
        age_s = now_s - mtime_s
        if age_s < 0 or age_s > lookback_s:
            continue
        rows.append(
            {
                "path": "/".join(path.split("/")[-3:]),
                "age_s": round(age_s, 1),
                "size_bytes": int(entry.get("size", 0)),
            }
        )
    rows.sort(key=lambda row: row["size_bytes"], reverse=True)
    return rows[:top]


def rank_file_touches(
    pre: Mapping[str, Mapping[str, int]],
    post: Mapping[str, Mapping[str, int]],
    *,
    top: int = 8,
) -> tuple[list[dict[str, Any]], int]:
    """`(ranked_touches, touched_count)` for files whose mtime moved.

    Rows are `{path, bytes}`, and **zero-byte rows sort first**: a file that was
    written without changing size is the whole reason this channel exists
    (journald's mmap'd journal), and it would be invisible in `file_growth`.

    This answers *who was asked*, not *what reached the device*: mtime moves
    when a write lands in page cache, which is upstream of any fsync. A row here
    is a writer to attribute, not a proven cause of device work — and only files
    present at both ends are counted, so a rotation cannot manufacture one.
    """
    rows: list[dict[str, Any]] = []
    for path, after in post.items():
        before = pre.get(path)
        if before is None or after.get("mtime_ns") == before.get("mtime_ns"):
            continue
        rows.append(
            {
                "path": "/".join(path.split("/")[-3:]),
                "bytes": after.get("size", 0) - before.get("size", 0),
            }
        )
    # Zero-byte touches first (the size channel's blind spot), then by bytes.
    rows.sort(key=lambda row: (row["bytes"] != 0, -row["bytes"]))
    return rows[:top], len(rows)


def rank_file_growth(
    pre: Mapping[str, int],
    post: Mapping[str, int],
    *,
    top: int = 8,
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    """`(ranked_growth, total_bytes, group_totals)` across the window.

    Only files present at both ends: a file that appears mid-window (log
    rotation) has no baseline, and reporting its whole size as growth would
    manufacture a writer out of a rename.
    """
    rows: list[dict[str, Any]] = []
    total = 0
    groups: dict[str, int] = {}
    for path, after in post.items():
        before = pre.get(path)
        if before is None or after <= before:
            continue
        grew = after - before
        total += grew
        group = growth_group(path)
        groups[group] = groups.get(group, 0) + grew
        rows.append({"path": "/".join(path.split("/")[-3:]), "bytes": grew})
    rows.sort(key=lambda row: row["bytes"], reverse=True)
    return rows[:top], total, groups


# --- sampling -------------------------------------------------------------


def iter_pids(proc_root: Path) -> list[int]:
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    pids: list[int] = []
    for entry in entries:
        name = entry.name
        if name.isdigit():
            pids.append(int(name))
    return pids


def proc_io_readable(proc_root: Path = Path("/proc")) -> bool:
    """Whether /proc/<pid>/io is readable for processes this user does not own.

    Probed against **pid 1** specifically, because reading our *own* io always
    succeeds: a probe that accepted that would report the writer channel as
    available on a host where every root-owned writer — px-alive, journald — is
    invisible, which is worse than reporting it unavailable. Measured live:
    `/proc/1/io` is `EACCES` for `pi` and readable for root.
    """
    try:
        (proc_root / "1" / "io").read_text()
        return True
    except OSError:
        return False


def _read_proc_io(pid_dir: Path) -> tuple[dict[str, int] | None, bool]:
    """`(counters, denied)` for one pid.

    `EACCES` is a fact about privilege and is reported separately; every other
    error (a process that exited mid-walk) is not, or the record would blame
    root for a race.
    """
    try:
        text = (pid_dir / "io").read_text()
    except PermissionError:
        return None, True
    except OSError:
        return None, False
    return parse_proc_io(text), False


def blocked_threads(pid: int, pid_dir: Path) -> list[dict[str, Any]]:
    """Secondary threads of `pid` sitting in D state, with their comms.

    Not a nice-to-have: a daemon whose background health-write thread is stuck
    in uninterruptible sleep cannot be SIGKILLed, which is exactly the
    `Processes still around after SIGKILL` line in #287's journal excerpt, and
    the group leader's own state is `S` while that is true. One cheap stat read
    per thread (~300 on this host), skipped entirely when the task directory is
    unreadable.
    """
    try:
        tids = [
            int(entry.name)
            for entry in (pid_dir / "task").iterdir()
            if entry.name.isdigit()
        ]
    except (OSError, ValueError):
        return []
    blocked: list[dict[str, Any]] = []
    for tid in tids:
        if tid == pid:
            continue  # the leader is already covered by /proc/<pid>/stat
        text = _read_text(pid_dir / "task" / str(tid) / "stat")
        if text is None:
            continue
        parsed = parse_proc_stat(text)
        if parsed.get("state") == "D":
            blocked.append({"tid": tid, "comm": parsed.get("comm") or "?"})
    return blocked


def sample_processes(
    proc_root: Path, *, allow_proc_io: bool
) -> dict[int, dict[str, Any]]:
    """One walk of /proc: per-pid io counters (privileged) + state + run delay.

    Deliberately small files only — the cgroup unit and the blocked symbols are
    resolved afterwards, for the handful of pids that made the record, rather
    than for all ~150 of them.
    """
    out: dict[int, dict[str, Any]] = {}
    for pid in iter_pids(proc_root):
        pid_dir = proc_root / str(pid)
        stat_text = _read_text(pid_dir / "stat")
        if stat_text is None:
            continue  # exited mid-walk (or is not ours to read)
        entry: dict[str, Any] = parse_proc_stat(stat_text)
        sched_text = _read_text(pid_dir / "schedstat")
        if sched_text is not None:
            entry.update(parse_schedstat(sched_text))
        if allow_proc_io:
            counters, denied = _read_proc_io(pid_dir)
            if counters is not None:
                entry["io"] = counters
            elif denied:
                # Kept, not dropped: a process whose io we may not read is
                # evidence ("a writer we cannot see"), not absence of evidence.
                entry["io_denied"] = True
        stalled = blocked_threads(pid, pid_dir)
        if stalled:
            entry["blocked_threads"] = stalled
        out[pid] = entry
    return out


def read_unit(pid: int, proc_root: Path) -> str | None:
    """The systemd unit a pid belongs to, from /proc/<pid>/cgroup, or None.

    This is the cgroup/unit attribution #247 asked for by name: a pid tells you
    which process, the unit tells you which *thing to change*.
    """
    text = _read_text(proc_root / str(pid) / "cgroup")
    if text is None:
        return None
    for line in text.splitlines():
        # "0::/system.slice/x.service" — split on both colons, not the first
        # one, or every v2 cgroup line parses as ":/system.slice/...".
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        path = parts[2].strip()
        for prefix in _UNIT_PREFIXES:
            marker = "/" + prefix
            if marker in path:
                return path.split(marker, 1)[1] or None
        if path and path != "/":
            return path.strip("/") or None
    return None


def _wchan_symbol(path: Path) -> str | None:
    symbol = (_read_text(path) or "").strip()
    if not symbol or symbol == "0":
        return None
    return symbol


def read_wchan(pid: int, proc_root: Path) -> str | None:
    """The kernel symbol a process is blocked in, e.g. `jbd2_log_wait_commit`.

    "0" is what the kernel reports for a task that is not blocked at this
    instant, which is why this is only read for tasks observed in D state.

    **It is also what the kernel reports for a task this uid may not ptrace**,
    and it does that *silently* — the read succeeds and returns "0" for a
    process that is genuinely blocked, not `EACCES`. Measured on `picar`
    2026-09-18, 8 consecutive passes: **65 root-owned sleeping processes
    returned 0 symbols and 0 errors, while the 23-24 pi-owned ones returned
    21-22 symbols**, so on this host `wchan` names a writer only when the
    observer can already read that writer's `/proc/<pid>/io`.

    That asymmetry is why a `None` here is never reported as "not blocked":
    the record derives `wchan_withheld` from the io denial it *has* measured
    and says so out loud. The remedy is to run the observer as root — this uid
    cannot see the symbol, and pretending "0" means "running" is the failure
    mode this whole investigation is about.
    """
    return _wchan_symbol(proc_root / str(pid) / "wchan")


def read_task_wchan(pid: int, tid: int, proc_root: Path) -> str | None:
    """`read_wchan` for one secondary thread of a process."""
    return _wchan_symbol(proc_root / str(pid) / "task" / str(tid) / "wchan")


def delayacct_enabled(paths: Paths = DEFAULT_PATHS) -> bool | None:
    """Whether the kernel is populating `delayacct_blkio_ticks` (None: unknown).

    `kernel.task_delayacct` defaults to 0 on this kernel, and it is a *global*
    switch: turning it on only affects tasks forked afterwards, so the field
    stays 0 for every long-lived daemon until it is restarted. Two consequences
    worth writing down rather than rediscovering:

    * a record taken with this reading `False` has no per-process block-IO
      wait, and that is "not measured", never "waited zero";
    * the switch needs root, so on `picar` this channel is an ask (#247), not
      a capability — see docs/operations/io-attribution.md.
    """
    text = _read_text(paths.proc / "sys" / "kernel" / "task_delayacct")
    if text is None:
        return None
    return text.strip() == "1"


def _tick_ms() -> float:
    """One `delayacct_blkio_ticks` tick in milliseconds (USER_HZ; 100 here)."""
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        hz = 100
    return 1000.0 / hz if hz else 10.0


def sample_window(
    paths: Paths,
    pids: Sequence[int],
    *,
    samples: int,
    interval_s: float = DEFAULT_SAMPLE_INTERVAL_S,
    thread_pids: Sequence[int] = (),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Read blocked state *across* the window instead of sleeping through it.

    A two-ended walk — one /proc sweep at t0, another at t1 — can only see a
    stall that happens to be blocked at one of those two instants. #247's
    residual after zram is a burst, so a 3 s window sampled twice is mostly
    blind, and the window is otherwise spent doing nothing.

    What is read, and why exactly this:

    * ``/proc/<pid>/stat`` for every pid, every sample. `state` and
      `delayacct_blkio_ticks` are the two readings that survive the privilege
      boundary — on `picar`, 65 root-owned sleeping processes returned a
      `wchan` of 0 across 8 passes while pi-owned ones returned symbols — so
      D state is the only signal left that can name a *root-owned* writer.
    * per-thread D state for a rotating quarter of the process set, plus any
      pid already known to have had a blocked thread at t0. px-alive's health
      thread blocks behind a leader that stays `S` (#287), so leader state
      alone cannot see that writer; rotating keeps the cost bounded while
      still visiting every process once per rotation.
    * `wchan` for any task observed in D — a symbol when this uid may read it,
      nothing when it may not, which is what `wchan_withheld` later calls out.

    Pids that *started* inside the window are not in `pids` and so are not
    sampled; the closing walk still sees them. Never raises: a missing /proc
    yields an empty tally, and `samples`/`span_s` are returned so an empty tally
    can be told from a sampler that never ran.

    Ticks are kept on a target clock rather than by sleeping `interval_s` after
    each sample, so `samples` really does tile the window it claims to: on the
    robot a sample costs ~40 ms of /proc reads, and a version that slept a full
    interval *in addition* would stretch a 3 s window to 3.2 s and quietly
    misreport every `d_samples/samples` fraction.
    """
    ordered = list(dict.fromkeys(pids))
    watch = list(dict.fromkeys(thread_pids))
    buckets = max(1, min(THREAD_ROTATION_DIVISOR, len(ordered)))
    rotation = [ordered[index::buckets] for index in range(buckets)] if ordered else [[]]

    per: dict[int, dict[str, Any]] = {}
    taken = 0
    total = max(1, int(samples))
    started = monotonic()
    for index in range(total):
        for pid in ordered:
            entry = per.get(pid)
            if entry is None:
                entry = per[pid] = {
                    "comm": None,
                    "samples": 0,
                    "d_samples": 0,
                    "thread_d_samples": 0,
                    # *Which* samples, not just how many. Two processes each in D
                    # once is a different finding depending on whether it was the
                    # same 0.5 s: co-blocking points at whatever they share (the
                    # `mmc` bus, the journal) rather than at two independent
                    # writers, and a count alone cannot tell the two apart.
                    "d_at": [],
                    "thread_d_at": [],
                    "wchan": {},
                    "thread_wchan": {},
                    "blkio_ticks_first": None,
                    "blkio_ticks_last": None,
                }
            parsed = parse_proc_stat(_read_text(paths.proc / str(pid) / "stat") or "")
            if not parsed:
                continue  # exited mid-window
            entry["samples"] += 1
            entry["comm"] = parsed.get("comm") or entry["comm"]
            ticks = parsed.get("delayacct_blkio_ticks")
            if ticks is not None:
                if entry["blkio_ticks_first"] is None:
                    entry["blkio_ticks_first"] = ticks
                entry["blkio_ticks_last"] = ticks
            if parsed.get("state") == "D":
                entry["d_samples"] += 1
                entry["d_at"].append(index)
                symbol = read_wchan(pid, paths.proc)
                if symbol:
                    entry["wchan"][symbol] = entry["wchan"].get(symbol, 0) + 1
        for pid in dict.fromkeys(watch + rotation[index % buckets]):
            entry = per.get(pid)
            if entry is None:
                continue
            threads = blocked_threads(pid, paths.proc / str(pid))
            if not threads:
                continue
            entry["thread_d_samples"] += 1
            entry["thread_d_at"].append(index)
            for thread in threads:
                symbol = read_task_wchan(pid, thread["tid"], paths.proc)
                if symbol:
                    key = symbol
                    entry["thread_wchan"][key] = entry["thread_wchan"].get(key, 0) + 1
        taken += 1
        if taken < total:
            sleep(max(0.0, started + taken * interval_s - monotonic()))

    span_s = monotonic() - started
    return {
        "samples": taken,
        "interval_s": interval_s,
        # The span actually covered, so `d_samples/samples` can be checked
        # against a clock instead of assumed from the requested window.
        "span_s": round(span_s, 3),
        # How long a full thread rotation takes: a block shorter than this can
        # fall between two visits to the same process, and that limit is part
        # of the reading rather than a footnote.
        "thread_coverage_s": round(interval_s * buckets, 2),
        "pids": per,
    }


# --- ranking --------------------------------------------------------------


def _delta(
    pre: Mapping[str, int] | None, post: Mapping[str, int] | None, key: str
) -> int:
    if not pre or not post:
        return 0
    before = pre.get(key)
    after = post.get(key)
    if before is None or after is None:
        return 0
    return after - before


def rank_writers(
    pre: Mapping[int, Mapping[str, Any]],
    post: Mapping[int, Mapping[str, Any]],
    *,
    top: int = 8,
) -> tuple[list[dict[str, Any]], int, int]:
    """`(ranked, writers_with_activity, total_write_bytes)` for the window.

    Ranked on `write_bytes` (disk, not `wchar`), tie-broken by `read_bytes`, and
    only pids present at both ends — a process that started or exited inside the
    window has no meaningful delta, and reporting one would manufacture a
    writer out of an unrelated process launch.
    """
    rows: list[dict[str, Any]] = []
    total_write = 0
    for pid, after in post.items():
        before = pre.get(pid)
        if not before:
            continue
        write_bytes = _delta(before.get("io"), after.get("io"), "write_bytes")
        read_bytes = _delta(before.get("io"), after.get("io"), "read_bytes")
        syscw = _delta(before.get("io"), after.get("io"), "syscw")
        syscr = _delta(before.get("io"), after.get("io"), "syscr")
        wchar = _delta(before.get("io"), after.get("io"), "wchar")
        # `wchar` is part of the activity test even though it is not the
        # ranking key: a record that quietly omitted the camera pipeline moving
        # 2.4 MB/s through a pipe would be exactly the blind spot #247 hit, and
        # "0 bytes to disk, 2.4 MB through a pipe" is the useful reading.
        if not any((write_bytes, read_bytes, syscw, syscr, wchar)):
            continue
        total_write += max(0, write_bytes)
        rows.append(
            {
                "pid": pid,
                "comm": after.get("comm") or before.get("comm") or "?",
                "write_bytes": write_bytes,
                "read_bytes": read_bytes,
                "syscw": syscw,
                "syscr": syscr,
                "wchar": wchar,
            }
        )
    rows.sort(key=lambda row: (row["write_bytes"], row["read_bytes"]), reverse=True)
    return rows[:top], len(rows), total_write


def rank_stalled(
    pre: Mapping[int, Mapping[str, Any]],
    post: Mapping[int, Mapping[str, Any]],
    *,
    top: int = 8,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """`(ranked, stalled_processes, blocked_threads, max_run_delay_ns)`.

    D state is uninterruptible sleep, which on this host has meant a filesystem
    syscall that cannot complete — the same thing that kills a daemon's
    heartbeat (#287), and which SIGKILL cannot interrupt. A process counts as
    stalled if its group leader *or* any secondary thread is in D state, which
    are different readings: a blocked health-writing thread leaves a daemon
    unkillable while its main loop keeps beating. Run delay is reported
    alongside so CPU contention cannot be mistaken for IO wait.
    """
    rows: list[dict[str, Any]] = []
    stalled_processes = 0
    blocked_thread_count = 0
    max_delay = 0
    for pid, after in post.items():
        before = pre.get(pid) or {}
        delay = _delta(before, after, "run_delay_ns")
        if delay > max_delay:
            max_delay = delay
        threads = list(
            after.get("blocked_threads") or before.get("blocked_threads") or []
        )
        stalled_now = (
            (after.get("state") == "D") or (before.get("state") == "D") or bool(threads)
        )
        if stalled_now:
            stalled_processes += 1
            blocked_thread_count += len(threads)
        if not stalled_now and delay <= 0:
            continue
        rows.append(
            {
                "pid": pid,
                "comm": after.get("comm") or before.get("comm") or "?",
                "state": after.get("state") or before.get("state"),
                "d_state": bool(stalled_now),
                "blocked_threads": threads,
                "run_delay_ms": round(delay / 1_000_000, 1),
            }
        )
    # D-state tasks first (that is the phenomenon), then the longest run delay.
    rows.sort(key=lambda row: (row["d_state"], row["run_delay_ms"]), reverse=True)
    return rows[:top], stalled_processes, blocked_thread_count, max_delay


def rank_blocked_in_window(
    window: Mapping[str, Any],
    procs_post: Mapping[int, Mapping[str, Any]],
    denied_pids: Collection[int] = (),
    *,
    delayacct_on: bool | None = None,
    top: int = 8,
) -> tuple[list[dict[str, Any]], int]:
    """`(ranked, blocked_pids)` for who was blocked *during* the window.

    This is the channel that survives the privilege boundary, and it is the
    answer to "who wrote" for a writer whose `wchan` and `/proc/<pid>/io` are
    both withheld: the kernel will not say *what* a root-owned process is
    waiting for, but it will say that it is in uninterruptible sleep, for how
    many of the samples, and whether a thread of it is the one stuck.

    Ranked on the worst of the two sample counts rather than their sum: a
    process with a blocked *thread* for the whole window is the #287 shape
    (healthy leader, wedged health write) and must not sort below a process
    that flickered into D once. `wchan_withheld` is set only where the io
    denial was measured for that pid — a `None` symbol is otherwise just
    "blocked somewhere this kernel does not name".

    `blkio_wait_ms` is emitted only when `delayacct_on is True`. The field is
    *present and zero* in `/proc/<pid>/stat` on a kernel that is not accounting
    for it, so emitting the delta unconditionally would print a measured
    "waited 0 ms" for every process on this host — the exact anti-observability
    failure (#306) this record exists to avoid. Measured on the robot
    2026-09-18: present-and-zero in every process's stat, `task_delayacct=0`.
    """
    denied = set(denied_pids)
    samples = int(window.get("samples") or 0)
    rows: list[dict[str, Any]] = []
    for pid, entry in (window.get("pids") or {}).items():
        if not isinstance(pid, int):
            continue
        d_samples = int(entry.get("d_samples") or 0)
        thread_samples = int(entry.get("thread_d_samples") or 0)
        severity = max(d_samples, thread_samples)
        if severity <= 0:
            continue
        symbols = list(entry.get("wchan") or {}) + list(entry.get("thread_wchan") or {})
        pid_entry = procs_post.get(pid) or {}
        row: dict[str, Any] = {
            "pid": pid,
            "comm": entry.get("comm") or pid_entry.get("comm") or "?",
            "d_samples": d_samples,
            "thread_d_samples": thread_samples,
            "samples": samples,
            "d_share": round(severity / samples, 2) if samples else None,
            "d_at": list(entry.get("d_at") or []),
            "thread_d_at": list(entry.get("thread_d_at") or []),
            "wchan": symbols[0] if symbols else None,
            "wchan_withheld": not symbols and pid in denied,
        }
        first = entry.get("blkio_ticks_first")
        last = entry.get("blkio_ticks_last")
        if delayacct_on is True and first is not None and last is not None:
            # Only present when the kernel is accounting for it; absent means
            # "not measured", which is why this key is conditional. A zero here
            # is then a real zero.
            row["blkio_wait_ms"] = round((last - first) * _tick_ms(), 1)
        rows.append(row)
    rows.sort(
        key=lambda row: (
            max(row["d_samples"], row["thread_d_samples"]),
            row["d_samples"] + row["thread_d_samples"],
        ),
        reverse=True,
    )
    return rows[:top], len(rows)


def blocked_by_sample(window: Mapping[str, Any]) -> list[list[dict[str, Any]]]:
    """`[[{pid, comm}, ...], ...]` — who was blocked in each sampled instant.

    The count in `blocked_in_window` says *how much*; this says *together*. The
    distinction decides which investigation is worth running: processes blocked
    in the same sample share a resource (this host has one `mmc` bus carrying
    both the SD card and the SDIO WiFi, and one ext4 journal), while processes
    blocked in different samples are independent writers. Built from the whole
    per-pid map, not from the `top`-truncated rows.

    A pid blocked as a *thread* counts as blocked in that sample; that is the
    #287 shape and it must not be invisible in the overlap view.
    """
    samples = int(window.get("samples") or 0)
    out: list[list[dict[str, Any]]] = [[] for _ in range(max(0, samples))]
    for pid, entry in (window.get("pids") or {}).items():
        if not isinstance(pid, int):
            continue
        row = {"pid": pid, "comm": entry.get("comm") or "?"}
        indices = set(entry.get("d_at") or []) | set(entry.get("thread_d_at") or [])
        for index in sorted(indices):
            if 0 <= index < len(out):
                out[index].append(row)
    return out


def co_blocked_sample_count(window: Mapping[str, Any]) -> int:
    """How many sampled instants had **more than one** process blocked.

    A single sentence a reader can act on: non-zero means the stall is shared,
    which is where "the device was idle and everything waited" lives.
    """
    return sum(1 for sample in blocked_by_sample(window) if len(sample) > 1)


def wchan_withheld_pids(
    procs_post: Mapping[int, Mapping[str, Any]],
    denied_pids: Collection[int],
    window: Mapping[str, Any] | None = None,
) -> set[int]:
    """Pids that were blocked while their `wchan` was withheld from this uid.

    Derived, not guessed: `/proc/<pid>/wchan` prints "0" (successfully) for a
    task this uid may not ptrace, so the only honest test is the io denial
    measured on the same pid. Counted over the whole walk and the whole window
    rather than over the `top`-truncated rows, because a count that silently
    inherits a display limit is worse than no count.
    """
    denied = set(denied_pids)
    out: set[int] = set()
    for pid, entry in procs_post.items():
        if pid in denied and (entry.get("state") == "D" or entry.get("blocked_threads")):
            out.add(pid)
    for pid, entry in ((window or {}).get("pids") or {}).items():
        if pid not in denied:
            continue
        if (entry.get("d_samples") or 0) or (entry.get("thread_d_samples") or 0):
            out.add(pid)
    return out


def wchan_unavailable_reason(withheld: int) -> str | None:
    """One sentence for a record whose blocked writers are partly unnamed."""
    if not withheld:
        return None
    return (
        f"{withheld} process(es) were blocked with their wchan withheld: the kernel "
        "answers /proc/<pid>/wchan with '0' — not an error — for any task this uid "
        "may not ptrace, so a root-owned writer (px-alive, journald, a jbd2 kthread) "
        "reads as 'running' there. State and delayacct fields are unaffected; run "
        "this as root for the symbol."
    )


def delayacct_note(enabled: bool | None) -> str:
    """What the delayacct reading means, in one sentence, for the record."""
    if enabled is True:
        return (
            "per-process block-IO wait is present: each blocked_in_window row "
            "carries blkio_wait_ms (kernel.task_delayacct=1)"
        )
    if enabled is False:
        return (
            "kernel.task_delayacct=0, so blkio_wait_ms is absent — not zero. The "
            "field is populated only for tasks forked after it is enabled, so "
            "turning it on needs the suspect units restarted to be visible."
        )
    return "kernel.task_delayacct is unreadable on this host; delayacct not measured"


def sample_inflight(paths: Paths = DEFAULT_PATHS) -> dict[str, dict[str, int]]:
    """``{device: {"reads": n, "writes": n}}`` from ``/sys/block/*/inflight``.

    The one gauge the delta channels cannot supply. ``ms_writing`` and
    ``ms_io`` say how much time the queue *was* occupied; they cannot say
    whether anything is occupied *now*, and the difference between "the device
    is saturated" and "nothing is in flight yet every task is waiting on IO"
    decides which investigation is worth running. Measured on `picar`
    2026-09-17: a 64 % ``some`` / 58 % ``full`` io-PSI stall with this device
    at 0/0 inflight and ~2 % busy, i.e. the SD card was idle while the whole
    task set was blocked — see docs/operations/io-attribution.md.

    Never raises, and never omits a device for reading zero: 0/0 is the
    finding, not a missing sample.
    """
    out: dict[str, dict[str, int]] = {}
    try:
        candidates = sorted(paths.sys_block.glob("*/inflight"))
    except (OSError, ValueError):
        return out
    for path in candidates[:_MAX_DEVICES * 4]:
        name = path.parent.name
        if name.startswith(_IGNORED_DEVICE_PREFIXES):
            continue
        try:
            fields = path.read_text().split()
            out[name] = {"reads": int(fields[0]), "writes": int(fields[1])}
        except (OSError, ValueError, IndexError):
            continue
        if len(out) >= _MAX_DEVICES:
            break
    return out


#: ext4 counters worth reading. `session_write_kbytes` is the discriminator:
#: it counts bytes written *to this filesystem*, so a stall where the device
#: moves 500 KB and this moves 4 KB is metadata/journal work with no file to
#: attribute it to — which is why every file channel comes back empty and why
#: "nobody wrote" is the wrong reading. `lifetime_write_kbytes` is the card-wear
#: figure (1.03 TB on `picar` as of 2026-09-17).
EXT4_FIELDS = (
    "session_write_kbytes",
    "lifetime_write_kbytes",
    "delayed_allocation_blocks",
    "errors_count",
)

#: Counters to difference across the window; the rest are point readings.
EXT4_COUNTERS = ("session_write_kbytes",)


def sample_ext4(paths: Paths = DEFAULT_PATHS) -> dict[str, Any]:
    """`{fs, session_write_kbytes, ...}` for the first ext4 filesystem found.

    World-readable, and the complement of every other channel here: `/proc/*/io`
    and the file watchlist both answer "which file grew", and neither can see
    journal/metadata writes, which have no file. This answers "did the
    filesystem write anything at all", so a record can say *the device moved
    500 KB and the filesystem wrote nothing* instead of leaving an empty writer
    list to be misread.

    Never raises; `{}` means "not watched", never "nothing was written".
    """
    out: dict[str, Any] = {}
    try:
        candidates = sorted(paths.ext4_sysfs.glob("*"))
    except (OSError, ValueError):
        return out
    for entry in candidates:
        try:
            if not (entry / "session_write_kbytes").is_file():
                continue
        except OSError:
            continue
        out["fs"] = entry.name
        for field in EXT4_FIELDS:
            try:
                out[field] = int((entry / field).read_text().strip())
            except (OSError, ValueError):
                continue
        try:
            out["journal_task"] = (entry / "journal_task").read_text().strip()
        except OSError:
            pass
        break
    return out


def unattributed_write_share(
    devices: Mapping[str, Mapping[str, int]], write_bytes_total: int
) -> tuple[int, int, float | None]:
    """`(device_bytes, unattributed_bytes, share)` for the busiest real device.

    The question this answers is "how much of what reached the disk has a
    process's name on it". Measured on `picar` 2026-09-17, 60 s window:
    `mmcblk0` wrote **2596 KB** while every readable process together wrote
    **92 KB** — 3.5 % — and the two journal files' *mtime* moved with a `size`
    delta of 0. The rest is journal and metadata, which is charged to kernel
    threads (`jbd2`, `kblockd`, `flush-179:0`), so no `/proc/<pid>/io` channel
    can ever name it, privileged or not.

    The caveat that keeps this honest: `write_bytes` charges page-cache writes
    to the task that *dirtied* the page, so a process that writes a lot and
    never fsyncs is still counted; and metadata written on its behalf is not.
    A high share therefore means "mostly journal/metadata", never "nobody
    wrote anything".

    `None` share when the busiest device wrote nothing: 0/0 is not 0 %.
    """
    device = None
    for name, counters in sorted(devices.items()):
        sectors = counters.get("sectors_written", 0)
        if sectors <= 0:
            continue
        if device is None or sectors > devices[device].get("sectors_written", 0):
            device = name
    if device is None:
        return 0, 0, None
    device_bytes = int(devices[device].get("sectors_written", 0)) * 512
    unattributed = max(0, device_bytes - max(0, write_bytes_total))
    share = round(unattributed / device_bytes, 3) if device_bytes else None
    return device_bytes, unattributed, share


def _device_deltas(
    pre: Mapping[str, Mapping[str, int]],
    post: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for name, after in post.items():
        if name.startswith(_IGNORED_DEVICE_PREFIXES):
            continue
        before = pre.get(name)
        if not before:
            continue
        deltas = {
            key: _delta(before, after, key)
            for key in (
                "writes_completed",
                "sectors_written",
                "ms_writing",
                "ms_io",
                "reads_completed",
                "sectors_read",
                "ms_reading",
            )
        }
        # Derived, because "the card is slow" needs a number and the counters for
        # it are already here. `ms_io` is queue-occupied *time*, so dividing by
        # the writes it served gives average queue time per write — ~10-20 ms on
        # a healthy card, and measured 2026-09-18 at **~18 s across 89 writes
        # moving 446 KB** during the post-burst phase (#247), where the queue was
        # only 16 % occupied and every daemon was still blocked. Reported as
        # `None` rather than 0 when nothing was written: 0/0 is not "fast".
        writes = deltas.get("writes_completed", 0)
        if writes > 0:
            # `ms_io` is queue-occupied time for *all* I/O, so the read time is
            # subtracted out first: measured on `picar` 2026-09-18, this card
            # reads at ~1.5 ms per read and writes at ~86 ms per write (median
            # over 150 records), and a metric that mixed the two would move with
            # the read mix instead of with the write path.
            busy_ms = max(0, deltas.get("ms_io", 0) - deltas.get("ms_reading", 0))
            deltas["ms_per_write"] = round(busy_ms / writes, 1)
            deltas["kb_per_write"] = round(
                deltas.get("sectors_written", 0) * 512 / 1024 / writes, 1
            )
        if deltas["writes_completed"] or deltas["reads_completed"] or deltas["ms_io"]:
            out[name] = deltas
    return dict(list(out.items())[:_MAX_DEVICES])


def _attach_units(
    rows: list[dict[str, Any]],
    proc_root: Path,
    *,
    want_wchan: bool = False,
    denied_pids: Collection[int] = (),
) -> None:
    denied = set(denied_pids)
    for row in rows:
        pid = row.get("pid")
        if not isinstance(pid, int):
            continue
        row["unit"] = read_unit(pid, proc_root)
        if want_wchan:
            if row.get("d_state"):
                row["wchan"] = read_wchan(pid, proc_root)
                if row["wchan"] is None and pid in denied:
                    # "0" is what a withheld wchan looks like. Saying so keeps
                    # `wchan: null` from reading as "not blocked in anything".
                    row["wchan_withheld"] = True
            for thread in row.get("blocked_threads") or []:
                tid = thread.get("tid")
                if isinstance(tid, int):
                    thread["wchan"] = read_task_wchan(pid, tid, proc_root)
                    if thread["wchan"] is None and pid in denied:
                        thread["wchan_withheld"] = True


# --- the record -----------------------------------------------------------


def _unavailable_reason(
    allow_proc_io: bool, privileged: bool, io_denied: int
) -> str | None:
    """One sentence naming the gap in the writer list, or None if there is none.

    The writer channel degrades in three distinct ways and they must not read
    alike: not attempted at all, attempted without the privilege to see other
    users' processes, and attempted *with* it while some process refused anyway
    (a non-dumpable process). Only the last is a partial list under conditions
    where completeness was expected.
    """
    if not allow_proc_io:
        return "writer channel not attempted: --no-proc-io"
    if io_denied and privileged:
        return (
            f"{io_denied} process(es) refused /proc/<pid>/io despite this uid "
            "being able to read other users' — non-dumpable, invisible here"
        )
    if io_denied:
        return (
            f"{io_denied} process(es) refused /proc/<pid>/io — root-owned writers "
            "(px-alive, journald) are among them; run this as root to see them"
        )
    if not privileged:
        return (
            "privilege probe says other users' /proc/<pid>/io is not readable, "
            "but no process refused one in this snapshot"
        )
    return None


def capture(
    trigger: Mapping[str, Any] | None = None,
    *,
    paths: Paths = DEFAULT_PATHS,
    window_s: float = DEFAULT_WINDOW_S,
    interval_s: float = DEFAULT_SAMPLE_INTERVAL_S,
    allow_proc_io: bool = True,
    privileged: bool | None = None,
    growth_patterns: Sequence[str] = (),
    top: int = 8,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Take one bounded snapshot across a stall and return the record.

    Order matters: the process walk at t0 *is* the stall (the caller triggered
    because PSI is high or the heartbeat is late right now), and the walk at t1
    only exists to difference against it. Between them the window is **sampled**,
    not slept through — see `sample_window` for exactly what is read and why
    state rather than `wchan` is the reading that survives the privilege
    boundary. The observer-ethics objection that made the first version of this
    a two-ended walk still stands, so the sampler answers it three ways rather
    than by polling harder: it runs *only* inside an already-triggered window,
    it reads one small /proc file per pid per sample instead of the whole walk,
    and it reports `window_samples` plus its own `observer_read_bytes` so the
    instrument is in its own record.

    One record per trigger and a 60 s cooldown are unchanged: the failure mode
    this guards against is an observer that becomes the writer it is hunting.
    """
    started = monotonic()
    window_samples = max(1, int(round(window_s / interval_s))) if interval_s > 0 else 1

    psi_pre = host_load_fields("pre")
    sys_ctx_pre = parse_procs_stat(_read_text(paths.proc_stat) or "")
    disk_pre = parse_diskstats(_read_text(paths.diskstats) or "")
    vmstat_pre = parse_vmstat(_read_text(paths.vmstat) or "")
    uptime_text = _read_text(paths.uptime)
    uptime_s: float | None = None
    if uptime_text:
        try:
            uptime_s = round(float(uptime_text.split()[0]), 1)
        except (ValueError, IndexError):
            uptime_s = None
    inflight_pre = sample_inflight(paths)
    ext4_pre = sample_ext4(paths)
    delayacct = delayacct_enabled(paths)
    procs_pre = sample_processes(paths.proc, allow_proc_io=allow_proc_io)
    files_pre = sample_file_meta(growth_patterns) if growth_patterns else {}
    observer_pre = parse_proc_io(_read_text(paths.proc / "self" / "io") or "")

    window = sample_window(
        paths,
        list(procs_pre),
        samples=window_samples,
        interval_s=interval_s,
        # Carried from t0 so a thread already known to be stuck keeps being
        # watched for the whole window, whatever the rotation lands on.
        thread_pids=[
            pid for pid, entry in procs_pre.items() if entry.get("blocked_threads")
        ],
        sleep=sleep,
        monotonic=monotonic,
    )

    procs_post = sample_processes(paths.proc, allow_proc_io=allow_proc_io)
    files_post = sample_file_meta(growth_patterns) if growth_patterns else {}
    disk_post = parse_diskstats(_read_text(paths.diskstats) or "")
    vmstat_post = parse_vmstat(_read_text(paths.vmstat) or "")
    inflight_post = sample_inflight(paths)
    ext4_post = sample_ext4(paths)
    observer_post = parse_proc_io(_read_text(paths.proc / "self" / "io") or "")
    psi_post = host_load_fields("post")
    sys_ctx_post = parse_procs_stat(_read_text(paths.proc_stat) or "")

    elapsed = round(monotonic() - started, 3)

    # The kernel's account of the window, taken *after* it so the read is not
    # competing with the stall it is describing, and bounded so it cannot become
    # the writer it is hunting. `child_io` differences around it because the
    # observer's own /proc/self/io cannot see a child's reads.
    child_before = child_io()
    kernel_log = kernel_log_window(elapsed + KERNEL_LOG_PAD_S)
    child_mid = child_io()
    unit_log = unit_log_window(
        max(elapsed + KERNEL_LOG_PAD_S, UNIT_LOG_LOOKBACK_S)
    )
    child_after = child_io()

    io_denied = sum(1 for entry in procs_post.values() if entry.get("io_denied"))
    # The measured io refusal is also the only honest test for "this kernel is
    # hiding a wchan from us", so it is carried into every wchan reading below.
    denied_pids = {
        pid for pid, entry in procs_post.items() if entry.get("io_denied")
    }
    writers, writer_count, write_bytes_total = rank_writers(
        procs_pre, procs_post, top=top
    )
    _attach_units(writers, paths.proc)
    stalled, d_state_count, blocked_thread_count, max_run_delay = rank_stalled(
        procs_pre, procs_post, top=top
    )
    _attach_units(stalled, paths.proc, want_wchan=True, denied_pids=denied_pids)
    blocked_window, blocked_window_count = rank_blocked_in_window(
        window, procs_post, denied_pids, delayacct_on=delayacct, top=top
    )
    _attach_units(blocked_window, paths.proc)
    withheld = wchan_withheld_pids(procs_post, denied_pids, window)

    devices = _device_deltas(disk_pre, disk_post)
    (
        device_write_bytes,
        unattributed_bytes,
        unattributed_share,
    ) = unattributed_write_share(devices, write_bytes_total)
    growth, growth_total, growth_groups = rank_file_growth(
        {path: entry["size"] for path, entry in files_pre.items()},
        {path: entry["size"] for path, entry in files_post.items()},
    )
    touched, touched_count = rank_file_touches(files_pre, files_post)
    recent = recent_file_writes(files_pre, now_s=time.time())

    record: dict[str, Any] = {
        "reason": (trigger or {}).get("reason", "manual"),
        "window_s": elapsed,
        "requested_window_s": window_s,
        # The single most important field for reading this record honestly: with
        # allow_proc_io off, `writers` is not "nothing wrote", it is "nothing
        # readable wrote", and an absent explanation there is the failure mode
        # #306 is about.
        "writer_channel_attempted": bool(allow_proc_io),
        # The probe (can this uid read *other users'* io at all) and the
        # measurement (how many processes refused) are separate facts, and the
        # interesting states are exactly where they disagree — so both are
        # recorded rather than collapsed into one "privileged" boolean.
        "privileged": bool(allow_proc_io) if privileged is None else privileged,
        "writers_unreadable_count": io_denied,
        "writers_unavailable_reason": _unavailable_reason(
            allow_proc_io,
            bool(allow_proc_io) if privileged is None else privileged,
            io_denied,
        ),
        "processes_seen": len(procs_post),
        "writers_with_activity": writer_count,
        "write_bytes_total": write_bytes_total,
        "d_state_count": d_state_count,
        "blocked_thread_count": blocked_thread_count,
        "max_run_delay_ms": round(max_run_delay / 1_000_000, 1),
        "uptime_s": uptime_s,
        "trigger": dict(trigger or {}),
        "psi_pre": psi_pre,
        "psi_post": psi_post,
        # The context for reading those two: `full` tracking `some` means "every
        # runnable task was stalled", which on an idle box is one or two tasks
        # and not a system-wide freeze. Measured on `picar` 2026-09-18: during
        # 25-45 % episodes `procs_running` was 1-3.
        "procs_running_pre": sys_ctx_pre.get("running"),
        "procs_running_post": sys_ctx_post.get("running"),
        "procs_blocked_pre": sys_ctx_pre.get("blocked"),
        "procs_blocked_post": sys_ctx_post.get("blocked"),
        "devices": devices,
        # How much of what reached the disk has a process's name on it. A record
        # whose device wrote megabytes with `write_bytes_total` in the kilobytes
        # is a record of journal/metadata traffic — see the docstring, and do
        # not read a high share as "nobody wrote".
        "device_write_bytes": device_write_bytes,
        "unattributed_write_bytes": unattributed_bytes,
        "unattributed_write_share": unattributed_share,
        # Pre is the one that matters: the caller triggered *because* PSI was
        # high, so the walk at t0 is inside the stall. Post shows recovery.
        # A record whose device shows 0/0 here while `devices` shows a queue
        # that was busy, and `stalled` shows the whole task set blocked, is a
        # record of a wedged queue — not of a busy one — and no writer exists
        # in it to find.
        "device_inflight_pre": inflight_pre,
        "device_inflight_post": inflight_post,
        # The filesystem's own view, and the only field here that separates
        # "file data" from "metadata/journal": `write_kbytes_delta` is bytes
        # written *to the filesystem* across the window. A record where the
        # device moved 500 KB and this moved 4 KB is a record of journal work —
        # which has no file, so no file channel can ever name it.
        "ext4": _ext4_window(ext4_pre, ext4_post),
        "vmstat": {
            key: _delta(vmstat_pre, vmstat_post, key) for key in VMSTAT_COUNTERS
        },
        "vmstat_end": {key: vmstat_post.get(key) for key in VMSTAT_GAUGES},
        "writers": writers,
        # The file-level writer channel: names files (and through them writers)
        # when /proc/<pid>/io is refused. `file_growth_watched` is the honesty
        # field — growth outside the watchlist is invisible, and saying so is
        # the difference between a measurement and a claim.
        "file_growth": growth,
        "file_growth_total_bytes": growth_total,
        "file_growth_groups": growth_groups,
        "file_growth_watched": len(files_post),
        # The same watchlist read through mtime instead of size. This is the
        # only channel here that can see a writer whose writes change no file
        # size — journald's preallocated mmap'd journal, and ext4 metadata —
        # which is exactly the shape the 2026-09-17 records show: 128-524 KB
        # written to mmcblk0 with `file_growth_groups` empty and 115 paths
        # watched. `bytes` is 0 for those rows, and they sort first.
        "file_touched": touched,
        "file_touched_count": touched_count,
        # What was written *just before* this stall, which is where the writer
        # of a delayed writeback lives: a timer dirties tens of megabytes, exits,
        # and the card is still absorbing it minutes later, so every
        # window-scoped channel comes back empty. Ranked by size — see
        # `recent_file_writes`. Measured 2026-09-18: the 60 MB
        # `/var/cache/apt/pkgcache.bin`, mtime 138 s before the record, named the
        # writer in one field (#402).
        "file_recent": recent,
        "stalled": stalled,
        # The window's own reading — who was in uninterruptible sleep *during*
        # it, sampled rather than inferred from its two ends. This is the
        # channel that still names a root-owned writer: state is readable for
        # every process, while wchan and /proc/<pid>/io are not, and a burst
        # inside the window is exactly what two endpoint walks miss. Read
        # `d_samples`/`samples` as a fraction of the window, `thread_d_samples`
        # as the #287 shape (wedged health thread, healthy leader), and prefer
        # this list over `writers` when `writers_unreadable_count` is large.
        "blocked_in_window": blocked_window,
        "blocked_in_window_count": blocked_window_count,
        # Who was blocked *together*, sample by sample. The count above cannot
        # distinguish "two processes, one after the other" from "two processes
        # at the same instant" — and the second is the evidence for a shared
        # resource (one `mmc` bus, one journal) rather than two writers.
        "blocked_at_samples": blocked_by_sample(window),
        "co_blocked_samples": co_blocked_sample_count(window),
        "window_samples": window.get("samples"),
        "window_sample_interval_s": window.get("interval_s"),
        "window_span_s": window.get("span_s"),
        # How long a full per-thread rotation takes. A thread block shorter
        # than this can fall between two visits to the same process — the
        # sampler's stated resolution, not a hidden limitation.
        "window_thread_coverage_s": window.get("thread_coverage_s"),
        # A blocked process whose wchan is missing is *unnamed*, not
        # unblocked: the kernel answers /proc/<pid>/wchan with "0" for tasks
        # this uid may not ptrace. Counted over the whole walk and window, not
        # over the truncated rows.
        "wchan_withheld_count": len(withheld),
        "wchan_unavailable_reason": wchan_unavailable_reason(len(withheld)),
        # Per-process block-IO wait, the one *wait* reading readable for a
        # root-owned process — off by default on this kernel, hence the note
        # in every record rather than a silent absence.
        "delayacct": {
            "enabled": delayacct,
            "note": delayacct_note(delayacct),
        },
        # The instrument accounting for itself: if this record ever shows the
        # observer as the top writer, the observer is the defect.
        # The kernel's own account of the window: mmc/SDIO/ext4/journal errors,
        # undervoltage, thermal throttling. This is the only channel here that
        # can explain a *wedge* — a stall with the device idle and no writer to
        # name — and it is readable unprivileged on `picar`. Read `available`
        # before `lines`: unavailable is not quiet.
        "kernel_log": kernel_log,
        # What systemd itself did in the window. The only channel that can name
        # a timer-driven writer: `apt-daily` dirtied ~87 MB and exited before any
        # of it reached the card, leaving 48 MB of device writes with 37 KB
        # attributable to a process (#402). Read its `Starting`/`Deactivated`
        # timestamps against `window_s`.
        "unit_log": unit_log,
        # The instrument accounting for the one thing outside /proc/self/io.
        "kernel_log_child_read_bytes": max(
            0, child_mid["read_bytes"] - child_before["read_bytes"]
        ),
        "kernel_log_child_write_bytes": max(
            0, child_mid["write_bytes"] - child_before["write_bytes"]
        ),
        "unit_log_child_read_bytes": max(
            0, child_after["read_bytes"] - child_mid["read_bytes"]
        ),
        "unit_log_child_write_bytes": max(
            0, child_after["write_bytes"] - child_mid["write_bytes"]
        ),
        "observer_write_bytes": _delta(observer_pre, observer_post, "write_bytes"),
        "observer_read_bytes": _delta(observer_pre, observer_post, "read_bytes"),
    }
    return record


def trigger_reason(
    *,
    io_some_avg10: float | None,
    heartbeat_age_s: float | None,
    alive: bool,
    io_threshold: float,
    heartbeat_threshold_s: float,
) -> str | None:
    """Why to take a snapshot now, or None to stay quiet.

    Two cheap signals, both of which the system already publishes:

    * io PSI crossing `io_threshold` — the stall itself.
    * px-alive's heartbeat age crossing `heartbeat_threshold_s` while the
      daemon is still alive. The aliveness gate is what keeps a *stopped*
      daemon's stale file from triggering forever; a stall long enough to stop
      the beats is exactly the case worth capturing (#287's 82 s park), and a
      dead daemon is a different failure that systemd already reports.
    """
    reasons: list[str] = []
    if io_some_avg10 is not None and io_some_avg10 >= io_threshold:
        reasons.append("io_psi")
    if (
        alive
        and heartbeat_age_s is not None
        and heartbeat_age_s >= heartbeat_threshold_s
    ):
        reasons.append("heartbeat_age")
    return "+".join(reasons) if reasons else None
