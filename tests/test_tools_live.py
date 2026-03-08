"""Live tool tests — run on actual hardware (no PX_DRY).

Requires:
  - PiCar-X hardware connected (servos, sonar, camera, speaker)
  - wheels_on_blocks=true and confirm_motion_allowed=true in session
  - px-alive systemd service running (tests will yield it via SIGUSR1)
  - sudo access (PX_BYPASS_SUDO is NOT set)

Run with:  sudo .venv/bin/python -m pytest tests/test_tools_live.py -v -s
Skip with: pytest -m "not live"
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Mark every test in this module as "live" so they can be selected/skipped
pytestmark = pytest.mark.live


def _is_hardware_available():
    """Quick probe: can we reach the robot_hat MCU on I2C?"""
    try:
        result = subprocess.run(
            ["sudo", "-n", "/usr/bin/python3", "-c",
             "import smbus2; bus = smbus2.SMBus(1); bus.read_byte(0x14); print('ok')"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


# Skip the entire module if hardware is not available
_hw_ok = _is_hardware_available()
if not _hw_ok:
    pytestmark = [pytestmark, pytest.mark.skip(reason="PiCar-X hardware not available")]


def live_env(**extra):
    """Build a live (non-dry) environment for tool execution."""
    env = os.environ.copy()
    env["PROJECT_ROOT"] = str(PROJECT_ROOT)
    env.pop("PX_DRY", None)  # ensure NOT dry
    env.update(extra)
    return env


def run_tool(args, env, check=True, timeout=30):
    """Run a tool and return parsed JSON payload.

    For sudo commands, passes PX_* env vars through explicitly since
    sudo strips the environment by default.
    """
    if args and args[0] == "sudo":
        # Build sudo command with env var passthrough
        px_vars = {k: v for k, v in env.items()
                   if k.startswith("PX_") or k in (
                       "PROJECT_ROOT", "LOG_DIR", "HOME", "PATH",
                       "PYTHONPATH", "VIRTUAL_ENV",
                   )}
        sudo_args = ["sudo"]
        for k, v in px_vars.items():
            sudo_args.append(f"{k}={v}")
        sudo_args.extend(args[1:])
        args = sudo_args

    result = subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
        env=env,
    )
    stdout = result.stdout.strip()
    if not stdout:
        return {"_rc": result.returncode, "_stderr": result.stderr}
    # Take last JSON line
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    return {"_raw": stdout, "_rc": result.returncode}


# ── Non-GPIO tools (no sudo needed) ────────────────────────────


class TestNonGpioLive:

    def test_tool_status(self):
        payload = run_tool(["bin/tool-status"], live_env())
        assert payload["status"] == "ok"
        assert payload["dry"] is False

    def test_tool_time(self):
        payload = run_tool(["bin/tool-time"], live_env())
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert "spoken" in payload
        assert payload["voice_rc"] == 0

    def test_tool_weather(self):
        payload = run_tool(["bin/tool-weather"], live_env(), timeout=20)
        assert payload["status"] == "ok"
        assert "temp_C" in payload
        assert "summary" in payload

    def test_tool_voice(self):
        payload = run_tool(
            ["bin/tool-voice"],
            live_env(PX_TEXT="Live test voice"),
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert payload["returncode"] == 0

    def test_tool_qa(self):
        payload = run_tool(
            ["bin/tool-qa"],
            live_env(PX_TEXT="Live test QA output"),
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False

    def test_tool_play_sound(self):
        payload = run_tool(
            ["bin/tool-play-sound"],
            live_env(PX_SOUND="beep"),
        )
        assert payload["status"] == "ok"
        assert payload["sound"] == "beep"
        assert payload["dry"] is False

    def test_tool_recall(self):
        payload = run_tool(["bin/tool-recall"], live_env(), timeout=45)
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert "notes" in payload
        assert "spoken" in payload

    def test_tool_timer(self):
        payload = run_tool(
            ["bin/tool-timer"],
            live_env(PX_TIMER_SECONDS="5", PX_TIMER_LABEL="test"),
            timeout=45,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert payload["seconds"] == 5  # min clamp is 5
        assert payload["label"] == "test"
        assert "timer_id" in payload
        assert "pid" in payload
        # Wait for it to finish so it doesn't leave orphan processes
        time.sleep(6)


# ── GPIO tools (need sudo, yield px-alive) ──────────────────────


class TestGpioLive:

    def test_tool_look(self):
        payload = run_tool(
            ["sudo", "bin/tool-look"],
            live_env(PX_PAN="20", PX_TILT="10"),
            timeout=15,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert payload["pan"] == 20
        assert payload["tilt"] == 10

    def test_tool_look_returns_to_center(self):
        payload = run_tool(
            ["sudo", "bin/tool-look"],
            live_env(PX_PAN="0", PX_TILT="0"),
            timeout=15,
        )
        assert payload["status"] == "ok"
        assert payload["pan"] == 0
        assert payload["tilt"] == 0

    def test_tool_sonar(self):
        payload = run_tool(
            ["sudo", "bin/tool-sonar"],
            live_env(),
            timeout=20,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert "closest_cm" in payload
        assert "closest_angle" in payload
        assert "readings" in payload
        assert len(payload["readings"]) >= 1
        # Distances should be positive
        for angle, dist in payload["readings"]:
            assert dist > 0

    def test_tool_emote_curious(self):
        payload = run_tool(
            ["sudo", "bin/tool-emote"],
            live_env(PX_EMOTE="curious"),
            timeout=15,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert payload["emote"] == "curious"

    def test_tool_emote_happy(self):
        payload = run_tool(
            ["sudo", "bin/tool-emote"],
            live_env(PX_EMOTE="happy"),
            timeout=15,
        )
        assert payload["status"] == "ok"
        assert payload["emote"] == "happy"

    def test_tool_drive_forward(self):
        payload = run_tool(
            ["sudo", "bin/tool-drive"],
            live_env(PX_DIRECTION="forward", PX_SPEED="20", PX_DURATION="1"),
            timeout=15,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert payload["direction"] == "forward"
        assert payload["speed"] == 20

    def test_tool_drive_backward(self):
        payload = run_tool(
            ["sudo", "bin/tool-drive"],
            live_env(PX_DIRECTION="backward", PX_SPEED="20", PX_DURATION="1"),
            timeout=15,
        )
        assert payload["status"] == "ok"
        assert payload["direction"] == "backward"

    def test_tool_stop(self):
        payload = run_tool(
            ["sudo", "bin/tool-stop"],
            live_env(),
            timeout=10,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False

    def test_tool_circle(self):
        payload = run_tool(
            ["sudo", "bin/tool-circle"],
            live_env(PX_SPEED="25", PX_DURATION="3"),
            timeout=20,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False

    def test_tool_figure8(self):
        payload = run_tool(
            ["sudo", "bin/tool-figure8"],
            live_env(PX_SPEED="25", PX_DURATION="3", PX_REST="0.5"),
            timeout=25,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False

    def test_tool_perform(self):
        steps = json.dumps([
            {"emote": "curious", "speak": "Live test", "pause": 0.3},
            {"look": {"pan": 15, "tilt": 5}, "speak": "Looking around"},
            {"emote": "happy"},
        ])
        payload = run_tool(
            ["sudo", "bin/tool-perform"],
            live_env(PX_PERFORM_STEPS=steps),
            timeout=20,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert payload["steps"] == 3

    def test_tool_face(self):
        payload = run_tool(
            ["sudo", "bin/tool-face"],
            live_env(),
            timeout=25,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert "angle" in payload
        assert "closest_cm" in payload

    def test_tool_wander(self):
        payload = run_tool(
            ["sudo", "bin/tool-wander"],
            live_env(PX_WANDER_STEPS="2"),
            timeout=40,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert payload["steps"] == 2

    def test_tool_photograph(self):
        payload = run_tool(
            ["sudo", "bin/tool-photograph"],
            live_env(),
            timeout=15,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert payload["size_bytes"] > 0
        # Verify the file exists
        assert Path(payload["path"]).exists()

    def test_tool_describe_scene(self):
        payload = run_tool(
            ["sudo", "bin/tool-describe-scene"],
            live_env(),
            timeout=45,
        )
        assert payload["status"] == "ok"
        assert payload["dry"] is False
        assert "description" in payload


# ── API tools ───────────────────────────────────────────────────


class TestApiLive:

    def test_api_start_stop_cycle(self):
        # Start
        start = run_tool(["bin/tool-api-start"], live_env(), timeout=10)
        assert start["status"] in ("ok", "already_running")
        if start["status"] == "ok":
            assert "pid" in start
            time.sleep(2)  # let it bind

        # Stop
        stop = run_tool(["bin/tool-api-stop"], live_env(), timeout=10)
        assert stop["status"] == "ok"
        assert stop["stopped"] is True

    def test_api_stop_when_not_running(self):
        # Make sure it's stopped first
        run_tool(["bin/tool-api-stop"], live_env(), check=False, timeout=10)
        time.sleep(1)
        payload = run_tool(["bin/tool-api-stop"], live_env(), timeout=10)
        assert payload["status"] == "not_running"
