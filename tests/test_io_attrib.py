"""#247's narrowed ask, tested: sample at the stall, rank writers, and say what
could not be read.

The fixtures are synthetic /proc trees, never the host's. The reader has to work
on a machine with no /proc at all (this repo is developed off-robot), and a test
that read the real /proc/*/io would depend on root, on which processes happen to
be running, and on the stall it is meant to observe.
"""

from __future__ import annotations

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
    return io_attrib.Paths(
        proc=proc,
        pressure_io=tmp_path / "pressure_io",
        diskstats=tmp_path / "diskstats",
        vmstat=tmp_path / "vmstat",
        uptime=tmp_path / "uptime",
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
):
    """Two-ended fixture: process walk and device counters both advance once."""
    paths = _write_proc_tree(
        tmp_path,
        procs,
        self_io={"read_bytes": 0, "write_bytes": 0},
        diskstats=diskstats,
        vmstat=vmstat,
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

    real_sample = io_attrib.sample_file_sizes
    calls = {"n": 0}

    def growing(patterns, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_sample(patterns, **kwargs)
        (watched / "px-mind.log").write_text("x" * 1500)
        return real_sample(patterns, **kwargs)

    monkeypatch.setattr(io_attrib, "sample_file_sizes", growing)
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
