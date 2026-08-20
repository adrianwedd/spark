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
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from pxh import brain, brain_daemon  # noqa: E402
from pxh import logging as pxlogging  # noqa: E402

# A whole supervisor's acquire, in a process that shares nothing with this
# one but the filesystem. Deliberately calls the real entry point rather than
# reimplementing it: a child that took only one of the two required locks
# would reproduce the very defect this file exists to close.
_CHILD_ACQUIRE = """
from pxh import brain_daemon
print("acquired" if brain_daemon.acquire_supervisor_lock() else "refused")
"""


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
        spec = brain.spec_for_session(brain.BRAIN_SESSION)
        assert spec.socket == brain.brain_socket()
        assert brain_daemon.supervisor_lock_path() == Path(
            spec.socket + ".supervisor.lock"
        )


class TestContention:
    """Acceptance criterion 1, and the four cases criterion 2 splits into.

    The namespace a supervisor claims is not the socket alone while the
    bridge holds. It is the pair (socket, checkout), because every supervisor
    must also take its checkout's legacy lock. The four cases below state
    that pair exhaustively; the fourth is the post-removal target, and is the
    only one where the socket alone is the whole answer.
    """

    def test_two_checkouts_on_one_socket_cannot_both_acquire(
        self, monkeypatch, tmp_path
    ):
        """Case 1 — same socket, different checkouts: must contend.

        Exactly the case the old checkout-relative key could not see, and the
        defect #221 is about.
        """
        monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", str(tmp_path / "px-mind"))
        assert brain_daemon.acquire_supervisor_lock() is True
        try:
            monkeypatch.setattr(brain, "brain_root", lambda: tmp_path / "checkout-b")
            assert brain_daemon.acquire_supervisor_lock() is False, (
                "a second supervisor on the same socket must lose, whatever "
                "checkout it runs from"
            )
        finally:
            brain_daemon.release_supervisor_lock()

    def test_different_sockets_and_different_state_roots_coexist(
        self, monkeypatch, tmp_path
    ):
        """Case 2 — different sockets *and* different state roots: coexist.

        Run in a subprocess, and not for isolation theatre. `_supervisor_fd`
        and `_legacy_fd` are process globals, so a second in-process
        `acquire_supervisor_lock()` would overwrite the first supervisor's
        fds and leak its locks for the rest of the session. A subprocess is
        also the only form that can catch the failure mode a same-process
        test cannot see at all: an acquire that consults process state and
        returns True because *this* process already holds authority.

        This is the case the suite itself relies on. The autouse fixtures
        give every test a tmp_path socket *and* a tmp_path brain_root, so
        both halves of the pair differ per test and tests never contend.
        """
        monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", str(tmp_path / "socket-a"))
        assert brain_daemon.acquire_supervisor_lock() is True
        try:
            env = dict(os.environ)
            env["PX_BRAIN_TMUX_SOCKET"] = str(tmp_path / "socket-b")
            # brain_root() reads PX_STATE_DIR, so this is what actually moves
            # the child's legacy lock — the parent's brain_root is a
            # monkeypatched attribute the child cannot inherit.
            env["PX_STATE_DIR"] = str(tmp_path / "state-b")
            env["PYTHONPATH"] = str(SRC)
            proc = subprocess.run(
                [sys.executable, "-c", _CHILD_ACQUIRE],
                capture_output=True, text=True, env=env, timeout=30,
            )
            assert proc.stdout.strip() == "acquired", (
                "a supervisor on its own socket and its own state root must "
                f"start: stdout={proc.stdout!r} stderr={proc.stderr!r}"
            )
        finally:
            brain_daemon.release_supervisor_lock()

    def test_different_sockets_in_one_checkout_contend_during_migration(
        self, monkeypatch, tmp_path
    ):
        """Case 3 — different sockets, same checkout: contend, by design.

        The bridge's cost, stated rather than hidden. Both supervisors must
        take the same checkout-relative legacy lock, so the second loses even
        though its socket is free. Pinning it is the point: the honest
        migration-era contract is the pair, and a test that asserted plain
        "different sockets coexist" here would be asserting a fiction that
        only passes by skipping half of what a supervisor acquires.
        """
        monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", str(tmp_path / "socket-a"))
        assert brain_daemon.acquire_supervisor_lock() is True
        try:
            monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", str(tmp_path / "socket-b"))
            assert brain_daemon.acquire_supervisor_lock() is False, (
                "while the bridge holds, one checkout admits one supervisor "
                "regardless of socket"
            )
            # And it lost on the legacy lock specifically, not the socket —
            # otherwise this passes for the wrong reason the day the socket
            # key regresses.
            free = brain_daemon.supervisor_lock_path()
            fd = os.open(str(free), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            brain_daemon.release_supervisor_lock()

    def test_after_removal_the_socket_alone_is_the_namespace(
        self, monkeypatch, tmp_path
    ):
        """Case 4 — the post-removal target, as an executable contract.

        The follow-up deletes `_BRIDGE_HOLDS_LEGACY_LOCK`, the legacy path
        and this test together. Until then this is what the removal gate
        buys: case 3 inverts, and the socket becomes the whole namespace.
        """
        monkeypatch.setattr(brain_daemon, "_BRIDGE_HOLDS_LEGACY_LOCK", False)
        monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", str(tmp_path / "socket-a"))
        assert brain_daemon.acquire_supervisor_lock() is True
        first = brain_daemon._supervisor_fd
        try:
            monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", str(tmp_path / "socket-b"))
            assert brain_daemon.acquire_supervisor_lock() is True, (
                "with the legacy lock gone, one checkout admits one "
                "supervisor per socket"
            )
            assert brain_daemon._legacy_fd is None
        finally:
            # The second acquire overwrote the globals, so release the first
            # supervisor's fd by hand or it stays locked for the session.
            brain_daemon.release_supervisor_lock()
            brain_daemon._drop_lock(first)


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
