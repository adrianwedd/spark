import io
import json
import os
import shlex
import subprocess
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(cmd, extra_env=None):
    env = os.environ.copy()
    env.setdefault("PROJECT_ROOT", str(PROJECT_ROOT))
    env.setdefault("PX_BYPASS_SUDO", "1")
    env.setdefault("PX_VOICE_DEVICE", "null")
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    return json.loads(result.stdout.strip())

def test_px_diagnostics_dry_run(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    env = {
        "PX_DRY": "1",
        "PX_SESSION_PATH": str(tmp_path / "session.json"),
        "LOG_DIR": str(log_dir),
    }
    summary = run(["bin/px-diagnostics", "--no-motion", "--short"], env)
    assert summary["status"] == "ok"
    assert summary["dry"] is True
    names = [check["name"] for check in summary["checks"]]
    assert "status" in names
    assert "sensors" in names
    assert "speaker" in names
    assert "microphone" in names

def test_px_dance_dry_run(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    env = {
        "PX_DRY": "1",
        "PX_SESSION_PATH": str(tmp_path / "session.json"),
        "LOG_DIR": str(log_dir),
    }
    summary = run(["bin/px-dance", "--voice", "Demo"], env)
    assert summary["status"] == "ok"
    assert summary["dry"] is True
    names = [entry["name"] for entry in summary["sequence"]]
    assert names[0] == "voice" and "circle" in names and "figure8" in names


def test_px_frigate_stream_dry_run(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    env = {
        "PX_DRY": "1",
        "PROJECT_ROOT": str(PROJECT_ROOT),
        "LOG_DIR": str(log_dir),
    }
    result = subprocess.run(
        ["bin/px-frigate-stream", "--host", "example.local", "--stream", "test", "--dry-run"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
        env={**{k: v for k, v in os.environ.items()}, **env},
    )
    payload = json.loads(result.stdout.strip())
    assert payload["status"] == "dry-run"
    assert "camera" in payload["commands"]
    assert payload["commands"]["ffmpeg"][-1].endswith("test")

    # The dry-run payload and the go2rtc exec: source were once two
    # hand-maintained copies of the same command line, and they drifted: only
    # the config is ever executed, so dry-run was describing a command that
    # wasn't the one running. Pin them to the same source.
    exec_source = payload["go2rtc_config"]["streams"]["test"][0]
    assert exec_source == "exec:" + shlex.join(payload["commands"]["camera"])

    # Without --bitrate, rpicam-vid runs at its ~10 Mbps default, which Frigate
    # records verbatim at ~58 GB/day and fills the recorder's disk.
    assert "--bitrate" in payload["commands"]["camera"]


def _load_frigate_stream_module():
    """Exec px-frigate-stream's embedded Python heredoc as a module.

    The script is bash wrapping a `python3 - <<'PY'` heredoc, and px-env
    unconditionally overwrites PROJECT_ROOT with the real repo root — so
    GO2RTC_BIN cannot be redirected at a fake binary from the outside, and the
    go2rtc-exits-early path is unreachable via subprocess. Executing the
    heredoc directly is what makes that path testable at all.
    """
    src = (PROJECT_ROOT / "bin" / "px-frigate-stream").read_text(encoding="utf-8")
    body = src.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    module = types.ModuleType("px_frigate_stream")
    module.__dict__["__name__"] = "px_frigate_stream"
    exec(compile(body, "px-frigate-stream", "exec"), module.__dict__)
    return module


def test_frigate_stream_reports_go2rtc_startup_failure(tmp_path, monkeypatch, capsys):
    """An early go2rtc exit must surface go2rtc's stderr, not a NameError.

    `finally` reads start_ts on every path out, including this `return 1`.
    Binding start_ts only on the success path made the failure path raise
    NameError from finally — masking the one message you need to diagnose why
    go2rtc would not start.
    """
    module = _load_frigate_stream_module()
    monkeypatch.setattr(module, "PID_FILE", tmp_path / "px-frigate-stream.pid")
    monkeypatch.setattr(module, "GO2RTC_BIN", tmp_path / "go2rtc")
    (tmp_path / "go2rtc").write_text("#!/bin/sh\nexit 1\n")

    class _DeadProc:
        stderr = io.BytesIO(b"bind: address already in use")
        stdout = io.BytesIO(b"")

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 1

        def kill(self):
            pass

    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: _DeadProc())
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(sys, "argv", ["px-frigate-stream", "--host", "example.local"])

    assert module.main() == 1
    assert "address already in use" in capsys.readouterr().err
