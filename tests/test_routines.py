import json
import os
import subprocess
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
    sensors = next(check for check in summary["checks"] if check["name"] == "sensors")
    assert sensors["raw"].startswith("PiCar-X Telemetry Snapshot")
    assert '\\"stdout\\"' not in sensors["raw"]


def test_px_voice_test_has_valid_shell_syntax():
    subprocess.run(
        ["bash", "-n", "bin/px-voice-test"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

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
