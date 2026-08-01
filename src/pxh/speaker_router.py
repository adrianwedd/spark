"""Pick the Nest speaker nearest Adrian. Pure logic — testable without HA or hardware.

Resolution order (spec §Architecture-1):
  1. last-heard room, if fresh (< SPEAKER_STICKY_S) and available
  2. SPEAKER_DEFAULT_ROOM, if available
  3. first available entry in ANNOUNCE_ALLOWED_TARGETS order
  4. None — caller falls back to the onboard speaker
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.request
from pathlib import Path

from pxh import spark_config

STATE_DIR = Path(os.environ.get("PROJECT_ROOT", ".")) / "state"
LAST_HEARD_PATH = STATE_DIR / "last_heard.json"


def _last_heard_entity(last_heard: dict | None, now_ts: float) -> str | None:
    if not isinstance(last_heard, dict):
        return None
    room = last_heard.get("room")
    ts_raw = last_heard.get("ts")
    entity = spark_config.SPEAKER_ROOMS.get(room) if isinstance(room, str) else None
    if not entity or not isinstance(ts_raw, str):
        return None
    try:
        ts = dt.datetime.fromisoformat(ts_raw)
    except ValueError:
        return None
    if ts.tzinfo is None:          # naive timestamps are treated as UTC
        ts = ts.replace(tzinfo=dt.timezone.utc)
    if now_ts - ts.timestamp() >= spark_config.SPEAKER_STICKY_S:
        return None                # stale-as-absent: don't strand SPARK talking to an empty shed
    return entity


def choose_target(last_heard: dict | None, available: set[str] | None,
                  now_ts: float) -> str | None:
    """Return one media_player entity, or None (caller uses onboard speaker).

    available=None means HA was unreachable: skip the availability filter and
    trust the cast to fail forward (spec §Error-handling).
    """
    allowed = set(spark_config.ANNOUNCE_ALLOWED_TARGETS)

    def _ok(entity: str | None) -> bool:
        # Membership in ANNOUNCE_ALLOWED_TARGETS is mandatory: tool-announce
        # filters targets against it anyway, so returning a non-allowed entity
        # would error the whole route instead of degrading to the next choice.
        return (bool(entity) and entity in allowed
                and (available is None or entity in available))

    entity = _last_heard_entity(last_heard, now_ts)
    if _ok(entity):
        return entity
    default = spark_config.SPEAKER_ROOMS.get(spark_config.SPEAKER_DEFAULT_ROOM)
    if _ok(default):
        return default
    for candidate in spark_config.ANNOUNCE_ALLOWED_TARGETS:
        if _ok(candidate):
            return candidate
    return None


def read_last_heard(path: Path | None = None) -> dict | None:
    p = path or LAST_HEARD_PATH
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _get_state(entity: str, ha_base: str, ha_token: str, timeout: float) -> str | None:
    req = urllib.request.Request(
        f"{ha_base}/api/states/{entity}",
        headers={"Authorization": f"Bearer {ha_token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()).get("state")
    except Exception:
        return None


def fetch_available(entities: list[str], ha_base: str, ha_token: str,
                    timeout: float = 2.0) -> set[str] | None:
    # Parallel: sequential 3s-timeout requests over ~6 entities is an ~18s
    # worst case before the route even starts. Concurrent, the floor is one
    # timeout (~2s).
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, len(entities))) as pool:
        states = list(pool.map(
            lambda e: _get_state(e, ha_base, ha_token, timeout), entities))
    got = dict(zip(entities, states))
    if all(v is None for v in got.values()):
        return None                       # HA unreachable — caller skips the filter
    return {e for e, s in got.items() if s not in (None, "unavailable", "unknown")}


def resolve_speaker() -> str | None:
    """One-call resolution for tool-voice: state + config + live availability."""
    candidates = list(dict.fromkeys(
        list(spark_config.SPEAKER_ROOMS.values()) + list(spark_config.ANNOUNCE_ALLOWED_TARGETS)))
    available = fetch_available(candidates, spark_config.HA_BASE_URL,
                                os.environ.get("PX_HA_TOKEN", ""))
    return choose_target(read_last_heard(), available, time.time())
