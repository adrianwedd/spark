from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _start_fake_alive(log_dir: Path) -> subprocess.Popen:
    code = r"""
import os
import signal
import sys
import time
from pathlib import Path

log_dir = Path(sys.argv[1])
pid_file = log_dir / "px-alive.pid"
paused_file = log_dir / "px-alive.paused"

def pause(_sig, _frame):
    paused_file.write_text(str(os.getpid()))

def resume(_sig, _frame):
    paused_file.unlink(missing_ok=True)

def stop(_sig, _frame):
    paused_file.unlink(missing_ok=True)
    pid_file.unlink(missing_ok=True)
    raise SystemExit(0)

signal.signal(signal.SIGUSR1, pause)
signal.signal(signal.SIGUSR2, resume)
signal.signal(signal.SIGTERM, stop)
pid_file.write_text(str(os.getpid()))
while True:
    time.sleep(0.05)
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(log_dir)])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if (log_dir / "px-alive.pid").exists():
            return process
        time.sleep(0.02)
    process.terminate()
    raise AssertionError("fake px-alive did not start")


def test_gpio_lease_pauses_and_resumes_same_alive_process(tmp_path):
    process = _start_fake_alive(tmp_path)
    env = os.environ.copy()
    env.update({
        "LOG_DIR": str(tmp_path),
        "PX_GPIO_LOCK_TIMEOUT": "2",
        "PX_DRY": "0",
    })
    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                "source bin/px-env; yield_alive; "
                "test \"$(cat \"$LOG_DIR/px-alive.paused\")\" = "
                "\"$(cat \"$LOG_DIR/px-alive.pid\")\"",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert process.poll() is None

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and (tmp_path / "px-alive.paused").exists():
            time.sleep(0.02)
        assert not (tmp_path / "px-alive.paused").exists()
        assert process.poll() is None
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=3)


def test_dry_run_does_not_signal_alive_or_create_lease(tmp_path):
    process = _start_fake_alive(tmp_path)
    env = os.environ.copy()
    env.update({"LOG_DIR": str(tmp_path), "PX_DRY": "1"})
    try:
        result = subprocess.run(
            ["bash", "-c", "source bin/px-env; yield_alive --dry-run"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=3,
        )
        assert result.returncode == 0
        assert not (tmp_path / "px-alive.paused").exists()
        assert not (tmp_path / "gpio-owner.lock").exists()
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=3)
