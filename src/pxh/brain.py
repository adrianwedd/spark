"""Ask SPARK's persistent Claude session a question and get an answer back.

This is the replacement for `claude -p`. `tmux_claude` can already *type into* a
resident Claude Code session; what was missing — and what kept every call site on
one-shot subprocesses — is a way to read a reply. Scraping the pane was the
obvious option and the wrong one: `capture-pane` returns rendered terminal
output, so a reply would be at the mercy of wrapping, spinners, ANSI escapes and
the pane's scrollback height. Instead the session answers the way it does
everything else, by running a tool: `bin/tool-brain-reply` writes the answer to a
file and we poll for it.

    pane for humans, filesystem for machines

The mailbox lives at `state/brain/<session>/`:

    inbox/<id>.json     request written by ask_brain, deleted by the reply tool
    outbox/<id>.json    reply written by tool-brain-reply, read and removed here
    dead/<id>.json      requests swept aside when a session is (re)created
    current.json        the in-flight request — what wedge detection keys on
    validation.json     proof a real handshake landed — what readiness means now

Everything here degrades rather than raises. A caller that gets `None` falls back
to the Ollama tier chain exactly as it does today when Claude is unreachable;
that is the whole contract, and it is why no failure path below is allowed to
propagate an exception into a daemon.

## Why the directory is world-writable

`state/health/` learned this the hard way and the reasoning transfers verbatim:
SPARK's daemons do not all run as the same user. Anything reached under `sudo`
(the wander path elevates for GPIO) would write here as root, and a root-created
0755 directory locks every `pi` daemon out of `atomic_write`'s `mkstemp`, which
needs *directory* write permission. So the mailbox directories are created 1777
— sticky, world-writable, like `/tmp` — and re-chmodded on every write so
whichever user wins the creation race, both can still write. The single-flight
lock file gets the same treatment for the same reason: a root-created 0644 lock
would hand every `pi` daemon an EACCES instead of a queue position.

Do not "tighten" either of these to 0755/0644.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from . import health, tmux_claude
from .logging import log_event
from .state import PROJECT_ROOT, atomic_write
from .time import utc_timestamp

try:  # pragma: no cover - exercised by the import-failure path only
    from filelock import FileLock, Timeout as FileLockTimeout
except ImportError:  # pragma: no cover
    FileLock = None  # type: ignore[assignment]
    FileLockTimeout = Exception  # type: ignore[assignment,misc]


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

BRAIN_SESSION = "spark-brain"
IO_SESSION = "spark-io"

# The one spelling of the reply tool, and the allowlist pattern that admits it.
#
# Claude Code matches a `Bash(...)` rule against the command it is about to run,
# by prefix — so this pattern admits an absolute invocation and nothing else. A
# bare `tool-brain-reply` or a repo-relative `bin/tool-brain-reply` misses it and
# raises a permission dialog instead, which is a wedge: the session is waiting on
# a human who isn't there, while a daemon waits on the session. Absolute is also
# the only spelling that can work for the io session at all, whose cwd is
# deliberately outside the repository.
#
# Everything that names the tool derives from here: both allowlists, the nudge,
# and — via the {{TOOL_BRAIN_REPLY}} placeholder that bin/px-claude-session
# substitutes at launch — both system prompts. They are one interface, and when
# they disagreed every request hit the dialog.
TOOL_BRAIN_REPLY = str(Path(PROJECT_ROOT) / "bin" / "tool-brain-reply")
TOOL_BRAIN_REPLY_ALLOW = f"Bash({TOOL_BRAIN_REPLY}:*)"

# Which session handles which kind of request. This is a trust boundary, not
# load balancing: `io` kinds carry text SPARK did not write — a social post
# being QA'd, a stranger's message to the public chat endpoint — and that text
# reaches a session holding exactly one tool, from a scratch cwd, with no
# repository access. Anything absent from this map runs on the privileged
# brain, so adding a kind that handles untrusted input means adding it HERE.
_IO_KINDS = frozenset({"post_qa", "public_chat", "obi_chat"})

# Per-kind wall-clock deadline. These bound one turn; the per-type cooldowns and
# daily cap in claude_session.py still sit in front of ask_brain and bound how
# many turns happen at all.
_DEADLINE_S: dict[str, int] = {
    "post_qa": 120,
    "public_chat": 60,
    "obi_chat": 60,
    "reflection": 120,
    "research": 300,
    "compose": 300,
    "blog": 300,
    "consolidate": 600,
    "self_debug": 900,
    "evolve": 1800,
}
DEFAULT_DEADLINE_S = 300

# How long to wait for the single-flight lock before giving up and falling back.
# Deliberately short: a caller queued behind a slow turn is better served by the
# Ollama tiers than by blocking a daemon loop for minutes.
LOCK_WAIT_S = float(os.environ.get("PX_BRAIN_LOCK_WAIT_S", "10"))

POLL_INTERVAL_S = 0.25

# Replies are answers, not payloads. A cap keeps a runaway session (or a
# hijacked io session) from filling the SD card through the reply channel.
MAX_REPLY_BYTES = 256 * 1024

# --------------------------------------------------------------------------
# Validation budget (§2.6)
# --------------------------------------------------------------------------

# The glyph wait that `ensure_session()` already performs internally. This is
# the SAME object, not a copy of the number: there is one glyph wait per session
# start, it lives inside ensure_session, and this term accounts for it. An
# earlier draft waited again in the supervisor and spent the same 45s twice.
STARTUP_CEILING_S = tmux_claude.STARTUP_TIMEOUT_S

# Let the pane finish drawing before typing into it.
SETTLE_S = float(os.environ.get("PX_BRAIN_SETTLE_S", "2"))

# One first turn: read a small JSON file, run one Bash tool. Generous for that,
# because a first turn pays model warm-up and permission evaluation.
HANDSHAKE_TIMEOUT_S = float(os.environ.get("PX_BRAIN_HANDSHAKE_TIMEOUT_S", "60"))
HANDSHAKE_ATTEMPTS = int(os.environ.get("PX_BRAIN_HANDSHAKE_ATTEMPTS", "2"))

# Total time one validation may consume. NOT derived from systemd: px-brain is
# Type=simple with no TimeoutStartSec and no WatchdogSec, so systemd has no
# slowness timeout to breach. What binds is that tick() walks both sessions in
# one thread, so time spent validating one is time the other is not getting a
# health write. The 0.6 leaves margin for the rest of the tick.
VALIDATION_CEILING_S = 0.6 * min(health.STALE_AFTER_S["px-brain"],
                                 health.STALE_AFTER_S["px-brain-io"])

# The four states. These exact strings reach log lines and px-brain-status,
# because the vocabulary a human uses to describe the fault should be the
# vocabulary the tool prints.
VALIDATED = "validated"
VALIDATING = "validating"
NO_MARKER = "no_marker"
SESSION_ABSENT = "session_absent"

# Unlike the mailbox directories, this file has exactly one writer — the
# supervisor, always `pi` — and every other process only reads it. Handing
# write permission to uids that never write would let a confused caller forge a
# `validated` marker for a session that cannot answer, which is the exact claim
# this design exists to make unforgeable.
_MARKER_MODE = 0o644

# The model the launcher gives a session when nothing overrides it. Must stay
# equal to bin/px-claude-session's own default, which
# test_the_configured_model_default_matches_the_launcher pins.
DEFAULT_TMUX_MODEL = "claude-haiku-4-5-20251001"

_DIR_MODE = 0o1777


def _log(event: str, **fields: Any) -> None:
    """Structured log line. Never raises — logging must not break a fallback."""
    try:
        log_event("brain", {"event": event, **fields})
    except Exception:  # noqa: BLE001 - logging is never load-bearing
        pass


def session_for_kind(kind: str) -> str:
    return IO_SESSION if kind in _IO_KINDS else BRAIN_SESSION


def deadline_for_kind(kind: str) -> int:
    return _DEADLINE_S.get(kind, DEFAULT_DEADLINE_S)


def spec_for_session(session: str) -> tmux_claude.SessionSpec:
    """tmux configuration for one session, including its launcher envelope.

    The io session's envelope is the security property: one tool, and a cwd
    outside the repository so a prompt-injected turn has nothing local to read.
    """
    socket = os.environ.get("PX_BRAIN_TMUX_SOCKET", tmux_claude.SOCKET)
    if session == IO_SESSION:
        cwd = session_dir(IO_SESSION)
        return tmux_claude.SessionSpec(
            name=IO_SESSION,
            socket=socket,
            cwd=str(cwd),
            env={
                "PX_BRAIN_SESSION": IO_SESSION,
                "PX_CLAUDE_ALLOWED_TOOLS": TOOL_BRAIN_REPLY_ALLOW,
                "PX_CLAUDE_CWD": str(cwd),
            },
        )
    return tmux_claude.SessionSpec(
        name=BRAIN_SESSION,
        socket=socket,
        cwd=str(PROJECT_ROOT),
        env={"PX_BRAIN_SESSION": BRAIN_SESSION},
    )


# --------------------------------------------------------------------------
# Mailbox layout
# --------------------------------------------------------------------------

def _state_dir() -> Path:
    root = Path(os.environ.get("PROJECT_ROOT", PROJECT_ROOT))
    return Path(os.environ.get("PX_STATE_DIR", root / "state"))


def brain_root() -> Path:
    return _state_dir() / "brain"


def session_dir(session: str) -> Path:
    # Session names are internal constants, but normalise defensively — a
    # traversal here would let a caller-supplied name escape state/.
    safe = session.replace("/", "_").replace("..", "_").strip() or "unknown"
    return brain_root() / safe


def inbox_dir(session: str) -> Path:
    return session_dir(session) / "inbox"


def outbox_dir(session: str) -> Path:
    return session_dir(session) / "outbox"


def dead_dir(session: str) -> Path:
    return session_dir(session) / "dead"


def current_path(session: str) -> Path:
    return session_dir(session) / "current.json"


def validation_path(session: str) -> Path:
    return session_dir(session) / "validation.json"


def configured_model(session: str) -> str:
    """The model this session was launched (or last switched) to.

    Read from the environment the launcher reads, so the supervisor's idea of
    the configured model and the session's actual model come from one source.
    """
    return os.environ.get("PX_CLAUDE_TMUX_MODEL", DEFAULT_TMUX_MODEL)


def read_validation_marker(session: str) -> dict[str, Any] | None:
    """The marker as written, or None if absent or unreadable.

    Reads are lenient in one direction only: anything we cannot parse is absent,
    never validated. `ValueError` is in the except clause (not just `OSError`)
    because a corrupt file's `read_text(encoding="utf-8")` raises
    `UnicodeDecodeError`, a `ValueError`, not an `OSError` — and this function
    sits on the `ask_brain` chain, whose only contract with its callers is
    "never raise, return None on failure". `json.JSONDecodeError` is already a
    `ValueError`, so this narrows nothing there; it only widens the SD-card
    corruption case.
    """
    try:
        data = json.loads(validation_path(session).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_validation_marker(session: str, *, state: str, request_id: str,
                            model: str, attempt: int) -> bool:
    """Record the outcome of a handshake. Returns False if it could not land."""
    if not ensure_mailbox(session):
        return False
    marker = {
        "state": state,
        "request_id": request_id,
        "model": model,
        "attempt": attempt,
        "updated_at": utc_timestamp(),
    }
    path = validation_path(session)
    try:
        atomic_write(path, json.dumps(marker, indent=2))
    except OSError:
        return False
    try:
        # atomic_write's mkstemp yields 0600, which every reader but the writer
        # would get EACCES on. 0644 is the mode; the chmod is how it gets there.
        os.chmod(path, _MARKER_MODE)
    except OSError:
        pass
    return True


def clear_validation_marker(session: str) -> None:
    """Delete the marker. Every reader now sees `no_marker`."""
    try:
        validation_path(session).unlink()
    except OSError:
        pass


def _marker_age_s(marker: dict[str, Any]) -> float:
    """Seconds since the marker was written; +inf if it does not say."""
    stamp = marker.get("updated_at")
    if not isinstance(stamp, str):
        return float("inf")
    try:
        import datetime as _dt

        written = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if written.tzinfo is None:
            written = written.replace(tzinfo=_dt.timezone.utc)
        return (_dt.datetime.now(_dt.timezone.utc) - written).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def session_state(session: str, model: str | None = None) -> str:
    """Derive whether a session may be trusted with a request, at read time.

    Never stored — same discipline as health.py, and for the same reason: a dead
    supervisor must not be able to leave a lying `validated` behind. `model` is
    optional and usually omitted; a caller that accepts the session's own model
    asks only whether the session can answer at all.
    """
    if not tmux_claude.session_exists(spec_for_session(session)):
        return SESSION_ABSENT
    marker = read_validation_marker(session)
    if marker is None:
        return NO_MARKER
    state = marker.get("state")
    if state == VALIDATED:
        if model and marker.get("model") != model:
            return NO_MARKER
        return VALIDATED
    if state == VALIDATING and _marker_age_s(marker) <= VALIDATION_CEILING_S:
        return VALIDATING
    # A stale `validating` marker means a supervisor died mid-handshake. The
    # repair is the same as any other "nobody is working on it".
    return NO_MARKER


def _ensure_dir(path: Path) -> Path | None:
    """Create a mailbox directory world-writable. Returns None if we cannot.

    Never raises: this is called on the request path of a daemon that must
    survive a read-only filesystem by falling back, not by crashing.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    try:
        if (path.stat().st_mode & 0o7777) != _DIR_MODE:
            os.chmod(path, _DIR_MODE)
    except OSError:
        pass  # not the owner — whoever created it already set the mode
    return path


