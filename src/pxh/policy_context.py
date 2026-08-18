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

  * session — a FileLock timeout yields {}. Quiet mode then reads as off. The
    lock is held for milliseconds, and blocking a turn on a contended session
    file would make SPARK mute under load.
  * awareness — an unreadable snapshot yields {}, so the on-call/hot-mic rule
    goes inactive rather than muting SPARK for as long as px-mind is down. That
    is voice_loop's long-standing choice, moved rather than changed. Quiet mode
    and night silence read nothing from this file, so the two rules that must
    hold unconditionally are unaffected by it either way.

Both fail *open*, which is only defensible because the rules that matter most
do not depend on either read succeeding: night silence needs a clock, and quiet
mode is re-checked upstream by both dispatchers.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

from filelock import Timeout as FileLockTimeout

from pxh import policy
from pxh.state import load_session

PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parents[2]))


def state_dir() -> Path:
    """Where awareness.json lives. Honours PX_STATE_DIR so tests isolate."""
    return Path(os.environ.get("PX_STATE_DIR", str(PROJECT_ROOT / "state")))


def load_session_for_policy() -> Dict[str, Any]:
    """Session dict, or {} if the lock is contended. Never raises."""
    try:
        return load_session() or {}
    except FileLockTimeout:
        return {}
    except (OSError, ValueError):
        return {}


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
    """
    return policy.evaluate(
        action,
        params or {},
        effect="audio",
        origin="interactive",
        session=load_session_for_policy(),
        awareness=load_awareness(warn_prefix=warn_prefix),
        now=time.time(),
    )
