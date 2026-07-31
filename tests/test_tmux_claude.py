"""Tests for the persistent tmux Claude session driver.

The point of these is the workarounds, not the happy path. Every one of them
exists because of a failure mode that only appears in production — no attached
client, a session-name target, a missing TERM — so a regression would look like
"injection intermittently does nothing" on a robot with nobody watching.
"""
import subprocess

import pytest

from pxh import tmux_claude


class _Recorder:
    """Captures subprocess.run calls and replays scripted results."""

    def __init__(self, results):
        self.calls = []
        self._results = list(results)

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, "env": kwargs.get("env") or {}})
        rc, out = self._results.pop(0) if self._results else (0, "")
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")


@pytest.fixture(autouse=True)
def _quiet_sleep(monkeypatch):
    monkeypatch.setattr(tmux_claude.time, "sleep", lambda _s: None)


def _install(monkeypatch, results):
    rec = _Recorder(results)
    monkeypatch.setattr(tmux_claude.subprocess, "run", rec)
    return rec


def test_every_tmux_call_targets_sparks_own_socket(monkeypatch):
    """A shared default socket means `tmux kill-server` in a login shell takes
    SPARK's brain with it."""
    rec = _install(monkeypatch, [(0, "")])
    tmux_claude.session_exists()
    assert rec.calls[0]["cmd"][:3] == ["tmux", "-S", tmux_claude.SOCKET]


def test_inject_targets_the_pane_id_not_the_session_name(monkeypatch):
    """Workaround 1: session-name targets intermittently fail "no current
    client" while pane-ID targets keep working."""
    rec = _install(monkeypatch, [
        (0, ""),          # has-session
        (0, "%7\n"),      # list-panes -> pane id
        (0, ""),          # send-keys -l   (via script)
        (0, ""),          # send-keys Enter (via script)
    ])
    assert tmux_claude.inject("hello") is True
    # send-keys is embedded in the `script -qec` command string, not a separate
    # argv element, so these have to be matched against the joined command.
    send_calls = [" ".join(c["cmd"]) for c in rec.calls if "send-keys" in " ".join(c["cmd"])]
    assert len(send_calls) == 2
    for call in send_calls:
        assert "%7" in call, f"must target the pane id, got {call}"
        assert f"-t {tmux_claude.SESSION}" not in call


def test_send_keys_goes_through_a_pty(monkeypatch):
    """Workaround 2: tmux 3.3a's send-keys needs a controlling tty, which a
    plain subprocess does not have."""
    rec = _install(monkeypatch, [(0, ""), (0, "%1\n"), (0, ""), (0, "")])
    tmux_claude.inject("hello")
    send_calls = [c for c in rec.calls if "send-keys" in " ".join(c["cmd"])]
    assert send_calls, "no send-keys call recorded"
    for call in send_calls:
        assert call["cmd"][0] == "script", f"send-keys must be pty-wrapped: {call['cmd']}"


def test_pty_calls_always_carry_a_term(monkeypatch):
    """Workaround 3: px-mind's systemd unit sets PATH but no TERM, and tmux
    refuses to attach without one."""
    monkeypatch.delenv("TERM", raising=False)
    rec = _install(monkeypatch, [(0, ""), (0, "%1\n"), (0, ""), (0, "")])
    tmux_claude.inject("hello")
    for call in rec.calls:
        if call["cmd"][0] == "script":
            assert call["env"].get("TERM"), "pty wrapper must set TERM"


def test_inject_sends_text_and_enter_separately(monkeypatch):
    """Workaround 4: one combined send races the terminal and drops characters."""
    rec = _install(monkeypatch, [(0, ""), (0, "%1\n"), (0, ""), (0, "")])
    tmux_claude.inject("hello world")
    sends = [c["cmd"] for c in rec.calls if c["cmd"][0] == "script"]
    assert "-l" in " ".join(sends[0]), "first send must be literal text"
    assert "Enter" in " ".join(sends[1]), "second send must be Enter"


def test_inject_flattens_newlines(monkeypatch):
    """A bare newline inside send-keys -l submits early, splitting one prompt
    into several truncated turns."""
    rec = _install(monkeypatch, [(0, ""), (0, "%1\n"), (0, ""), (0, "")])
    tmux_claude.inject("line one\nline two\n\nline three")
    literal = [c["cmd"] for c in rec.calls if c["cmd"][0] == "script"][0]
    payload = " ".join(literal)
    assert "\n" not in payload
    assert "line one line two line three" in payload


def test_ensure_session_waits_for_the_prompt_not_just_the_session(monkeypatch):
    """Workaround 5: new-session returns long before Claude is listening."""
    rec = _install(monkeypatch, [
        (1, ""),               # has-session -> absent
        (0, ""),               # new-session
        (0, "starting up"),    # capture-pane -> not ready yet
        (0, f"{tmux_claude.READY_GLYPH} "),  # capture-pane -> ready
    ])
    assert tmux_claude.ensure_session(timeout_s=10) is True
    captures = [c for c in rec.calls if "capture-pane" in c["cmd"]]
    assert len(captures) == 2, "must keep polling until the prompt glyph appears"


def test_ensure_session_is_idempotent(monkeypatch):
    rec = _install(monkeypatch, [(0, "")])
    assert tmux_claude.ensure_session() is True
    assert not any("new-session" in c["cmd"] for c in rec.calls), \
        "must not restart a session that is already up"


def test_tmux_failure_never_raises(monkeypatch):
    """This sits under the cognitive loop — a tmux hiccup must degrade to the
    existing LLM chain, not kill the daemon."""
    def _boom(*a, **k):
        raise OSError("tmux not installed")

    monkeypatch.setattr(tmux_claude.subprocess, "run", _boom)
    assert tmux_claude.session_exists() is False
    assert tmux_claude.inject("hello") is False
    assert tmux_claude.ensure_session(timeout_s=1) is False
    assert "tmux not installed" in tmux_claude.last_error()


def test_inject_refuses_an_empty_prompt(monkeypatch):
    rec = _install(monkeypatch, [(0, "")])
    assert tmux_claude.inject("   \n  ") is False
    assert not any("send-keys" in " ".join(c["cmd"]) for c in rec.calls)


def test_holder_client_attaches_read_only(monkeypatch):
    """A read-write holder would let a stray keystroke reach the brain, and
    `attach` without -r steals the pane size from the session."""
    captured = {}

    class _Proc:
        def poll(self):
            return None

    def _popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        return _Proc()

    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr(tmux_claude.subprocess, "Popen", _popen)
    holder = tmux_claude.HolderClient()
    holder.start()

    assert holder.alive() is True
    joined = " ".join(captured["cmd"])
    assert "attach-session -r" in joined, "holder must be read-only"
    assert f"stty cols {tmux_claude.PANE_WIDTH}" in joined, \
        "holder must match the session size or window-size=latest shrinks the pane"
    assert captured["env"].get("TERM"), "attach refuses without TERM"


def test_holder_start_is_idempotent(monkeypatch):
    starts = []

    class _Proc:
        def poll(self):
            return None

    monkeypatch.setattr(tmux_claude.subprocess, "Popen",
                        lambda cmd, **k: (starts.append(cmd), _Proc())[1])
    holder = tmux_claude.HolderClient()
    holder.start()
    holder.start()
    assert len(starts) == 1, "must not spawn a second holder over a live one"
