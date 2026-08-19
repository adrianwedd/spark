"""The wake grant: a window in which SPARK may answer the person who summoned him.

"Hey Spark" is a deliberate summons, and answering a summons is not the same
act as speaking unbidden. Night silence, quiet mode and on-call suppression all
exist to stop SPARK *initiating* audio; none of them were ever meant to make
him unable to reply when addressed. This module is the fact that distinguishes
the two cases, and policy.py is where that fact is applied.

What a grant is NOT
-------------------
It is a capability over one narrow thing — an audible, interactive reply — and
confers no other authority. policy.evaluate() consults it only for
``effect="audio"`` and ``origin="interactive"``; everywhere else the verdict
with a grant equals the verdict without one. It does not mutate session state
either: quiet mode stays set, the conversation speaks through it, and quiet
behaviour resumes on its own when the window closes. bin/tool-quiet remains the
only way to actually end the dysregulation protocol.

Why lifetime is not measured in wall time
-----------------------------------------
This Pi has no RTC. Its clock has been observed stepping ~49 minutes during
boot as NTP corrects the restored time. A grant with a wall-clock expiry would
therefore either die the instant the step landed or outlive its window by most
of an hour, decided entirely by which direction the correction went — and the
window would be at its most fragile precisely when the machine is least
settled. So validity is bound to two things that a clock correction cannot
move:

  * ``CLOCK_BOOTTIME``, which counts from boot, is immune to NTP, and (unlike
    CLOCK_MONOTONIC) keeps counting across suspend, so a suspended robot
    cannot bank credit toward a window that should have closed.
  * the kernel's boot id, so a grant from a previous boot is invalid on its
    face.

The file lives on tmpfs and is already destroyed by a reboot; the boot id means
that guarantee does not *depend* on the file's location being right. Two
independent reasons for the same property, because the property is the point.

A UTC stamp is recorded alongside, and is diagnostic only. Nothing here reads
it. The test suite pins that by making time.time() raise and requiring
is_grant_active() to keep answering.

Who may open one
----------------
bin/px-wake-listen, from a real detection, and nothing else. Turns belonging to
that conversation extend the window; inactivity closes it; MAX_CONVERSATION_S
caps the total so a room with a television in it cannot hold the gate open all
night one legitimate-looking turn at a time.

One residual, on the record
---------------------------
Everything on this box runs as ``pi``, so file permissions are not a trust
boundary and this module does not pretend otherwise: a process holding a shell
could write a well-formed grant. What is actually enforced is narrower and
still worth having — no caller can pass wake-ness as an argument
(policy_context.evaluate_audio_sink does not accept it), and no environment
variable is read as a truth value. PX_WAKE_GRANT_DIR names a *location*; the
document found there must still carry a matching boot id and an unexpired
boottime. That closes the prompt-driven and accidental bypasses, which are the
ones that actually happen here — including SPARK's own resident brain session,
whose tool envelope is the whole bin/ directory.

Blacklisted from self-evolution alongside policy.py — see
pxh.claude_session.BLACKLIST_FILES.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import uuid
from pathlib import Path

GRANT_DIR_ENV = "PX_WAKE_GRANT_DIR"

# Runtime state, deliberately NOT pxh.runtime_paths. That module falls back to
# state/ on hosts without /run/spark, which is right for a heartbeat (a stale
# one reads as stale) and wrong for a grant: a fallback would put a permission
# document on the SD card, where it would survive the reboot that is supposed
# to destroy it. A grant that cannot be written is simply not granted.
DEFAULT_GRANT_DIR = Path("/run/spark/wake")
GRANT_FILENAME = "wake_grant.json"

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")

DEFAULT_TTL_S = 180.0
MAX_CONVERSATION_S = 900.0

# CLOCK_BOOTTIME is Linux-only; CI and dev machines that lack it fall back to
# CLOCK_MONOTONIC, which has the same NTP immunity and the same reset-at-boot
# behaviour, and differs only across suspend.
_CLOCK = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)


def boottime() -> float:
    """Seconds since boot. Never moves when the wall clock is corrected."""
    return time.clock_gettime(_CLOCK)


def boot_id() -> str | None:
    """The kernel's boot id, or None if it cannot be read (→ no grants)."""
    try:
        value = BOOT_ID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def grant_dir() -> Path:
    configured = os.environ.get(GRANT_DIR_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_GRANT_DIR


def grant_path() -> Path:
    return grant_dir() / GRANT_FILENAME


def _utc_hint() -> str:
    """Diagnostic only. Never consulted when deciding validity."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _is_number(value: object) -> bool:
    # bool is an int subclass, and `True` as an expiry would otherwise read as
    # boottime 1 — nonsense that happens to type-check.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_grant() -> dict | None:
    """The grant if one is valid *right now*, else None. Never raises.

    Every failure mode resolves to None, which suppresses audio. That is the
    correct direction: the question this answers is "may SPARK speak during a
    window that would otherwise be silent", and the honest answer to a
    malformed, foreign or unreadable document is no.
    """
    try:
        raw = grant_path().read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None

    current = boot_id()
    if current is None:
        return None
    if not isinstance(doc.get("boot_id"), str) or doc["boot_id"] != current:
        return None
    if not isinstance(doc.get("conversation_id"), str):
        return None

    opened, expires = doc.get("opened_boottime"), doc.get("expires_boottime")
    if not _is_number(opened) or not _is_number(expires):
        return None
    if expires < opened:
        return None
    # A window wider than the cap was never produced by this module, so it is
    # treated as corruption rather than honoured as a very generous grant.
    if expires > opened + MAX_CONVERSATION_S:
        return None

    now = boottime()
    if opened > now + 1.0:      # opened in the future: not from this boot's clock
        return None
    if expires <= now:
        return None
    return doc


def is_grant_active() -> bool:
    """Whether a wake conversation is currently open.

    The single question policy_context asks. Deliberately silent — no logging,
    because log_event() timestamps with the wall clock and the validity path
    must stay unable to reach it.
    """
    return read_grant() is not None


def _write(doc: dict) -> bool:
    path = grant_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)          # atomic: a reader never sees a half file
        return True
    except OSError:
        return False


def open_grant(*, ttl_s: float = DEFAULT_TTL_S) -> str | None:
    """Open a window. Only bin/px-wake-listen, from a real detection.

    Returns the conversation id that may later refresh or close it, or None if
    no grant could be written — in which case SPARK simply stays as quiet as he
    was before.
    """
    current = boot_id()
    if current is None:
        return None
    now = boottime()
    conversation_id = uuid.uuid4().hex
    doc = {
        "conversation_id": conversation_id,
        "boot_id": current,
        "opened_boottime": now,
        "expires_boottime": now + min(float(ttl_s), MAX_CONVERSATION_S),
        "turns": 1,
        "opened_utc": _utc_hint(),
    }
    return conversation_id if _write(doc) else None


def refresh_grant(conversation_id: str, *, ttl_s: float = DEFAULT_TTL_S) -> bool:
    """Extend the window for a turn belonging to the conversation that owns it.

    Refuses on three counts, each of which matters:

      * a different conversation's id — an ambient detection elsewhere must not
        prolong this one;
      * an already-expired grant — inactivity closes the window, and a late
        turn asks to be summoned again rather than resumed;
      * anything past MAX_CONVERSATION_S from the original summons.
    """
    doc = read_grant()
    if doc is None or doc.get("conversation_id") != conversation_id:
        return False

    now = boottime()
    ceiling = doc["opened_boottime"] + MAX_CONVERSATION_S
    expires = min(now + float(ttl_s), ceiling)
    # Never shorten: a short-TTL refresh mid-window should not close it early.
    doc["expires_boottime"] = max(expires, doc["expires_boottime"])
    doc["turns"] = int(doc.get("turns", 1)) + 1
    doc["refreshed_utc"] = _utc_hint()
    return _write(doc)


def close_grant(conversation_id: str) -> None:
    """End the window when the conversation ends. Idempotent, never raises.

    Scoped to the owning conversation so a stale one finishing cannot silence
    the summons that replaced it.
    """
    doc = read_grant()
    if doc is None or doc.get("conversation_id") != conversation_id:
        return
    try:
        grant_path().unlink()
    except OSError:
        pass
