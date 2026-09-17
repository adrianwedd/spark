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
* **stall channel** — ``/proc/<pid>/stat`` (state), ``schedstat`` (run delay),
  ``wchan``, plus ``/proc/diskstats`` write-queue time, ``/proc/vmstat`` and
  PSI. World-readable even for root-owned processes, and it is what separates
  "a readable process was writing" from "the device was busy and no readable
  process was writing" — the latter being the signature of ext4 journal work
  (``jbd2/*`` kthreads) or of a writer this process is not allowed to see.

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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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


# --- readers (all total, none raising) -----------------------------------


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, ValueError):
        return None


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
    """Whether `pid` exists. Used to gate the heartbeat trigger, not to trust it."""
    try:
        return (proc_root / str(pid)).is_dir()
    except OSError:
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
    """
    return _wchan_symbol(proc_root / str(pid) / "wchan")


def read_task_wchan(pid: int, tid: int, proc_root: Path) -> str | None:
    """`read_wchan` for one secondary thread of a process."""
    return _wchan_symbol(proc_root / str(pid) / "task" / str(tid) / "wchan")


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
        if deltas["writes_completed"] or deltas["reads_completed"] or deltas["ms_io"]:
            out[name] = deltas
    return dict(list(out.items())[:_MAX_DEVICES])


def _attach_units(
    rows: list[dict[str, Any]], proc_root: Path, *, want_wchan: bool = False
) -> None:
    for row in rows:
        pid = row.get("pid")
        if not isinstance(pid, int):
            continue
        row["unit"] = read_unit(pid, proc_root)
        if want_wchan:
            if row.get("d_state"):
                row["wchan"] = read_wchan(pid, proc_root)
            for thread in row.get("blocked_threads") or []:
                tid = thread.get("tid")
                if isinstance(tid, int):
                    thread["wchan"] = read_task_wchan(pid, tid, proc_root)


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
    only exists to difference against it. Two walks of /proc, ~3 s apart, then
    stop — this is not a sampler, and a version of it that polled like one would
    be a candidate for the very list it produces.
    """
    started = monotonic()

    psi_pre = host_load_fields("pre")
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
    procs_pre = sample_processes(paths.proc, allow_proc_io=allow_proc_io)
    files_pre = sample_file_meta(growth_patterns) if growth_patterns else {}
    observer_pre = parse_proc_io(_read_text(paths.proc / "self" / "io") or "")

    sleep(max(0.0, window_s))

    procs_post = sample_processes(paths.proc, allow_proc_io=allow_proc_io)
    files_post = sample_file_meta(growth_patterns) if growth_patterns else {}
    disk_post = parse_diskstats(_read_text(paths.diskstats) or "")
    vmstat_post = parse_vmstat(_read_text(paths.vmstat) or "")
    inflight_post = sample_inflight(paths)
    ext4_post = sample_ext4(paths)
    observer_post = parse_proc_io(_read_text(paths.proc / "self" / "io") or "")
    psi_post = host_load_fields("post")

    elapsed = round(monotonic() - started, 3)

    io_denied = sum(1 for entry in procs_post.values() if entry.get("io_denied"))
    writers, writer_count, write_bytes_total = rank_writers(
        procs_pre, procs_post, top=top
    )
    _attach_units(writers, paths.proc)
    stalled, d_state_count, blocked_thread_count, max_run_delay = rank_stalled(
        procs_pre, procs_post, top=top
    )
    _attach_units(stalled, paths.proc, want_wchan=True)

    growth, growth_total, growth_groups = rank_file_growth(
        {path: entry["size"] for path, entry in files_pre.items()},
        {path: entry["size"] for path, entry in files_post.items()},
    )
    touched, touched_count = rank_file_touches(files_pre, files_post)

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
        "devices": _device_deltas(disk_pre, disk_post),
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
        "stalled": stalled,
        # The instrument accounting for itself: if this record ever shows the
        # observer as the top writer, the observer is the defect.
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
