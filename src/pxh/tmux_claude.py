"""Drive a persistent interactive Claude Code session living in tmux.

This replaces the one-shot `claude -p` subprocess pattern. A single long-lived
session keeps its context warm across calls, and — more importantly — answers
by running SPARK's own `bin/tool-*` scripts rather than printing JSON we then
have to parse back out of stdout.

The approach and every workaround below are ported from Adrian's ClawdCraft
bridge (`bridge/clawd.js`), which has been driving a Claude session in tmux on
pi5 long enough to have found the sharp edges. They are not re-derived here:

1.  `send-keys` targets the PANE ID, not the session name. Session-name
    targeting intermittently fails with "no current client" while every other
    subcommand keeps working.
2.  tmux 3.3a's `send-keys` fails whenever the server has NO attached client
    (command clients do not count) — i.e. exactly when nothing is watching.
    Fixed upstream in 3.4; picar is on 3.3a, so we hold a permanent read-only
    client to keep "at least one attached client" true.
3.  That holder needs a pty (`script(1)`) and a TERM. px-mind's systemd unit
    sets PATH but no TERM, and `tmux attach` refuses without one.
4.  Text and Enter are two separate sends with a gap. A single send races the
    terminal's own input handling and drops characters.
5.  Readiness is the prompt glyph appearing in `capture-pane`, not the session
    merely existing — `new-session` returns long before Claude is listening.

A dedicated socket (not the default one) keeps this server clear of Adrian's
own tmux sessions, so `tmux kill-server` in a login shell cannot take SPARK's
brain with it. Note the socket path is per-uid: anything invoked under sudo
lands in a different namespace and will not find this session.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parents[2]))

SOCKET = os.environ.get("PX_CLAUDE_TMUX_SOCKET", "/tmp/tmux-1000/px-mind")
SESSION = os.environ.get("PX_CLAUDE_TMUX_SESSION", "px-mind")
LAUNCHER = str(PROJECT_ROOT / "bin" / "px-claude-session")

# The session is sized explicitly so `window-size=latest` cannot shrink the
# pane when the read-only holder attaches with a different idea of the size.
PANE_WIDTH = 200
PANE_HEIGHT = 50

# Claude Code's input prompt glyph. Presence in capture-pane means "listening".
READY_GLYPH = "❯"

STARTUP_TIMEOUT_S = 45.0
_ENTER_GAP_S = 0.3

_last_error = ""


@dataclass(frozen=True)
class SessionSpec:
    """Everything that distinguishes one Claude session from another.

    Two of these exist, and the split is a trust boundary rather than a
    performance one: the privileged brain runs at the repo root holding SPARK's
    own tools, while the io session chews on untrusted text (social QA, public
    chat) from a scratch cwd with exactly one tool. Passing the spec explicitly
    keeps that boundary visible at every call site instead of burying it in
    module-level state that the wrong caller can inherit by accident.
    """

    name: str
    socket: str = SOCKET
    launcher: str = LAUNCHER
    cwd: str = str(PROJECT_ROOT)
    env: dict[str, str] = field(default_factory=dict)


def default_spec() -> SessionSpec:
    """The original single-session configuration, for callers predating the split."""
    return SessionSpec(name=SESSION)


def _spec(spec: SessionSpec | None) -> SessionSpec:
    return spec if spec is not None else default_spec()


def last_error() -> str:
    """Most recent tmux stderr, for callers that log a failure reason."""
    return _last_error


def _tmux(*args: str, timeout: float = 5.0, socket: str | None = None) -> str | None:
    """Run a tmux command on SPARK's socket. Returns stdout, or None on failure.

    Never raises: this sits under the cognitive loop, and a tmux hiccup must
    degrade to the existing LLM chain rather than kill the daemon.
    """
    global _last_error
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket or SOCKET, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _last_error = str(exc)
        return None
    if proc.returncode != 0:
        _last_error = (proc.stderr or "").strip()
        return None
    return proc.stdout


def _tmux_via_pty(*args: str, timeout: float = 5.0, socket: str | None = None) -> bool:
    """Run a tmux command wrapped in script(1) so it has a controlling tty.

    Workaround 2 above: on 3.3a `send-keys` consults the "current client" even
    when given an explicit target, and a plain subprocess has no tty to offer.
    """
    global _last_error
    cmd = ("tmux -S " + shlex.quote(socket or SOCKET) + " "
           + " ".join(shlex.quote(a) for a in args))
    try:
        proc = subprocess.run(
            ["script", "-qec", cmd, "/dev/null"],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "TERM": os.environ.get("TERM") or "tmux-256color"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _last_error = str(exc)
        return False
    if proc.returncode != 0:
        _last_error = (proc.stderr or proc.stdout or "").strip()
        return False
    return True


def session_exists(spec: SessionSpec | None = None) -> bool:
    s = _spec(spec)
    return _tmux("has-session", "-t", s.name, socket=s.socket) is not None


def brain_pane(spec: SessionSpec | None = None) -> str | None:
    """Pane ID of the session's first pane — the injection target (workaround 1)."""
    s = _spec(spec)
    out = _tmux("list-panes", "-t", s.name, "-F", "#{pane_id}", socket=s.socket)
    if not out:
        return None
    lines = [line for line in out.strip().splitlines() if line]
    return lines[0] if lines else None


