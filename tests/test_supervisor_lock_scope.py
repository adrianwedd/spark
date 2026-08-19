"""#221 — the supervisor guard must be scoped to the resource it guards.

The guard was keyed to `brain_root()` (checkout-relative) while the thing it
protects — the tmux socket every brain session runs on — is host-global. One
tmux server, five checkouts on this host, and a private guard per checkout;
`flock` is per-inode, so two supervisors in different checkouts contended over
nothing.

The same asymmetry is what let an autouse fixture disable the guard by
accident: relocating the *mailbox* silently relocated the *guard*, because one
was derived from the other.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from pxh import brain, brain_daemon  # noqa: E402
from pxh import logging as pxlogging  # noqa: E402


class TestSocketScopedGuard:
    """Item 1 — one resolution, two consumers."""

    def test_brain_socket_prefers_the_brain_specific_variable(self, monkeypatch):
        """PX_BRAIN_TMUX_SOCKET is the canonical seam because brain.py:223 is
        what actually consumes it."""
        monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", "/tmp/tmux-test/brain-a")
        monkeypatch.setenv("PX_CLAUDE_TMUX_SOCKET", "/tmp/tmux-test/claude-b")
        assert brain.brain_socket() == "/tmp/tmux-test/brain-a"

    def test_brain_socket_reads_the_claude_fallback_at_call_time(self, monkeypatch):
        """tmux_claude.SOCKET is bound at import (tmux_claude.py:43), so
        reading it is reading whatever the environment said when the module
        first loaded. The fallback is resolved at call time so an override can
        actually take effect."""
        monkeypatch.delenv("PX_BRAIN_TMUX_SOCKET", raising=False)
        monkeypatch.setenv("PX_CLAUDE_TMUX_SOCKET", "/tmp/tmux-test/claude-b")
        assert brain.brain_socket() == "/tmp/tmux-test/claude-b"

    def test_the_guard_is_keyed_to_the_socket_it_guards(self, monkeypatch):
        monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", "/tmp/tmux-test/px-mind")
        assert brain_daemon.supervisor_lock_path() == Path(
            "/tmp/tmux-test/px-mind.supervisor.lock"
        )

    def test_the_guard_does_not_move_when_only_the_checkout_moves(
        self, monkeypatch, tmp_path
    ):
        """The defect stated as a test. Relocating the mailbox used to
        relocate the guard, which is how the autouse fixture disabled it."""
        monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", "/tmp/tmux-test/px-mind")
        before = brain_daemon.supervisor_lock_path()
        monkeypatch.setattr(brain, "brain_root", lambda: tmp_path / "other-checkout")
        assert brain_daemon.supervisor_lock_path() == before

    def test_sessions_and_the_guard_resolve_the_same_socket(self, monkeypatch):
        """Keyed off literally the same call, so the two cannot drift."""
        monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", "/tmp/tmux-test/px-mind")
        spec = brain.spec_for_session(brain.IO_SESSION)
        assert spec.socket == brain.brain_socket()
        assert str(brain_daemon.supervisor_lock_path()).startswith(spec.socket)


class TestContention:
    """Acceptance criteria 1 and 2."""

    def test_two_checkouts_on_one_socket_cannot_both_acquire(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", str(tmp_path / "px-mind"))
        assert brain_daemon.acquire_supervisor_lock() is True
        try:
            # A second supervisor, same socket, different state root — which is
            # exactly the case the old checkout-relative key could not see.
            monkeypatch.setattr(brain, "brain_root", lambda: tmp_path / "checkout-b")
            assert brain_daemon.acquire_supervisor_lock() is False, (
                "a second supervisor on the same socket must lose, whatever "
                "checkout it runs from"
            )
        finally:
            brain_daemon.release_supervisor_lock()

    def test_supervisors_on_different_sockets_coexist(self, monkeypatch, tmp_path):
        """Synthetic sockets are separate namespaces; tests must not contend."""
        monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", str(tmp_path / "socket-a"))
        assert brain_daemon.acquire_supervisor_lock() is True
        try:
            monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", str(tmp_path / "socket-b"))
            other = brain_daemon.supervisor_lock_path()
            fd = os.open(str(other), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            brain_daemon.release_supervisor_lock()


class TestMigrationBridge:
    """Item 2. Changing the key while the incumbent holds the legacy lock
    would itself open a two-supervisor window."""

    def test_the_bridge_holds_the_legacy_lock_too(self, monkeypatch, tmp_path):
        """A pre-bridge binary in the same checkout guards on
        brain_root()/.supervisor.lock and knows nothing about the socket lock,
        so the new binary must hold both."""
        monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", str(tmp_path / "px-mind"))
        assert brain_daemon.acquire_supervisor_lock() is True
        try:
            legacy = brain.brain_root() / ".supervisor.lock"
            fd = os.open(str(legacy), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                with pytest.raises(OSError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        finally:
            brain_daemon.release_supervisor_lock()

    def test_a_lost_legacy_race_releases_the_socket_lock(self, monkeypatch, tmp_path):
        """Otherwise a supervisor that correctly refused to start leaves the
        socket guarded by a process that is not supervising, and nothing can
        ever start again."""
        monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", str(tmp_path / "px-mind"))
        root = brain.brain_root()
        root.mkdir(parents=True, exist_ok=True)
        holder = os.open(str(root / ".supervisor.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert brain_daemon.acquire_supervisor_lock() is False
            socket_lock = brain_daemon.supervisor_lock_path()
            fd = os.open(str(socket_lock), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)


class TestRecordIdentity:
    """Item 3 — records carried ts and event and nothing identifying the
    emitting process, which is what let pytest output masquerade as a
    concurrency event."""

    def test_every_record_identifies_its_emitting_process(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            brain_daemon, "log_event", lambda name, payload: captured.update(payload)
        )
        brain_daemon._log("supervisor_start")
        assert captured["event"] == "supervisor_start"
        assert captured["pid"] == os.getpid()
        assert captured["instance"] == brain_daemon._INSTANCE_ID
        assert captured["boot_id"] == brain_daemon._BOOT_ID

    def test_instance_id_survives_pid_reuse(self):
        """pid alone cannot separate two supervisors across a restart, which
        is the case forensics actually needs to distinguish."""
        assert len(brain_daemon._INSTANCE_ID) == 12
        int(brain_daemon._INSTANCE_ID, 16)  # hex, or this raises

    def test_boot_id_is_immune_to_a_clock_step(self):
        """No RTC on this host; timesyncd stepped the clock ~49 minutes
        forward on 2026-08-19 and materially confused one reconstruction."""
        path = Path("/proc/sys/kernel/random/boot_id")
        if not path.exists():
            pytest.skip("no boot_id on this kernel")
        assert brain_daemon._BOOT_ID == path.read_text(encoding="utf-8").strip()


class TestObservabilityIsolation:
    """Item 4 — test isolation must cover observability as well as state."""

    def test_log_dir_is_resolved_at_call_time(self, monkeypatch, tmp_path):
        """LOG_DIR was resolved once at import (logging.py:45) and read as a
        module global by log_event, so the documented override could never
        take effect in-process."""
        monkeypatch.setenv("LOG_DIR", str(tmp_path / "elsewhere"))
        assert pxlogging.log_dir() == tmp_path / "elsewhere"

    def test_log_event_writes_where_log_dir_points_now(self, monkeypatch, tmp_path):
        target = tmp_path / "logs-now"
        monkeypatch.setenv("LOG_DIR", str(target))
        pxlogging.log_event("scope-probe", {"event": "probe"})
        line = (target / "tool-scope-probe.log").read_text(encoding="utf-8").splitlines()[0]
        assert json.loads(line)["event"] == "probe"

    def test_ordinary_tests_do_not_write_production_logs(self):
        """Acceptance criterion 3, asserted from inside an ordinary test."""
        assert pxlogging.log_dir() != ROOT / "logs"

    def test_the_fixture_moves_the_socket_with_the_log_dir(self, tmp_path):
        """Setting both in one fixture is the point: a synthetic socket
        implies a synthetic guard by construction, so a test cannot acquire a
        namespace without also acquiring the guard that belongs to it."""
        assert str(pxlogging.log_dir()).startswith(str(tmp_path))
        assert brain.brain_socket().startswith(str(tmp_path))

    def test_a_test_that_sets_its_own_log_dir_still_wins(self, monkeypatch, tmp_path):
        """Mirrors test_a_test_that_sets_its_own_session_path_still_wins: the
        fixture sets its value at setup, so a test's own setenv runs after."""
        mine = tmp_path / "mine"
        monkeypatch.setenv("LOG_DIR", str(mine))
        assert pxlogging.log_dir() == mine
