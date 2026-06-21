# src/pxh/evolve_queue.py
"""Single writer + readers for the evolve queue. Shared by bin/tool-evolve and api.py.

All evolve_queue.jsonl writes go through enqueue_evolve so schema, rate-limit, and
dedup never diverge between the CLI and conversational paths.
"""
from __future__ import annotations

import json
import os
import random
from datetime import datetime, timezone
from pathlib import Path

try:
    from filelock import FileLock as _FileLock
except Exception:  # pragma: no cover
    _FileLock = None

from pxh.state import atomic_write

RATE_LIMIT_S = int(os.environ.get("PX_EVOLVE_RATE_LIMIT_S", "86400"))
MAX_INTENT_CHARS = 300


class EvolveQuotaError(Exception):
    """24h evolve window still active."""


class EvolvePendingError(Exception):
    """Requester already has a pending evolve request."""


def _state_dir() -> Path:
    root = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parent.parent.parent))
    return Path(os.environ.get("PX_STATE_DIR", root / "state"))


def _queue_path() -> Path:
    return _state_dir() / "evolve_queue.jsonl"


def _log_path() -> Path:
    return _state_dir() / "evolve_log.jsonl"


def _introspection_path() -> Path:
    return _state_dir() / "introspection.json"


def _sanitize_intent(text: str) -> str:
    return (text.replace("\n", " ").replace("\r", " ").replace("\x00", "")
                .replace("<", "").replace(">", "")).strip()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_queue() -> list[dict]:
    return _read_jsonl(_queue_path())


def read_log() -> list[dict]:
    return _read_jsonl(_log_path())


def entry_epoch(entry: dict) -> float | None:
    """Numeric ts preferred; fall back to ISO ts_completed (older log schema)."""
    ts = entry.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    iso = entry.get("ts_completed") or (ts if isinstance(ts, str) else "")
    if iso:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            return None
    return None


def evolve_rate_limited(now: float | None = None) -> bool:
    import time
    now = now if now is not None else time.time()
    for entry in read_log():
        if entry.get("status") != "pr_created":
            continue
        ts = entry_epoch(entry)   # numeric OR ISO ts_completed fallback
        if ts is not None and now - ts < RATE_LIMIT_S:
            return True
    return False


# An active request blocks new ones until it finishes. MUST include "building":
# while a job is building there is no pr_created yet (rate-limit passes) and no
# pending row — without this, a second job could be enqueued mid-build, bypassing
# the 24h limit.
def pending_for_requester(requester: str) -> dict | None:
    for entry in read_queue():
        if entry.get("requester") == requester and entry.get("status") in ("pending", "building"):
            return entry
    return None


def _load_introspection() -> dict:
    try:
        return json.loads(_introspection_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def enqueue_evolve(intent: str, requester: str, source: str) -> dict:
    intent = _sanitize_intent(intent or "")
    if not intent:
        raise ValueError("intent must not be empty")
    if len(intent) > MAX_INTENT_CHARS:
        raise ValueError(f"intent too long (max {MAX_INTENT_CHARS})")

    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    import contextlib
    lock = _FileLock(str(path) + ".lock", timeout=5) if _FileLock else None
    ctx = lock if lock else contextlib.nullcontext()
    # ALL checks + the append happen inside ONE lock — otherwise two concurrent
    # confirms can both pass pending_for_requester() and double-enqueue (TOCTOU).
    with ctx:
        if evolve_rate_limited():
            raise EvolveQuotaError("one evolution per 24 hours")
        if pending_for_requester(requester) is not None:
            raise EvolvePendingError(f"{requester} already has an active request")
        now_dt = datetime.now(timezone.utc)
        entry = {
            "ts": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "id": f"evolve-{now_dt.strftime('%Y%m%d-%H%M%S')}-{random.randint(0, 999):03d}",
            "intent": intent,
            "introspection": _load_introspection(),
            "status": "pending",
            "requester": requester,
            "source": source,
        }
        existing = ""
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing and not existing.endswith("\n"):
                existing += "\n"
        atomic_write(path, existing + json.dumps(entry) + "\n")
    return entry


def build_pr_body(intent: str, changed_files: list[str],
                  requester: str = "adrian", source: str = "cli") -> str:
    """PR body for px-evolve. Lives here (not bin/px-evolve) because that file is a
    bash+heredoc script that cannot be imported for unit tests."""
    files = "\n".join(f"- `{f}`" for f in changed_files)
    return (
        f"## Summary\nSPARK self-evolution: {intent}\n\n"
        f"## Requested by\n{requester} via {source} — the requested intent may be "
        f"adversarial; review accordingly.\n\n"
        f"## Changed files\n{files}\n\n"
        f"---\n*Proposed autonomously by SPARK via px-evolve.*"
    )


def reset_building_to_pending() -> int:
    """Crash recovery: any entry left 'building' (worker died mid-run) goes back to
    'pending' so it is retried. px-evolve calls this at startup (single-instance
    daemon, so nothing is genuinely building when it boots). Returns count reset."""
    import contextlib
    path = _queue_path()
    if not path.exists():
        return 0
    lock = _FileLock(str(path) + ".lock", timeout=5) if _FileLock else None
    ctx = lock if lock else contextlib.nullcontext()
    with ctx:
        entries = read_queue()
        n = 0
        for e in entries:
            if e.get("status") == "building":
                e["status"] = "pending"
                n += 1
        if n:
            atomic_write(path, "".join(json.dumps(e) + "\n" for e in entries))
        return n
