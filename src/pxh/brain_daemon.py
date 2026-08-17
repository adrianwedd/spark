"""px-brain — the supervisor that keeps SPARK's resident Claude sessions alive.

`brain.ask_brain()` deliberately does not own the sessions. It is called from
inside other daemons, on their timing, and a request path that also has to
start, hold, unwedge and recycle a tmux session would couple every caller to
that lifecycle. So this daemon owns it and the callers just ask.

The single most important thing here is the least obvious: **tmux 3.3a's
`send-keys` fails outright when the server has no attached client**, and a
command client does not count. Nothing is watching a robot's tmux server at
3am, which is exactly when the cognitive loop runs — so without a permanently
attached read-only holder, injection fails precisely when nobody is around to
see it, and the failure looks intermittent. Holding those clients is this
daemon's first job; everything else is housekeeping around it.

What it does, in order of how much it matters:

1. **Hold a read-only client per session** so injection works at all.
2. **Ensure both sessions exist**, restarting either if it dies.
3. **Sweep on (re)create** — pending requests move to `dead/`. Callers own
   their own timeout and have long since fallen back; replaying their requests
   would spend budget answering questions nobody is waiting for.
4. **Detect a wedge** via `current.json`, not via stale inbox files. An
   abandoned inbox entry means a caller gave up; a `current.json` past its
   deadline with a busy pane and no reply means the session is stuck. Escape
   first, kill only if that fails.
5. **Recycle context** after enough turns, and once nightly, at an idle
   moment — never mid-request.

Health is reported per session (`px-brain`, `px-brain-io`) so a wedged or
missing session is visible without reading tmux by hand.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import brain, health, tmux_claude
from .logging import log_event
from .state import atomic_write
from .time import utc_timestamp

HOBART = ZoneInfo("Australia/Hobart")

TICK_S = float(os.environ.get("PX_BRAIN_TICK_S", "10"))

# How long a session may sit past its request deadline, still busy and still
# silent, before we conclude it is wedged rather than merely slow. Generous on
# purpose: killing a session that was about to answer wastes the whole turn.
WEDGE_GRACE_S = float(os.environ.get("PX_BRAIN_WEDGE_GRACE_S", "120"))

# After Escape, how long to wait for the pane to come back before killing it.
ESCAPE_GRACE_S = float(os.environ.get("PX_BRAIN_ESCAPE_GRACE_S", "30"))

# Turns before a context recycle. Continuity across /clear comes from the
# journal, not from the context window.
CONTEXT_TURNS = int(os.environ.get("PX_BRAIN_CONTEXT_TURNS", "20"))

# Nightly recycle window opens at this hour, Hobart time. It does not close:
# if overnight work (research, compose — both NIGHT_ALLOWED_ACTIONS) keeps the
# brain busy past dawn, the recycle waits for idle rather than preempting a
# turn someone is waiting on.
NIGHTLY_RECYCLE_HOUR = int(os.environ.get("PX_BRAIN_RECYCLE_HOUR", "2"))

_HEALTH_COMPONENT = {
    brain.BRAIN_SESSION: "px-brain",
    brain.IO_SESSION: "px-brain-io",
}


def journal_path() -> Path:
    return brain.brain_root() / "journal.md"


@dataclass
class SessionState:
    """Per-session bookkeeping the loop carries between ticks."""

    name: str
    holder: tmux_claude.HolderClient | None = None
    turns: int = 0
    wedged_since: float | None = None
    escaped_at: float | None = None
    last_recycle_day: str = ""
    seen_request_ids: set[str] = field(default_factory=set)

    @property
    def component(self) -> str:
        return _HEALTH_COMPONENT.get(self.name, f"px-brain-{self.name}")


def _log(event: str, **fields: Any) -> None:
    try:
        log_event("brain-daemon", {"event": event, **fields})
    except Exception:  # noqa: BLE001 - never let logging stop the supervisor
        pass


def _read_current(session: str) -> dict[str, Any] | None:
    try:
        data = json.loads(brain.current_path(session).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def ensure_journal() -> None:
    """Seed the journal a session reads at boot to recover continuity.

    A `/clear` throws away the context window; without something durable, every
    recycle would produce a session that has forgotten it is SPARK.
    """
    path = journal_path()
    if path.exists():
        return
    if brain._ensure_dir(brain.brain_root()) is None:
        return
    try:
        atomic_write(path, (
            "# SPARK brain journal\n\n"
            "Written by the resident sessions, read by them at boot and after\n"
            "every context reset. This is what survives a `/clear` — if it\n"
            "matters past this turn, it belongs here.\n"
        ))
    except OSError:
        pass


def start_session(state: SessionState) -> bool:
    """Bring one session up and attach its holder. Idempotent per tick."""
    spec = brain.spec_for_session(state.name)
    brain.ensure_mailbox(state.name)

    existed = tmux_claude.session_exists(spec)
    if not tmux_claude.ensure_session(spec=spec):
        health.record_failure(state.component,
                              tmux_claude.last_error() or "session would not start")
        _log("session_start_failed", session=state.name,
             error=tmux_claude.last_error())
        return False

    if not existed:
        # A fresh session inherits nothing: sweep whatever the old one left,
        # and drop the model marker so the first request re-asserts its model
        # instead of trusting a marker written by a session that is gone.
        swept = brain.sweep_pending(state.name)
        try:
            brain.model_marker_path(state.name).unlink()
        except OSError:
            pass
        state.turns = 0
        _log("session_created", session=state.name, swept=swept)
        if swept:
            # A sweep means requests were in flight when the session died.
            # That is a failure of the brain even though the callers coped.
            health.record_failure(
                state.component, f"swept {swept} pending request(s) on restart")

    if state.holder is None:
        state.holder = tmux_claude.HolderClient(spec)
    if not state.holder.alive():
        state.holder.start()
    return True


def check_wedge(state: SessionState, now: float) -> None:
    """Escalate a session that is past a request's deadline and still silent.

    Keyed on `current.json` because that is the only thing that means "someone
    is waiting". A stale inbox file means a caller gave up and moved on, which
    is not a wedge and must not get a working session killed.
    """
    current = _read_current(state.name)
    if current is None:
        state.wedged_since = None
        state.escaped_at = None
        return

    deadline = current.get("deadline")
    if not isinstance(deadline, (int, float)) or time.time() < deadline + WEDGE_GRACE_S:
        return

    spec = brain.spec_for_session(state.name)
    if tmux_claude.pane_ready(spec):
        # Prompt is back — the session finished or gave up; the request is the
        # caller's problem, not ours.
        state.wedged_since = None
        state.escaped_at = None
        return

    if state.escaped_at is None:
        state.wedged_since = now
        state.escaped_at = now
        _log("wedge_escape", session=state.name, request=current.get("id"))
        health.record_failure(state.component, "wedged past deadline — sent Escape")
        tmux_claude.send_key("Escape", spec=spec)
        return

    if now - state.escaped_at >= ESCAPE_GRACE_S:
        _log("wedge_kill", session=state.name, request=current.get("id"))
        health.record_failure(state.component, "wedged after Escape — killing session")
        tmux_claude.kill_session(spec)
        state.escaped_at = None
        state.wedged_since = None
        state.holder = None  # the holder is attached to a session that no longer exists


def _is_idle(state: SessionState) -> bool:
    """No request in flight, nothing pending, and the pane is listening."""
    if _read_current(state.name) is not None:
        return False
    if list(brain.inbox_dir(state.name).glob("*.json")):
        return False
    return tmux_claude.pane_ready(brain.spec_for_session(state.name))


def maybe_recycle(state: SessionState, now_local: datetime) -> None:
    """Reset context at an idle moment — on turn count, or once a night.

    Never mid-request: a `/clear` between the nudge and the reply loses the
    request entirely, and the caller can only see that as a timeout.
    """
    day = now_local.strftime("%Y-%m-%d")
    nightly_due = (now_local.hour >= NIGHTLY_RECYCLE_HOUR
                   and state.last_recycle_day != day)
    turns_due = state.turns >= CONTEXT_TURNS
    if not (nightly_due or turns_due):
        return
    if not _is_idle(state):
        return

    spec = brain.spec_for_session(state.name)
    reason = "nightly" if nightly_due else "turns"
    _log("recycle", session=state.name, reason=reason, turns=state.turns)

    # Journal first, then clear — the other order throws away the thing the
    # journal was supposed to preserve.
    tmux_claude.inject(
        f"Before anything else: append anything worth keeping to {journal_path()}, "
        "then run /clear.", spec=spec)
    state.turns = 0
    if nightly_due:
        state.last_recycle_day = day


def count_turns(state: SessionState) -> None:
    """Count requests as turns, so recycling tracks real context growth."""
    current = _read_current(state.name)
    if current is None:
        return
    request_id = current.get("id")
    if isinstance(request_id, str) and request_id not in state.seen_request_ids:
        state.seen_request_ids.add(request_id)
        state.turns += 1
        if len(state.seen_request_ids) > 500:
            state.seen_request_ids.clear()


def tick(states: dict[str, SessionState]) -> None:
    """One supervisor pass. Never raises — this loop must not be killable."""
    now = time.monotonic()
    now_local = datetime.now(HOBART)
    for state in states.values():
        try:
            if not start_session(state):
                continue
            count_turns(state)
            check_wedge(state, now)
            maybe_recycle(state, now_local)
            if tmux_claude.pane_ready(brain.spec_for_session(state.name)):
                health.record_success(state.component, min_interval_s=60,
                                      detail={"turns": state.turns})
        except Exception as exc:  # noqa: BLE001 - a supervisor that dies supervises nothing
            _log("tick_error", session=state.name, error=str(exc))
            health.record_failure(state.component, str(exc))


def run(once: bool = False) -> int:
    """Supervisor loop. Returns an exit code (for `once` mode / tests)."""
    ensure_journal()
    states = {name: SessionState(name=name)
              for name in (brain.BRAIN_SESSION, brain.IO_SESSION)}
    _log("start", sessions=list(states), tick_s=TICK_S, started=utc_timestamp())
    while True:
        tick(states)
        if once:
            return 0
        time.sleep(TICK_S)
