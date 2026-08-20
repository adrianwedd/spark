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
   longest, because the glyph cannot tell us a session can answer. Health
   success is recorded only for a session whose marker says `validated`,
   never on the strength of the prompt glyph — a permission dialog renders
   the glyph too, which is exactly how a session that could not answer a
   single request used to report `ok` indefinitely. A tmux server restart
   mid-handshake is self-recovering and needs no special handling: the
   session disappears, the next tick reads `session_absent`, recreates,
   sweeps and handshakes with a fresh id. Stated so nobody adds machinery
   for it. A handshake does not count toward context-recycle turns, and a
   failed one does not kill the session — see the comments in
   `run_handshake` for both.

Health is reported per session (`px-brain`, `px-brain-io`) so a wedged or
missing session is visible without reading tmux by hand.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from dataclasses import dataclass, field, replace
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


def _read_boot_id() -> str:
    """The kernel's boot id.

    Earns its place on this host specifically: there is no RTC, timesyncd
    stepped the clock ~49 minutes forward at 11:17:37 on 2026-08-19, and
    boot_id is the one member of the identity tuple a clock step cannot move.
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip() or "unknown"
    except OSError:
        return "unknown"


_INSTANCE_ID = uuid.uuid4().hex[:12]   # distinguishes instances across PID reuse
_BOOT_ID = _read_boot_id()


def _log(event: str, **fields: Any) -> None:
    """Emit one brain-daemon record, identified by its emitting process.

    Records carried `ts` and `event` and nothing else, which is what allowed
    pytest output to masquerade as a concurrency event in #221: 43 duplicated
    `start` records read as two supervisors when they were one test suite.
    pid alone is not enough across a restart, and a wall-clock ts is not
    enough on a host whose clock steps.
    """
    try:
        log_event("brain-daemon", {
            "event": event,
            "pid": os.getpid(),
            "instance": _INSTANCE_ID,
            "boot_id": _BOOT_ID,
            **fields,
        })
    except Exception:  # noqa: BLE001 - never let logging stop the supervisor
        pass


def _read_current(session: str) -> dict[str, Any] | None:
    try:
        data = json.loads(brain.current_path(session).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        # ValueError, not just OSError: a corrupt file's read_text raises
        # UnicodeDecodeError (a ValueError), and json.JSONDecodeError already
        # is one too. See brain.read_validation_marker for the same reasoning.
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


def start_session(state: SessionState,
                  now_local: datetime | None = None) -> bool:
    """Bring one session up and attach its holder. Idempotent per tick."""
    spec = brain.spec_for_session(state.name)
    # Only matters at actual session creation — tmux_claude.ensure_session
    # only applies -e env args on new-session, not on an idempotent recheck
    # — but harmless to set unconditionally. Lets px-claude-session's exit
    # log (bin/px-claude-session) name which supervisor process and boot
    # started it, which is exactly the axis a comparison between spark-brain
    # and spark-io needs after a death.
    spec = replace(spec, env={
        **spec.env,
        "PX_BRAIN_SUPERVISOR_INSTANCE": _INSTANCE_ID,
        "PX_BRAIN_SUPERVISOR_BOOT_ID": _BOOT_ID,
    })
    brain.ensure_mailbox(state.name)

    existed = tmux_claude.session_exists(spec)
    if not existed:
        # Drop the dead session's validation BEFORE the new one boots, not
        # after. `ensure_session` blocks waiting for the prompt glyph — tens of
        # seconds — and for that whole window `ask_brain` would read a
        # `validated` marker belonging to a session that no longer exists, and
        # inject a real request into a Claude that is still starting up or
        # sitting on a trust dialog. Clearing it here leaves the session at
        # `no_marker` for the boot, which is what makes callers fall back
        # instead of typing into it.
        #
        # Clearing before a failed `ensure_session` is right too: the session
        # is gone either way, and nothing should trust its old round trip.
        brain.clear_validation_marker(state.name)

    if not tmux_claude.ensure_session(spec=spec):
        health.record_failure(state.component,
                              tmux_claude.last_error() or "session would not start")
        _log("session_start_failed", session=state.name,
             error=tmux_claude.last_error())
        return False

    if not existed:
        # A fresh session inherits nothing. The validation marker is already
        # gone (cleared above, before the boot); sweep what the old one left.
        swept = brain.sweep_pending(state.name)
        state.turns = 0
        # Creating a session IS a context reset, so record it as today's. An
        # empty `last_recycle_day` made every session born after
        # NIGHTLY_RECYCLE_HOUR instantly "nightly due": it spent its first turn
        # on a journal+/clear with nothing to write, and the busy pane starved
        # the handshake that a create is supposed to be followed by.
        local = now_local or datetime.now(HOBART)
        state.last_recycle_day = local.strftime("%Y-%m-%d")
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
        # Clearing happens under the lock, not before acquiring it. The marker
        # is the only record that a model change is owed: `handshake_reason`
        # derives "model_change" by comparing the marker's model against the
        # configured one, so clearing it on a path that can still bail — a busy
        # lock — erases the reason too. The next tick would read `no_marker`,
        # skip the `/model` branch below, and then write a marker naming the
        # newly configured model that nothing was ever switched to. Under the
        # lock, a defer leaves the marker untouched and the change still owed.
        #
        # Narrow sweep: exactly the file the aged marker names, and nothing
        # else. See §2.3 step 1 — this records the orphan of a supervisor that
        # died mid-handshake, which nothing else will ever claim. It reads the
        # marker, so it has to stay ahead of the clear.
        if reason == "no_marker":
            marker = brain.read_validation_marker(session) or {}
            stale_id = marker.get("request_id")
            if isinstance(stale_id, str) and stale_id:
                brain.sweep_one(session, stale_id)
        brain.clear_validation_marker(session)

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
            # is already gone (cleared above, ahead of this keystroke), so a
            # supervisor killed between here and the handshake leaves
            # `no_marker` rather than a marker vouching for a session that has
            # just been retuned.
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
            if not tmux_claude.inject(brain.nudge_line(session, request_id), spec=spec):
                health.record_failure(state.component,
                                      tmux_claude.last_error() or "handshake inject failed")
                continue
            # Billed only once the turn is actually delivered — an injection
            # that failed above never reached the session, so counting it here
            # would inflate the meter the spec designates as the restart-loop
            # signal with turns nobody ever answered.
            brain.record_request("handshake")
            if _await_handshake_reply(session, request_id, nonce,
                                      brain.HANDSHAKE_TIMEOUT_S):
                brain.write_validation_marker(session, state=brain.VALIDATED,
                                              request_id=request_id, model=model,
                                              attempt=attempt)
                brain.cleanup_request(session, request_id)
                health.record_success(state.component,
                                      detail={"model": model, "attempt": attempt})
                _log("handshake_ok", session=session, attempt=attempt, model=model)
                # Deliberately NOT state.turns += 1. The original design (spec
                # §2.7, docs/superpowers/specs/2026-08-17-brain-handshake-
                # validation-design.md) counted a handshake as a turn like any
                # other, reasoning that it is a real turn of context. A
                # 2026-08-20 audit of session recreations found the practical
                # effect instead: a session doing nothing but answering health
                # checks ages into a context recycle on protocol traffic
                # alone. CONTEXT_TURNS exists to bound growth from real work
                # (see count_turns(), which only counts caller requests); a
                # handshake is supervisor plumbing and must not spend that
                # budget. Pinned by
                # test_twenty_successful_handshakes_alone_never_trigger_a_recycle.
                #
                # The quiet window's job is done: the thing it was protecting
                # (a slow recycle turn) is over, one way or another, by the
                # time a handshake succeeds. Clearing it here means the window
                # cannot outlive the recycle it exists to protect.
                state.last_recycle_at = 0.0
                return True

        # Attempts exhausted. Earlier this killed the session outright — see
        # docs/superpowers/specs/2026-08-17-brain-handshake-validation-
        # design.md §2.3 step 7 — on the theory that a session failing to
        # answer a handshake is broken and should be replaced. A 2026-08-20
        # audit of session recreations found 6 of 19 traced to exactly this
        # branch: a slow or momentarily confused session, not a dead one,
        # killed by its own diagnostic. A failed handshake is proof the
        # session is not currently trustable, not proof the process must
        # die.
        #
        # So: clear the marker — every reader now sees no_marker, and
        # ask_brain will not route to this session — and record the
        # failure, but leave the session and its holder alone. The next
        # tick's handshake_reason() sees no_marker and tries again on its
        # own schedule (§2.3's third trigger, unaffected by this change).
        # If the session really is wedged rather than merely unvalidated,
        # check_wedge() is the path that kills it — on the concrete
        # evidence of a live caller request past its deadline, not on a
        # diagnostic ping failing twice. Pinned by
        # test_a_silent_session_is_escaped_and_retried_but_not_killed and
        # test_a_failed_handshake_leaves_the_holder_attached.
        health.record_failure(
            state.component,
            f"handshake failed after {brain.HANDSHAKE_ATTEMPTS} attempts")
        _log("handshake_failed", session=session,
             attempts=brain.HANDSHAKE_ATTEMPTS, request=request_id)
        brain.clear_validation_marker(session)
        brain.cleanup_request(session, request_id)
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
        except (OSError, ValueError):
            # ValueError catches UnicodeDecodeError too (see _read_current);
            # json.JSONDecodeError is already a ValueError.
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
        if state.last_recycle_at:
            since_recycle = time.monotonic() - state.last_recycle_at
            # The marker is legitimately absent here — the supervisor cleared
            # it moments ago as part of a recycle, and the pane is busy with
            # the very turn that clears the context. Nudging in now is the
            # "never inject into a busy pane" hazard, by construction, on
            # every recycle. During this window the session records neither
            # success nor failure, same as `validating` does: with the idle
            # extension below, the blackout can run up to VALIDATION_CEILING_S
            # (180s) plus a full handshake budget (up to 167s) on top of that
            # — long enough to cross the 300s `STALE_AFTER_S` window and
            # surface as `stale` rather than `missing`. That crossover is
            # accepted, not a bug: `stale` is still an alarm, and this window
            # still cannot report `ok`. The trade is against splicing two
            # prompts into one.
            #
            # The fixed RECYCLE_QUIET_S is only a floor: a recycle's
            # journal-append-then-/clear turn is a real Claude turn, not a
            # keystroke, and can run longer than one HANDSHAKE_TIMEOUT_S on a
            # slow night. If the pane is still busy when the floor expires,
            # extend the quiet window rather than nudging a session that is
            # demonstrably still working — but only up to VALIDATION_CEILING_S
            # from the recycle, never unboundedly. An unbounded idle gate on
            # this path would break the state machine's closure property (see
            # design doc §2.3): a session whose pane never shows a glyph again
            # would never be handshaked and so never repaired. Bounding the
            # extension keeps every no_marker session reachable while still
            # giving a slow recycle turn room to finish undisturbed.
            if since_recycle < RECYCLE_QUIET_S:
                return None
            if since_recycle < brain.VALIDATION_CEILING_S and not _is_idle(state):
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
            if not start_session(state, now_local):
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
_legacy_fd: int | None = None


def supervisor_lock_path() -> Path:
    """The guard, keyed to the socket it guards.

    Was brain_root()/".supervisor.lock" — checkout-relative, while the socket
    is host-global. flock is per-inode, so that gave each checkout a private
    guard contending with nothing, and let a fixture that relocated the
    mailbox silently relocate the guard along with it (#221).

    /tmp is correct here: the guard must not outlive the tmux server it
    guards, and neither survives a reboot.
    """
    return Path(brain.brain_socket() + ".supervisor.lock")


# Migration bridge — removal is gated by #224.
# Do not remove/disable this independently; #224 owns the proof that no
# pre-bridge binary remains runnable.
#
# Deliberately not env-readable: a guard an operator can weaken from the
# environment is not a guard. Its post-removal contract is pinned by
# test_after_removal_the_socket_alone_is_the_namespace.
_BRIDGE_HOLDS_LEGACY_LOCK = True


def legacy_supervisor_lock_path() -> Path:
    """The pre-bridge, checkout-relative guard.

    Held in addition to the socket lock for the duration of the migration.

    Migration bridge — removal is gated by #224.
    """
    return brain.brain_root() / ".supervisor.lock"


def _take_lock(path: Path) -> tuple[int | None, str]:
    """flock `path` exclusively, non-blocking.

    Returns (fd, "") on success, or (None, hint) where hint is the holder's
    pid or the errno that stopped us. LOCK_NB throughout: the dual acquire
    below must not be able to deadlock, and a lost race should fail fast
    rather than block a supervisor for the length of it.
    """
    try:
        # No O_TRUNC: truncating before we hold the lock would erase the
        # winner's pid, which is the only hint the loser gets.
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        return None, str(exc)
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
        return None, hint
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
    except OSError:
        pass
    return fd, ""


def _drop_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        # Close even if the unlock raised — otherwise the module believes it
        # is unguarded while the kernel still holds the lock on an
        # unreachable fd, and every later acquire in this process fails
        # forever.
        try:
            os.close(fd)
        except OSError:
            pass


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

    Both locks, socket first then legacy, for the duration of the migration.
    Changing the key while an incumbent holds only the legacy lock would
    itself open a two-supervisor window, and operator ordering (`systemctl
    stop` before the swap) is not sufficient on its own. One case stays open
    and no bridge can close it: a pre-bridge binary in checkout A against a
    post-bridge binary in checkout B, because the old binary knows nothing
    about the socket lock. That is not a regression — old-vs-old across
    checkouts is unguarded today, which is the defect being fixed.

    The bridge widens the guard, and the cost is stated rather than hidden:
    while it holds, two supervisors on *different* sockets in the *same*
    checkout also contend, because both must take that checkout's legacy
    lock. That is intended compatibility behaviour, not a bug — during
    migration the namespace is the pair (socket, checkout), and only after
    the legacy lock is removed does the socket alone become the complete
    namespace. Both halves are pinned by TestContention.

    Migration bridge — removal is gated by #224.
    """
    global _supervisor_fd, _legacy_fd
    if brain._ensure_dir(brain.brain_root()) is None:
        _log("supervisor_lock_unavailable", reason="brain root not writable")
        return False

    socket_lock = supervisor_lock_path()
    if brain._ensure_dir(socket_lock.parent) is None:
        _log("supervisor_lock_unavailable", reason="socket dir not writable")
        return False

    fd, hint = _take_lock(socket_lock)
    if fd is None:
        _log("supervisor_already_running", scope="socket", holder_pid_hint=hint,
             note="pid is a hint written by the holder and may be stale")
        return False

    if not _BRIDGE_HOLDS_LEGACY_LOCK:
        _supervisor_fd = fd
        _legacy_fd = None
        return True

    legacy_fd, legacy_hint = _take_lock(legacy_supervisor_lock_path())
    if legacy_fd is None:
        # Release the socket lock before refusing. A supervisor that correctly
        # declines to start must not leave the socket guarded by a process
        # that is not supervising it — nothing could ever start again.
        _drop_lock(fd)
        _log("supervisor_already_running", scope="legacy",
             holder_pid_hint=legacy_hint,
             note="a pre-bridge supervisor holds the checkout-relative guard")
        return False

    _supervisor_fd = fd
    _legacy_fd = legacy_fd
    return True


def release_supervisor_lock() -> None:
    """Drop both guards. For tests — the daemon holds them until it exits."""
    global _supervisor_fd, _legacy_fd
    _drop_lock(_legacy_fd)
    _drop_lock(_supervisor_fd)
    _legacy_fd = None
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
