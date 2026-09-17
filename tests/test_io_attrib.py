"""#247's narrowed ask, tested: sample at the stall, rank writers, and say what
could not be read.

The fixtures are synthetic /proc trees, never the host's. The reader has to work
on a machine with no /proc at all (this repo is developed off-robot), and a test
that read the real /proc/*/io would depend on root, on which processes happen to
be running, and on the stall it is meant to observe.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pxh.io_attrib as io_attrib

PRESSURE = (
    "some avg10=96.40 avg60=1.00 avg300=0.50 total=1\n"
    "full avg10=90.00 avg60=0.90 avg300=0.40 total=1\n"
)
IDLE_DISK = "179 0 mmcblk0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
ACTIVE_DISK = "179 0 mmcblk0 10 0 80 5 61 0 1024 10824 0 12000 0 0 0 0 0 0 0\n"
IDLE_VMSTAT = "pswpin 10\npswpout 20\nnr_dirty 4\nnr_writeback 0\n"
ACTIVE_VMSTAT = "pswpin 12\npswpout 20\nnr_dirty 12\nnr_writeback 3\n"


# --- fixture builders -----------------------------------------------------


def _write_proc_tree(
    tmp_path,
    procs: dict[int, dict],
    *,
    self_io: dict[str, int] | None = None,
    diskstats: str = "",
    vmstat: str = "",
    uptime: str = "12345.67 9000.00\n",
    inflight: dict[str, tuple[int, int]] | None = None,
    ext4_fs: str = "mmcblk0p2",
    ext4_fields: dict[str, object] | None = None,
):
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, spec in procs.items():
        pid_dir = proc / str(pid)
        pid_dir.mkdir()
        (pid_dir / "stat").write_text(
            f"{pid} ({spec.get('comm', 'python3')}) {spec.get('state', 'S')} 1 1 1\n"
        )
        (pid_dir / "io").write_text(
            "\n".join(f"{key}: {value}" for key, value in spec.get("io", {}).items())
            + "\n"
        )
        (pid_dir / "schedstat").write_text(
            f"{spec.get('cpu_ns', 1_000_000)} {spec.get('run_delay_ns', 0)} 3\n"
        )
        (pid_dir / "cgroup").write_text(
            spec.get("cgroup", "0::/system.slice/px-wake-listen.service\n")
        )
        (pid_dir / "wchan").write_text(spec.get("wchan", "0"))
        for tid, thread in (spec.get("threads") or {}).items():
            task_dir = pid_dir / "task" / str(tid)
            task_dir.mkdir(parents=True)
            (task_dir / "stat").write_text(
                f"{tid} ({thread.get('comm', 'python3')}) {thread.get('state', 'S')} 1 1\n"
            )
            (task_dir / "wchan").write_text(thread.get("wchan", "0"))
    self_dir = proc / "self"
    self_dir.mkdir()
    (self_dir / "io").write_text(
        "\n".join(f"{key}: {value}" for key, value in (self_io or {}).items()) + "\n"
    )
    (tmp_path / "diskstats").write_text(diskstats)
    (tmp_path / "vmstat").write_text(vmstat)
    (tmp_path / "uptime").write_text(uptime)
    (tmp_path / "pressure_io").write_text(PRESSURE)
    sys_block = tmp_path / "sys_block"
    sys_block.mkdir()
    ext4 = tmp_path / "sys_fs_ext4" / (ext4_fs or "mmcblk0p2")
    ext4.mkdir(parents=True)
    for field, value in (ext4_fields or {}).items():
        (ext4 / field).write_text(f"{value}\n")
    for name, (reads, writes) in (inflight or {}).items():
        (sys_block / name).mkdir()
        (sys_block / name / "inflight").write_text(f"{reads} {writes}\n")
    return io_attrib.Paths(
        proc=proc,
        pressure_io=tmp_path / "pressure_io",
        diskstats=tmp_path / "diskstats",
        vmstat=tmp_path / "vmstat",
        uptime=tmp_path / "uptime",
        sys_block=sys_block,
        ext4_sysfs=tmp_path / "sys_fs_ext4",
    )


def _diskstats_line(
    name: str, *, writes: int, sectors: int, ms_writing: int, ms_io: int
) -> str:
    return (
        f"179 0 {name} 10 0 80 5 {writes} 0 {sectors} {ms_writing} 0 {ms_io} 0 "
        "0 0 0 0 0 0\n"
    )


def _monotonic(first: float = 0.0, second: float = 3.0):
    values = iter([first, second])
    return lambda: next(values, second)


def _stub_host_load(monkeypatch, **overrides):
    payload = {"psi_io_some_avg10": 96.4, "psi_io_full_avg10": 90.0, "load1": 0.8}
    payload.update(overrides)
    monkeypatch.setattr(
        io_attrib,
        "host_load_fields",
        lambda prefix: {f"{key}_{prefix}": value for key, value in payload.items()},
    )


# --- parsing --------------------------------------------------------------


def test_parse_proc_io_reads_the_real_format():
    text = (
        "rchar: 261988\n"
        "wchar: 195\n"
        "syscr: 1132\n"
        "syscw: 13\n"
        "read_bytes: 40960\n"
        "write_bytes: 16384\n"
        "cancelled_write_bytes: 0\n"
    )
    assert io_attrib.parse_proc_io(text) == {
        "rchar": 261988,
        "wchar": 195,
        "syscr": 1132,
        "syscw": 13,
        "read_bytes": 40960,
        "write_bytes": 16384,
        "cancelled_write_bytes": 0,
    }


def test_parse_proc_io_ignores_unknown_keys_and_junk_lines():
    assert io_attrib.parse_proc_io("rchar: not-a-number\nfoo: 1\nsyscw: 4\n") == {
        "syscw": 4
    }


def test_parse_proc_stat_handles_comm_with_spaces_and_parens():
    # (sd-pam) and a comm containing both spaces and parentheses: a whitespace
    # split reads the wrong field, which is why the parser uses the outermost
    # parens instead.
    parsed = io_attrib.parse_proc_stat("4242 (python3 /opt/x (1)) D 1 4242\n")
    assert parsed == {"comm": "python3 /opt/x (1)", "state": "D"}


def test_parse_proc_stat_returns_empty_on_malformed_input():
    assert io_attrib.parse_proc_stat("not a stat line") == {}


def test_parse_schedstat_reads_run_delay():
    assert io_attrib.parse_schedstat("9109616218 445202281 8131\n") == {
        "cpu_ns": 9109616218,
        "run_delay_ns": 445202281,
        "timeslices": 8131,
    }


def test_parse_diskstats_maps_mmcblk0_fields():
    parsed = io_attrib.parse_diskstats(
        _diskstats_line(
            "mmcblk0", writes=61, sectors=1024, ms_writing=10824, ms_io=12000
        )
    )["mmcblk0"]
    assert parsed["writes_completed"] == 61
    assert parsed["sectors_written"] == 1024
    assert parsed["ms_writing"] == 10824
    assert parsed["ms_io"] == 12000


def test_parse_diskstats_tolerates_pre_5_5_field_widths():
    # Kernels before 5.5 stop at weighted_ms_io; nothing after it may be
    # invented from a shorter line.
    parsed = io_attrib.parse_diskstats(
        "179 0 mmcblk0 10 0 80 5 61 0 1024 10824 0 12000 0\n"
    )
    assert parsed["mmcblk0"]["ms_io"] == 12000
    assert "flush_completed" not in parsed["mmcblk0"]


def test_parse_vmstat_keeps_swap_and_reclaim_only():
    parsed = io_attrib.parse_vmstat(
        "pswpin 2474\npswpout 41856\nnr_dirty 12\nnr_writeback 3\nsomething_else 99\n"
    )
    assert parsed == {
        "pswpin": 2474,
        "pswpout": 41856,
        "nr_dirty": 12,
        "nr_writeback": 3,
    }


# --- heartbeat + liveness -------------------------------------------------


def test_read_heartbeat_age_returns_age_and_mode(tmp_path):
    beat = tmp_path / "alive_heartbeat.json"
    beat.write_text('{"ts": 1000.0, "mode": "lease_wait"}')
    age, mode = io_attrib.read_heartbeat_age(beat, now=1007.5)
    assert (round(age, 1), mode) == (7.5, "lease_wait")


def test_read_heartbeat_age_is_none_when_missing_or_malformed(tmp_path):
    assert io_attrib.read_heartbeat_age(tmp_path / "nope.json") == (None, None)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert io_attrib.read_heartbeat_age(bad) == (None, None)
    no_ts = tmp_path / "no_ts.json"
    no_ts.write_text('{"mode": "running"}')
    assert io_attrib.read_heartbeat_age(no_ts) == (None, None)


def test_process_alive_and_read_pid_file(tmp_path):
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)
    assert io_attrib.process_alive(4242, proc)
    assert not io_attrib.process_alive(4243, proc)
    pid_file = tmp_path / "px-alive.pid"
    pid_file.write_text("4242\n")
    assert io_attrib.read_pid_file(pid_file) == 4242
    pid_file.write_text("garbage\n")
    assert io_attrib.read_pid_file(pid_file) is None
    assert io_attrib.read_pid_file(tmp_path / "missing.pid") is None


def test_read_unit_extracts_the_service_name(tmp_path):
    proc = tmp_path / "proc"
    (proc / "7").mkdir(parents=True)
    (proc / "7" / "cgroup").write_text("0::/system.slice/px-alive.service\n")
    assert io_attrib.read_unit(7, proc) == "px-alive.service"
    (proc / "8").mkdir()
    (proc / "8" / "cgroup").write_text("0::/\n")
    assert io_attrib.read_unit(8, proc) is None


def test_read_wchan_reports_only_a_real_blocked_symbol(tmp_path):
    proc = tmp_path / "proc"
    (proc / "9").mkdir(parents=True)
    (proc / "9" / "wchan").write_text("0")
    assert io_attrib.read_wchan(9, proc) is None
    (proc / "9" / "wchan").write_text("jbd2_log_wait_commit\n")
    assert io_attrib.read_wchan(9, proc) == "jbd2_log_wait_commit"


# --- ranking --------------------------------------------------------------


def test_rank_writers_deltas_and_sorts_by_disk_bytes():
    pre = {
        1: {"comm": "systemd-journald", "io": {"write_bytes": 100, "read_bytes": 0}},
        2: {"comm": "px-alive", "io": {"write_bytes": 100, "read_bytes": 0}},
    }
    post = {
        1: {
            "comm": "systemd-journald",
            "io": {"write_bytes": 100 + 4096, "read_bytes": 0},
        },
        2: {"comm": "px-alive", "io": {"write_bytes": 100 + 1024, "read_bytes": 512}},
    }
    rows, count, total = io_attrib.rank_writers(pre, post)
    assert [row["pid"] for row in rows] == [1, 2]
    assert rows[0]["write_bytes"] == 4096
    assert rows[1]["read_bytes"] == 512
    assert (count, total) == (2, 5120)


def test_rank_writers_prefers_disk_writes_over_pipe_wchar():
    # #247's original sample ranked 2.4 MB/s of `wchar` from go2rtc/rpicam-vid
    # that never reached the disk. Ranking on write_bytes is that fix.
    pre = {
        1: {"comm": "go2rtc", "io": {"wchar": 0, "write_bytes": 0}},
        2: {"comm": "systemd-journald", "io": {"wchar": 0, "write_bytes": 0}},
    }
    post = {
        1: {"comm": "go2rtc", "io": {"wchar": 2_400_000, "write_bytes": 0}},
        2: {"comm": "systemd-journald", "io": {"wchar": 100, "write_bytes": 8192}},
    }
    rows, _, _ = io_attrib.rank_writers(pre, post)
    assert rows[0]["comm"] == "systemd-journald"
    assert rows[1]["wchar"] == 2_400_000  # still reported, just not the ranking


def test_rank_writers_skips_pids_with_no_delta_or_no_baseline():
    pre = {1: {"comm": "quiet", "io": {"write_bytes": 5}}}
    post = {
        1: {"comm": "quiet", "io": {"write_bytes": 5}},
        2: {"comm": "just-started", "io": {"write_bytes": 99}},
    }
    assert io_attrib.rank_writers(pre, post) == ([], 0, 0)


def test_rank_writers_reports_nothing_when_io_is_unavailable():
    pre = {1: {"comm": "root-owned"}}
    post = {1: {"comm": "root-owned"}}
    assert io_attrib.rank_writers(pre, post) == ([], 0, 0)


def test_rank_stalled_finds_d_state_and_run_delay():
    pre = {
        10: {"comm": "python3", "state": "D", "run_delay_ns": 0},
        11: {"comm": "rpicam-vid", "state": "S", "run_delay_ns": 0},
        12: {"comm": "idle", "state": "S", "run_delay_ns": 0},
    }
    post = {
        10: {"comm": "python3", "state": "D", "run_delay_ns": 1_000_000},
        11: {"comm": "rpicam-vid", "state": "S", "run_delay_ns": 500_000_000},
        12: {"comm": "idle", "state": "S", "run_delay_ns": 0},
    }
    rows, d_state, blocked_threads, max_delay = io_attrib.rank_stalled(pre, post)
    assert d_state == 1
    assert blocked_threads == 0
    assert max_delay == 500_000_000
    assert [row["pid"] for row in rows] == [10, 11]
    assert rows[0]["d_state"] is True
    assert rows[1]["run_delay_ms"] == 500.0


# --- trigger --------------------------------------------------------------


def test_trigger_reason_is_quiet_below_both_thresholds():
    assert (
        io_attrib.trigger_reason(
            io_some_avg10=12.0,
            heartbeat_age_s=2.0,
            alive=True,
            io_threshold=40.0,
            heartbeat_threshold_s=7.0,
        )
        is None
    )


def test_trigger_reason_fires_on_io_psi_at_the_threshold():
    assert (
        io_attrib.trigger_reason(
            io_some_avg10=40.0,
            heartbeat_age_s=0.5,
            alive=True,
            io_threshold=40.0,
            heartbeat_threshold_s=7.0,
        )
        == "io_psi"
    )


def test_trigger_reason_fires_on_a_late_heartbeat_only_when_alive():
    kwargs = dict(
        io_some_avg10=0.0,
        heartbeat_age_s=8.0,
        io_threshold=40.0,
        heartbeat_threshold_s=7.0,
    )
    assert io_attrib.trigger_reason(alive=True, **kwargs) == "heartbeat_age"
    # A stopped daemon leaves a stale file behind; without the aliveness gate
    # that would trigger forever and bury the real stalls.
    assert io_attrib.trigger_reason(alive=False, **kwargs) is None


def test_trigger_reason_names_both_signals():
    assert (
        io_attrib.trigger_reason(
            io_some_avg10=99.0,
            heartbeat_age_s=9.0,
            alive=True,
            io_threshold=40.0,
            heartbeat_threshold_s=7.0,
        )
        == "io_psi+heartbeat_age"
    )


def test_trigger_reason_handles_unreadable_signals():
    assert (
        io_attrib.trigger_reason(
            io_some_avg10=None,
            heartbeat_age_s=None,
            alive=True,
            io_threshold=40.0,
            heartbeat_threshold_s=7.0,
        )
        is None
    )


# --- capture --------------------------------------------------------------


def _stall_procs():
    return {
        1: {"comm": "systemd", "state": "S", "io": {"write_bytes": 1000}},
        42: {
            "comm": "px-alive",
            "state": "D",
            "wchan": "jbd2_log_wait_commit",
            "io": {"write_bytes": 0},
            "cgroup": "0::/system.slice/px-alive.service\n",
            "run_delay_ns": 0,
        },
        43: {
            "comm": "systemd-journald",
            "state": "S",
            "io": {"write_bytes": 4096},
            "cgroup": "0::/system.slice/systemd-journald.service\n",
        },
    }


def _stall_post(procs):
    post = {pid: dict(spec) for pid, spec in procs.items()}
    post[43]["io"] = {"write_bytes": 4096 + 262144}
    post[42]["run_delay_ns"] = 12_000_000
    return post


def _wire_capture(
    tmp_path,
    monkeypatch,
    procs,
    post,
    *,
    diskstats=IDLE_DISK,
    diskstats_post=ACTIVE_DISK,
    vmstat=IDLE_VMSTAT,
    vmstat_post=ACTIVE_VMSTAT,
    ext4_fields=None,
):
    """Two-ended fixture: process walk and device counters both advance once."""
    paths = _write_proc_tree(
        tmp_path,
        procs,
        self_io={"read_bytes": 0, "write_bytes": 0},
        diskstats=diskstats,
        vmstat=vmstat,
        ext4_fields=ext4_fields or {"session_write_kbytes": 1000, "errors_count": 0},
    )
    _stub_host_load(monkeypatch)
    walks = {"n": 0}

    def fake_sample(proc_root, *, allow_proc_io):
        walks["n"] += 1
        return procs if walks["n"] == 1 else post

    monkeypatch.setattr(io_attrib, "sample_processes", fake_sample)

    real_read_text = io_attrib._read_text

    def fake_read_text(path):
        # After the first walk, every counter read is the "post" end of the
        # window: capture() reads diskstats/vmstat before and after the sleep.
        if walks["n"] >= 1:
            if path == paths.diskstats:
                return diskstats_post
            if path == paths.vmstat:
                return vmstat_post
        return real_read_text(path)

    monkeypatch.setattr(io_attrib, "_read_text", fake_read_text)
    return paths


def test_capture_records_the_writer_the_device_and_the_stalled_process(
    tmp_path, monkeypatch
):
    procs = _stall_procs()
    paths = _wire_capture(tmp_path, monkeypatch, procs, _stall_post(procs))
    record = io_attrib.capture(
        {"reason": "io_psi", "io_psi_some_avg10": 96.4},
        paths=paths,
        window_s=3.0,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )

    assert record["reason"] == "io_psi"
    assert record["trigger"]["io_psi_some_avg10"] == 96.4
    assert record["privileged"] is True
    assert record["window_s"] == 3.0
    assert record["writers_unavailable_reason"] is None
    assert record["writers"][0]["comm"] == "systemd-journald"
    assert record["writers"][0]["write_bytes"] == 262144
    assert record["writers"][0]["unit"] == "systemd-journald.service"
    assert record["write_bytes_total"] == 262144
    assert record["d_state_count"] == 1
    assert record["blocked_thread_count"] == 0
    assert record["max_run_delay_ms"] == 12.0
    assert record["stalled"][0]["comm"] == "px-alive"
    assert record["stalled"][0]["wchan"] == "jbd2_log_wait_commit"
    assert record["devices"]["mmcblk0"]["writes_completed"] == 61
    assert record["devices"]["mmcblk0"]["ms_io"] == 12000
    assert record["vmstat"]["pswpin"] == 2
    assert record["vmstat"]["pswpout"] == 0
    assert record["vmstat_end"]["nr_dirty"] == 12
    assert record["uptime_s"] == 12345.7
    assert record["psi_pre"]["psi_io_some_avg10_pre"] == 96.4
    assert record["psi_post"]["psi_io_some_avg10_post"] == 96.4
    assert record["observer_write_bytes"] == 0


def test_capture_without_the_writer_channel_keeps_the_stall_channel(
    tmp_path, monkeypatch
):
    """The unprivileged record is not an empty record.

    This is the shape an unprivileged deployment produces: no writer list, an
    explicit reason, and the stall channel intact — who was blocked, and how
    busy the device was.
    """
    procs = {42: {"comm": "px-alive", "state": "D", "io": {"write_bytes": 0}}}
    paths = _wire_capture(tmp_path, monkeypatch, procs, procs)
    record = io_attrib.capture(
        {"reason": "heartbeat_age"},
        paths=paths,
        window_s=1.0,
        allow_proc_io=False,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert record["writer_channel_attempted"] is False
    assert (
        record["writers_unavailable_reason"]
        == "writer channel not attempted: --no-proc-io"
    )
    assert record["writers"] == []
    assert record["d_state_count"] == 1
    assert record["devices"]["mmcblk0"]["ms_io"] == 12000


def test_capture_survives_a_missing_proc_tree(tmp_path, monkeypatch):
    _stub_host_load(monkeypatch)
    record = io_attrib.capture(
        {"reason": "manual"},
        paths=io_attrib.Paths(
            proc=tmp_path / "no-proc",
            pressure_io=tmp_path / "no-pressure",
            diskstats=tmp_path / "no-diskstats",
            vmstat=tmp_path / "no-vmstat",
            uptime=tmp_path / "no-uptime",
        ),
        window_s=1.0,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    # Never raises and never fabricates: an absent /proc is an empty record,
    # not an exception thrown inside the stall it was called to measure.
    assert record["processes_seen"] == 0
    assert record["writers"] == []
    assert record["devices"] == {}
    assert record["uptime_s"] is None


def test_capture_drops_virtual_devices_and_keeps_zram(tmp_path, monkeypatch):
    procs = {1: {"comm": "systemd", "io": {"write_bytes": 0}}}
    idle_all = (
        _diskstats_line("ram0", writes=0, sectors=0, ms_writing=0, ms_io=0)
        + _diskstats_line("zram0", writes=0, sectors=0, ms_writing=0, ms_io=0)
        + _diskstats_line("mmcblk0", writes=0, sectors=0, ms_writing=0, ms_io=0)
    )
    active_all = (
        _diskstats_line("ram0", writes=500, sectors=100, ms_writing=900, ms_io=900)
        + _diskstats_line("zram0", writes=500, sectors=100, ms_writing=900, ms_io=900)
        + _diskstats_line(
            "mmcblk0", writes=61, sectors=1024, ms_writing=900, ms_io=1000
        )
    )
    paths = _wire_capture(
        tmp_path,
        monkeypatch,
        procs,
        procs,
        diskstats=idle_all,
        diskstats_post=active_all,
        vmstat=IDLE_VMSTAT,
        vmstat_post=ACTIVE_VMSTAT,
    )
    record = io_attrib.capture(
        {"reason": "manual"},
        paths=paths,
        window_s=1.0,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    # zram0 stays on purpose: it is the swap device now, and showing memory
    # churn next to a stalled mmcblk0 is how swap gets ruled back in or out.
    assert set(record["devices"]) == {"mmcblk0", "zram0"}


def test_blocked_threads_finds_a_stuck_health_thread_behind_a_healthy_leader(tmp_path):
    """px-alive's shape: group leader fine, background fsync thread in D state.

    That combination is unkillable (SIGKILL does not interrupt D state) and
    invisible to /proc/<pid>/stat, which reports only the leader.
    """
    proc = tmp_path / "proc"
    pid_dir = proc / "42"
    (pid_dir / "task" / "43").mkdir(parents=True)
    (pid_dir / "task" / "42").mkdir(parents=True)
    (pid_dir / "task" / "43" / "stat").write_text("43 (px-alive-health) D 1 1\n")
    (pid_dir / "task" / "42" / "stat").write_text("42 (python3) S 1 1\n")
    assert io_attrib.blocked_threads(42, pid_dir) == [
        {"tid": 43, "comm": "px-alive-health"}
    ]
    # No task directory at all is a normal case, not an error.
    assert io_attrib.blocked_threads(42, tmp_path / "elsewhere") == []


def test_read_task_wchan_reads_the_thread_symbol(tmp_path):
    proc = tmp_path / "proc"
    thread_dir = proc / "42" / "task" / "43"
    thread_dir.mkdir(parents=True)
    (thread_dir / "wchan").write_text("jbd2_log_wait_commit")
    assert io_attrib.read_task_wchan(42, 43, proc) == "jbd2_log_wait_commit"


def test_rank_stalled_counts_a_process_whose_thread_is_blocked(tmp_path):
    pre = {42: {"comm": "python3", "state": "S", "run_delay_ns": 0}}
    post = {
        42: {
            "comm": "python3",
            "state": "S",
            "run_delay_ns": 0,
            "blocked_threads": [{"tid": 43, "comm": "px-alive-health"}],
        }
    }
    rows, stalled, blocked_threads, _ = io_attrib.rank_stalled(pre, post)
    assert (stalled, blocked_threads) == (1, 1)
    assert rows[0]["d_state"] is True
    assert rows[0]["blocked_threads"] == [{"tid": 43, "comm": "px-alive-health"}]


def test_capture_attributes_a_blocked_thread_with_its_own_wchan(tmp_path, monkeypatch):
    procs = {
        42: {
            "comm": "python3",
            "state": "S",
            "io": {"write_bytes": 0},
            "cgroup": "0::/system.slice/px-alive.service\n",
            "threads": {
                43: {
                    "comm": "px-alive-health",
                    "state": "D",
                    "wchan": "jbd2_log_wait_commit",
                }
            },
        }
    }
    # No fake sampler here: the point is that the real walk finds the thread.
    paths = _write_proc_tree(tmp_path, procs, self_io={}, diskstats=IDLE_DISK)
    _stub_host_load(monkeypatch)
    record = io_attrib.capture(
        {"reason": "heartbeat_age"},
        paths=paths,
        window_s=1.0,
        allow_proc_io=False,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert record["d_state_count"] == 1
    assert record["blocked_thread_count"] == 1
    row = record["stalled"][0]
    assert row["unit"] == "px-alive.service"
    assert row["blocked_threads"][0]["tid"] == 43
    assert row["blocked_threads"][0]["wchan"] == "jbd2_log_wait_commit"


# --- the privilege probe, and the measurement that backs it ---------------


def test_proc_io_readable_is_not_satisfied_by_reading_our_own_io(tmp_path):
    """The probe must ask about *other users'* processes.

    An earlier version accepted a readable `/proc/<self>/io` as proof, which is
    always true — so an unprivileged observer reported `privileged: true` while
    px-alive and journald were invisible. Reading pid 1 is the question that
    actually distinguishes the two.
    """
    proc = tmp_path / "proc"
    (proc / "7").mkdir(parents=True)
    (proc / "7" / "io").write_text("write_bytes: 1\n")
    assert io_attrib.proc_io_readable(proc) is False  # nothing readable at pid 1
    (proc / "1").mkdir()
    (proc / "1" / "io").write_text("write_bytes: 1\n")
    assert io_attrib.proc_io_readable(proc) is True


def test_read_proc_io_separates_denial_from_absence(tmp_path, monkeypatch):
    pid_dir = tmp_path / "proc" / "7"
    pid_dir.mkdir(parents=True)
    (pid_dir / "io").write_text("write_bytes: 5\n")
    assert io_attrib._read_proc_io(pid_dir) == ({"write_bytes": 5}, False)
    # No file at all: a process that exited mid-walk, not a privilege story.
    assert io_attrib._read_proc_io(tmp_path / "elsewhere") == (None, False)
    real_read_text = Path.read_text

    def denied(self, *args, **kwargs):
        if self.name == "io":
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    assert io_attrib._read_proc_io(pid_dir) == (None, True)


def test_capture_unprivileged_still_reports_what_it_could_read(tmp_path, monkeypatch):
    """A partial writer list plus a refusal count beats no list at all."""
    procs = {
        7: {"comm": "go2rtc", "state": "S", "io": {"write_bytes": 0}},
        9: {"comm": "systemd-journald", "state": "S", "io_missing": True},
    }
    paths = _write_proc_tree(
        tmp_path,
        procs,
        self_io={"read_bytes": 0, "write_bytes": 0},
        diskstats=IDLE_DISK,
        vmstat=IDLE_VMSTAT,
    )
    _stub_host_load(monkeypatch)
    real_read_proc_io = io_attrib._read_proc_io

    def fake_read_proc_io(pid_dir):
        if pid_dir.name == "9":
            return None, True
        return real_read_proc_io(pid_dir)

    monkeypatch.setattr(io_attrib, "_read_proc_io", fake_read_proc_io)

    record = io_attrib.capture(
        {"reason": "io_psi"},
        paths=paths,
        window_s=1.0,
        privileged=False,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert record["writer_channel_attempted"] is True
    assert record["privileged"] is False
    assert record["writers_unreadable_count"] == 1
    assert "root-owned writers" in record["writers_unavailable_reason"]


def test_capture_distinguishes_a_refusal_from_an_expected_invisibility(
    tmp_path, monkeypatch
):
    """`privileged: true` with a refusal means something different — say it."""
    procs = {9: {"comm": "systemd-journald", "state": "S", "io_missing": True}}
    paths = _write_proc_tree(
        tmp_path,
        procs,
        self_io={"read_bytes": 0, "write_bytes": 0},
        diskstats=IDLE_DISK,
        vmstat=IDLE_VMSTAT,
    )
    _stub_host_load(monkeypatch)
    real_read_proc_io = io_attrib._read_proc_io
    monkeypatch.setattr(
        io_attrib,
        "_read_proc_io",
        lambda pid_dir: (None, True)
        if pid_dir.name == "9"
        else real_read_proc_io(pid_dir),
    )
    record = io_attrib.capture(
        {"reason": "io_psi"},
        paths=paths,
        window_s=1.0,
        privileged=True,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert record["privileged"] is True
    assert record["writers_unreadable_count"] == 1
    assert "non-dumpable" in record["writers_unavailable_reason"]


def test_capture_says_nothing_about_unreadable_writers_when_none_were(
    tmp_path, monkeypatch
):
    procs = {1: {"comm": "systemd", "state": "S", "io": {"write_bytes": 0}}}
    paths = _wire_capture(tmp_path, monkeypatch, procs, procs)
    record = io_attrib.capture(
        {"reason": "io_psi"},
        paths=paths,
        window_s=1.0,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert record["writers_unreadable_count"] == 0
    assert record["writers_unavailable_reason"] is None


# --- the file-level writer channel (unprivileged attribution) -------------


def test_growth_group_classifies_the_watchlist():
    assert io_attrib.growth_group("/var/log/journal/abc/system.journal") == "journal"
    assert (
        io_attrib.growth_group("/home/pi/picar-x-hacking/state/health/px-alive.json")
        == "health"
    )
    assert (
        io_attrib.growth_group("/home/pi/picar-x-hacking/state/session.json") == "state"
    )
    assert io_attrib.growth_group("/home/pi/picar-x-hacking/logs/px-mind.log") == "logs"
    assert io_attrib.growth_group("/somewhere/else.txt") == "other"


def test_sample_file_sizes_stats_every_match_and_never_raises(tmp_path):
    (tmp_path / "a.log").write_text("x" * 10)
    (tmp_path / "b.log").write_text("y" * 20)
    (tmp_path / "c.json").write_text("{}")
    sizes = io_attrib.sample_file_sizes(
        [str(tmp_path / "*.log"), str(tmp_path / "*.json")]
    )
    assert sizes[str(tmp_path / "a.log")] == 10
    assert sizes[str(tmp_path / "b.log")] == 20
    assert sizes[str(tmp_path / "c.json")] == 2
    # A pattern that matches nothing, and a path that is not a file at all:
    # "not watched", never an exception.
    assert io_attrib.sample_file_sizes([str(tmp_path / "nope-*")]) == {}
    assert io_attrib.sample_file_sizes([str(tmp_path)]) == {}


def test_unattributed_share_names_the_journal_metadata_remainder():
    """`picar`, 2026-09-17: 2596 KB to the device, 92 KB to every readable
    process. The remainder is journal/metadata and has no process to name."""
    devices = {"mmcblk0": {"sectors_written": 5192}}     # 2596 KB
    device_bytes, unattributed, share = io_attrib.unattributed_write_share(devices, 94_208)
    assert device_bytes == 2_658_304
    assert unattributed == 2_658_304 - 94_208
    assert share == 0.965


def test_unattributed_share_is_none_when_the_device_wrote_nothing():
    """0/0 is not 0 %: an idle device has no share to report."""
    assert io_attrib.unattributed_write_share({}, 0) == (0, 0, None)
    assert io_attrib.unattributed_write_share({"mmcblk0": {"sectors_written": 0}}, 0) == (0, 0, None)


def test_unattributed_share_picks_the_busiest_device():
    devices = {
        "mmcblk0": {"sectors_written": 200},
        "zram0": {"sectors_written": 800},
    }
    device_bytes, unattributed, share = io_attrib.unattributed_write_share(devices, 1000)
    assert device_bytes == 800 * 512           # zram0 is busier in this window
    assert unattributed == 800 * 512 - 1000
    assert 0.99 < share < 1.0


def test_unattributed_share_never_goes_negative():
    """A process can be charged for writes the device counter did not see in the
    window (page-cache writes flushed later), so the remainder clamps at zero
    rather than reporting negative unattributed bytes."""
    devices = {"mmcblk0": {"sectors_written": 2}}          # 1 KB
    device_bytes, unattributed, share = io_attrib.unattributed_write_share(devices, 500_000)
    assert (device_bytes, unattributed, share) == (1024, 0, 0.0)


def test_capture_reports_the_share_alongside_the_device_deltas(tmp_path, monkeypatch):
    procs = _stall_procs()
    paths = _wire_capture(tmp_path, monkeypatch, procs, _stall_post(procs))
    record = io_attrib.capture(
        {"reason": "io_psi"},
        paths=paths,
        window_s=3.0,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    # The fixture's device writes 1024 sectors (512 KB) and one writer accounts
    # for 262144 B of it.
    assert record["device_write_bytes"] == 1024 * 512
    assert record["unattributed_write_bytes"] == 1024 * 512 - record["write_bytes_total"]
    assert 0.0 <= record["unattributed_write_share"] <= 1.0


def test_parse_procs_stat_reads_the_stall_context():
    """`full` tracking `some` on a box with one runnable task is not a freeze."""
    text = (
        "cpu  1 2 3 4 5 6 7 8 9 10\n"
        "procs_running 2\n"
        "procs_blocked 4\n"
        "btime 1758000000\n"
    )
    assert io_attrib.parse_procs_stat(text) == {"running": 2, "blocked": 4}
    assert io_attrib.parse_procs_stat("") == {}


def test_capture_records_the_stall_context(tmp_path, monkeypatch):
    procs = _stall_procs()
    paths = _wire_capture(tmp_path, monkeypatch, procs, _stall_post(procs))
    (tmp_path / "proc_stat").write_text("procs_running 1\nprocs_blocked 3\n")
    monkeypatch.setattr(
        io_attrib, "parse_procs_stat", lambda _t: {"running": 1, "blocked": 3}
    )
    record = io_attrib.capture(
        {"reason": "io_psi"},
        paths=paths,
        window_s=3.0,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert record["procs_running_pre"] == 1
    assert record["procs_blocked_post"] == 3


def test_sample_file_meta_carries_size_and_mtime(tmp_path):
    target = tmp_path / "ambient_sound.json"
    target.write_text("{}")
    meta = io_attrib.sample_file_meta([str(tmp_path / "*.json")])
    entry = meta[str(target)]
    assert entry["size"] == 2
    assert entry["mtime_ns"] > 0
    # The size half stays available under the old name, same walk.
    assert io_attrib.sample_file_sizes([str(tmp_path / "*.json")]) == {
        str(target): 2
    }


def test_rank_file_touches_sees_a_write_that_moves_no_size(tmp_path):
    """The journald shape: mtime advances, size does not — and a real append."""
    journal = tmp_path / "system.journal"
    log = tmp_path / "px-mind.log"
    journal.write_bytes(b"x" * 8388608)
    log.write_text("a")
    pre = io_attrib.sample_file_meta([str(tmp_path / "*")])
    time.sleep(0.01)
    journal.write_bytes(open(journal, "rb").read())     # rewrite, same length
    with open(log, "a") as handle:
        handle.write("bb")
    post = io_attrib.sample_file_meta([str(tmp_path / "*")])
    rows, count = io_attrib.rank_file_touches(pre, post)
    assert count == 2
    # Zero-byte touches first, because they are what the size channel cannot see.
    assert rows[0]["path"].endswith("system.journal")
    assert rows[0]["bytes"] == 0
    assert rows[1]["path"].endswith("px-mind.log")
    assert rows[1]["bytes"] == 2


def test_rank_file_touches_ignores_an_untouched_file(tmp_path):
    target = tmp_path / "quiet.json"
    target.write_text("{}")
    pre = io_attrib.sample_file_meta([str(tmp_path / "*")])
    post = io_attrib.sample_file_meta([str(tmp_path / "*")])
    rows, count = io_attrib.rank_file_touches(pre, post)
    assert (rows, count) == ([], 0)


def test_capture_records_touched_files_alongside_growth(tmp_path, monkeypatch):
    """A record must be able to say 'wrote, invisible to the size channel'."""
    procs = _stall_procs()
    paths = _wire_capture(tmp_path, monkeypatch, procs, _stall_post(procs))
    journal = tmp_path / "system.journal"
    journal.write_bytes(b"x" * 64)
    pre = {"size": 64, "mtime_ns": 1}
    post = {"size": 64, "mtime_ns": 2}
    ends = iter([{str(journal): pre}, {str(journal): post}])
    monkeypatch.setattr(
        io_attrib, "sample_file_meta", lambda _patterns=None, **_kw: next(ends, {})
    )
    record = io_attrib.capture(
        {"reason": "io_psi", "io_some_avg10": 44.0},
        paths=paths,
        window_s=3.0,
        growth_patterns=["/var/log/journal/*/*.journal"],
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert record["file_growth"] == []
    assert record["file_growth_total_bytes"] == 0
    assert len(record["file_touched"]) == 1
    assert record["file_touched"][0]["path"].endswith("system.journal")
    assert record["file_touched"][0]["bytes"] == 0
    assert record["file_touched_count"] == 1


def test_sample_ext4_reads_the_counters_and_the_journal_task(tmp_path):
    paths = _write_proc_tree(
        tmp_path,
        {},
        ext4_fields={
            "session_write_kbytes": 687840,
            "lifetime_write_kbytes": 1103980557,
            "delayed_allocation_blocks": 0,
            "errors_count": 0,
            "journal_task": "jbd2/mmcblk0p2-8",
        },
    )
    ext4 = io_attrib.sample_ext4(paths)
    assert ext4["fs"] == "mmcblk0p2"
    assert ext4["session_write_kbytes"] == 687840
    assert ext4["lifetime_write_kbytes"] == 1103980557
    assert ext4["delayed_allocation_blocks"] == 0
    assert ext4["journal_task"] == "jbd2/mmcblk0p2-8"
    # No ext4 on this host (container, macOS dev box): unwatched, not zero.
    assert io_attrib.sample_ext4(io_attrib.Paths(ext4_sysfs=tmp_path / "nope")) == {}


def test_ext4_window_reports_a_delta_and_says_nothing_when_unwatched():
    pre = {"fs": "mmcblk0p2", "session_write_kbytes": 1000, "errors_count": 0}
    post = {"fs": "mmcblk0p2", "session_write_kbytes": 1204, "errors_count": 0}
    assert io_attrib._ext4_window(pre, post)["write_kbytes_delta"] == 204
    assert io_attrib._ext4_window({}, {}) == {}
    # Mounted mid-window: no baseline means no delta, not a fabricated zero.
    assert "write_kbytes_delta" not in io_attrib._ext4_window({}, post)


def test_capture_records_filesystem_bytes_against_device_bytes(tmp_path, monkeypatch):
    """The discrimination this field exists for: the device moved 524 KB and the
    filesystem wrote 4 KB across the same window — metadata/journal work, which
    no file channel can attribute."""
    procs = _stall_procs()
    paths = _wire_capture(
        tmp_path,
        monkeypatch,
        procs,
        _stall_post(procs),
        diskstats="179 0 mmcblk0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
        diskstats_post="179 0 mmcblk0 0 0 0 0 57 0 1048 0 0 0 0 0 0 0 0 0\n",
        ext4_fields={"session_write_kbytes": 1000, "errors_count": 0},
    )
    ends = iter(
        [
            {"fs": "mmcblk0p2", "session_write_kbytes": 1000, "errors_count": 0},
            {"fs": "mmcblk0p2", "session_write_kbytes": 1004, "errors_count": 0},
        ]
    )
    monkeypatch.setattr(io_attrib, "sample_ext4", lambda _paths=None: next(ends))
    record = io_attrib.capture(
        {"reason": "io_psi"},
        paths=paths,
        window_s=3.0,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert record["ext4"]["write_kbytes_delta"] == 4
    assert record["devices"]["mmcblk0"]["sectors_written"] == 1048   # 524 KB
    assert record["writers_with_activity"] == 1
    assert record["file_growth"] == []


def test_rank_file_growth_reports_deltas_groups_and_total():
    pre = {
        "/var/log/journal/abc/system.journal": 1000,
        "/repo/logs/px-mind.log": 500,
        "/repo/state/health/px-alive.json": 169,
        "/repo/logs/quiet.log": 42,
    }
    post = {
        "/var/log/journal/abc/system.journal": 1000 + 152_000,
        "/repo/logs/px-mind.log": 500 + 4096,
        "/repo/state/health/px-alive.json": 169 + 169,
        "/repo/logs/quiet.log": 42,
    }
    rows, total, groups = io_attrib.rank_file_growth(pre, post)
    # Paths are shortened to their last three components: enough to name the
    # writer, short enough that the record stays one line per stall.
    assert [row["path"] for row in rows] == [
        "journal/abc/system.journal",
        "repo/logs/px-mind.log",
        "state/health/px-alive.json",
    ]
    assert rows[0]["bytes"] == 152_000
    assert total == 156_265
    assert groups == {"journal": 152_000, "logs": 4096, "health": 169}


def test_rank_file_growth_ignores_a_file_that_appeared_mid_window(tmp_path):
    """A rotated log has no baseline; its whole size is not growth."""
    pre = {"/repo/logs/px-mind.log": 100}
    post = {"/repo/logs/px-mind.log": 100, "/repo/logs/px-mind.log.1": 5_000_000}
    rows, total, groups = io_attrib.rank_file_growth(pre, post)
    assert (rows, total, groups) == ([], 0, {})


def test_capture_reports_the_file_channel_and_what_it_covered(tmp_path, monkeypatch):
    """End to end: the file that grows is named, and the record says how many
    files were watched so growth outside the watchlist is not mistaken for
    silence."""
    procs = {1: {"comm": "systemd", "state": "S", "io": {"write_bytes": 0}}}
    paths = _wire_capture(tmp_path, monkeypatch, procs, procs)
    # Named like the real watchlist so the group classification is exercised.
    watched = tmp_path / "logs"
    watched.mkdir()
    (watched / "px-mind.log").write_text("x" * 100)
    (watched / "quiet.log").write_text("y" * 100)

    real_sample = io_attrib.sample_file_meta
    calls = {"n": 0}

    def growing(patterns, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_sample(patterns, **kwargs)
        (watched / "px-mind.log").write_text("x" * 1500)
        return real_sample(patterns, **kwargs)

    monkeypatch.setattr(io_attrib, "sample_file_meta", growing)
    record = io_attrib.capture(
        {"reason": "io_psi"},
        paths=paths,
        window_s=1.0,
        growth_patterns=[str(watched / "*.log")],
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert len(record["file_growth"]) == 1
    assert record["file_growth"][0]["path"].endswith("logs/px-mind.log")
    assert record["file_growth"][0]["bytes"] == 1400
    assert record["file_growth_total_bytes"] == 1400
    assert record["file_growth_groups"] == {"logs": 1400}
    assert record["file_growth_watched"] == 2


def test_capture_without_growth_patterns_does_no_file_walk(tmp_path, monkeypatch):
    """The default is no file channel at all — the watchlist is opt-in, so a
    caller that did not ask for it pays nothing and claims nothing."""
    procs = {1: {"comm": "systemd", "state": "S", "io": {"write_bytes": 0}}}
    paths = _wire_capture(tmp_path, monkeypatch, procs, procs)
    record = io_attrib.capture(
        {"reason": "manual"},
        paths=paths,
        window_s=1.0,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert record["file_growth"] == []
    assert record["file_growth_watched"] == 0


# --- the observer's own pid file (operator hygiene) -----------------------


def test_write_pid_file_publishes_our_pid_and_removes_only_our_own(tmp_path):
    """`/usr/bin/python3 -` matches four daemons on this host, including root's
    px-battery-poll. A pid file that points at the wrong one is worse than none:
    `kill $(cat …)` then kills a daemon that was doing its job."""
    pid_file = tmp_path / "logs" / "px-io-attrib.pid"
    assert io_attrib.write_pid_file(pid_file) is True
    assert io_attrib.read_pid_file(pid_file) == os.getpid()

    # Someone else's file is left alone.
    pid_file.write_text("1")
    io_attrib.remove_pid_file(pid_file)
    assert io_attrib.read_pid_file(pid_file) == 1

    # Ours is removed.
    pid_file.write_text(str(os.getpid()))
    io_attrib.remove_pid_file(pid_file)
    assert not pid_file.exists()


def test_pid_file_helpers_never_raise(tmp_path):
    unwritable = tmp_path / "nope" / "deeper" / "file.pid"
    (tmp_path / "nope").write_text("not a directory")
    assert io_attrib.write_pid_file(unwritable) is False
    io_attrib.remove_pid_file(tmp_path / "missing.pid")
    assert io_attrib.read_pid_file(tmp_path / "missing.pid") is None


# --- the CLI, not just the module (the gap that let a crash ship) ---------
#
def test_default_watchlist_covers_the_shapes_that_actually_churn(tmp_path):
    """The 2026-09-18 gap: 112 KB written with `file_growth` and `file_touched`
    both empty, because the default set was `*.log` + `*.json` and this host
    churns `.jsonl`, `.lock`, `.out`, `.rotlock` and files one level down."""
    log_dir, state_dir = tmp_path / "logs", tmp_path / "state"
    (log_dir / "archive").mkdir(parents=True)
    (state_dir / "health").mkdir(parents=True)
    (state_dir / "brain").mkdir(parents=True)
    shapes = {
        log_dir / "px-mind.log": 1,
        log_dir / "hb-gaps.jsonl": 1,
        log_dir / "px-io-attrib.out": 1,
        log_dir / "px-mind.log.rotlock": 1,
        log_dir / "archive" / "px-mind.log.1": 1,
        state_dir / "thoughts-spark.jsonl": 1,
        state_dir / "session.json.lock": 1,
        state_dir / "health" / "px-alive.json": 1,
        state_dir / "brain" / "resident.log": 1,
    }
    for path in shapes:
        path.write_text("x")
    patterns = io_attrib.default_growth_patterns(log_dir, state_dir)
    covered = io_attrib.sample_file_sizes(patterns)
    missing = sorted(str(p) for p in shapes if str(p) not in covered)
    assert missing == [], f"watchlist misses {missing}"
    # Directories are not writers, and the journal stays in the set.
    assert io_attrib.sample_file_sizes([str(log_dir / "*")]) == {
        str(p): 1 for p in shapes if p.parent == log_dir
    }


# --- the inflight channel (#247) ------------------------------------------
#
# Every other channel in this module is a delta across the window, and a delta
# cannot express "nothing was in flight while every task waited". Measured on
# `picar` 2026-09-17: a 64 % some / 58 % full io-PSI stall with mmcblk0 at 0/0
# inflight, ~2 % busy, and a 169-byte fsync+replace completing in 7-52 ms.
# Without this field a record of that stall looks like a record of a busy
# device, which is the reading that sent the writer hunt to the wrong place.


def test_sample_inflight_reports_a_zeroed_device_rather_than_dropping_it(tmp_path):
    paths = _write_proc_tree(
        tmp_path, {}, inflight={"mmcblk0": (0, 0), "zram0": (1, 3)}
    )
    assert io_attrib.sample_inflight(paths) == {
        "mmcblk0": {"reads": 0, "writes": 0},
        "zram0": {"reads": 1, "writes": 3},
    }


def test_sample_inflight_skips_virtual_devices_and_survives_a_missing_tree(tmp_path):
    paths = _write_proc_tree(
        tmp_path, {}, inflight={"ram0": (9, 9), "loop3": (4, 4), "mmcblk0": (0, 2)}
    )
    assert io_attrib.sample_inflight(paths) == {"mmcblk0": {"reads": 0, "writes": 2}}
    # /sys/block absent (container, macOS dev box): empty, not an exception.
    assert io_attrib.sample_inflight(io_attrib.Paths(sys_block=tmp_path / "nope")) == {}


def test_capture_records_inflight_at_both_ends(tmp_path, monkeypatch):
    procs = _stall_procs()
    paths = _wire_capture(tmp_path, monkeypatch, procs, _stall_post(procs))
    seen = iter(
        [
            {"mmcblk0": {"reads": 0, "writes": 0}},   # inside the stall
            {"mmcblk0": {"reads": 0, "writes": 2}},   # recovering
        ]
    )
    monkeypatch.setattr(
        io_attrib, "sample_inflight", lambda _paths=None: next(seen, {})
    )
    record = io_attrib.capture(
        {"reason": "io_psi", "io_some_avg10": 96.4},
        paths=paths,
        window_s=3.0,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert record["device_inflight_pre"] == {"mmcblk0": {"reads": 0, "writes": 0}}
    assert record["device_inflight_post"] == {"mmcblk0": {"reads": 0, "writes": 2}}


# The unit tests exercise io_attrib's functions; they cannot see the argv
# plumbing or the order of statements in main(). The first version of the pid
# file wrote it *after* the startup line that reports it, so the observer died
# with UnboundLocalError on the robot while 44 module tests passed.


def test_cli_loop_starts_and_exits_cleanly(tmp_path):
    """Run the real entry point for ~2 s: no traceback, pid published and cleared."""
    import subprocess

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [str(root / "bin" / "px-io-attrib"),
         "--duration-h", "0.0006",       # ~2 s: one or two loop iterations
         "--io-threshold", "200",        # never a stall: no snapshot in this test
         "--heartbeat-file", str(tmp_path / "no-heartbeat.json"),
         "--growth-pattern", str(tmp_path / "nothing-*"),
         "--poll", "0.5"],
        cwd=str(root), capture_output=True, text=True, timeout=60,
        env={**os.environ, "LOG_DIR": str(log_dir), "PX_BYPASS_SUDO": "1",
             "PX_STATE_DIR": str(tmp_path / "state"), "PATH": os.environ.get("PATH", "")},
    )
    assert "Traceback" not in proc.stderr, proc.stderr[-800:]
    assert "observer start" in proc.stdout, proc.stdout[-400:]
    assert proc.returncode == 0
    # It publishes its own pid while running and clears it on a clean exit.
    assert not (log_dir / "px-io-attrib.pid").exists()


# --- the window channel, and the privilege boundary it exists to cross -----


def _field_line(pid: int, comm: str, state: str = "S", *, delayacct: int = 0) -> str:
    """A synthetic /proc/<pid>/stat line with a *correctly placed* field 42.

    Built by index rather than by counting words, because this field sits 39
    places past `comm` and an off-by-one here measures a different counter while
    looking entirely plausible. The alignment was verified against the robot's
    own `/proc/1/stat` on 2026-09-18: index 36 (`processor`) read 3, 37
    (`rt_priority`) 0, 38 (`policy`) 0, 39 (`delayacct_blkio_ticks`) 0.
    """
    fields = ["0"] * 40
    fields[0] = state
    fields[36] = "3"
    fields[39] = str(delayacct)
    return f"{pid} ({comm}) " + " ".join(fields) + "\n"


def _bare_paths(tmp_path, procs: dict[int, dict]) -> io_attrib.Paths:
    """A `Paths` over a hand-built /proc tree: only what a test actually reads."""
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, spec in procs.items():
        pid_dir = proc / str(pid)
        pid_dir.mkdir()
        (pid_dir / "stat").write_text(spec["stat"])
        (pid_dir / "io").write_text(spec.get("io", "write_bytes: 0\n"))
        (pid_dir / "wchan").write_text(spec.get("wchan", "0"))
        (pid_dir / "cgroup").write_text(spec.get("cgroup", "0::/system.slice/px-alive.service\n"))
        for tid, thread in (spec.get("threads") or {}).items():
            task = pid_dir / "task" / str(tid)
            task.mkdir(parents=True)
            (task / "stat").write_text(thread["stat"])
            (task / "wchan").write_text(thread.get("wchan", "0"))
    return io_attrib.Paths(
        proc=proc,
        pressure_io=tmp_path / "pressure_io",
        diskstats=tmp_path / "diskstats",
        vmstat=tmp_path / "vmstat",
        uptime=tmp_path / "uptime",
        sys_block=tmp_path / "sys_block",
        ext4_sysfs=tmp_path / "sys_fs_ext4",
    )


def test_parse_proc_stat_reads_delayacct_from_field_42():
    parsed = io_attrib.parse_proc_stat(_field_line(7, "python3", "D", delayacct=4242))
    assert parsed["comm"] == "python3"
    assert parsed["state"] == "D"
    assert parsed["delayacct_blkio_ticks"] == 4242


def test_parse_proc_stat_omits_delayacct_rather_than_defaulting_to_zero():
    """A short line is another kernel, not a process that never waited on IO."""
    parsed = io_attrib.parse_proc_stat("7 (python3) S 1 1 1\n")
    assert "delayacct_blkio_ticks" not in parsed


def test_delayacct_enabled_reads_the_sysctl(tmp_path):
    proc = tmp_path / "proc"
    (proc / "sys" / "kernel").mkdir(parents=True)
    paths = io_attrib.Paths(proc=proc)
    assert io_attrib.delayacct_enabled(paths) is None  # not there: not measured
    (proc / "sys" / "kernel" / "task_delayacct").write_text("0\n")
    assert io_attrib.delayacct_enabled(paths) is False
    (proc / "sys" / "kernel" / "task_delayacct").write_text("1\n")
    assert io_attrib.delayacct_enabled(paths) is True


def test_delayacct_note_never_lets_a_missing_reading_read_as_zero():
    off = io_attrib.delayacct_note(False)
    assert "absent" in off and "not zero" in off
    assert "blkio_wait_ms" in io_attrib.delayacct_note(True)
    assert "unreadable" in io_attrib.delayacct_note(None)


def test_sample_window_counts_d_state_across_the_window(tmp_path):
    """The burst that ends before t1 is the entire reason this exists."""
    paths = _bare_paths(tmp_path, {7: {"stat": _field_line(7, "px-alive", "S")}})
    stat = paths.proc / "7" / "stat"
    states = iter(["D", "D", "S", "S"])

    def step(_seconds):
        stat.write_text(_field_line(7, "px-alive", next(states, "S")))

    window = io_attrib.sample_window(
        paths, [7], samples=4, interval_s=0.5, sleep=step
    )
    entry = window["pids"][7]
    assert window["samples"] == 4
    assert window["span_s"] > 0
    assert entry["samples"] == 4
    assert entry["d_samples"] == 2  # samples 2 and 3, invisible to both ends
    assert entry["comm"] == "px-alive"


def test_sample_window_records_which_samples_were_blocked(tmp_path):
    """A count cannot say *when* — and when is what decides who shared a resource."""
    paths = _bare_paths(tmp_path, {7: {"stat": _field_line(7, "px-alive", "S")}})
    stat = paths.proc / "7" / "stat"
    states = iter(["D", "D", "S", "S"])

    def step(_seconds):
        stat.write_text(_field_line(7, "px-alive", next(states, "S")))

    window = io_attrib.sample_window(
        paths, [7], samples=4, interval_s=0.5, sleep=step
    )
    assert window["pids"][7]["d_at"] == [1, 2]


def test_blocked_by_sample_separates_co_blocking_from_two_lone_blocks():
    """One `mmc` bus, one journal: *together* is the evidence, not the tally."""
    window = {
        "samples": 3,
        "pids": {
            92: {"comm": "kworker/u21:0+brcmf_wq/mmc1:0001:1", "samples": 3,
                 "d_samples": 1, "thread_d_samples": 0, "d_at": [1],
                 "thread_d_at": [], "wchan": {}, "thread_wchan": {}},
            217: {"comm": "jbd2/mmcblk0p2-8", "samples": 3, "d_samples": 1,
                  "thread_d_samples": 0, "d_at": [1], "thread_d_at": [],
                  "wchan": {}, "thread_wchan": {}},
            7: {"comm": "lonely", "samples": 3, "d_samples": 1,
                "thread_d_samples": 0, "d_at": [2], "thread_d_at": [],
                "wchan": {}, "thread_wchan": {}},
        },
    }
    per_sample = io_attrib.blocked_by_sample(window)
    assert len(per_sample) == 3
    assert [row["pid"] for row in per_sample[0]] == []
    assert [row["pid"] for row in per_sample[1]] == [92, 217]
    assert [row["pid"] for row in per_sample[2]] == [7]
    assert io_attrib.co_blocked_sample_count(window) == 1


def test_blocked_by_sample_counts_a_blocked_thread_as_blocked(tmp_path):
    window = {
        "samples": 2,
        "pids": {
            42: {"comm": "px-alive", "samples": 2, "d_samples": 0,
                 "thread_d_samples": 1, "d_at": [], "thread_d_at": [1],
                 "wchan": {}, "thread_wchan": {}},
            217: {"comm": "jbd2/mmcblk0p2-8", "samples": 2, "d_samples": 1,
                  "thread_d_samples": 0, "d_at": [1], "thread_d_at": [],
                  "wchan": {}, "thread_wchan": {}},
        },
    }
    assert io_attrib.co_blocked_sample_count(window) == 1
    assert [row["pid"] for row in io_attrib.blocked_by_sample(window)[1]] == [42, 217]


def test_sample_window_records_a_symbol_only_where_the_kernel_gives_one(tmp_path):
    paths = _bare_paths(
        tmp_path,
        {
            7: {
                "stat": _field_line(7, "python3", "D"),
                "wchan": "jbd2_log_wait_commit",
            }
        },
    )
    window = io_attrib.sample_window(paths, [7], samples=2, interval_s=0.5)
    assert window["pids"][7]["wchan"] == {"jbd2_log_wait_commit": 2}


def test_sample_window_thread_walk_is_rotating_and_bounded(tmp_path, monkeypatch):
    """A full per-thread walk every sample would be the observer as the writer."""
    pids = list(range(1, 9))
    paths = _bare_paths(
        tmp_path, {pid: {"stat": _field_line(pid, "worker")} for pid in pids}
    )
    seen: list[int] = []
    monkeypatch.setattr(
        io_attrib, "blocked_threads", lambda pid, pid_dir: seen.append(pid) or []
    )
    window = io_attrib.sample_window(
        paths, pids, samples=2, interval_s=0.5, sleep=lambda _s: None
    )
    # 8 pids over 4 buckets, in order: [1,5] on the first sample, [2,6] on the second.
    assert seen == [1, 5, 2, 6]
    assert window["thread_coverage_s"] == 2.0  # a full rotation takes 4 samples


def test_sample_window_watches_a_known_blocked_thread_every_sample(tmp_path):
    """#287's shape: leader S, health thread D — the pid is carried, not rotated."""
    pids = list(range(1, 9))
    procs = {pid: {"stat": _field_line(pid, "worker")} for pid in pids}
    procs[8] = {
        "stat": _field_line(8, "px-alive", "S"),
        "threads": {9: {"stat": _field_line(9, "px-alive", "D")}},
    }
    paths = _bare_paths(tmp_path, procs)
    window = io_attrib.sample_window(
        paths, pids, samples=2, interval_s=0.5, thread_pids=[8], sleep=lambda _s: None
    )
    entry = window["pids"][8]
    assert entry["d_samples"] == 0  # the leader never enters D
    assert entry["thread_d_samples"] == 2  # the thread is stuck throughout


