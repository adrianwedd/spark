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
6. **Validate** — one handshake per tick, to the session that has waited
   longest, because the glyph cannot tell us a session can answer. A tmux
   server restart mid-handshake is self-recovering and needs no special
   handling: the session disappears, the next tick reads `session_absent`,
   recreates, sweeps and handshakes with a fresh id. Stated so nobody adds
   machinery for it.

Health is reported per session (`px-brain`, `px-brain-io`) so a wedged or
missing session is visible without reading tmux by hand.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
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

# How often to check the outbox during a handshake.
HANDSHAKE_POLL_S = float(os.environ.get("PX_BRAIN_HANDSHAKE_POLL_S", "0.25"))

# Turns before a context recycle. Continuity across /clear comes from the
# journal, not from the context window.
CONTEXT_TURNS = int(os.environ.get("PX_BRAIN_CONTEXT_TURNS", "20"))

# Nightly recycle window opens at this hour, Hobart time. It does not close:
# if overnight work (research, compose — both NIGHT_ALLOWED_ACTIONS) keeps the
# brain busy past dawn, the recycle waits for idle rather than preempting a
# turn someone is waiting on.
NIGHTLY_RECYCLE_HOUR = int(os.environ.get("PX_BRAIN_RECYCLE_HOUR", "2"))

# How long handshake_reason holds off after a recycle injects the
# journal+/clear turn, before it will consider the session due again. That
# injection is a real Claude turn, not a keystroke that lands instantly, so it
# gets a turn's own budget rather than a made-up number — the same one a
# handshake gets.
RECYCLE_QUIET_S = brain.HANDSHAKE_TIMEOUT_S

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
    # When this session last had a handshake attempted (monotonic). Validation
    # goes to whoever has waited longest — see _validate_one in tick().
    last_validation_attempt: float = 0.0
    # Monotonic time of the last recycle whose inject actually landed. A
    # recycle clears the marker, which makes handshake_reason see `no_marker`
    # in the very same tick — this is how it holds off nudging the pane it
    # just made busy with the journal+/clear turn. See RECYCLE_QUIET_S.
    last_recycle_at: float = 0.0

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
        # A fresh session inherits nothing: sweep whatever the old one left, and
        # drop the validation marker so nothing trusts a round trip that a
        # session which no longer exists once completed. Deleting it leaves the
        # session at `no_marker`, which is what makes the next tick handshake it.
        swept = brain.sweep_pending(state.name)
        brain.clear_validation_marker(state.name)
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


