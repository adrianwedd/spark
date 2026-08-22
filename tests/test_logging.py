"""pxh.logging.log_event() ownership model: motion tools (tool-drive,
tool-circle, tool-look, ...) sudo-elevate to root for GPIO and are often the
first to create a given logs/tool-<name>.log; everything else, including the
same tool name reached a different way, logs to it as pi. A root-created file
must never make later pi logging crash or silently vanish.

chmod alone does not fix this on a hardened kernel: fs.protected_regular=2
(confirmed live on this host via `sysctl fs.protected_regular` — a real,
intentional Debian default, not something to weaken) blocks *opening* a
regular file for writing whenever its owner is neither the directory's owner
nor the calling uid, regardless of the file's own mode bits, on any open that
carries O_CREAT — which a plain open(path, "a") always does, and so does
FileLock's own first acquire(). Only matching the file's owner to the
directory's owner (chown, not just chmod) satisfies the kernel's exemption.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pxh import logging as pxlog

REPO_ROOT = Path(pxlog.__file__).resolve().parents[2]


def test_log_event_writes_a_normal_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    pxlog.log_event("probe", {"hello": "world"})
    log_path = tmp_path / "tool-probe.log"
    assert log_path.exists()
    assert "hello" in log_path.read_text()


def test_log_dir_is_made_sticky_and_world_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    pxlog.log_event("probe", {"a": 1})
    mode = tmp_path.stat().st_mode & 0o7777
    assert mode == 0o1777, oct(mode)


def test_relax_mode_widens_permissions_to_match_the_directory_owner(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    f = d / "tool-x.log"
    f.write_text("x")
    f.chmod(0o600)

    pxlog._relax_mode(f, d)

    assert (f.stat().st_mode & 0o777) == 0o666
    assert f.stat().st_uid == d.stat().st_uid
    assert f.stat().st_gid == d.stat().st_gid


def test_log_event_never_crashes_when_the_lock_cannot_be_opened(tmp_path, monkeypatch):
    """The historical bug: a rotlock this uid cannot open raised an uncaught
    PermissionError out of log_event(), which is the LAST statement in most
    tool-* wrappers before they print their required JSON status line — a
    dropped log line must never mean a dropped tool response."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def acquire(self, *a, **k):
            raise PermissionError(13, "Permission denied")

        def release(self):
            pass

    monkeypatch.setattr(pxlog, "FileLock", _Boom)
    pxlog.log_event("probe", {"a": 1})  # must not raise
    assert not (tmp_path / "tool-probe.log").exists()


def test_log_event_never_crashes_when_the_data_file_cannot_be_opened(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    log_path = tmp_path / "tool-probe.log"
    log_path.write_text("pre-existing\n")
    log_path.chmod(0o000)
    try:
        pxlog.log_event("probe", {"a": 1})  # must not raise
    finally:
        log_path.chmod(0o644)


@pytest.mark.live
def test_log_event_survives_a_root_created_rotlock_and_data_file(tmp_path, monkeypatch):
    """End-to-end reproduction of the real regression, using real sudo — needs
    passwordless root on this host, so it is a `live` test rather than a
    portable CI one. A motion tool logs as root first; a pi-run caller (the
    common case for the same tool name reached a different way) must still
    be able to log afterward, and vice versa."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_dir.chmod(0o755)  # pi:pi, non-sticky — the state of logs/ before this fix
    monkeypatch.setenv("LOG_DIR", str(log_dir))

    # sudoers on this host has env_reset with a short env_keep list that does
    # not include LOG_DIR/PYTHONPATH, exactly like production's tool-* sudo
    # calls — so this must use the same `sudo env VAR=...` passthrough those
    # already rely on rather than subprocess.run's own env= kwarg, which sets
    # sudo's *own* environment, not what sudo forwards to its target.
    #
    # The venv interpreter, not bare `python3`: bin/tool-drive (the wrapper
    # that calls log_event) sources px-env, which activates .venv, before
    # running `python`. secure_path in sudoers strips a bare `python3` back
    # to the system interpreter, which has no filelock in root's site-packages
    # (it lives in pi's ~/.local) and would silently take the unlocked
    # fallback branch instead of reproducing the bug under test.
    venv_python = str(REPO_ROOT / ".venv" / "bin" / "python3")
    pythonpath = str(REPO_ROOT / "src")

    def _run_as_root(who: str) -> None:
        subprocess.run(
            ["sudo", "env", f"LOG_DIR={log_dir}", f"PYTHONPATH={pythonpath}",
             venv_python, "-c",
             f"from pxh.logging import log_event; log_event('drive', {{'who': '{who}'}})"],
            check=True, capture_output=True, text=True,
        )

    _run_as_root("root")
    log_path = log_dir / "tool-drive.log"
    assert log_path.stat().st_uid != 0, "log_event must chown down to the dir owner"

    pxlog.log_event("drive", {"who": "pi"})  # must succeed, not just avoid crashing

    _run_as_root("root-again")

    lines = log_path.read_text().splitlines()
    assert len(lines) == 3
    assert '"who": "root"' in lines[0]
    assert '"who": "pi"' in lines[1]
    assert '"who": "root-again"' in lines[2]
