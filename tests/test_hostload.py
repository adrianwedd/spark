import pxh.hostload as hostload


# -- host_load_fields --

def test_host_load_fields_preserves_existing_key_names():
    """load1_<prefix> and psi_cpu_avg10_<prefix> are consumed by existing
    production log analysis (#270/#283) — renaming either is a breaking
    change, not a refactor."""
    fields = hostload.host_load_fields("start")
    assert "load1_start" in fields
    assert "psi_cpu_avg10_start" in fields


def test_host_load_fields_adds_memory_io_swap_keys():
    fields = hostload.host_load_fields("end")
    for key in (
        "psi_mem_some_avg10_end",
        "psi_mem_full_avg10_end",
        "psi_io_some_avg10_end",
        "psi_io_full_avg10_end",
        "swap_free_kb_end",
    ):
        assert key in fields, f"missing {key}"


def test_host_load_fields_never_raises_when_proc_missing(monkeypatch):
    import pathlib

    real_read_text = pathlib.Path.read_text

    def broken_read_text(self, *a, **kw):
        if "/proc/" in str(self):
            raise OSError("no such file")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "read_text", broken_read_text)
    assert hostload.host_load_fields("x") == {}


def test_host_load_fields_values_are_floats():
    fields = hostload.host_load_fields("t")
    for value in fields.values():
        assert isinstance(value, float)


# -- _psi_avg10 --

def test_psi_avg10_parses_real_format(tmp_path):
    p = tmp_path / "cpu"
    p.write_text(
        "some avg10=1.23 avg60=0.50 avg300=0.10 total=12345\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    result = hostload._psi_avg10(p)
    assert result == {"some": 1.23, "full": 0.00}


def test_psi_avg10_missing_file_returns_empty(tmp_path):
    assert hostload._psi_avg10(tmp_path / "nope") == {}


# -- cgroup_pressure_fields --

def test_cgroup_pressure_fields_unknown_unit_raises():
    try:
        hostload.cgroup_pressure_fields("not-a-real-unit", "x")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_cgroup_pressure_fields_reads_mem_ratio(tmp_path, monkeypatch):
    unit_dir = tmp_path / "px-wake-listen.service"
    unit_dir.mkdir()
    (unit_dir / "memory.current").write_text("671088640\n")  # 640 MiB
    (unit_dir / "memory.high").write_text("671088640\n")     # 640 MiB -> ratio 1.0
    (unit_dir / "memory.events").write_text(
        "low 0\nhigh 795123\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"
    )
    monkeypatch.setitem(hostload._MONITORED_UNITS, "px-wake-listen", unit_dir)
    hostload._last_events_high.pop("px-wake-listen", None)

    fields = hostload.cgroup_pressure_fields("px-wake-listen", "at")
    assert fields["mem_ratio_px-wake-listen_at"] == 1.0
    assert fields["events_high_px-wake-listen_at"] == 795123.0
    assert fields["events_high_rate_px-wake-listen_at"] == 0.0  # first sample


def test_cgroup_pressure_fields_computes_rate_on_second_call(tmp_path, monkeypatch):
    unit_dir = tmp_path / "px-brain.service"
    unit_dir.mkdir()
    (unit_dir / "memory.current").write_text("100\n")
    (unit_dir / "memory.high").write_text("200\n")
    events_file = unit_dir / "memory.events"
    events_file.write_text("low 0\nhigh 10\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n")
    monkeypatch.setitem(hostload._MONITORED_UNITS, "px-brain", unit_dir)
    hostload._last_events_high.pop("px-brain", None)

    first = hostload.cgroup_pressure_fields("px-brain", "start")
    assert first["events_high_rate_px-brain_start"] == 0.0

    events_file.write_text("low 0\nhigh 25\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n")
    second = hostload.cgroup_pressure_fields("px-brain", "end")
    assert second["events_high_px-brain_end"] == 25.0
    assert second["events_high_rate_px-brain_end"] == 15.0


def test_cgroup_pressure_fields_omits_mem_ratio_when_high_is_max(tmp_path, monkeypatch):
    unit_dir = tmp_path / "px-wake-listen.service"
    unit_dir.mkdir()
    (unit_dir / "memory.current").write_text("100\n")
    (unit_dir / "memory.high").write_text("max\n")
    (unit_dir / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n")
    monkeypatch.setitem(hostload._MONITORED_UNITS, "px-wake-listen", unit_dir)
    hostload._last_events_high.pop("px-wake-listen", None)

    fields = hostload.cgroup_pressure_fields("px-wake-listen", "at")
    assert "mem_ratio_px-wake-listen_at" not in fields


def test_cgroup_pressure_fields_never_raises_when_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setitem(
        hostload._MONITORED_UNITS, "px-brain", tmp_path / "does-not-exist.service"
    )
    hostload._last_events_high.pop("px-brain", None)
    assert hostload.cgroup_pressure_fields("px-brain", "x") == {}