def _await_handshake_reply(session: str, request_id: str, nonce: str,
                           timeout_s: float) -> bool:
    """Wait for one reply and require it to echo the nonce."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        reply = brain.collect_reply(session, request_id)
        if reply is not None:
            body = reply.get("reply")
            return isinstance(body, dict) and body.get("echo") == nonce
        time.sleep(HANDSHAKE_POLL_S)
    return False


def run_handshake(state: SessionState, reason: str) -> bool:
    """Send one real request and require one real reply. Returns success.

    This is not a ping. It is `tool-brain-reply` executing under the real
    permission rules, from the real cwd, with the real allowlist — the same path
    every subsequent request takes. A success proves the allowlist spelling, the
    system prompt's placeholder substitution, the mailbox permissions and
    Claude's own onboarding state all line up, and each of those has broken once.

    `reason` is "no_marker" (aged or freshly created) or "model_change".
    """
    session = state.name
    state.last_validation_attempt = time.monotonic()

    # Narrow sweep: exactly the file the aged marker names, and nothing else.
    # See §2.3 step 1 — this records the orphan of a supervisor that died
    # mid-handshake, which nothing else will ever claim.
    if reason == "no_marker":
        marker = brain.read_validation_marker(session) or {}
        stale_id = marker.get("request_id")
        if isinstance(stale_id, str) and stale_id:
            brain.sweep_one(session, stale_id)
    brain.clear_validation_marker(session)

    lock = brain._lock_for(session)
    if lock is None:
        health.record_failure(state.component, "filelock unavailable")
        return False
    try:
        lock.acquire(timeout=brain.LOCK_WAIT_S)
    except Exception:  # noqa: BLE001 - filelock's Timeout, or an OSError
        # A caller is mid-turn. Injecting now splices two prompts into one; the
        # next tick is ten seconds away.
        _log("handshake_deferred", session=session, reason="lock busy")
        return False

    spec = brain.spec_for_session(session)
    model = brain.configured_model(session)
    request_id = str(uuid.uuid4())
    nonce = str(uuid.uuid4())
    try:
        # ensure_session already polls for the glyph internally, up to its own
        # STARTUP_TIMEOUT_S. Do not wait again here — that spends the same
        # budget twice (§2.6). It returns True on session_exists() alone when
        # the prompt never appeared, and that is fine: the glyph is a
        # best-effort hint about when to start typing, and the handshake below
        # is the authoritative readiness test.
        if not tmux_claude.ensure_session(spec=spec):
            health.record_failure(state.component,
                                  tmux_claude.last_error() or "session did not start")
            return False

        if reason == "model_change":
            # Ordering matters and this is the crash-safe direction: the marker
            # is already gone (cleared above, before the lock), so a supervisor
            # killed between here and the handshake leaves `no_marker` rather
            # than a marker vouching for a session that has just been retuned.
            _log("model_change", session=session, model=model)
            if not tmux_claude.inject(f"/model {model}", spec=spec):
                health.record_failure(state.component,
                                      tmux_claude.last_error() or "model switch failed")
                return False

        time.sleep(brain.SETTLE_S)

        # One deadline for the whole handshake, so a retry does not leave the
        # request looking abandoned to _is_idle() or check_wedge().
        deadline = time.time() + brain.HANDSHAKE_ATTEMPTS * brain.HANDSHAKE_TIMEOUT_S
        try:
            atomic_write(brain.inbox_dir(session) / f"{request_id}.json",
                         json.dumps({"id": request_id, "kind": "handshake",
                                     "payload": {"echo": nonce},
                                     "deadline": deadline,
                                     "created_at": utc_timestamp()}, indent=2))
            atomic_write(brain.current_path(session),
                         json.dumps({"id": request_id, "kind": "handshake",
                                     "deadline": deadline}, indent=2))
        except OSError as exc:
            # No marker names this id yet (VALIDATING isn't written until the
            # loop below), so the narrow sweep on the next handshake could
            # never find and recover an orphaned inbox entry left by a partial
            # write. Clean up now, on this same request_id, while we still can.
            brain.cleanup_request(session, request_id)
            health.record_failure(state.component, f"handshake write failed: {exc}")
            return False

        for attempt in range(1, brain.HANDSHAKE_ATTEMPTS + 1):
            brain.write_validation_marker(session, state=brain.VALIDATING,
                                          request_id=request_id, model=model,
                                          attempt=attempt)
            if attempt > 1:
                # Escape whatever the last attempt left in the input box. Alone,
                # never followed by Enter — that submits a stray turn.
                tmux_claude.send_key("Escape", spec=spec)
            brain.record_request("handshake")
            if not tmux_claude.inject(brain.nudge_line(session, request_id), spec=spec):
                health.record_failure(state.component,
                                      tmux_claude.last_error() or "handshake inject failed")
                continue
            if _await_handshake_reply(session, request_id, nonce,
                                      brain.HANDSHAKE_TIMEOUT_S):
                brain.write_validation_marker(session, state=brain.VALIDATED,
                                              request_id=request_id, model=model,
                                              attempt=attempt)
                brain.cleanup_request(session, request_id)
                health.record_success(state.component,
                                      detail={"model": model, "attempt": attempt})
                _log("handshake_ok", session=session, attempt=attempt, model=model)
                return True

        # Attempts exhausted. Kill it: the next tick sees session_absent,
        # recreates, sweeps, and handshakes with a new id.
        health.record_failure(
            state.component,
            f"handshake failed after {brain.HANDSHAKE_ATTEMPTS} attempts")
        _log("handshake_failed", session=session,
             attempts=brain.HANDSHAKE_ATTEMPTS, request=request_id)
        brain.clear_validation_marker(session)
        brain.cleanup_request(session, request_id)
        tmux_claude.kill_session(spec)
        if state.holder is not None:
            state.holder.stop()
            state.holder = None  # attached to a session that no longer exists
        return False
    finally:
        try:
            lock.release()
        except (RuntimeError, OSError):
            pass


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
        # WARNING: the glyph does not prove the session can answer — a
        # permission dialog renders it, and that is the exact failure the
        # handshake exists to catch. This branch is trusted anyway, and only
        # because `ask_brain`'s `finally:` removes `current.json` on every exit
        # path: a `current.json` still here past its deadline therefore means
        # the caller process itself died, which is a narrower claim than "the
        # pane looks fine". Do not reuse this reasoning anywhere the marker is
        # available instead. Recorded as a known limitation in
        # docs/superpowers/specs/2026-08-17-brain-handshake-validation-design.md §5.
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


def _pending_live(session: str) -> bool:
    """True if any inbox entry still has a waiter.

    The predicate `_is_idle` used to ask "is a request live?" and answer it with
    "does a file exist?" — and those differ exactly when a writer died. Every
    request carries `deadline` as wall-clock and `ask_brain` gives up precisely
    at it, so a pending entry past its deadline (with no `current.json`, which
    the caller checks first) has no waiter by construction. That holds for a
    dead handshake's request and a killed caller's alike.

    No grace period, deliberately, unlike `check_wedge`'s
    `deadline + WEDGE_GRACE_S`: `ask_brain`'s loop is `while time.time() <
    deadline` and its `finally:` removes the entry, so at the deadline the
    caller has already cleaned up and no slack is needed to be sure. The grace
    next door answers a different question — whether the *session* is stuck,
    where slack buys safety before an Escape and a kill.

    An unreadable or absent deadline counts as live. A predicate that cannot
    read a deadline must not become a reason to recycle over a real request.
    """
    now = time.time()
    for entry in brain.inbox_dir(session).glob("*.json"):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        if not isinstance(data, dict):
            return True
        deadline = data.get("deadline")
        if not isinstance(deadline, (int, float)):
            return True
        if now < deadline:
            return True
    return False


def _is_idle(state: SessionState) -> bool:
    """No request in flight, nothing live pending, and the pane is listening."""
    if _read_current(state.name) is not None:
        return False
    if _pending_live(state.name):
        return False
    return tmux_claude.pane_ready(brain.spec_for_session(state.name))


def maybe_recycle(state: SessionState, now_local: datetime) -> None:
    """Reset context at an idle moment — on turn count, or once a night.

    Never mid-request: a `/clear` between the nudge and the reply loses the
    request entirely, and the caller can only see that as a timeout. Held under
    the single-flight lock for the same reason, non-blocking: waiting for it
    would stall the supervisor for the length of a caller's deadline — up to
    1800s for `evolve` — and there is another tick in ten seconds.
    """
    day = now_local.strftime("%Y-%m-%d")
    nightly_due = (now_local.hour >= NIGHTLY_RECYCLE_HOUR
                   and state.last_recycle_day != day)
    turns_due = state.turns >= CONTEXT_TURNS
    if not (nightly_due or turns_due):
        return
    if not _is_idle(state):
        return

    lock = brain._lock_for(state.name)
    if lock is None:
        _log("recycle_deferred", session=state.name, reason="filelock unavailable")
        return
    try:
        lock.acquire(timeout=0)
    except Exception:  # noqa: BLE001 - filelock's Timeout, or an OSError
        _log("recycle_deferred", session=state.name, reason="lock busy")
        return

    try:
        spec = brain.spec_for_session(state.name)
        reason = "nightly" if nightly_due else "turns"
        _log("recycle", session=state.name, reason=reason, turns=state.turns)

        # Marker first, then the keystroke. A supervisor killed between the two
        # drops the lock at process death and leaves no marker, so the next
        # reader sees `no_marker`, falls back, and the next tick re-handshakes —
        # rather than injecting into a session whose context has just gone.
        brain.clear_validation_marker(state.name)

        # Journal before clearing — the other order throws away the thing the
        # journal was supposed to preserve.
        landed = tmux_claude.inject(
            f"Before anything else: append anything worth keeping to {journal_path()}, "
            "then run /clear.", spec=spec)
        if not landed:
            # The marker is already gone — that is fine, it makes the next
            # tick handshake this session, same as any other no_marker. But
            # turns, last_recycle_day and last_recycle_at must not move: they
            # are the bookkeeping for a recycle that landed, and advancing
            # them here would make the supervisor think it recycled — and
            # wait another CONTEXT_TURNS to try again — while the context was
            # never actually cleared.
            _log("recycle_inject_failed", session=state.name,
                 error=tmux_claude.last_error())
            return
        state.turns = 0
        state.last_recycle_at = time.monotonic()
        if nightly_due:
            state.last_recycle_day = day
    finally:
        try:
            lock.release()
        except (RuntimeError, OSError):
            pass


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


def handshake_reason(state: SessionState) -> str | None:
    """Why this session needs a handshake, or None if it does not.

    Level-triggered on derived state rather than edge-triggered on events, and
    that is the whole point: `no_marker` is the only state reachable by *aging*,
    so no edge can ever fire for it. A supervisor killed mid-handshake leaves a
    session that exists, was never validated, and ages out of `validating` with
    nobody watching — and nothing recreates it, because tmux still has it.
    """
    derived = brain.session_state(state.name)
    if derived == brain.SESSION_ABSENT:
        return None  # start_session recreates it; the next tick handshakes
    if derived == brain.VALIDATING:
        return None  # in progress, and a stale marker has already aged out
    if derived == brain.NO_MARKER:
        if (state.last_recycle_at
                and time.monotonic() - state.last_recycle_at < RECYCLE_QUIET_S):
            # The marker is legitimately absent here — the supervisor cleared
            # it moments ago as part of a recycle, and the pane is busy with
            # the very turn that clears the context. Nudging in now is the
            # "never inject into a busy pane" hazard, by construction, on
            # every recycle. During this window the session records neither
            # success nor failure, same as `validating` does: it reports
            # `missing` for up to a turn's length against a 300s staleness
            # window. That is the correct trade against splicing two prompts
            # into one.
            return None
        return "no_marker"
    marker = brain.read_validation_marker(state.name) or {}
    if marker.get("model") != brain.configured_model(state.name):
        # A caller that names a different model just falls back. Only the
        # supervisor changes a session's model, and only at an idle moment.
        return "model_change"
    return None


def _validate_one(live: list[SessionState]) -> None:
    """At most one handshake per tick, to the session that has waited longest.

    One per tick because two would double the sibling's health blackout.
    Longest-waited rather than first-in-iteration-order because level-triggered
    validation is self-perpetuating: a session that fails on every tick would
    consume the budget on every tick, and its healthy sibling would never be
    attempted at all — reporting failure not because anything is wrong with it,
    but because it never got a turn.
    """
    due: list[tuple[float, SessionState, str]] = []
    for state in live:
        try:
            reason = handshake_reason(state)
        except Exception as exc:  # noqa: BLE001
            _log("tick_error", session=state.name, error=str(exc))
            continue
        if reason is not None:
            due.append((state.last_validation_attempt, state, reason))
    if not due:
        return
    due.sort(key=lambda item: item[0])
    _, state, reason = due[0]
    try:
        run_handshake(state, reason)
    except Exception as exc:  # noqa: BLE001 - a supervisor that dies supervises nothing
        _log("tick_error", session=state.name, error=str(exc))
        health.record_failure(state.component, str(exc))


def tick(states: dict[str, SessionState]) -> None:
    """One supervisor pass. Never raises — this loop must not be killable."""
    now = time.monotonic()
    now_local = datetime.now(HOBART)
    live: list[SessionState] = []
    for state in states.values():
        try:
            if not start_session(state):
                continue
            count_turns(state)
            check_wedge(state, now)
            maybe_recycle(state, now_local)
            live.append(state)
        except Exception as exc:  # noqa: BLE001
            _log("tick_error", session=state.name, error=str(exc))
            health.record_failure(state.component, str(exc))

    # _validate_one already guards its own internals; this is belt and
    # braces around whatever's left (_log or health.record_failure raising
    # from inside one of its except handlers) — tick() must not raise no
    # matter where the exception comes from.
    try:
        _validate_one(live)
    except Exception as exc:  # noqa: BLE001
        _log("tick_error", session="_validate_one", error=str(exc))

    # Health after validation, so a session validated this tick reports it
    # immediately. Conditional on the marker and never on the glyph: a
    # permission dialog renders a prompt, so the glyph reported `ok` for a
    # session that could not answer a single request, forever.
    for state in live:
        try:
            if brain.session_state(state.name) == brain.VALIDATED:
                health.record_success(state.component, min_interval_s=60,
                                      detail={"turns": state.turns})
        except Exception as exc:  # noqa: BLE001
            _log("tick_error", session=state.name, error=str(exc))
            health.record_failure(state.component, str(exc))


# The supervisor's own fd, held for the process lifetime. Module-level because
# closing it would drop the lock: flock is released on the last close of the
# file, so a local variable going out of scope would silently unguard us.
_supervisor_fd: int | None = None


def supervisor_lock_path() -> Path:
    return brain.brain_root() / ".supervisor.lock"


def acquire_supervisor_lock() -> bool:
    """Take the single-instance guard. False means another supervisor has it.

    `bin/px-brain` has had no guard at all, and the obvious way to get two
    supervisors is an operator running it in a shell to watch it while systemd
    already has one. The damage is not subtle: `start_session`, `sweep_pending`,
    `kill_session` and `check_wedge` all run outside the per-session
    single-flight lock, so one supervisor can sweep the other's in-flight
    handshake into `dead/`.

    flock rather than px-mind's PID-file-plus-`/proc` pattern because a
    supervisor that can be SIGKILLed wants a guard the kernel releases at death
    — no stale-PID window, no PID-reuse `cmdline` check to get subtly wrong. And
    stdlib `fcntl` rather than `filelock` because this runs under
    `/usr/bin/python3`, where `brain.py` already carries an `ImportError` path
    that degrades to "no lock available"; a guard that can silently become no
    guard is not a guard.
    """
    global _supervisor_fd
    if brain._ensure_dir(brain.brain_root()) is None:
        _log("supervisor_lock_unavailable", reason="brain root not writable")
        return False
    path = supervisor_lock_path()
    try:
        # No O_TRUNC: truncating before we hold the lock would erase the
        # winner's pid, which is the only hint the loser gets.
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        _log("supervisor_lock_unavailable", reason=str(exc))
        return False
    try:
        # 0666, not the validation marker's 0644: the marker has exactly one
        # writer and carries a claim another uid could forge, so it stays
        # locked down. This file has no claim to forge — only a pid hint that
        # already gates nothing — and any uid that can legitimately run
        # bin/px-brain needs to open it read-write, same reasoning as the
        # session lock's _relax_lock_mode. Best-effort: whichever uid wins the
        # creation race, both must still be able to open it.
        os.chmod(path, 0o666)
    except OSError:
        pass
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # flock reports EWOULDBLOCK and nothing else — no F_GETLK equivalent —
        # so the pid below is whatever the holder wrote, and may be stale: a
        # crashed holder leaves its pid behind. Labelled as a hint because it
        # never gates a decision, only an operator's next step.
        try:
            hint = os.read(fd, 64).decode("utf-8", "replace").strip() or "unknown"
        except OSError:
            hint = "unknown"
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        _log("supervisor_already_running", holder_pid_hint=hint,
             note="pid is a hint written by the holder and may be stale")
        return False
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
    except OSError:
        pass
    _supervisor_fd = fd
    return True


def release_supervisor_lock() -> None:
    """Drop the guard. For tests — the daemon holds it until it exits."""
    global _supervisor_fd
    if _supervisor_fd is None:
        return
    try:
        fcntl.flock(_supervisor_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        # Close even if the unlock raised — otherwise the module believes it
        # is unguarded while the kernel still holds the lock on an
        # unreachable fd, and every later acquire in this process fails
        # forever.
        try:
            os.close(_supervisor_fd)
        except OSError:
            pass
    _supervisor_fd = None


def run(once: bool = False) -> int:
    """Supervisor loop. Returns an exit code (for `once` mode / tests)."""
    if not acquire_supervisor_lock():
        # StartLimitBurst=5 / StartLimitIntervalSec=300 means a losing copy
        # under systemd gives up after five attempts rather than restart-looping
        # forever, and px-brain's health goes stale — which is visible.
        return 1
    ensure_journal()
    states = {name: SessionState(name=name)
              for name in (brain.BRAIN_SESSION, brain.IO_SESSION)}
    _log("start", sessions=list(states), tick_s=TICK_S, started=utc_timestamp())
    while True:
        tick(states)
        if once:
            return 0
        time.sleep(TICK_S)
