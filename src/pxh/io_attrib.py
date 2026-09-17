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

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

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


def read_pid_file(path: Path) -> int | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


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
            io_text = _read_text(pid_dir / "io")
            if io_text is not None:
                entry["io"] = parse_proc_io(io_text)
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


def capture(
    trigger: Mapping[str, Any] | None = None,
    *,
    paths: Paths = DEFAULT_PATHS,
    window_s: float = DEFAULT_WINDOW_S,
    allow_proc_io: bool = True,
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
    procs_pre = sample_processes(paths.proc, allow_proc_io=allow_proc_io)
    observer_pre = parse_proc_io(_read_text(paths.proc / "self" / "io") or "")

    sleep(max(0.0, window_s))

    procs_post = sample_processes(paths.proc, allow_proc_io=allow_proc_io)
    disk_post = parse_diskstats(_read_text(paths.diskstats) or "")
    vmstat_post = parse_vmstat(_read_text(paths.vmstat) or "")
    observer_post = parse_proc_io(_read_text(paths.proc / "self" / "io") or "")
    psi_post = host_load_fields("post")

    elapsed = round(monotonic() - started, 3)

    writers, writer_count, write_bytes_total = rank_writers(
        procs_pre, procs_post, top=top
    )
    _attach_units(writers, paths.proc)
    stalled, d_state_count, blocked_thread_count, max_run_delay = rank_stalled(
        procs_pre, procs_post, top=top
    )
    _attach_units(stalled, paths.proc, want_wchan=True)

    record: dict[str, Any] = {
        "reason": (trigger or {}).get("reason", "manual"),
        "window_s": elapsed,
        "requested_window_s": window_s,
        # The single most important field for reading this record honestly: with
        # allow_proc_io off, `writers` is not "nothing wrote", it is "nothing
        # readable wrote", and an absent explanation there is the failure mode
        # #306 is about.
        "privileged": bool(allow_proc_io),
        "writers_unavailable_reason": None
        if allow_proc_io
        else "needs root: /proc/<pid>/io is EACCES for other users' processes",
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
        "vmstat": {
            key: _delta(vmstat_pre, vmstat_post, key) for key in VMSTAT_COUNTERS
        },
        "vmstat_end": {key: vmstat_post.get(key) for key in VMSTAT_GAUGES},
        "writers": writers,
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
