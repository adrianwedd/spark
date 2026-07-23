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


def test_tool_voice_lock_timeout(isolated_project):
    """PX_VOICE_LOCK_TIMEOUT lets callers fail fast when voice.lock is held."""
    from filelock import FileLock

    env = isolated_project["env"].copy()
    env["PX_DRY"] = "0"  # non-dry so the lock path is exercised
    env["PX_TEXT"] = "Lock contention test"
    env["PX_VOICE_LOCK_TIMEOUT"] = "1"

    log_dir = env.get("LOG_DIR", str(PROJECT_ROOT / "logs"))
    lock_path = str(Path(log_dir) / "voice.lock")
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    # Hold the lock externally so tool-voice hits the timeout
    with FileLock(lock_path, timeout=0):
        result = subprocess.run(
            ["bin/tool-voice"],
            cwd=PROJECT_ROOT, text=True, capture_output=True, check=False, env=env,
        )
    payload = parse_json(result.stdout.strip())
    assert payload["status"] == "error"
    assert "voice lock timeout" in payload["error"]


# ---------------------------------------------------------------------------
# Network TTS routing tests (GREMLIN / VIXEN personas)
# ---------------------------------------------------------------------------


def test_voice_gremlin_dry_skips_network_tts(isolated_project):
    """Dry mode with GREMLIN persona skips network TTS, outputs single JSON."""
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_TEXT"] = "Humanity. What a concept."
    env["PX_PERSONA"] = "gremlin"
    env["_PX_VOICE_PERSONA_DONE"] = "1"  # skip Ollama rephrase
    stdout = run_tool(["bin/tool-voice"], env)
    lines = stdout.strip().splitlines()
    assert len(lines) == 1, f"Expected single JSON line, got {len(lines)}: {lines}"
    payload = json.loads(lines[0])
    assert payload["status"] == "ok"
    assert payload["dry"] is True


def test_voice_vixen_dry_skips_network_tts(isolated_project):
    """Dry mode with VIXEN persona skips network TTS, outputs single JSON."""
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_TEXT"] = "Oh darling, the universe is vast."
    env["PX_PERSONA"] = "vixen"
    env["_PX_VOICE_PERSONA_DONE"] = "1"
    stdout = run_tool(["bin/tool-voice"], env)
    lines = stdout.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "ok"
    assert payload["dry"] is True


