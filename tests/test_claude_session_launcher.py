"""Tests for bin/px-claude-session's exit instrumentation.

spark-brain has vanished from tmux 12 times with no repo-owned kill path.
`exec`ing claude directly left nothing running to observe how or when it
stopped. These tests drive the real script against a fake `claude` binary
(no tmux, no real Claude Code) and check what lands in the log.
"""
import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "px-claude-session"


def _write_fake_claude(path: Path, *, exit_code=None, signal=None) -> None:
    assert (exit_code is None) != (signal is None), "pick exactly one"
    body = [
        "#!/usr/bin/env bash",
        'if [[ "${1:-}" == "--version" ]]; then',
        '    echo "fake-claude 9.9.9"',
        "    exit 0",
        "fi",
        'echo "fake claude stdout"',
        'echo "boom stderr line 1" >&2',
        'echo "boom stderr line 2" >&2',
    ]
    if signal is not None:
        body.append(f"kill -{signal} $$")
        body.append("sleep 5")  # never reached if the signal is delivered
    else:
        body.append(f"exit {exit_code}")
    path.write_text("\n".join(body) + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run_launcher(tmp_path, fake_claude, *, session="spark-brain"):
    log_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    log_dir.mkdir()
    state_dir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("system prompt with {{TOOL_BRAIN_REPLY}} in it\n")

    env = os.environ.copy()
    env["LOG_DIR"] = str(log_dir)
    env["PX_STATE_DIR"] = str(state_dir)
    env["PX_CLAUDE_BIN"] = str(fake_claude)
    env["PX_CLAUDE_TMUX_PROMPT"] = str(prompt)
    env["PX_BRAIN_SESSION"] = session

    result = subprocess.run(
        [str(LAUNCHER)], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=30,
    )
    log_path = log_dir / "tool-claude-session.log"
    events = []
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))
    return result, events


def test_a_normal_exit_records_pid_start_and_exit(tmp_path):
    fake_claude = tmp_path / "fake-claude"
    _write_fake_claude(fake_claude, exit_code=3)

    result, events = _run_launcher(tmp_path, fake_claude)

    assert result.returncode == 3
    assert "boom stderr line 1" in result.stderr, \
        "stderr must still reach the terminal live, not only the log"

    starts = [e for e in events if e.get("event") == "start"]
    exits = [e for e in events if e.get("event") == "exit"]
    assert len(starts) == 1 and len(exits) == 1

    start, exit_ = starts[0], exits[0]
    assert start["session"] == "spark-brain"
    assert start["claude_version"] == "fake-claude 9.9.9"
    assert isinstance(start["pid"], int) and start["pid"] > 0
    assert start["boot_id"]

    assert exit_["pid"] == start["pid"]
    assert exit_["exit_code"] == 3
    assert "signal" not in exit_
    assert exit_["duration_s"] >= 0
    assert "boom stderr line 1" in exit_["stderr_tail"]
    assert "boom stderr line 2" in exit_["stderr_tail"]


def test_a_signal_kill_is_recorded_as_a_signal_not_an_exit_code(tmp_path):
    fake_claude = tmp_path / "fake-claude"
    _write_fake_claude(fake_claude, signal="TERM")

    result, events = _run_launcher(tmp_path, fake_claude)

    assert result.returncode == 128 + 15  # SIGTERM
    exits = [e for e in events if e.get("event") == "exit"]
    assert len(exits) == 1
    assert exits[0]["signal"] == 15
    assert "exit_code" not in exits[0]
