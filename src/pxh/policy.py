"""Behavioural policy — constitutional invariants that hold regardless of which
prompt, persona, or dispatcher proposed an action.

SPARK's conversational *style* lives in docs/prompts/*.md and may vary by
persona or adapt with lived experience. A small set of *behavioural* rules must
not: quiet mode, night silence, and on-call/hot-mic suppression have to hold
even when a persona prompt has fully replaced the SPARK system prompt (see
voice_loop.py's persona swap, which does not supplement — it replaces).

This module is the chokepoint. Both dispatchers classify their own action
vocabulary into an Effect and call evaluate() before dispatch; policy.py never
enumerates tool or action names itself, so a new audio-producing tool only has
to be classified "audio" at its own call site to inherit every rule here.

evaluate() is pure: no file I/O, no subprocess calls, no session mutation, and
no imports from mind.py or voice_loop.py (the dependency runs one way, so there
is no cycle). Callers pass the session/awareness dicts they already hold.

This module and tests/test_policy_invariants.py are blacklisted from
self-evolution — see pxh.claude_session.BLACKLIST_FILES.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

from pxh import spark_config  # module handle: bounds read at call time

HOBART_TZ = ZoneInfo("Australia/Hobart")

Origin = Literal["interactive", "autonomous"]
Effect = Literal["audio", "presence", "other"]


@dataclass(frozen=True)
class PolicyVerdict:
    allowed: bool
    reason: str
    suggest_presence_substitute: bool = False


def is_night_hour(hour: int) -> bool:
    """True during the unconditional night-silence window (Hobart hour-of-day).

    Pure clock check with no policy attached, so mind.py::_is_night_silence can
    delegate here without policy.py importing mind.py. The autonomous
    enforcement rule stays where it is; only the clock semantics are shared.
    """
    start, end = spark_config.night_silence_bounds()
    return hour >= start or hour < end


def evaluate(
    action: str,
    params: dict,
    *,
    effect: Effect,
    origin: Origin,
    session: dict,
    awareness: dict,
    now: float,
    session_available: bool = True,
    _depth: int = 0,
) -> PolicyVerdict:
    """Decide whether an action may proceed. Returns a verdict; never executes.

    `effect` and `origin` are supplied by the caller and never inferred — the
    caller owns its vocabulary, and the two dispatchers stay legible about
    which one they are.

    `session_available` is how a caller says "I could not read the session" as
    distinct from "I read it and quiet mode is off". An empty dict cannot carry
    both meanings: `{}` is a *fact* (no quiet flag set), and a caller that
    passes it after a failed read has already answered the question this module
    exists to answer, in the permissive direction, without any evidence. The
    default is True because a caller holding a session dict it actually read
    has nothing to declare.

    On a blocked verdict with suggest_presence_substitute, the caller picks a
    presence-safe action from its own vocabulary and re-evaluates *that* action
    here with _depth=1 before executing it. If the rules would ever block at
    _depth >= 1, this raises rather than returning: substitution must never be
    able to recurse or to produce a less-restricted action, and that guarantee
    is mechanical rather than assumed as the presence-safe set changes.
    """
    if effect != "audio":
        return PolicyVerdict(allowed=True, reason="effect_not_audio")

    def _block(reason: str) -> PolicyVerdict:
        if _depth >= 1:
            raise ValueError(
                f"policy: {reason} blocked a substitute at _depth={_depth} — a "
                f"presence-safe substitute must never be classified "
                f"effect='audio' (action={action!r})"
            )
        return PolicyVerdict(
            allowed=False, reason=reason, suggest_presence_substitute=True
        )

    # Rule 0 — the session could not be read, so rule 1 cannot be evaluated.
    # Quiet mode is constitutional and binds both origins, which means a caller
    # with no session has no basis for the claim "the dysregulation protocol is
    # not running" — and that claim is precisely what proceeding would assert.
    # Unknown therefore resolves the same way as known-quiet: no audio.
    #
    # This is the one rule whose posture is about *evidence* rather than state,
    # so it is deliberately not folded into rule 1. `session.get(...) is True`
    # must keep meaning "I read the session and the flag is off" and nothing
    # else; the moment it also has to mean "I have no idea", the rule below
    # stops being checkable.
    if not session_available:
        return _block("session_unavailable")

    # Rule 1 — quiet mode / dysregulation protocol. Both origins: an active
    # meltdown does not care who initiated the turn.
    if session.get("spark_quiet_mode") is True:
        return _block("quiet_mode")

    # Rules 2 and 3 are interactive-only. The autonomous loop already enforces
    # night silence (NIGHT_ALLOWED_ACTIONS) and on-call suppression in mind.py,
    # with their own tests; duplicating them here would put one invariant in
    # two places. The interactive path had neither.
    if origin == "interactive":
        hour = dt.datetime.fromtimestamp(now, tz=HOBART_TZ).hour
        if is_night_hour(hour):
            return _block("night_silence")

        ha_ctx = awareness.get("ha_context") or {}
        if ha_ctx.get("adrian_on_call") or ha_ctx.get("adrian_mic_active"):
            return _block("on_call")

    return PolicyVerdict(allowed=True, reason="ok")