def test_voice_spark_no_network_tts(isolated_project):
    """SPARK persona (or no persona) never triggers network TTS."""
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_TEXT"] = "I notice the morning light."
    env["PX_PERSONA"] = "spark"
    env["_PX_VOICE_PERSONA_DONE"] = "1"
    stdout = run_tool(["bin/tool-voice"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True
    # No network-tts player should appear
    assert "network-tts" not in payload.get("player", "")


def test_voice_network_tts_fallback_on_dead_server(isolated_project):
    """When TTS server is unreachable, falls back to espeak (dry-mode safe)."""
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "0"  # non-dry to exercise network path
    env["PX_TEXT"] = "Fallback test"
    env["PX_PERSONA"] = "gremlin"
    env["_PX_VOICE_PERSONA_DONE"] = "1"
    # Point to a port nothing is listening on
    env["PX_TTS_GREMLIN"] = "http://127.0.0.1:19999"
    result = subprocess.run(
        ["bin/tool-voice"],
        cwd=PROJECT_ROOT, text=True, capture_output=True, check=False, env=env,
    )
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1, f"Single JSON contract violated: {lines}"
    payload = json.loads(lines[0])
    # Should have fallen through to espeak (which may fail on macOS CI, but
    # the tool should still produce a valid JSON payload)
    assert payload["status"] in ("ok", "error")
    assert "network-tts" not in payload.get("player", "")


def test_voice_single_json_contract(isolated_project):
    """tool-voice must always emit exactly one JSON line, regardless of persona."""
    for persona in ("gremlin", "vixen", "spark", ""):
        env = isolated_project["env"].copy()
        env["PX_DRY"] = "1"
        env["PX_TEXT"] = "Contract test"
        if persona:
            env["PX_PERSONA"] = persona
        env["_PX_VOICE_PERSONA_DONE"] = "1"
        stdout = run_tool(["bin/tool-voice"], env)
        lines = stdout.strip().splitlines()
        assert len(lines) == 1, (
            f"Persona '{persona}' emitted {len(lines)} lines: {lines}"
        )
        payload = json.loads(lines[0])
        assert "status" in payload


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
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    unused_port = probe.getsockname()[1]

    env = isolated_project["env"].copy()
    env["PX_FRIGATE_HOST"] = f"http://127.0.0.1:{unused_port}"
    try:
        result = subprocess.run(
            ["bin/tool-frigate-events"],
            cwd=PROJECT_ROOT,
            capture_output=True, text=True, env=env, timeout=10,
        )
    finally:
        probe.close()
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
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    monkeypatch.setenv("PX_SESSION_PATH", str(isolated_project["session_path"]))

    # Import after env is patched so the module uses the right session path
    from fastapi.testclient import TestClient
    import importlib
    import pxh.api as _api_mod
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


# ── Explore mode / abort scenario tests ──────────────────────────────────────

def test_wander_explore_mode_dry(isolated_project):
    """Explore mode accepts --mode explore, runs time-boxed, emits correct JSON."""
    from pxh.state import default_state
    state = default_state()
    state["confirm_motion_allowed"] = True
    state["roaming_allowed"] = True
    isolated_project["session_path"].write_text(json.dumps(state))

    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_WANDER_MODE"] = "explore"
    env["PX_WANDER_DURATION_S"] = "3"
    env["PX_WANDER_STEPS"] = "3"

    import datetime as dt2
    battery = {"ts": dt2.datetime.now(dt2.timezone.utc).isoformat(),
               "pct": 80, "volts": 8.0, "charging": False}
    (Path(isolated_project["state_dir"]) / "battery.json").write_text(json.dumps(battery))

    stdout = run_tool(["bin/tool-wander"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["mode"] == "explore"
    assert payload["dry"] is True


def test_wander_avoid_mode_unchanged(isolated_project):
    """Existing avoid behaviour preserved with --mode avoid."""
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_WANDER_STEPS"] = "2"
    env["PX_WANDER_MODE"] = "avoid"
    stdout = run_tool(["bin/tool-wander"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["dry"] is True


def test_wander_explore_roaming_gate_in_tool(isolated_project):
    """tool-wander rejects explore mode when roaming_allowed is false."""
    from pxh.state import default_state
    state = default_state()
    state["confirm_motion_allowed"] = True
    state["roaming_allowed"] = False
    isolated_project["session_path"].write_text(json.dumps(state))

    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_WANDER_MODE"] = "explore"
    env["PX_WANDER_STEPS"] = "2"
    result = subprocess.run(
        ["bin/tool-wander"], cwd=PROJECT_ROOT,
        text=True, capture_output=True, check=False, env=env,
    )
    payload = parse_json(result.stdout.strip())
    assert payload["status"] == "blocked"
    assert "roaming" in payload["reason"]


def test_wander_explore_abort_on_listening(isolated_project):
    """Session listening=true causes immediate abort."""
    from pxh.state import default_state
    state = default_state()
    state["confirm_motion_allowed"] = True
    state["roaming_allowed"] = True
    state["listening"] = True
    isolated_project["session_path"].write_text(json.dumps(state))

    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_WANDER_MODE"] = "explore"
    env["PX_WANDER_DURATION_S"] = "10"

    import datetime as dt2
    battery = {"ts": dt2.datetime.now(dt2.timezone.utc).isoformat(),
               "pct": 80, "volts": 8.0, "charging": False}
    (Path(isolated_project["state_dir"]) / "battery.json").write_text(json.dumps(battery))

    stdout = run_tool(["bin/tool-wander"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload.get("abort_reason") == "someone is talking"


def test_wander_explore_abort_on_charging(isolated_project):
    """Battery charging triggers abort."""
    from pxh.state import default_state
    state = default_state()
    state["confirm_motion_allowed"] = True
    state["roaming_allowed"] = True
    isolated_project["session_path"].write_text(json.dumps(state))

    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_WANDER_MODE"] = "explore"
    env["PX_WANDER_DURATION_S"] = "10"

    import datetime as dt2
    battery = {"ts": dt2.datetime.now(dt2.timezone.utc).isoformat(),
               "pct": 80, "volts": 8.0, "charging": True}
    (Path(isolated_project["state_dir"]) / "battery.json").write_text(json.dumps(battery))

    stdout = run_tool(["bin/tool-wander"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload.get("abort_reason") == "battery charging"


def test_wander_explore_abort_on_roaming_disabled(isolated_project):
    """roaming_allowed=false triggers abort in explore loop."""
    from pxh.state import default_state
    state = default_state()
    state["confirm_motion_allowed"] = True
    state["roaming_allowed"] = False
    isolated_project["session_path"].write_text(json.dumps(state))

    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_WANDER_MODE"] = "explore"
    env["PX_WANDER_DURATION_S"] = "10"

    import datetime as dt2
    battery = {"ts": dt2.datetime.now(dt2.timezone.utc).isoformat(),
               "pct": 80, "volts": 8.0, "charging": False}
    (Path(isolated_project["state_dir"]) / "battery.json").write_text(json.dumps(battery))

    # Note: tool-wander blocks this at the gate level (status=blocked),
    # because roaming gate is checked unconditionally even in dry mode.
    # So we expect "blocked" not "ok" with abort_reason.
    result = subprocess.run(
        ["bin/tool-wander"], cwd=PROJECT_ROOT,
        text=True, capture_output=True, check=False, env=env,
    )
    payload = parse_json(result.stdout.strip())
    assert payload["status"] == "blocked"
    assert "roaming" in payload["reason"]


def test_wander_explore_abort_on_stale_battery(isolated_project):
    """battery.json older than 60s triggers abort."""
    from pxh.state import default_state
    state = default_state()
    state["confirm_motion_allowed"] = True
    state["roaming_allowed"] = True
    isolated_project["session_path"].write_text(json.dumps(state))

    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_WANDER_MODE"] = "explore"
    env["PX_WANDER_DURATION_S"] = "10"

    import datetime as dt2
    old_ts = (dt2.datetime.now(dt2.timezone.utc) - dt2.timedelta(seconds=120)).isoformat()
    battery = {"ts": old_ts, "pct": 80, "volts": 8.0, "charging": False}
    (Path(isolated_project["state_dir"]) / "battery.json").write_text(json.dumps(battery))

    stdout = run_tool(["bin/tool-wander"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload.get("abort_reason") == "battery data stale or missing"


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


def test_tool_research_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_RESEARCH_QUERY"] = "Why do magnets work?"
    result = subprocess.run(
        [str(PROJECT_ROOT / "bin" / "tool-research")],
        capture_output=True, text=True, env=env, timeout=10,
    )
    data = parse_json(result.stdout)
    assert data["status"] == "ok"
    assert data["dry"] is True


def test_tool_compose_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_COMPOSE_TOPIC"] = "morning light"
    result = subprocess.run(
        [str(PROJECT_ROOT / "bin" / "tool-compose")],
        capture_output=True, text=True, env=env, timeout=10,
    )
    data = parse_json(result.stdout)
    assert data["status"] == "ok"
    assert data["dry"] is True


def test_tool_blog_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_BLOG_TOPIC"] = "Why robots dream"
    result = subprocess.run(
        [str(PROJECT_ROOT / "bin" / "tool-blog")],
        capture_output=True, text=True, env=env, timeout=10,
    )
    data = parse_json(result.stdout)
    assert data["status"] == "ok"
    assert data["dry"] is True


def test_tool_announce_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_ANNOUNCE_TEXT"] = "Dinner is ready"
    stdout = run_tool(["bin/tool-announce"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "dry"
    assert payload["voice"] == "data"
    assert payload["targets"]  # default target resolved


import http.server
import json as _json
import threading


class _StubHandler(http.server.BaseHTTPRequestHandler):
    captured = []  # class-level capture: list of (method, path, body)

    def log_message(self, *a):  # silence
        pass

    def _send(self, code, obj):
        body = _json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # HA state lookups: /api/states/<entity>
        _StubHandler.captured.append(("GET", self.path, None))
        self._send(200, {"state": "idle"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = _json.loads(self.rfile.read(length) or b"{}")
        _StubHandler.captured.append(("POST", self.path, body))
        if self.path.endswith("/announce"):
            self._send(200, {"audio_url": "http://192.168.0.100:7862/audio/abc123.wav",
                             "voice": "data", "cached": False, "duration_s": 1.2})
        else:  # HA play_media
            self._send(200, [{"entity_id": "media_player.nest_hub_max", "state": "playing"}])


def _start_stub():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_tool_announce_live_path_posts_relay_and_ha(isolated_project, monkeypatch):
    _StubHandler.captured = []
    srv = _start_stub()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "0"
    env["PX_ANNOUNCE_TEXT"] = "Dinner is ready"
    env["PX_BYPASS_SUDO"] = "1"
    # Point both relay and HA at the stub via spark_config override env (see Step 5 note).
    env["PX_ANNOUNCE_RELAY_URL"] = base
    env["PX_HA_HOST"] = base
    env["ANNOUNCE_RELAY_TOKEN"] = "t"
    env["PX_HA_TOKEN"] = "t"
    env["PX_NIGHT_SILENCE_START_H"] = "99"   # force "never night" — deterministic
    env["PX_NIGHT_SILENCE_END_H"] = "0"
    try:
        stdout = run_tool(["bin/tool-announce"], env)
    finally:
        srv.shutdown()
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["audio_url"].endswith("/audio/abc123.wav")
    assert payload["targets"] == ["media_player.nest_hub_max"]
    paths = [p for (_, p, _) in _StubHandler.captured]
    assert any(p.endswith("/announce") for p in paths)
    assert any("/api/services/media_player/play_media" in p for p in paths)


def _run_announce_against_stub(isolated_project, text, *, private):
    _StubHandler.captured = []
    srv = _start_stub()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "0"
    env["PX_ANNOUNCE_TEXT"] = text
    if private:
        env["PX_ANNOUNCE_PRIVATE"] = "1"
    env["PX_BYPASS_SUDO"] = "1"
    env["PX_ANNOUNCE_RELAY_URL"] = base
    env["PX_HA_HOST"] = base
    env["ANNOUNCE_RELAY_TOKEN"] = "t"
    env["PX_HA_TOKEN"] = "t"
    env["PX_NIGHT_SILENCE_START_H"] = "99"
    env["PX_NIGHT_SILENCE_END_H"] = "0"
    try:
        payload = parse_json(run_tool(["bin/tool-announce"], env))
    finally:
        srv.shutdown()
    history = _json.loads(isolated_project["session_path"].read_text())["history"]
    return payload, _json.dumps(history)


def test_tool_announce_private_redacts_session_history(isolated_project):
    payload, history_json = _run_announce_against_stub(
        isolated_project, "SECRET-DM-PAYLOAD-XYZ", private=True)
    assert payload["status"] == "ok"
    # The private DM text must never land in local session bookkeeping.
    assert "SECRET-DM-PAYLOAD-XYZ" not in history_json
    assert "announce" in history_json   # entry still recorded, just redacted


def test_tool_announce_public_keeps_session_history_text(isolated_project):
    payload, history_json = _run_announce_against_stub(
        isolated_project, "Dinner is ready", private=False)
    assert payload["status"] == "ok"
    assert "Dinner is ready" in history_json


def test_tool_announce_suppressed_during_night_silence(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "0"
    env["PX_ANNOUNCE_TEXT"] = "Should not play"
    env["PX_NIGHT_SILENCE_START_H"] = "0"    # force "always night"
    env["PX_NIGHT_SILENCE_END_H"] = "24"
    # No relay/HA stub: if the gate is broken it'll error trying to reach the relay,
    # which is itself a failure — a working gate returns before any network egress.
    stdout = run_tool(["bin/tool-announce"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "night_silence"


def test_tool_announce_resolves_single_target_from_multiple(isolated_project):
    # Even if multiple allowed targets are requested, v1 casts to exactly one (echo).
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_ANNOUNCE_TEXT"] = "hi"
    env["PX_ANNOUNCE_TARGETS"] = "media_player.nest_hub_max,media_player.nest_mini"
    payload = parse_json(run_tool(["bin/tool-announce"], env))
    assert payload["status"] == "dry"
    assert len(payload["targets"]) == 1


# ---------------------------------------------------------------------------
# tool-research / tool-compose: outputs must be visible to reflection memory
# ---------------------------------------------------------------------------

import types as _types
from pathlib import Path as _Path

_TOOLS_ROOT = _Path(__file__).parent.parent


def _load_tool_heredoc(name, monkeypatch, tmp_path, extra_env=None):
    """Exec a bin/tool-* embedded Python heredoc into a namespace with a fake
    pxh.claude_session so main() can run without a live Claude CLI."""
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PX_DRY", "0")
    for k, v in (extra_env or {}).items():
        monkeypatch.setenv(k, v)

    fake_cs = _types.ModuleType("pxh.claude_session")
    fake_cs.SessionBudgetExhausted = type("SessionBudgetExhausted", (Exception,), {})
    fake_cs.run_claude_session = lambda **kw: _types.SimpleNamespace(
        returncode=0, stdout="A thoughtful multi-paragraph exploration of the topic.",
        model_used="claude-haiku-test")
    monkeypatch.setitem(sys.modules, "pxh.claude_session", fake_cs)

    text = (_TOOLS_ROOT / "bin" / name).read_text(encoding="utf-8")
    py = text.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    ns = {"__name__": f"{name.replace('-', '_')}_mod"}
    exec(compile(py, name, "exec"), ns)  # noqa: S102 — loading our own tool code for testing
    return ns


def test_tool_research_writes_note_key(tmp_path, monkeypatch, capsys):
    """Research results must carry a 'note' key so load_notes() can surface them."""
    ns = _load_tool_heredoc("tool-research", monkeypatch, tmp_path,
                            {"PX_RESEARCH_QUERY": "why is the sky blue"})
    ns["main"]()
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "ok"
    rec = json.loads((tmp_path / "notes-spark.jsonl").read_text().strip())
    assert rec.get("note", "").startswith("Research: why is the sky blue")
    assert "exploration" in rec["note"]  # includes a response excerpt


def test_tool_compose_writes_note_record(tmp_path, monkeypatch, capsys):
    """Compositions must leave a note in notes-spark.jsonl (full text stays in compositions file)."""
    ns = _load_tool_heredoc("tool-compose", monkeypatch, tmp_path,
                            {"PX_COMPOSE_TOPIC": "the wind in the eucalyptus"})
    ns["main"]()
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "ok"
    # full text still lands in compositions file
    comp = json.loads((tmp_path / "compositions-spark.jsonl").read_text().strip())
    assert comp["text"].startswith("A thoughtful")
    # and a note summary lands in notes
    note_rec = json.loads((tmp_path / "notes-spark.jsonl").read_text().strip())
    assert note_rec.get("note", "").startswith("Composed: the wind in the eucalyptus")
