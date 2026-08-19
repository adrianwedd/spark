"""Reads the facts pxh.policy refuses to read for itself.

`policy.evaluate()` is pure by design — no file I/O, no clock, no imports from
the dispatchers — so somebody has to hand it a session, an awareness snapshot
and a timestamp. Until now that somebody was voice_loop.py, in private helpers.
The audio *sink* (bin/tool-voice) needs exactly the same three facts, and a
third copy of the loading rules is how two enforcement points quietly drift
apart. This module is the one loader.

Splitting it from policy.py rather than relaxing that module keeps the property
that makes policy.evaluate() cheap to reason about and trivial to test: given
the same arguments it always returns the same verdict, and it cannot be
influenced by whatever happens to be on disk.

The failure postures here are deliberate and they differ:

  * session — a failed read is reported as a failed read. load_session_for_policy
    returns a SessionRead carrying both the data and whether it was actually
    obtained, and policy.evaluate() suppresses audio when it was not. Quiet mode
    is constitutional and binds both origins, so "I could not establish quiet
    state" must not resolve to "quiet mode is off": that resolution is a claim,
    made in the permissive direction, with no evidence behind it. This posture
    replaces an earlier one that returned a bare {} and let the rule read it as
    not-quiet. The argument for the old posture — that blocking on a contended
    session file would mute SPARK under load — does not survive contact with the
    sink: bin/tool-voice calls update_session() on the very same lock a few lines
    later, so a lock contended long enough to fail this read was going to fail
    that write too. The old posture did not buy speech under load; it bought one
    unlogged utterance during a meltdown.
  * awareness — an unreadable snapshot yields {}, so the on-call/hot-mic rule
    goes inactive rather than muting SPARK for as long as px-mind is down. That
    is voice_loop's long-standing choice, moved rather than changed, and it is
    load-bearing in a way the session read is not: awareness.json is written by
    a daemon that is routinely down, whereas the session is written by whatever
    is running. Quiet mode and night silence read nothing from this file.

So the two postures are now opposites, on purpose. The rule that must hold
unconditionally fails closed; the rule that degrades gracefully fails open.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from filelock import Timeout as FileLockTimeout

from pxh import policy, wake_grant
from pxh.state import load_session

PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parents[2]))


def state_dir() -> Path:
    """Where awareness.json lives. Honours PX_STATE_DIR so tests isolate."""
    return Path(os.environ.get("PX_STATE_DIR", str(PROJECT_ROOT / "state")))


@dataclass(frozen=True)
class SessionRead:
    """A session read, plus whether it actually happened.

    Two fields rather than one dict because the two facts are independent and
    a dict can only carry one of them. `data == {}` says the session holds no
    fields; `available is False` says there is no session to speak of. Callers
    that conflate the two answer a policy question by accident, which is the
    bug this type exists to make unrepresentable.
    """

    data: Dict[str, Any]
    available: bool


def load_session_for_policy(*, warn_prefix: str = "[policy]") -> SessionRead:
    """Read the session for a policy decision. Never raises.

    A failed read returns `SessionRead({}, available=False)`, which
    policy.evaluate() treats as grounds to suppress audio — see rule 0 there.

    The except clause is deliberately broad. Under the old fail-open posture
    that would have been indefensible, because a swallowed bug would have
    permitted speech; under fail-closed it cannot permit anything, and the
    alternative is an unhandled traceback out of a sink whose callers parse
    stdout as JSON. Every failure is announced on stderr rather than swallowed
    silently, so "SPARK went quiet" is never a mystery.

    One residual, on the record: pxh.state.load_session() self-heals a corrupt
    session by backing it up and returning defaults, so corruption arrives here
    as a successful read of a session with no quiet flag. That is state.py's
    behaviour and predates this module; closing it means changing what
    load_session() promises, not what this function catches.
    """
    try:
        return SessionRead(dict(load_session() or {}), available=True)
    except FileLockTimeout as exc:
        reason = f"session lock contended ({exc})"
    except Exception as exc:  # noqa: BLE001 — see docstring; cannot permit
        reason = f"session read failed ({type(exc).__name__}: {exc})"
    print(f"{warn_prefix} policy: {reason} — quiet mode indeterminate, "
          f"audio suppressed this turn", file=sys.stderr)
    return SessionRead({}, available=False)


def load_awareness(*, warn_prefix: str = "[policy]") -> Dict[str, Any]:
    """Best-effort awareness read for policy's on-call/hot-mic check."""
    path = state_dir() / "awareness.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"{warn_prefix} policy: awareness read failed ({exc}) — "
              f"on-call rule inactive this turn", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def wake_grant_active() -> bool:
    """Whether a wake conversation is open, as a fact read off disk.

    The one place either chokepoint asks. It exists as a named function rather
    than an inline call so both the dispatcher and the sink demonstrably load
    the *same* fact the *same* way, and so a test can pin the call site: an
    evolution PR that deleted the lookup and read an environment variable
    instead fails tests/test_policy_invariants.py, which px-evolve cannot edit.

    Never raises and never logs. pxh.wake_grant resolves every failure to "no
    grant", which suppresses — the correct direction for a question that only
    ever unblocks audio.
    """
    return wake_grant.is_grant_active()


def evaluate_audio_sink(
    action: str,
    params: Dict[str, Any] | None = None,
    *,
    warn_prefix: str = "[policy]",
) -> policy.PolicyVerdict:
    """Evaluate an audio effect at a sink, loading the context itself.

    `origin` is pinned to "interactive" and is not a parameter. A sink cannot
    know who called it — that is the whole reason it needs its own gate — and
    "interactive" is the stricter of the two origins, so guessing wrong can
    only ever suppress, never permit. The autonomous loop is unaffected in
    practice: mind.py's NIGHT_ALLOWED_ACTIONS are all silent, and its on-call
    rule already suppresses every speaking action, so nothing it legitimately
    dispatches reaches a window this check would newly close.

    `effect` is pinned to "audio" for the same reason. A caller that could
    declare its own effect could declare its way out of the gate, and the
    verdict would then be decided by the least trustworthy party in the chain.

    The wake grant is loaded here for exactly that reason and is deliberately
    not a parameter. It is the one input that can *unblock* audio, so it is the
    last thing that should arrive as a caller's assertion — the sink reads the
    document itself, checks it against this boot and this boot's clock, and
    believes nothing anyone hands it. An environment variable can say where to
    look; nothing it says is taken as true.

    A session the sink could not read suppresses rather than proceeds. The
    sink is the last boundary before the speaker, so it is the worst possible
    place to guess: there is nothing downstream to catch a wrong guess, and the
    thing a wrong guess produces is an utterance during a meltdown or at 3am.
    """
    session = load_session_for_policy(warn_prefix=warn_prefix)
    return policy.evaluate(
        action,
        params or {},
        effect="audio",
        origin="interactive",
        session=session.data,
        session_available=session.available,
        awareness=load_awareness(warn_prefix=warn_prefix),
        now=time.time(),
        wake_grant=wake_grant_active(),
    )