def ensure_mailbox(session: str) -> bool:
    """Create the full mailbox tree for a session. Idempotent."""
    for path in (brain_root(), session_dir(session), inbox_dir(session),
                 outbox_dir(session), dead_dir(session)):
        if _ensure_dir(path) is None:
            return False
    return True


def sweep_pending(session: str) -> int:
    """Move every pending inbox entry to dead/. Returns how many were swept.

    Called when a session is (re)created. Daemons own their own timeout and
    fallback, so by the time a session restarts, every request that was in
    flight has already been answered by a fallback path — replaying them would
    produce answers nobody is waiting for, and burn budget doing it.
    """
    if not ensure_mailbox(session):
        return 0
    swept = 0
    for entry in sorted(inbox_dir(session).glob("*.json")):
        try:
            entry.replace(dead_dir(session) / entry.name)
            swept += 1
        except OSError:
            continue
    try:
        current_path(session).unlink()
    except OSError:
        pass
    return swept


def sweep_one(session: str, request_id: str) -> bool:
    """Move exactly one named inbox entry to dead/. Returns whether it moved.

    The narrow sweep (§2.3 step 1). Unlike `sweep_pending` this names its target
    rather than globbing, which is what makes it safe to run without the
    single-flight lock: there is no discovery step that could pick up a request
    written by someone still waiting on it. It exists to record the orphan of a
    supervisor that died mid-handshake — the replacement handshake mints a fresh
    id, so nothing else will ever claim that file.
    """
    if not ensure_mailbox(session):
        return False
    entry = inbox_dir(session) / f"{request_id}.json"
    try:
        entry.replace(dead_dir(session) / entry.name)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Request metering