def test_rank_blocked_in_window_marks_a_withheld_wchan(tmp_path):
    """`None` means two different things, and the record must not blur them."""
    window = {
        "samples": 4,
        "pids": {
            42: {"comm": "px-alive", "samples": 4, "d_samples": 4,
                 "thread_d_samples": 0, "wchan": {}, "thread_wchan": {}},
            7: {"comm": "python3", "samples": 4, "d_samples": 2, "thread_d_samples": 0,
                "wchan": {"jbd2_log_wait_commit": 2}, "thread_wchan": {}},
            9: {"comm": "arecord", "samples": 4, "d_samples": 1,
                "thread_d_samples": 0, "wchan": {}, "thread_wchan": {}},
        },
    }
    rows, count = io_attrib.rank_blocked_in_window(
        window, {42: {"comm": "px-alive"}}, {42}, top=8
    )
    assert count == 3
    assert [row["pid"] for row in rows] == [42, 7, 9]
    assert rows[0]["comm"] == "px-alive"
    assert rows[0]["d_share"] == 1.0
    assert rows[0]["wchan"] is None and rows[0]["wchan_withheld"] is True
    assert rows[1]["wchan"] == "jbd2_log_wait_commit"
    assert rows[1]["wchan_withheld"] is False
    # Blocked, no symbol, and *not* hidden from us: the kernel has no name for it.
    assert rows[2]["wchan"] is None and rows[2]["wchan_withheld"] is False


