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
    assert 'BIN_DIR' not in restart_alive_body and '"bin"' not in restart_alive_body, (
        "_restart_alive must not build a path to bin/px-alive and run it; "
        "let systemd own the process lifecycle."
    )
    assert "Popen" not in restart_alive_body, (
        "_restart_alive must not Popen px-alive — the child outlives the voice "
        "turn and squats on the pidfile the systemd unit needs."
    )


def test_stop_alive_still_signals_by_pid(restart_alive_body):
    """Stopping stays a SIGTERM to the pid: it must not disable the unit.

    `systemctl stop` would be wrong here — Restart=always means systemd brings
    px-alive straight back, defeating the GPIO yield the voice turn needs.
    """
    src = SCRIPT.read_text()
    start = src.index("def _stop_alive(")
    body = src[start : src.index("def _restart_alive(")]
    assert "kill" in body, "_stop_alive must signal px-alive by pid to yield GPIO"
    assert "systemctl" not in body, (
        "_stop_alive must not use systemctl stop; Restart=always would "
        "immediately respawn px-alive and it would retake the GPIO handle."
    )
