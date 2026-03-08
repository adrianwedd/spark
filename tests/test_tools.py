import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_tool(args, env):
    """Helper to run a tool with a specific environment."""
    result = subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def parse_json(output: str):
    return json.loads(output.splitlines()[-1])


def test_tool_status_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-status"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True


def test_tool_circle_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_SPEED"] = "26"
    env["PX_DURATION"] = "3"
    stdout = run_tool(["bin/tool-circle"], env)
    payload = parse_json(stdout)
    assert payload["dry"] is True
    assert payload["speed"] == 26
    assert payload["duration"] == 3.0


def test_tool_figure8_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_SPEED"] = "27"
    env["PX_DURATION"] = "3"
    env["PX_REST"] = "0.5"
    stdout = run_tool(["bin/tool-figure8"], env)
    payload = parse_json(stdout)
    assert payload["dry"] is True
    assert payload["speed"] == 27
    assert payload["rest"] == 0.5


def test_tool_stop_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-stop"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True


def test_tool_voice_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_TEXT"] = "Hello PiCar-X"
    stdout = run_tool(["bin/tool-voice"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True


def test_tool_weather_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-weather"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "dry-run"
    assert "Dry-run" in payload["summary"]

def test_tool_photograph_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-photograph"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert payload["size_bytes"] == 0


def test_tool_qa_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_TEXT"] = "The sky is blue."
    stdout = run_tool(["bin/tool-qa"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True


def test_tool_play_sound_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_SOUND"] = "chime"
    stdout = run_tool(["bin/tool-play-sound"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["sound"] == "chime"
    assert payload["dry"] is True


def test_tool_play_sound_invalid_name(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_SOUND"] = "explosion"
    result = subprocess.run(
        ["bin/tool-play-sound"],
        cwd=PROJECT_ROOT, text=True, capture_output=True, check=False, env=env,
    )
    payload = parse_json(result.stdout.strip())
    assert payload["status"] == "error"


def test_tool_face_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-face"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert payload["angle"] == 0


def test_tool_describe_scene_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-describe-scene"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert len(payload["description"]) > 0


def test_tool_timer_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_TIMER_SECONDS"] = "10"
    env["PX_TIMER_LABEL"] = "pasta"
    stdout = run_tool(["bin/tool-timer"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["seconds"] == 10
    assert payload["label"] == "pasta"
    assert "timer_id" in payload
    assert "pid" in payload


def test_tool_wander_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_WANDER_STEPS"] = "2"
    stdout = run_tool(["bin/tool-wander"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["steps"] == 2
    assert payload["dry"] is True


def test_tool_chat_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_TEXT"] = "how are you feeling"
    stdout = run_tool(["bin/tool-chat"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert "model" in payload


def test_tool_chat_missing_text(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env.pop("PX_TEXT", None)
    result = subprocess.run(
        ["bin/tool-chat"],
        cwd=PROJECT_ROOT, text=True, capture_output=True, check=False, env=env,
    )
    payload = parse_json(result.stdout.strip())
    assert payload["status"] == "error"


def test_px_wake_set_and_pulse(isolated_project):
    env = isolated_project["env"]
    session_path = isolated_project["session_path"]

    run_tool(["bin/px-wake", "--set", "on"], env)
    data = json.loads(session_path.read_text())
    assert data["listening"] is True

    run_tool(["bin/px-wake", "--set", "off"], env)
    data = json.loads(session_path.read_text())
    assert data["listening"] is False

    run_tool(["bin/px-wake", "--pulse", "0.1"], env)
    data = json.loads(session_path.read_text())
    assert data["listening"] is False