# --------------------------------------------------------------------------

def _meter_path() -> Path:
    return brain_root() / "meter.json"


def record_request(kind: str) -> None:
    """Count one brain request, per kind, per day.

    This exists because reflection Tier 2 has historically bypassed
    `claude_session.py`'s quota accounting entirely, so its spend was invisible
    — hundreds of unbudgeted calls with nothing to show for them but a bill.
    `ask_brain` is the first single chokepoint every Claude request passes
    through, so counting here cannot be bypassed the way the old per-call-site
    accounting was. This is observability, not a quota: it never blocks.
    """
    if _ensure_dir(brain_root()) is None:
        return
    day = utc_timestamp()[:10]
    try:
        data = json.loads(_meter_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    if data.get("day") != day:
        data = {"day": day, "by_kind": {}}
    by_kind = data.setdefault("by_kind", {})
    by_kind[kind] = by_kind.get(kind, 0) + 1
    data["total"] = sum(by_kind.values())
    data["updated_ts"] = utc_timestamp()
    try:
        atomic_write(_meter_path(), json.dumps(data, indent=2))
    except OSError:
        pass


def meter_summary() -> dict[str, Any]:
    try:
        return json.loads(_meter_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"day": None, "by_kind": {}, "total": 0}


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------

def _lock_for(session: str):
    """Single-flight lock for one session, or None if locking is unavailable.

    Two concurrent `send-keys` runs interleave into one garbled prompt, so
    without a lock the failure mode is not "slow" but "both answers wrong".
    Callers treat None as a hard failure and fall back.
    """
    if FileLock is None:
        return None
    lock_path = session_dir(session) / ".lock"
    lock = FileLock(str(lock_path))
    return lock


def _relax_lock_mode(session: str) -> None:
    """Make the lock file writable by every uid that might need it."""
    try:
        os.chmod(session_dir(session) / ".lock", 0o666)
    except OSError:
        pass


def nudge_line(session: str, request_id: str) -> str:
    return (
        f"NEW REQUEST {inbox_dir(session)}/{request_id}.json — read it, do the "
        f"work, then reply with: {TOOL_BRAIN_REPLY} {request_id} '<json>'"
    )


def collect_reply(session: str, request_id: str) -> dict[str, Any] | None:
    """Read and remove the reply, if it has landed.

    `tool-brain-reply` writes via atomic rename, so a file that exists is a
    complete file — but a reply that fails to parse is still treated as absent
    rather than as an error, so a half-written file from a future writer that
    forgets that guarantee degrades into a timeout instead of a wrong answer.
    """
    path = outbox_dir(session) / f"{request_id}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, ValueError):
        return None
    try:
        reply = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(reply, dict):
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return reply


def cleanup_request(session: str, request_id: str) -> None:
    """Drop this request's inbox entry, and clear current.json IF it is ours.

    `current.json` is what wedge detection keys on, so leaving a stale one
    behind after a caller-side timeout would report a healthy session as
    wedged and get it killed — that half of the cleanup runs unconditionally.
    But `current.json` can belong to someone else: a caller that bails at
    ask_brain's post-lock recheck runs this in its `finally` while the
    supervisor's own in-flight request may already be sitting in the file.
    Deleting on request-id, not unconditionally, keeps "cleanup touches only
    this request" true regardless of who else is using the file right now.

    That id check needs a readable file to compare against, and unreadable
    splits three ways rather than one. Absent is fine — nothing to clean up.
    But corrupt JSON, valid JSON that isn't an object, or an object missing
    "id" all describe a file no request can ever claim by id — leaving one of
    those behind is the exact permanent-stale-file failure this function
    exists to prevent, so those are unlinked on sight rather than treated as
    "not ours". Only a *readable, id-bearing* file naming someone else is left
    alone.
    """
    try:
        (inbox_dir(session) / f"{request_id}.json").unlink()
    except OSError:
        pass
    try:
        raw = current_path(session).read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError:
        return
    try:
        current = json.loads(raw)
    except json.JSONDecodeError:
        current = None
    if not (isinstance(current, dict) and "id" in current):
        # Unclaimable — no future cleanup_request call could ever match it.
        try:
            current_path(session).unlink()
        except OSError:
            pass
        return
    if current.get("id") == request_id:
        try:
            current_path(session).unlink()
        except OSError:
            pass


def ask_brain(
    kind: str,
    payload: Any,
    timeout_s: float | None = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Send one request to the persistent Claude session and wait for its reply.

    Returns the reply dict, or None on any failure — no session, no lock, an
    unvalidated session, or no answer before the deadline. None means "fall
    back", and every caller must have a fallback; there is deliberately no
    exception path.
    """
    session = session_for_kind(kind)
    if timeout_s is None:
        timeout_s = float(deadline_for_kind(kind))

    if not ensure_mailbox(session):
        _log("brain_unavailable", kind=kind, session=session,
             reason="mailbox not writable")
        return None

    # Fast path, before the lock. During startup this is the common case, and a
    # caller that queued behind the supervisor's lock would spend LOCK_WAIT_S
    # learning what the marker already said.
    state = session_state(session, model=model)
    if state != VALIDATED:
        _log("brain_unavailable", kind=kind, session=session,
             reason="session not validated", state=state)
        return None

    lock = _lock_for(session)
    if lock is None:
        _log("brain_unavailable", kind=kind, session=session,
             reason="filelock unavailable")
        return None

    started = time.monotonic()
    try:
        lock.acquire(timeout=LOCK_WAIT_S)
    except (FileLockTimeout, OSError):
        _log("brain_busy", kind=kind, session=session,
                  waited_s=round(time.monotonic() - started, 2))
        return None
    _relax_lock_mode(session)

    request_id = str(uuid.uuid4())
    try:
        # Re-derive on the far side of the lock. The check above is
        # check-then-act across a lock boundary, and the window is not
        # theoretical: the supervisor holds this same lock for a model change or
        # a recycle, so a caller can pass the check, block here, and wake up
        # after a `/clear`. A slow supervisor costs ten seconds; a quick one
        # gets a real request injected into a session that has just forgotten
        # its identity prompt, and a confident answer produced with no context.
        # Only this second check tells those apart.
        state = session_state(session, model=model)
        if state != VALIDATED:
            _log("brain_unavailable", kind=kind, session=session,
                 reason="invalidated while waiting for the lock", state=state)
            return None

        spec = spec_for_session(session)

        # The deadline travels with the request so the session can see how long
        # it has, and so wedge detection has something to compare against.
        deadline = time.time() + timeout_s
        request = {
            "id": request_id,
            "kind": kind,
            "payload": payload,
            "deadline": deadline,
            "created_at": utc_timestamp(),
        }
        try:
            atomic_write(inbox_dir(session) / f"{request_id}.json",
                         json.dumps(request, indent=2))
            atomic_write(current_path(session),
                         json.dumps({"id": request_id, "kind": kind,
                                     "deadline": deadline}, indent=2))
        except OSError as exc:
            _log("brain_unavailable", kind=kind, session=session, reason=str(exc))
            return None

        record_request(kind)

        if not tmux_claude.inject(nudge_line(session, request_id), spec=spec):
            _log("brain_unavailable", kind=kind, session=session,
                      reason=tmux_claude.last_error() or "inject failed")
            return None

        while time.time() < deadline:
            reply = collect_reply(session, request_id)
            if reply is not None:
                _log("brain_reply", kind=kind, session=session,
                          duration_s=round(time.monotonic() - started, 2))
                return reply
            time.sleep(POLL_INTERVAL_S)

        _log("brain_timeout", kind=kind, session=session,
                  timeout_s=timeout_s)
        return None
    finally:
        cleanup_request(session, request_id)
        try:
            lock.release()
        except (RuntimeError, OSError):
            pass


async def ask_brain_async(
    kind: str,
    payload: Any,
    timeout_s: float | None = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """`ask_brain` for api.py's async handlers.

    The sync version polls with `time.sleep` and must never be called on the
    event loop — one brain request would stall every other HTTP request for the
    length of a Claude turn.
    """
    import asyncio

    return await asyncio.to_thread(ask_brain, kind, payload, timeout_s, model)
