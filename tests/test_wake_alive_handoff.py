"""px-wake-listen must hand px-alive back to systemd, never spawn it directly.

Regression guard for the restart loop observed 2026-07-31: `_restart_alive`
spawned `sudo -n bin/px-alive` as a *child of px-wake-listen*. That child wrote
logs/px-alive.pid and took the GPIO handle, so the systemd unit — which is
Restart=always — started, hit px-alive's single-instance guard ("another
px-alive already running — exiting"), and died, forever. It reached 40 restarts
before anyone noticed, and `systemctl is-active px-alive` reported `activating`
the whole time rather than `failed`.

The direct spawn also dropped the unit's `--no-face` flag, so the squatting
instance ran a different configuration than the one the unit defines.

px-mind already does this correctly (`sudo -n systemctl start px-alive`), and
sudoers grants pi NOPASSWD on start/stop/restart of px-alive specifically, so
delegating to systemd is both the supported and the already-established path.

Killing px-alive by pid arms Restart=always: systemd retries every 15s for the
whole voice turn. The exploring.json guard (same contract as px-wander and
tool-announce) makes each retry exit cleanly instead of retaking GPIO mid-turn,
and a refresher thread keeps the file inside px-alive's 60s staleness window.

bin/px-wake-listen runs its Python as a heredoc and cannot be imported, so these
assert against the source text.
"""

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "px-wake-listen"


@pytest.fixture(scope="module")
def restart_alive_body() -> str:
    """The body of _restart_alive(), up to the next top-level def."""
    src = SCRIPT.read_text()
    start = src.index("def _restart_alive(")
    rest = src[start:]
    end = re.search(r"\n(?=def )", rest)
    return rest[: end.start()] if end else rest


def test_restart_alive_delegates_to_systemctl(restart_alive_body):
    """Restarting must go through systemd, so systemd stays the owner."""
    assert "systemctl" in restart_alive_body, (
        "_restart_alive must restart px-alive via systemctl, not by spawning it. "
        "A directly-spawned px-alive holds the pidfile and GPIO, which traps the "
        "systemd unit in a permanent start/exit restart loop."
    )


def test_restart_alive_does_not_spawn_px_alive_directly(restart_alive_body):
    """No path may exec bin/px-alive itself — that is what created the squatter."""
    assert '"bin"' not in restart_alive_body, (
        "_restart_alive must not build a path to bin/px-alive and run it; "
        "let systemd own the process lifecycle."
    )
    assert "Popen" not in restart_alive_body, (
        "_restart_alive must not Popen px-alive — the child outlives the voice "
        "turn and squats on the pidfile the systemd unit needs."
    )


def test_restart_alive_uses_start_not_restart():
    """The sudoers-verified verb is `start` (mind.py precedent), and it is the
    right one: after the pid-kill the unit sits in auto-restart, so `start`
    cancels the backoff timer and runs now, while staying a no-op if a retry
    already brought px-alive back. `restart` would bounce a healthy instance."""
    src = SCRIPT.read_text()
    start = src.index("def _restart_alive(")
    rest = src[start:]
    end = re.search(r"\n(?=def )", rest)
    body = rest[: end.start()] if end else rest
    assert '"start"' in body and '"restart"' not in body


def test_stop_alive_still_signals_by_pid():
    """Stopping stays a SIGTERM to the pid, keeping Restart=always armed.

    Not because `systemctl stop` would respawn px-alive — an explicit stop
    disarms Restart=always and the unit stays down. Precisely because of that:
    pid-kill keeps systemd's auto-restart armed as a dead-man's switch, so if
    px-wake-listen crashes mid-turn, px-alive recovers on its own within ~15s
    instead of staying dead forever. The exploring.json guard is what makes
    those mid-turn retries exit cleanly rather than retake GPIO.
    """
    src = SCRIPT.read_text()
    start = src.index("def _stop_alive(")
    body = src[start : src.index("def _restart_alive(")]
    assert "kill" in body, "_stop_alive must signal px-alive by pid to yield GPIO"
    assert "systemctl" not in body, (
        "_stop_alive must not systemctl stop the unit; an explicit stop disarms "
        "Restart=always, losing the dead-man's-switch recovery if wake-listen "
        "dies mid-turn."
    )


def test_stop_alive_raises_exploring_guard():
    """The pid-kill arms a 15s systemd respawn; exploring.json is what stops
    that respawn retaking GPIO mid-turn (px-alive exits cleanly while the file
    is fresh — same contract px-wander and tool-announce rely on)."""
    src = SCRIPT.read_text()
    start = src.index("def _stop_alive(")
    body = src[start : src.index("def _restart_alive(")]
    assert "_set_exploring(True)" in body, (
        "_stop_alive must write exploring.json before yielding GPIO, or the "
        "systemd auto-restart retakes it ~15s into the voice turn."
    )


def test_restart_alive_clears_exploring_guard(restart_alive_body):
    """The guard must drop before systemd is asked to start px-alive, or the
    fresh instance reads it and exits cleanly — recreating the outage."""
    assert "_set_exploring(False)" in restart_alive_body


def test_exploring_refresh_beats_staleness_window():
    """px-alive ignores exploring.json older than 60s. A single voice turn can
    block >60s (Claude CLI cold start alone is 15–80s), so a refresher must
    rewrite the file on an interval well inside that window."""
    src = SCRIPT.read_text()
    m = re.search(r"_EXPLORING_REFRESH_S\s*=\s*(\d+)", src)
    assert m, "px-wake-listen must define _EXPLORING_REFRESH_S"
    assert int(m.group(1)) <= 30, (
        "refresh interval must sit well inside px-alive's 60s staleness window"
    )
    assert "_exploring_stop.wait(_EXPLORING_REFRESH_S)" in src, (
        "a refresher thread must re-write exploring.json while the turn runs"
    )