def pane_ready(spec: SessionSpec | None = None) -> bool:
    """True once Claude is actually listening, not merely once tmux returned."""
    s = _spec(spec)
    pane = _tmux("capture-pane", "-t", s.name, "-p", socket=s.socket)
    return pane is not None and READY_GLYPH in pane


def ensure_session(timeout_s: float = STARTUP_TIMEOUT_S,
                   spec: SessionSpec | None = None) -> bool:
    """Start the session if it is not already up. Idempotent."""
    s = _spec(spec)
    if session_exists(s):
        return True
    Path(s.socket).parent.mkdir(parents=True, exist_ok=True)
    # `-e` passes the launcher's configuration through tmux rather than through
    # our own environment: the session outlives this process, so anything set
    # here has to travel with the session itself.
    env_args: list[str] = []
    for key, value in sorted(s.env.items()):
        env_args += ["-e", f"{key}={value}"]
    created = _tmux(
        "new-session", "-d", "-s", s.name,
        "-x", str(PANE_WIDTH), "-y", str(PANE_HEIGHT),
        *env_args,
        "-c", s.cwd, s.launcher,
        socket=s.socket,
    )
    if created is None:
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(1.0)
        if pane_ready(s):
            return True
    # The session exists but never showed a prompt. Report it as up only if
    # tmux still has it: a caller that falls back is better served by "the
    # brain is wedged" than by a hard failure that hides a half-started state.
    return session_exists(s)


def inject(text: str, spec: SessionSpec | None = None) -> bool:
    """Type one line into the session and press Enter.

    Newlines are flattened: a bare newline inside send-keys -l submits the
    prompt early, so a multi-line payload would arrive as several truncated
    turns instead of one.
    """
    s = _spec(spec)
    if not ensure_session(spec=s):
        return False
    clean = " ".join(text.split())
    if not clean:
        _last_error = "empty prompt"
        return False
    target = brain_pane(s) or s.name
    if not _tmux_via_pty("send-keys", "-t", target, "-l", clean, socket=s.socket):
        return False
    time.sleep(_ENTER_GAP_S)
    return _tmux_via_pty("send-keys", "-t", target, "Enter", socket=s.socket)


def reset_context(spec: SessionSpec | None = None) -> bool:
    """Clear the session's context without restarting it (Claude's /clear)."""
    return inject("/clear", spec=spec)


def kill_session(spec: SessionSpec | None = None) -> bool:
    s = _spec(spec)
    return _tmux("kill-session", "-t", s.name, socket=s.socket) is not None


class HolderClient:
    """Keeps one read-only client attached so send-keys works on tmux 3.3a.

    Without this, injection fails precisely when no human is watching — which
    is the normal state for a daemon, so the failure would look intermittent
    and correlate with nobody being around to see it.
    """

    def __init__(self, spec: SessionSpec | None = None) -> None:
        self._proc: subprocess.Popen | None = None
        self._spec = _spec(spec)

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        attach = (
            f"stty cols {PANE_WIDTH} rows {PANE_HEIGHT} 2>/dev/null; "
            f"exec tmux -S {shlex.quote(self._spec.socket)} "
            f"attach-session -r -t {shlex.quote(self._spec.name)}"
        )
        try:
            self._proc = subprocess.Popen(
                ["script", "-qec", attach, "/dev/null"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                # systemd provides no TERM and `tmux attach` refuses without one.
                env={**os.environ, "TERM": os.environ.get("TERM") or "tmux-256color"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            global _last_error
            _last_error = str(exc)
            self._proc = None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        """Kill the holder. A leaked holder is harmless but accumulates."""
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
