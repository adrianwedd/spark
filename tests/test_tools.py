import json
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_tool_frigate_events_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-frigate-events"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert isinstance(payload["events"], list)
    assert len(payload["events"]) > 0
    assert "summary" in payload


def test_tool_frigate_events_unreachable(isolated_project):
    """tool-frigate-events should fail gracefully when Frigate is not reachable."""
    env = isolated_project["env"].copy()
    env["PX_FRIGATE_HOST"] = "http://127.0.0.1:19999"  # nothing listening there
    result = subprocess.run(
        ["bin/tool-frigate-events"],
        cwd=PROJECT_ROOT,
        capture_output=True, text=True, env=env, timeout=10,
    )
    payload = parse_json(result.stdout.strip())
    assert payload["status"] == "error"
    assert "frigate" in payload["error"].lower() or "reach" in payload["error"].lower()


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


def test_tool_chat_vixen_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_TEXT"] = "hello gorgeous"
    stdout = run_tool(["bin/tool-chat-vixen"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert payload["persona"] == "VIXEN"
    assert "model" in payload


def test_tool_chat_vixen_missing_text(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env.pop("PX_TEXT", None)
    result = subprocess.run(
        ["bin/tool-chat-vixen"],
        cwd=PROJECT_ROOT, text=True, capture_output=True, check=False, env=env,
    )
    payload = parse_json(result.stdout.strip())
    assert payload["status"] == "error"


def test_tool_look_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_PAN"] = "25"
    env["PX_TILT"] = "10"
    stdout = run_tool(["bin/tool-look"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert payload["pan"] == 25
    assert payload["tilt"] == 10


def test_tool_sonar_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-sonar"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert "closest_cm" in payload
    assert "readings" in payload


def test_tool_emote_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_EMOTE"] = "happy"
    stdout = run_tool(["bin/tool-emote"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert payload["emote"] == "happy"


def test_tool_emote_invalid_name(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_EMOTE"] = "rage"
    result = subprocess.run(
        ["bin/tool-emote"],
        cwd=PROJECT_ROOT, text=True, capture_output=True, check=False, env=env,
    )
    payload = parse_json(result.stdout.strip())
    assert payload["status"] == "error"
    assert "valid" in payload


def test_tool_drive_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_DIRECTION"] = "forward"
    env["PX_SPEED"] = "25"
    env["PX_DURATION"] = "2"
    env["PX_STEER"] = "10"
    stdout = run_tool(["bin/tool-drive"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert payload["direction"] == "forward"
    assert payload["speed"] == 25
    assert payload["duration"] == 2.0
    assert payload["steer"] == 10


def test_tool_drive_invalid_direction(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_DIRECTION"] = "sideways"
    result = subprocess.run(
        ["bin/tool-drive"],
        cwd=PROJECT_ROOT, text=True, capture_output=True, check=False, env=env,
    )
    payload = parse_json(result.stdout.strip())
    assert payload["status"] == "error"


def test_tool_time_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-time"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert "spoken" in payload
    assert len(payload["spoken"]) > 0


def test_tool_perform_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_PERFORM_STEPS"] = json.dumps([
        {"emote": "curious", "speak": "Testing", "pause": 0.3},
        {"look": {"pan": 10, "tilt": 5}},
    ])
    stdout = run_tool(["bin/tool-perform"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert payload["steps"] == 2


@pytest.mark.skipif(sys.platform != "linux", reason="/proc/{pid} only exists on Linux")
def test_px_alive_pid_race_not_duplicate(isolated_project):
    """Second px-alive start should exit cleanly if PID file already shows a live process."""
    import os
    pid_file = Path(isolated_project["log_dir"]) / "px-alive.pid"
    # Write our own PID as if we're px-alive instance 1
    pid_file.write_text(str(os.getpid()))
    env = {**isolated_project["env"], "PX_ALIVE_PID": str(pid_file), "PX_DRY": "1"}
    result = subprocess.run(
        [str(PROJECT_ROOT / "bin" / "px-alive"), "--dry-run"],
        capture_output=True, text=True, env=env, timeout=10,
    )
    # Should have exited cleanly (rc=0) without overwriting the PID file
    assert result.returncode == 0, f"expected rc=0, got {result.returncode}: {result.stderr}"
    assert pid_file.read_text().strip() == str(os.getpid()), "PID file was overwritten by second instance"


def test_tool_perform_missing_steps(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env.pop("PX_PERFORM_STEPS", None)
    result = subprocess.run(
        ["bin/tool-perform"],
        cwd=PROJECT_ROOT, text=True, capture_output=True, check=False, env=env,
    )
    payload = parse_json(result.stdout.strip())
    assert payload["status"] == "error"


def test_tool_recall_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-recall"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert "notes" in payload
    assert "spoken" in payload


def test_tool_api_start_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-api-start"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True


def test_tool_api_stop_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-api-stop"], env)
    payload = parse_json(stdout)
    # Either "ok" dry or "not_running" — both are valid
    assert payload["status"] in ("ok", "not_running")


def test_tool_voice_persona_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_TEXT"] = "Hello world"
    env["PX_PERSONA"] = "vixen"
    stdout = run_tool(["bin/tool-voice-persona"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert payload["persona"] == "vixen"


def test_tool_voice_persona_missing_text(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_PERSONA"] = "vixen"
    env.pop("PX_TEXT", None)
    result = subprocess.run(
        ["bin/tool-voice-persona"],
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

# ── SPARK Phase 2 tools ──────────────────────────────────────────────────────

def test_tool_routine_load_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_ROUTINE_ACTION"] = "load"
    env["PX_ROUTINE_NAME"] = "morning"
    stdout = run_tool(["bin/tool-routine"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["routine"] == "morning"
    assert payload["step"] == 0


def test_tool_routine_unknown_name(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_ROUTINE_ACTION"] = "load"
    env["PX_ROUTINE_NAME"] = "nonexistent"
    result = subprocess.run(
        ["bin/tool-routine"],
        cwd=PROJECT_ROOT, text=True, capture_output=True, check=False, env=env,
    )
    payload = parse_json(result.stdout.strip())
    assert payload["status"] == "error"


def test_tool_routine_status_no_active(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_ROUTINE_ACTION"] = "status"
    stdout = run_tool(["bin/tool-routine"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert "no active routine" in payload.get("note", "")


def test_tool_checkin_ask_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_CHECKIN_ACTION"] = "ask"
    stdout = run_tool(["bin/tool-checkin"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["action"] == "ask"


def test_tool_checkin_record_known_mood(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_CHECKIN_ACTION"] = "record"
    env["PX_CHECKIN_MOOD"] = "tired"
    stdout = run_tool(["bin/tool-checkin"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["mood"] == "tired"


def test_tool_checkin_record_unknown_mood(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_CHECKIN_ACTION"] = "record"
    env["PX_CHECKIN_MOOD"] = "zorblaxian"
    stdout = run_tool(["bin/tool-checkin"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["mood"] == "zorblaxian"


def test_tool_celebrate_with_text(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_CELEBRATE_TEXT"] = "You finished the whole morning routine!"
    stdout = run_tool(["bin/tool-celebrate"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert "morning routine" in payload["text"]


def test_tool_celebrate_no_text(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env.pop("PX_CELEBRATE_TEXT", None)
    stdout = run_tool(["bin/tool-celebrate"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["text"]  # some generic cheer


def test_tool_transition_warn_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_TRANSITION_ACTION"] = "warn"
    env["PX_TRANSITION_MINUTES"] = "5"
    env["PX_TRANSITION_LABEL"] = "school"
    stdout = run_tool(["bin/tool-transition"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["action"] == "warn"
    assert payload["minutes"] == 5


def test_tool_transition_buffer_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_TRANSITION_ACTION"] = "buffer"
    stdout = run_tool(["bin/tool-transition"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["action"] == "buffer"


def test_tool_transition_arrived_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_TRANSITION_ACTION"] = "arrived"
    stdout = run_tool(["bin/tool-transition"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["action"] == "arrived"


# ── SPARK Phase 3 tools ──────────────────────────────────────────────────────

def test_tool_quiet_start_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_QUIET_ACTION"] = "start"
    stdout = run_tool(["bin/tool-quiet"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["quiet_mode"] is True


def test_tool_quiet_end_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_QUIET_ACTION"] = "end"
    stdout = run_tool(["bin/tool-quiet"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["quiet_mode"] is False


def test_tool_breathe_simple_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_BREATHE_TYPE"] = "simple"
    env["PX_BREATHE_ROUNDS"] = "1"
    stdout = run_tool(["bin/tool-breathe"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["type"] == "simple"
    assert payload["rounds"] == 1


def test_tool_breathe_box_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_BREATHE_TYPE"] = "box"
    env["PX_BREATHE_ROUNDS"] = "2"
    stdout = run_tool(["bin/tool-breathe"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["type"] == "box"


def test_tool_dopamine_menu_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_DOPAMINE_ENERGY"] = "medium"
    env["PX_DOPAMINE_CONTEXT"] = "free"
    stdout = run_tool(["bin/tool-dopamine-menu"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert len(payload["picks"]) >= 1


def test_tool_dopamine_menu_invalid_energy(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_DOPAMINE_ENERGY"] = "ultraviolet"  # invalid → falls back to medium
    stdout = run_tool(["bin/tool-dopamine-menu"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["energy"] == "medium"


def test_tool_sensory_check_ask_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_SENSORY_ACTION"] = "ask"
    stdout = run_tool(["bin/tool-sensory-check"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["action"] == "ask"


def test_tool_sensory_check_record_known(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_SENSORY_ACTION"] = "record"
    env["PX_SENSORY_ISSUE"] = "too loud"
    stdout = run_tool(["bin/tool-sensory-check"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert "loud" in payload["issue"]


def test_tool_repair_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    stdout = run_tool(["bin/tool-repair"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["repair"]
    assert payload["reconnect"]


# ── SPARK Phase 4 tools ──────────────────────────────────────────────────────

def test_tool_gws_calendar_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_CALENDAR_ACTION"] = "today"
    # dry-run: tool runs in dry mode but gws call still happens (returns auth error in real mode)
    # We just check it doesn't crash and produces valid JSON
    result = subprocess.run(
        ["bin/tool-gws-calendar"],
        cwd=PROJECT_ROOT, text=True, capture_output=True, check=False, env=env,
    )
    assert result.stdout.strip()
    payload = parse_json(result.stdout.strip())
    assert payload["status"] in ("ok", "error")  # ok if authed, error if not


def test_tool_gws_sheets_log_dry(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_SHEETS_ID"] = "fake_sheet_id"
    env["PX_SHEETS_EVENT"] = "mood"
    env["PX_SHEETS_DETAIL"] = "felt tired"
    stdout = run_tool(["bin/tool-gws-sheets-log"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    assert payload["row"][1] == "mood"


def test_tool_gws_sheets_log_missing_id(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env.pop("PX_SHEETS_ID", None)
    result = subprocess.run(
        ["bin/tool-gws-sheets-log"],
        cwd=PROJECT_ROOT, text=True, capture_output=True, check=False, env=env,
    )
    payload = parse_json(result.stdout.strip())
    assert payload["status"] == "error"
    assert "PX_SHEETS_ID" in payload["error"]


@pytest.mark.skipif(not __import__("importlib").util.find_spec("fastapi"), reason="fastapi not installed")
def test_session_history_clear(isolated_project, monkeypatch):
    """POST /api/v1/session/history/clear should wipe history and return count."""
    import os
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    monkeypatch.setenv("PX_SESSION_PATH", str(isolated_project["session_path"]))

    # Import after env is patched so the module uses the right session path
    from fastapi.testclient import TestClient
    import importlib, pxh.api as _api_mod
    importlib.reload(_api_mod)

    # Seed some history
    from pxh.state import update_session, load_session
    update_session(history_entry={"event": "test", "text": "garbled phrase"})
    update_session(history_entry={"event": "test", "text": "another entry"})

    # Use context manager so lifespan (which calls _load_token) runs
    with TestClient(_api_mod.app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/api/v1/session/history/clear",
            headers={"Authorization": "Bearer testtoken"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["cleared"] >= 2

    assert load_session().get("history", []) == []


@pytest.mark.skipif(sys.platform != "linux", reason="/proc/{pid} only exists on Linux")
def test_tool_photograph_camera_busy(isolated_project):
    """tool-photograph should fail gracefully when frigate stream PID file is present."""
    pid_file = Path(isolated_project["log_dir"]) / "px-frigate-stream.pid"
    import os
    pid_file.write_text(str(os.getpid()))  # our own PID = definitely alive
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    result = subprocess.run(
        [str(PROJECT_ROOT / "bin" / "tool-photograph")],
        capture_output=True, text=True, env=env, timeout=10,
    )
    data = parse_json(result.stdout.strip())
    assert data["status"] == "error"
    assert "camera busy" in data["error"]