def test_rank_blocked_in_window_prefers_a_wedged_thread_over_a_flicker():
    window = {
        "samples": 4,
        "pids": {
            1: {"comm": "flicker", "samples": 4, "d_samples": 1,
                "thread_d_samples": 0, "wchan": {}, "thread_wchan": {}},
            2: {"comm": "px-alive", "samples": 4, "d_samples": 0,
                "thread_d_samples": 4, "wchan": {}, "thread_wchan": {}},
        },
    }
    rows, _ = io_attrib.rank_blocked_in_window(window, {})
    assert [row["pid"] for row in rows] == [2, 1]
    assert rows[0]["wchan_withheld"] is False  # nothing was measured as denied


def test_rank_blocked_in_window_reports_delayacct_wait_only_when_present():
    base = {"comm": "px-alive", "samples": 2, "d_samples": 2, "thread_d_samples": 0,
            "wchan": {}, "thread_wchan": {}}
    ticks = {"samples": 2, "pids": {7: {**base, "blkio_ticks_first": 100,
                                        "blkio_ticks_last": 250}}}
    rows, _ = io_attrib.rank_blocked_in_window(
        ticks, {7: {"comm": "px-alive"}}, delayacct_on=True
    )
    assert rows[0]["blkio_wait_ms"] == round(150 * io_attrib._tick_ms(), 1)
    # Present-and-zero in /proc/<pid>/stat on a kernel that is not accounting
    # for it: a delta emitted anyway would read as a measured "waited 0 ms".
    zeroed, _ = io_attrib.rank_blocked_in_window(
        {"samples": 2, "pids": {7: {**base, "blkio_ticks_first": 0,
                                    "blkio_ticks_last": 0}}},
        {7: {"comm": "px-alive"}},
        delayacct_on=False,
    )
    assert "blkio_wait_ms" not in zeroed[0]
    absent, _ = io_attrib.rank_blocked_in_window(
        {"samples": 2, "pids": {7: {**base, "blkio_ticks_first": None,
                                    "blkio_ticks_last": None}}},
        {7: {"comm": "px-alive"}},
        delayacct_on=True,
    )
    assert "blkio_wait_ms" not in absent[0]


