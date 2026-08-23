"""Canonical quiet-mode transition state — #209.

`spark_quiet_mode` was a naked boolean with three writers (`bin/tool-quiet`,
`bin/tool-transition`, the dashboard PATCH) and no provenance: nothing on
disk recorded who set it, why, or whether it was ever meant to clear itself.
This module is the one place that decides what a session's quiet-mode
record *means* — every reader converges on `resolve()`, every writer
converges on `new_state()` via `state.set_quiet_mode()`/`clear_quiet_mode()`.

Deliberately pure, same discipline as `pxh.policy`: no file I/O, no clock
reads (callers pass `now`), no imports from `state.py`. `state.py` is the
only importer, so there is no circular dependency between the derivation
logic here and the lock/atomic-write machinery that owns the session file.

The canonical record (`quiet_state` in session.json)::

    {"enabled": bool, "source": str, "reason": str|None,
     "set_at": float|None, "expires_at": float|None}

`expires_at: None` means indefinite — the Three S's protocol
(`bin/tool-quiet start`) is deliberately indefinite; only a temporary buffer
(`bin/tool-transition buffer`) sets a TTL. Expiry is resolved lazily at read
time (see `resolve()`) — nothing here ever writes back when a window has
lapsed, so there is no sweeper to keep alive and no write path hidden inside
what callers expect to be a read.

Byte-level session.json corruption (a JSON-decode failure) is `pxh.state`'s
concern and #208's, not this module's: by the time data reaches
`migrate_legacy()`/`resolve()`, `json.loads()` has already succeeded, so
`data` is well-formed JSON. What this module handles is a *field* that
parsed fine but doesn't match the shape it should — a `quiet_state` that is
the wrong type, or a dict missing `enabled`. That case fails conservatively:
`enabled=True`. A `quiet_state` key that was never written at all migrates
the legacy bare bool instead (see `migrate_legacy`) — absence is not the
same claim as garbage, and only the second is treated as evidence something
is broken.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

QUIET_STATE_KEY = "quiet_state"
LEGACY_KEY = "spark_quiet_mode"

SOURCE_UNKNOWN = "unknown"
SOURCE_MALFORMED = "malformed_fallback"


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _sanitized_expires_at(value: Any) -> Optional[float]:
    """A non-numeric/non-finite `expires_at` is untrustworthy.

    Treated as indefinite (no expiry) rather than letting garbage resolve
    toward "not quiet" — the same conservative direction as a malformed
    record. A negative or already-past timestamp is not garbage, though: it
    is an ordinary (if unusual) value and compares normally in `resolve()`.
    """
    if value is None:
        return None
    if _is_finite_number(value):
        return float(value)
    return None


def new_state(
    *,
    enabled: bool,
    source: str,
    reason: Optional[str] = None,
    set_at: Optional[float] = None,
    expires_at: Optional[float] = None,
) -> Dict[str, Any]:
    """Build a canonical quiet_state record. Pure — does not touch a session."""
    return {
        "enabled": bool(enabled),
        "source": source,
        "reason": reason,
        "set_at": set_at,
        "expires_at": expires_at,
    }


def _well_formed(record: Any) -> bool:
    """A structured record is well-formed iff it is a dict with a strict
    bool `enabled`. `is True`/`is False` identity, not truthy coercion —
    same discipline as `policy.py`'s `session.get(...) is True` checks, so a
    stray `"true"` or `1` cannot silently count as a real toggle.
    """
    if not isinstance(record, dict):
        return False
    enabled = record.get("enabled")
    return enabled is True or enabled is False


def migrate_legacy(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return the canonical quiet_state record implied by a session dict.

    Never mutates `data`. Precedence, in order:

      1. A well-formed structured `quiet_state` wins outright — even over a
         disagreeing legacy `spark_quiet_mode` bool. Once structured state
         exists it is authoritative; a stale bare bool left over from before
         this module never overrides it.
      2. A `quiet_state` that is *present but malformed* (wrong type, or a
         dict without a strict-bool `enabled`) fails conservatively:
         `enabled=True`, `source="malformed_fallback"`. Something attempted
         to write structured state and produced garbage — that is evidence
         a write went wrong, not evidence quiet mode is off, so it is not
         treated the same as case 3 below.
      3. An absent `quiet_state` (missing key, or explicit JSON `null`)
         migrates the legacy bare bool: `spark_quiet_mode is True` ->
         `enabled=True, source="unknown"`, indefinite (no `expires_at` was
         ever recorded, so none is invented); anything else (`False`,
         missing, any non-`True` value) -> `enabled=False, source="unknown"`.
         Never guessed beyond that, never given an expiry it didn't have.
    """
    raw = data.get(QUIET_STATE_KEY)

    if raw is not None and not _well_formed(raw):
        return new_state(
            enabled=True,
            source=SOURCE_MALFORMED,
            reason="quiet_state was present but malformed",
        )

    if raw is not None:
        return new_state(
            enabled=raw["enabled"],
            source=raw.get("source") or SOURCE_UNKNOWN,
            reason=raw.get("reason"),
            set_at=raw.get("set_at"),
            expires_at=_sanitized_expires_at(raw.get("expires_at")),
        )

    return new_state(enabled=data.get(LEGACY_KEY) is True, source=SOURCE_UNKNOWN)


def resolve(data: Dict[str, Any], *, now: float) -> bool:
    """The single derivation of "is quiet mode currently active for `data`".

    Deterministic and read-time only — this never mutates `data` and never
    writes anything back when a temporary window has lapsed. A `quiet_state`
    with `enabled=True` and a past `expires_at` therefore reads as inactive
    here without anyone having cleared the record on disk; the record stays
    `enabled=True` until a real writer (`state.set_quiet_mode()` /
    `clear_quiet_mode()`) changes it. Expiry is a property of what a caller
    currently sees, not a state transition anyone recorded.
    """
    state = migrate_legacy(data)
    if not state["enabled"]:
        return False
    expires_at = state["expires_at"]
    return expires_at is None or now < expires_at
