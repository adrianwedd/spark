"""The observer must not be able to stop silently (#247).

The instrument that measures silent stalls was itself silently stopped for an
hour on 2026-09-18 (03:57 → 04:55) because it is a hand-started process until the
root unit is installed. `bin/px-io-attrib-ensure` is the interim: cron calls it,
it starts the observer when the observer is not running, and it says nothing —
and writes nothing — when it is.

The liveness check is the part worth testing, because "the pid file exists" is
not the same claim as "the observer is running": a stale file, or a reused pid,
would keep the observer *down* while this script reported everything fine.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENSURE = ROOT / "bin" / "px-io-attrib-ensure"
HAS_PROC = Path("/proc").is_dir()


def _stub(tmp_path) -> Path:
    """A stand-in for the observer: records its argv and exits."""
    marker = tmp_path / "observer-argv.txt"
    stub = tmp_path / "stub-observer"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" > "{marker}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _run(tmp_path, *, pid: int | None, stub: Path | None = None):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    if pid is not None:
        (log_dir / "px-io-attrib.pid").write_text(f"{pid}\n", encoding="utf-8")
    env = {
        **os.environ,
        "LOG_DIR": str(log_dir),
        "PX_IO_ATTRIB_CMD": str(stub or _stub(tmp_path)),
    }
    return subprocess.run(
        [str(ENSURE)], env=env, capture_output=True, text=True, timeout=60
    )


def _wait_for(path: Path, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def test_it_starts_the_observer_when_nothing_is_running(tmp_path):
    """Absent pid file: start one, and log the transition rather than silence."""
    proc = _run(tmp_path, pid=None)
    assert proc.returncode == 0, proc.stderr
    assert "started it" in proc.stdout
    assert "absent" in proc.stdout
    marker = tmp_path / "observer-argv.txt"
    assert _wait_for(marker), "the observer stub was never launched"
    # The observer is launched *as the observer*, with its threshold — not as a
    # bare process an operator would have to guess about later.
    assert "--io-threshold 25" in marker.read_text(encoding="utf-8")


def test_it_passes_the_threshold_through(tmp_path):
    proc = subprocess.run(
        [str(ENSURE)],
        env={
            **os.environ,
            "LOG_DIR": str(tmp_path / "logs2"),
            "PX_IO_ATTRIB_CMD": str(_stub(tmp_path)),
            "PX_IO_ATTRIB_IO_THRESHOLD": "10",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "10%" in proc.stdout
    assert _wait_for(tmp_path / "observer-argv.txt")
    assert "--io-threshold 10" in (tmp_path / "observer-argv.txt").read_text(
        encoding="utf-8"
    )


def test_it_says_nothing_and_starts_nothing_when_the_observer_is_up(tmp_path):
    """The healthy case is silent — no log line, no second process."""
    live = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "--io-threshold", "25"]
    )
    try:
        proc = _run(tmp_path, pid=live.pid)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "", "a healthy minute must not write a log line"
        assert not (tmp_path / "observer-argv.txt").exists()
    finally:
        live.terminate()
        live.wait(timeout=10)


def test_a_pid_file_is_not_a_lock(tmp_path):
    """A live pid that is not the observer must not keep the observer down."""
    if not HAS_PROC:
        pytest.skip("the argv check is the /proc path; macOS uses the kill-0 probe")
    live = subprocess.Popen(["/bin/sleep", "30"])
    try:
        proc = _run(tmp_path, pid=live.pid)
        assert proc.returncode == 0, proc.stderr
        assert "started it" in proc.stdout
        assert _wait_for(tmp_path / "observer-argv.txt")
    finally:
        live.terminate()
        live.wait(timeout=10)


def test_it_starts_the_observer_when_the_pid_file_is_stale(tmp_path):
    """A crashed observer leaves a pid file behind; it must not be trusted."""
    proc = _run(tmp_path, pid=999_999)
    assert proc.returncode == 0, proc.stderr
    assert "started it" in proc.stdout
    assert _wait_for(tmp_path / "observer-argv.txt")


def test_the_healthy_path_touches_nothing_on_disk(tmp_path):
    """Every minute on an SD card: a script that rewrites a file is a writer."""
    live = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "--io-threshold", "25"]
    )
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    pid_file = log_dir / "px-io-attrib.pid"
    pid_file.write_text(f"{live.pid}\n", encoding="utf-8")
    env = {
        **os.environ,
        "LOG_DIR": str(log_dir),
        "PX_IO_ATTRIB_CMD": str(_stub(tmp_path)),
    }
    try:
        before = (pid_file.stat().st_mtime_ns, pid_file.read_bytes())
        for _ in range(3):  # three "minutes"
            assert (
                subprocess.run(
                    [str(ENSURE)], env=env, capture_output=True, text=True, timeout=60
                ).returncode
                == 0
            )
        after = (pid_file.stat().st_mtime_ns, pid_file.read_bytes())
        assert before == after, "the ensure script rewrote state on a healthy run"
        assert not (log_dir / "px-io-attrib.out").exists(), "nothing to append to"
        assert not (tmp_path / "observer-argv.txt").exists()
    finally:
        live.terminate()
        live.wait(timeout=10)