def test_wchan_withheld_pids_counts_past_the_reporting_limit():
    """A count that silently inherits the `top` limit is worse than no count."""
    window = {
        "samples": 1,
        "pids": {pid: {"comm": "x", "samples": 1, "d_samples": 1,
                       "thread_d_samples": 0, "wchan": {}, "thread_wchan": {}}
                 for pid in range(1, 13)},
    }
    procs_post = {pid: {"comm": "x", "state": "D", "io_denied": True}
                  for pid in range(1, 13)}
    rows, count = io_attrib.rank_blocked_in_window(
        window, procs_post, set(procs_post), top=3
    )
    assert len(rows) == 3 and count == 12
    withheld = io_attrib.wchan_withheld_pids(procs_post, set(procs_post), window)
    assert len(withheld) == 12
    assert "withheld" in io_attrib.wchan_unavailable_reason(len(withheld))
    assert io_attrib.wchan_unavailable_reason(0) is None


def test_capture_records_a_root_owned_writer_through_the_window_channel(
    tmp_path, monkeypatch
):
    """The headline claim: an unreadable writer is still a *named* writer here."""
    procs = {
        42: {
            "comm": "px-alive",
            "state": "S",
            "io_denied": True,  # what /proc/<pid>/io did, measured
            "cgroup": "0::/system.slice/px-alive.service\n",
            "threads": {43: {"comm": "px-alive", "state": "D", "wchan": "0"}},
        },
        7: {"comm": "python3", "state": "S", "io": {"write_bytes": 0}},
    }
    paths = _wire_capture(tmp_path, monkeypatch, procs, procs)
    record = io_attrib.capture(
        {"reason": "heartbeat_age"},
        paths=paths,
        window_s=3.0,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert record["window_samples"] == 6
    assert record["window_sample_interval_s"] == 0.5
    assert record["blocked_in_window_count"] == 1
    assert len(record["blocked_at_samples"]) == record["window_samples"]
    assert record["co_blocked_samples"] == 0
    row = record["blocked_in_window"][0]
    assert row["pid"] == 42 and row["comm"] == "px-alive"
    assert row["d_samples"] == 0  # the leader never enters D ...
    assert row["thread_d_samples"] >= 1  # ... the health thread is what is stuck
    assert row["unit"] == "px-alive.service"
    assert row["wchan"] is None and row["wchan_withheld"] is True
    assert record["wchan_withheld_count"] == 1
    assert "withheld" in record["wchan_unavailable_reason"]
    assert "the kernel answers" in record["wchan_unavailable_reason"]
    assert record["delayacct"]["enabled"] is None
    assert "unreadable" in record["delayacct"]["note"]


def test_capture_says_nothing_about_withheld_wchan_when_none_were(tmp_path, monkeypatch):
    procs = {7: {"comm": "python3", "state": "D", "io": {"write_bytes": 0}}}
    paths = _wire_capture(tmp_path, monkeypatch, procs, procs)
    record = io_attrib.capture(
        {"reason": "io_psi"},
        paths=paths,
        window_s=1.0,
        monotonic=_monotonic(),
        sleep=lambda _s: None,
    )
    assert record["wchan_withheld_count"] == 0
    assert record["wchan_unavailable_reason"] is None
    assert record["blocked_in_window"][0]["wchan_withheld"] is False
