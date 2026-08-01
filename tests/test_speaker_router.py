import datetime as dt

from pxh import spark_config
from pxh.speaker_router import choose_target

OFFICE = "media_player.googlehome1094"
SHED = "media_player.laura_s_room_speaker"
LIVING = "media_player.nest_hub_max"


def _iso(now_ts: float, age_s: float) -> str:
    return dt.datetime.fromtimestamp(now_ts - age_s, tz=dt.timezone.utc).isoformat()


def test_config_speaker_rooms_shape():
    assert spark_config.SPEAKER_ROOMS["office"] == OFFICE
    assert spark_config.SPEAKER_DEFAULT_ROOM in spark_config.SPEAKER_ROOMS
    assert "media_player.shack_speakers" not in spark_config.SPEAKER_ROOMS.values()
    # every routable entity must be announce-allowed (tool-announce filters on this)
    assert set(spark_config.SPEAKER_ROOMS.values()) <= set(spark_config.ANNOUNCE_ALLOWED_TARGETS)


def test_fresh_last_heard_wins():
    now = 1_000_000.0
    lh = {"room": "shed", "ts": _iso(now, 60)}
    assert choose_target(lh, {OFFICE, SHED, LIVING}, now) == SHED


def test_stale_last_heard_falls_to_default_room():
    now = 1_000_000.0
    lh = {"room": "shed", "ts": _iso(now, spark_config.SPEAKER_STICKY_S + 1)}
    assert choose_target(lh, {OFFICE, SHED, LIVING}, now) == OFFICE


def test_unavailable_last_heard_falls_to_default_room():
    now = 1_000_000.0
    lh = {"room": "shed", "ts": _iso(now, 60)}
    assert choose_target(lh, {OFFICE, LIVING}, now) == OFFICE


def test_unavailable_default_falls_to_first_allowed():
    now = 1_000_000.0
    # office unavailable → first available entry of ANNOUNCE_ALLOWED_TARGETS order
    assert choose_target(None, {LIVING, SHED}, now) == LIVING


def test_nothing_available_returns_none():
    assert choose_target(None, set(), 1_000_000.0) is None


def test_ha_unreachable_skips_availability_check():
    # available=None → default room wins without filtering
    assert choose_target(None, None, 1_000_000.0) == OFFICE


def test_malformed_last_heard_is_absent():
    now = 1_000_000.0
    for bad in ({}, {"room": "office"}, {"ts": _iso(now, 5)},
                {"room": "nonexistent", "ts": _iso(now, 5)},
                {"room": "shed", "ts": "not-a-date"}):
        assert choose_target(bad, {OFFICE, SHED, LIVING}, now) == OFFICE


def test_room_not_in_allowlist_never_routes(monkeypatch):
    """SPEAKER_ROOMS is a routing map, not an allowlist — every routed entity
    must also pass ANNOUNCE_ALLOWED_TARGETS, or a future room-map typo
    bypasses the announce allowlist entirely."""
    monkeypatch.setitem(spark_config.SPEAKER_ROOMS, "garage", "media_player.rogue")
    now = 1_000_000.0
    lh = {"room": "garage", "ts": _iso(now, 60)}
    assert choose_target(lh, {"media_player.rogue", OFFICE, SHED, LIVING}, now) == OFFICE


import json
import time

import pxh.speaker_router as sr


def test_read_last_heard_missing_and_malformed(tmp_path):
    assert sr.read_last_heard(tmp_path / "nope.json") is None
    bad = tmp_path / "last_heard.json"
    bad.write_text("{not json")
    assert sr.read_last_heard(bad) is None


def test_read_last_heard_roundtrip(tmp_path):
    p = tmp_path / "last_heard.json"
    p.write_text(json.dumps({"room": "office", "ts": "2026-08-01T04:00:00+00:00"}))
    assert sr.read_last_heard(p) == {"room": "office", "ts": "2026-08-01T04:00:00+00:00"}


def test_fetch_available_filters_unavailable(monkeypatch):
    states = {"media_player.a": "idle", "media_player.b": "unavailable",
              "media_player.c": "playing"}
    monkeypatch.setattr(sr, "_get_state", lambda e, base, tok, timeout: states.get(e))
    got = sr.fetch_available(list(states), "http://ha", "tok")
    assert got == {"media_player.a", "media_player.c"}


def test_fetch_available_all_errors_returns_none(monkeypatch):
    monkeypatch.setattr(sr, "_get_state", lambda e, base, tok, timeout: None)
    assert sr.fetch_available(["media_player.a"], "http://ha", "tok") is None


def test_resolve_speaker_end_to_end(tmp_path, monkeypatch):
    from pxh import spark_config
    p = tmp_path / "last_heard.json"
    now_iso = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat()
    p.write_text(json.dumps({"room": "living room", "ts": now_iso}))
    monkeypatch.setattr(sr, "LAST_HEARD_PATH", p)
    monkeypatch.setattr(sr, "fetch_available", lambda *a, **k: None)  # HA unreachable path
    assert sr.resolve_speaker() == "media_player.nest_hub_max"
