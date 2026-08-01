# Nest Speaker Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SPARK-initiated speech plays on the Nest speaker nearest Adrian (afterwords `data` voice via the M5 relay), with the onboard speaker as fallback.

**Architecture:** A pure-function router (`speaker_router.py`) picks one `media_player.*` entity from `state/last_heard.json` (written by `api.py` when the HA conversation component reports which room heard Adrian). `bin/tool-voice` becomes the routing chokepoint: it tries `bin/tool-announce` with the routed target and falls back to the existing onboard path on error — but **not** on night-silence suppression. `tool-announce` gains volume snapshot/restore around the cast.

**Tech Stack:** Python 3.11 (Pi system python for bin tools), FastAPI, HA custom component (aiohttp), pytest with the existing `isolated_project` + HTTP stub harness in `tests/test_tools.py`.

**Spec:** `docs/superpowers/specs/2026-07-31-nest-speaker-routing-design.md`. Prerequisites P1/P2 are DONE (2026-08-01): relay live on M5, G1/G2 passed, `ANNOUNCE_ENABLED=True`, targets corrected. P3 (warm synth latency) is measured in Task 7.

## Global Constraints

- Conversational replies (HA satellite → `async_set_speech`) already route correctly — do not touch that path.
- v1 is single-target only; `media_player.shack_speakers` must never be a routing candidate (echo + it's the music amp).
- Night-silence suppression must NOT fall through to the onboard speaker (spec: Error handling table).
- Area names are user-influenced strings: pass through `_sanitize_chat_text()` and match against `SPEAKER_ROOMS` keys — never trusted.
- Every failure degrades one step and never raises.
- All bin tools emit a single JSON object to stdout and support `PX_DRY=1`.
- Run tests with `python -m pytest` from the repo root, venv activated.

---

### Task 1: `speaker_router.choose_target()` — pure resolution logic + config

**Files:**
- Create: `src/pxh/speaker_router.py`
- Create: `tests/test_speaker_router.py`
- Modify: `src/pxh/spark_config.py` (after the `ANNOUNCE_*` block, ~line 70)

**Interfaces:**
- Produces: `choose_target(last_heard: dict | None, available: set[str] | None, now_ts: float) -> str | None`, and config `SPEAKER_ROOMS: dict[str, str]`, `SPEAKER_DEFAULT_ROOM: str`, `SPEAKER_STICKY_S: int`.
- `last_heard` is `{"room": str, "ts": "<iso8601 with tz>"}` or `None`. `available=None` means "HA unreachable — skip availability filtering" (treat everything as available). `now_ts` is epoch seconds.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_speaker_router.py
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_speaker_router.py -q`
Expected: FAIL — `ModuleNotFoundError: pxh.speaker_router` (and `AttributeError: SPEAKER_ROOMS` if run individually).

- [ ] **Step 3: Add config**

```python
# spark_config.py — append after ANNOUNCE_READ_TIMEOUT
# --- Speaker routing (Nest-first speech; see docs/superpowers/specs/2026-07-31-nest-speaker-routing-design.md)
SPEAKER_ROOMS = {                       # keys MUST match HA area names, lowercased (verified at deploy — Task 7).
                                        # Multi-word HA areas keep their spaces: "Living Room" → key "living room".
    "office": "media_player.googlehome1094",        # "Office Mini"
    "shed":   "media_player.laura_s_room_speaker",  # "Shed Mini"
    "living": "media_player.nest_hub_max",
}
SPEAKER_DEFAULT_ROOM = "office"
SPEAKER_STICKY_S = 1800     # last-heard older than this = Adrian probably moved; stale-as-absent
```

- [ ] **Step 4: Implement the router**

```python
# src/pxh/speaker_router.py
"""Pick the Nest speaker nearest Adrian. Pure logic — testable without HA or hardware.

Resolution order (spec §Architecture-1):
  1. last-heard room, if fresh (< SPEAKER_STICKY_S) and available
  2. SPEAKER_DEFAULT_ROOM, if available
  3. first available entry in ANNOUNCE_ALLOWED_TARGETS order
  4. None — caller falls back to the onboard speaker
"""
from __future__ import annotations

import datetime as dt

from pxh import spark_config


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
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_speaker_router.py -q` — expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/pxh/speaker_router.py tests/test_speaker_router.py src/pxh/spark_config.py
git commit -m "feat(speaker): pure room-to-entity router with sticky last-heard"
```

---

### Task 2: `read_last_heard()` / `fetch_available()` / `resolve_speaker()` — the impure shell

**Files:**
- Modify: `src/pxh/speaker_router.py`
- Modify: `tests/test_speaker_router.py`

**Interfaces:**
- Consumes: `choose_target` from Task 1.
- Produces:
  - `read_last_heard(path: Path | None = None) -> dict | None` — reads `state/last_heard.json` (default `STATE_DIR / "last_heard.json"`), `None` on missing/malformed.
  - `fetch_available(entities: list[str], ha_base: str, ha_token: str, timeout: float = 3.0) -> set[str] | None` — GET `/api/states/<e>` per entity; a state other than `"unavailable"`/`"unknown"` counts as available. Returns `None` if every request errored (HA unreachable).
  - `resolve_speaker() -> str | None` — the one-call convenience `tool-voice` uses: reads state + config + env (`PX_HA_TOKEN`), calls `choose_target(now_ts=time.time())`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_speaker_router.py`)

```python
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
    p.write_text(json.dumps({"room": "living", "ts": now_iso}))
    monkeypatch.setattr(sr, "LAST_HEARD_PATH", p)
    monkeypatch.setattr(sr, "fetch_available", lambda *a, **k: None)  # HA unreachable path
    assert sr.resolve_speaker() == "media_player.nest_hub_max"
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_speaker_router.py -q`; expected: new tests FAIL (`AttributeError: read_last_heard`).

- [ ] **Step 3: Implement** (append to `speaker_router.py`)

```python
import json
import os
import time
import urllib.request
from pathlib import Path

STATE_DIR = Path(os.environ.get("PROJECT_ROOT", ".")) / "state"
LAST_HEARD_PATH = STATE_DIR / "last_heard.json"


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
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_speaker_router.py -q`; expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/speaker_router.py tests/test_speaker_router.py
git commit -m "feat(speaker): last-heard reader, HA availability fetch, resolve_speaker()"
```

---

### Task 3: `api.py` records `{room, ts}` from chat posts carrying an area

**Files:**
- Modify: `src/pxh/api.py` — `PublicChatRequest` (~line 183) and `public_chat` (~line 1384)
- Test: `tests/test_public_chat.py` (append)

**Interfaces:**
- Consumes: `spark_config.SPEAKER_ROOMS`, existing `_sanitize_chat_text()` (api.py:1377), `_get_client_ip()` (api.py:144), `_public_state_dir()` (api.py:614 — there is **no** module-level `STATE_DIR` in api.py), `atomic_write` from `pxh.state`.
- Produces: `state/last_heard.json` = `{"room": "<SPEAKER_ROOMS key>", "ts": "<iso utc>"}`, the exact shape `read_last_heard()` consumes.

**Spoofing defense:** `/api/v1/public/chat` is unauthenticated and internet-exposed via the Cloudflare tunnel. The `area` hint is only accepted when the resolved client IP is private (RFC1918/loopback): the HA component posts directly on the LAN, while tunnel traffic arrives from localhost carrying a public `CF-Connecting-IP` that `_get_client_ip()` returns — so internet callers can never steer routing. Impact of the hint is nuisance-level either way, but the gate is one function.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_public_chat.py` — follow the file's existing TestClient fixture idiom; read its imports first and reuse them)

```python
# NOTE: starlette's TestClient reports peer host "testclient" (not an IP), so
# _area_trusted() would reject every test request. Monkeypatch it per-test.

def test_public_chat_area_writes_last_heard(client, monkeypatch):
    # `client` / state isolation fixtures: reuse whatever this file already uses
    # for POST /api/v1/public/chat tests; the assertion is about the side file.
    import json as _json
    from pxh import api as api_mod
    monkeypatch.setattr(api_mod, "_area_trusted", lambda ip: True)
    resp = client.post("/api/v1/public/chat",
                       json={"message": "hello", "area": "Office"})
    assert resp.status_code == 200
    data = _json.loads((api_mod._public_state_dir() / "last_heard.json").read_text())
    assert data["room"] == "office"
    assert "ts" in data


def test_public_chat_unknown_area_writes_nothing(client, monkeypatch):
    from pxh import api as api_mod
    monkeypatch.setattr(api_mod, "_area_trusted", lambda ip: True)
    lh = api_mod._public_state_dir() / "last_heard.json"
    if lh.exists():
        lh.unlink()
    resp = client.post("/api/v1/public/chat",
                       json={"message": "hello", "area": "<script>garage</script>"})
    assert resp.status_code == 200
    assert not lh.exists()


def test_public_chat_untrusted_ip_writes_nothing(client):
    # peer host "testclient" is not a private IP → hint rejected by default
    from pxh import api as api_mod
    lh = api_mod._public_state_dir() / "last_heard.json"
    if lh.exists():
        lh.unlink()
    resp = client.post("/api/v1/public/chat",
                       json={"message": "hello", "area": "Office"})
    assert resp.status_code == 200
    assert not lh.exists()


def test_area_trusted_ip_classes():
    from pxh.api import _area_trusted
    assert _area_trusted("192.168.0.200") is True    # HA on the LAN
    assert _area_trusted("127.0.0.1") is True        # local curl
    assert _area_trusted("203.0.113.7") is False     # tunnel / internet
    assert _area_trusted("testclient") is False      # not an IP at all


def test_public_chat_without_area_still_works(client):
    resp = client.post("/api/v1/public/chat", json={"message": "hello"})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_public_chat.py -k area -q`; expected: FAIL (422 or missing file — `area` field unknown is silently ignored by pydantic, so the file-existence assert is the failing one).

- [ ] **Step 3: Implement**

```python
# module level, near _get_client_ip (import ipaddress at the top):
def _area_trusted(ip: str) -> bool:
    """Routing hints only from the LAN — the HA component posts directly;
    tunnel traffic resolves to a public CF-Connecting-IP and is rejected.

    NOTE: implemented as explicit RFC1918 + loopback membership, NOT
    ipaddress.is_private — is_private also returns True for RFC 5737
    TEST-NET blocks (e.g. 203.0.113.7), which this gate must reject."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or any(addr in net for net in _RFC1918_NETWORKS)

# PublicChatRequest — add field:
    area: Optional[str] = Field(None, max_length=100)

# public_chat — after the rate-limit check, before prompt building:
    if req.area and _area_trusted(_get_client_ip(request)):
        _room = _sanitize_chat_text(req.area).strip().lower()
        if _room in spark_config.SPEAKER_ROOMS:
            try:
                from pxh.state import atomic_write
                atomic_write(
                    _public_state_dir() / "last_heard.json",
                    json.dumps({"room": _room,
                                "ts": datetime.now(timezone.utc).isoformat()}))
            except OSError:
                pass    # routing hint is best-effort; chat must never fail on it
```

(Match the file's actual import names — it may already import `datetime`/`timezone`/`json`. Use `_public_state_dir()`, NOT a bare `STATE_DIR` — api.py has no such module global and the write would raise `NameError`, which `except OSError` does not catch, 500ing the chat request. If `atomic_write`'s signature differs, follow `pxh/state.py`.)

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_public_chat.py -q`; expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/api.py tests/test_public_chat.py
git commit -m "feat(api): record last-heard room from chat posts carrying an HA area"
```

---

### Task 4: HA component resolves and sends the area name

**Files:**
- Modify: `ha/custom_components/spark_conversation/conversation.py` (`async_process`, ~line 48)

**Interfaces:**
- Consumes: `user_input.device_id` (may be None for non-satellite invocations), HA `device_registry` / `area_registry`.
- Produces: `"area": <area name or None>` key in the existing chat POST body.

There is no HA test harness in this repo — this task is code + the live verification in Task 7. Keep the change minimal and defensive.

- [ ] **Step 1: Implement**

```python
# add to imports (dr is already imported):
from homeassistant.helpers import area_registry as ar

# in async_process, before building the POST:
        area_name = None
        try:
            if user_input.device_id:
                device = dr.async_get(self.hass).async_get(user_input.device_id)
                if device and device.area_id:
                    area = ar.async_get(self.hass).async_get_area(device.area_id)
                    if area:
                        area_name = area.name
        except Exception:                       # a routing hint must never break chat
            _LOGGER.debug("area resolution failed", exc_info=True)

# and extend the json= body:
                json={"message": user_input.text,
                      "conversation_id": user_input.conversation_id,
                      "area": area_name},
```

- [ ] **Step 2: Sanity-compile** — `python -m py_compile ha/custom_components/spark_conversation/conversation.py`

- [ ] **Step 3: Commit**

```bash
git add ha/custom_components/spark_conversation/conversation.py
git commit -m "feat(ha): send the satellite's area name with each chat request"
```

---

### Task 5: `tool-announce` volume snapshot / restore around the cast

**Files:**
- Modify: `bin/tool-announce` (`_ha_state` ~line 123, `main` cast loop ~line 195)
- Test: `tests/test_tools.py` (extend `_StubHandler` + new test near line 1191)

**Interfaces:**
- Consumes: relay `duration_s` (already in the `/announce` response).
- Produces: after a successful cast — `media_player/volume_set` with the pre-cast `volume_level`. **Volume-only restore, no `media_play`**: casting `play_media` replaces the player's queue with the announcement URL, so a later `media_play` would replay the announcement, not resume Spotify — worse than not trying. No restore on `suppressed`/`error`/`dry` (those paths return before casting).
- Known accepted race: a human volume change during the announcement is overwritten by the snapshot. Single-user house, ≤20s window — documented, not defended.

- [ ] **Step 1: Write the failing test**

Extend `_StubHandler.do_GET` so states are configurable:

```python
    # class attribute:
    get_state = {"state": "idle"}
    def do_GET(self):
        _StubHandler.captured.append(("GET", self.path, None))
        self._send(200, _StubHandler.get_state)
```

New test (same env recipe as `test_tool_announce_live_path_posts_relay_and_ha`; the stub's `duration_s` is 1.2 so the restore sleep is ~2.2s):

```python
def test_tool_announce_restores_volume_and_playback(isolated_project):
    _StubHandler.captured = []
    _StubHandler.get_state = {"state": "playing",
                              "attributes": {"volume_level": 0.4, "app_name": "Spotify"}}
    srv = _start_stub()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    env = isolated_project["env"].copy()
    env.update({"PX_DRY": "0", "PX_ANNOUNCE_TEXT": "Dinner", "PX_BYPASS_SUDO": "1",
                "PX_ANNOUNCE_RELAY_URL": base, "PX_HA_HOST": base,
                "ANNOUNCE_RELAY_TOKEN": "t", "PX_HA_TOKEN": "t",
                "PX_NIGHT_SILENCE_START_H": "99", "PX_NIGHT_SILENCE_END_H": "0"})
    try:
        payload = parse_json(run_tool(["bin/tool-announce"], env))
    finally:
        srv.shutdown()
        _StubHandler.get_state = {"state": "idle"}
    assert payload["status"] == "ok"
    posts = [(p, b) for (m, p, b) in _StubHandler.captured if m == "POST"]
    vol = [b for (p, b) in posts if p.endswith("/media_player/volume_set")]
    play = [b for (p, b) in posts if p.endswith("/media_player/media_play")]
    assert vol and vol[0]["volume_level"] == 0.4
    assert not play   # media_play would replay the announcement, never issue it
```

And the malformed-duration case:

```python
def test_tool_announce_restores_volume_on_bad_duration(isolated_project):
    # same recipe, but make the stub's /announce response carry
    # {"duration_s": "garbage"} — volume_set must STILL fire (restore is in a
    # finally; a malformed relay field must not strand the speaker loud).
```

(Write it out fully — copy the recipe above, adjust the stub response and assertions.)

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_tools.py -k restore -q`; expected: FAIL (no volume_set POST captured).

- [ ] **Step 3: Implement**

In `bin/tool-announce`:

```python
def _ha_state_full(entity_id: str, ha_base: str, ha_token: str) -> dict | None:
    """Full state object (state + attributes) — None on any error."""
    # same urllib pattern as _ha_state, but return the parsed dict
    # then re-implement _ha_state as: (_ha_state_full(...) or {}).get("state")

def _ha_service(service: str, body: dict, ha_base: str, ha_token: str) -> None:
    """POST /api/services/media_player/<service>; best-effort, never raises."""
    try:
        _post_json(f"{ha_base}/api/services/media_player/{service}", body,
                   {"Authorization": f"Bearer {ha_token}"}, 5, 10)
    except Exception:
        pass
```

In `main()` — snapshot inside the cast loop, restore after it:

```python
        cast_ok = []
        snapshots: dict[str, dict] = {}
        for entity in targets:
            full = _ha_state_full(entity, cfg.HA_BASE_URL, ha_token) or {}
            state = full.get("state")
            if state == "unavailable":
                continue
            snapshots[entity] = full
            try:
                if _ha_cast(entity, audio_url, cfg.HA_BASE_URL, ha_token):
                    cast_ok.append(entity)
            except Exception:
                pass

        # Volume restore (spec §Interrupt-and-restore, narrowed): volume only —
        # play_media replaced the queue, so media_play would replay the
        # announcement, not resume the original app. Skipped when nothing cast.
        # duration_s is an external field: clamp it, and restore in a finally
        # so a malformed value or an interrupt can't strand the speaker loud.
        if cast_ok:
            try:
                try:
                    _dur = float(result.get("duration_s") or 0)
                except (TypeError, ValueError):
                    _dur = 0.0
                time.sleep(min(max(_dur, 0.0), 30.0) + 1.0)
            finally:
                for entity in cast_ok:
                    snap = snapshots.get(entity) or {}
                    vol = (snap.get("attributes") or {}).get("volume_level")
                    if isinstance(vol, (int, float)):
                        _ha_service("volume_set",
                                    {"entity_id": entity, "volume_level": vol},
                                    cfg.HA_BASE_URL, ha_token)
```

(`import time` at the top of the heredoc if not present.)

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_tools.py -k announce -q`; expected: all announce tests pass (existing ones must stay green — `do_GET` default is unchanged `idle`).

- [ ] **Step 5: Commit**

```bash
git add bin/tool-announce tests/test_tools.py
git commit -m "feat(announce): snapshot volume/playback before cast, restore after"
```

---

### Task 6: `tool-voice` routes SPARK speech through announce, onboard as fallback

**Files:**
- Modify: `bin/tool-voice` (in `main()`, after the persona-wrapper reroute ~line 90, before the network-TTS section)
- Modify: `src/pxh/spark_config.py` (one constant)
- Modify: `src/pxh/mind.py` (`_run_voice` timeout + urgent callers)
- Modify: `src/pxh/voice_loop.py` (`validate_action` voice branch: inject `PX_VOICE_NO_ROUTE=1`)
- Test: `tests/test_tools.py` (append), `tests/test_mind.py` (append)

**Interfaces:**
- Consumes: `pxh.speaker_router.resolve_speaker()` (Task 2), `bin/tool-announce` JSON contract (`status`: `ok` / `suppressed` / `error` / `dry`; success payload carries a `"targets"` list — verified against bin/tool-announce:210), `spark_config.ANNOUNCE_ENABLED`.
- Produces: tool-voice JSON gains `"route": "nest" | "onboard"`; on suppression it emits `{"status": "suppressed", "reason": "night_silence", "route": "nest"}` and does NOT speak onboard.

Routing applies only to SPARK speech: persona empty or `"spark"`. GREMLIN/VIXEN keep their character voices on the robot.

**Timeout architecture (why `PX_VOICE_NO_ROUTE` exists):** a route attempt can take up to `SPEAKER_ROUTE_TIMEOUT_S` (90s: cold synth ~33s + cast + restore sleep), but several existing callers kill tool-voice long before that — the voice-loop watchdog SIGTERMs the whole loop after 30s of a stale heartbeat (voice_loop.py:317, `PX_WATCHDOG_STALE_SECONDS`), and mind.py imposes 5s (critical battery, mind.py:1554), 20s (battery warn 1581, reflection-offline 3893), 30s (`_run_voice` 3136). A killed tool-voice never runs its onboard fallback — the speech is just lost. So:
- `PX_VOICE_NO_ROUTE=1` skips the Nest route entirely (straight onboard). Set by: the voice loop (the human is standing at the robot — onboard is the right speaker anyway, and it keeps the 30s watchdog honest) and mind.py's urgent paths (battery warn/critical, reflection-offline — alerts must not spend 90s casting).
- `_run_voice` (the greet/comment/weather path — where routing SHOULD happen) gets `timeout=spark_config.SPEAKER_ROUTE_TIMEOUT_S + 15` instead of 30.
- Urgent mind.py callers also set `PX_VOICE_URGENT=1` (see night rule below).

**Night-silence fallback rule (defense-in-depth):** when a route was *attempted* (SPARK persona, `ANNOUNCE_ENABLED`, no `PX_VOICE_NO_ROUTE`) and failed for any reason other than explicit suppression (dead relay, no target, timeout), tool-voice must re-check the night window itself (same `PX_NIGHT_SILENCE_*`-overridable bounds as tool-announce) before falling back onboard — otherwise a dead relay at 21:00 sidesteps the night gate that lives inside tool-announce. `PX_VOICE_URGENT=1` bypasses this check (battery warnings at night are deliberate). Interactive paths are unaffected: they set `PX_VOICE_NO_ROUTE=1`, never attempt the route, and keep today's behavior exactly.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tools.py`; `run_tool`/`parse_json`/`isolated_project` as elsewhere in the file)

```python
def test_tool_voice_routes_spark_speech_to_nest(isolated_project):
    """With a working relay stub and a routed target, tool-voice casts instead of espeak."""
    _StubHandler.captured = []
    srv = _start_stub()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    env = isolated_project["env"].copy()
    env.update({"PX_DRY": "0", "PX_TEXT": "hello there", "PX_PERSONA": "spark",
                "PX_BYPASS_SUDO": "1", "_PX_VOICE_PERSONA_DONE": "1",
                "PX_ANNOUNCE_RELAY_URL": base, "PX_HA_HOST": base,
                "ANNOUNCE_RELAY_TOKEN": "t", "PX_HA_TOKEN": "t",
                "PX_NIGHT_SILENCE_START_H": "99", "PX_NIGHT_SILENCE_END_H": "0"})
    try:
        payload = parse_json(run_tool(["bin/tool-voice"], env))
    finally:
        srv.shutdown()
    assert payload["route"] == "nest"
    assert payload["status"] == "ok"
    assert any("/announce" in p for (_, p, _) in _StubHandler.captured)


def test_tool_voice_falls_back_onboard_when_relay_dead(isolated_project):
    env = isolated_project["env"].copy()
    env.update({"PX_DRY": "1",             # dry keeps the fallback path silent + fast
                "PX_TEXT": "hello there", "PX_PERSONA": "spark",
                "PX_BYPASS_SUDO": "1", "_PX_VOICE_PERSONA_DONE": "1",
                "PX_ANNOUNCE_RELAY_URL": "http://127.0.0.1:1",   # dead port
                "PX_HA_HOST": "http://127.0.0.1:1",
                "PX_NIGHT_SILENCE_START_H": "99", "PX_NIGHT_SILENCE_END_H": "0"})
    payload = parse_json(run_tool(["bin/tool-voice"], env))
    assert payload["route"] == "onboard"
    assert payload["status"] == "ok"


def test_tool_voice_night_suppression_does_not_fall_back(isolated_project):
    _StubHandler.captured = []
    srv = _start_stub()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    env = isolated_project["env"].copy()
    env.update({"PX_DRY": "0", "PX_TEXT": "hello there", "PX_PERSONA": "spark",
                "PX_BYPASS_SUDO": "1", "_PX_VOICE_PERSONA_DONE": "1",
                "PX_ANNOUNCE_RELAY_URL": base, "PX_HA_HOST": base,
                "ANNOUNCE_RELAY_TOKEN": "t", "PX_HA_TOKEN": "t",
                "PX_NIGHT_SILENCE_START_H": "0", "PX_NIGHT_SILENCE_END_H": "24"})  # always night
    try:
        payload = parse_json(run_tool(["bin/tool-voice"], env))
    finally:
        srv.shutdown()
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "night_silence"


def test_tool_voice_gremlin_never_routes_to_nest(isolated_project):
    env = isolated_project["env"].copy()
    env.update({"PX_DRY": "1", "PX_TEXT": "hello", "PX_PERSONA": "gremlin",
                "PX_BYPASS_SUDO": "1", "_PX_VOICE_PERSONA_DONE": "1"})
    payload = parse_json(run_tool(["bin/tool-voice"], env))
    assert payload.get("route", "onboard") == "onboard"


def test_tool_voice_no_route_env_skips_nest(isolated_project):
    """PX_VOICE_NO_ROUTE=1 (voice loop, urgent mind callers) goes straight onboard."""
    _StubHandler.captured = []
    srv = _start_stub()
    base = f"http://{srv.server_address[0]}:{srv.server_address[1]}"
    env = isolated_project["env"].copy()
    env.update({"PX_DRY": "1", "PX_TEXT": "hello", "PX_PERSONA": "spark",
                "PX_BYPASS_SUDO": "1", "_PX_VOICE_PERSONA_DONE": "1",
                "PX_VOICE_NO_ROUTE": "1",
                "PX_ANNOUNCE_RELAY_URL": base, "PX_HA_HOST": base,
                "ANNOUNCE_RELAY_TOKEN": "t", "PX_HA_TOKEN": "t",
                "PX_NIGHT_SILENCE_START_H": "99", "PX_NIGHT_SILENCE_END_H": "0"})
    try:
        payload = parse_json(run_tool(["bin/tool-voice"], env))
    finally:
        srv.shutdown()
    assert payload.get("route", "onboard") == "onboard"
    assert not any("/announce" in p for (_, p, _) in _StubHandler.captured)


def test_tool_voice_dead_relay_at_night_stays_silent(isolated_project):
    """Route attempted + failed (dead relay) during night: the onboard fallback
    must NOT sidestep the night gate that lives inside tool-announce."""
    env = isolated_project["env"].copy()
    env.update({"PX_DRY": "0", "PX_TEXT": "hello there", "PX_PERSONA": "spark",
                "PX_BYPASS_SUDO": "1", "_PX_VOICE_PERSONA_DONE": "1",
                "PX_VOICE_PLAYER": "/bin/true",
                "PX_ANNOUNCE_RELAY_URL": "http://127.0.0.1:1",   # dead port
                "PX_HA_HOST": "http://127.0.0.1:1",
                "PX_NIGHT_SILENCE_START_H": "0", "PX_NIGHT_SILENCE_END_H": "24"})  # always night
    payload = parse_json(run_tool(["bin/tool-voice"], env))
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "night_silence"
```

Note on the dry-run test: routing must be attempted even under `PX_DRY=1`? No — keep it simple and safe: **when `PX_DRY=1`, skip the Nest route entirely** (route=onboard, dry payload as today). The first test uses `PX_DRY=0` against stubs; the fallback test uses `PX_DRY=1` only to keep espeak silent — so the fallback decision must happen *before* the dry check for the route attempt but the fallback path itself honors dry. Simplest implementation that satisfies both: attempt the route only when `PX_DRY=0`; under `PX_DRY=1` go straight onboard. Then the second test's dead-relay env is irrelevant — rewrite that test with `PX_DRY=0` and stub only HA (dead relay), asserting `route == "onboard"` and that espeak was *attempted* — but spawning espeak in CI is wrong. **Resolution:** under `PX_DRY=0` with a dead relay, tool-voice falls back to the espeak path; make the test override `PX_VOICE_PLAYER=/bin/true` (an accepted "player" that discards input) so the fallback is exercised without audio. Drop the `PX_DRY=1` variant. Write it this way.

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_tools.py -k tool_voice -q`; expected: new tests FAIL (`KeyError: 'route'`).

- [ ] **Step 3: Add the config constant**

```python
# spark_config.py, next to SPEAKER_STICKY_S
SPEAKER_ROUTE_TIMEOUT_S = 90   # tool-voice cap on one announce attempt (cold synth ~33s + cast + restore sleep); revisit after P3
```

- [ ] **Step 4: Implement in `bin/tool-voice`**

Insert after the persona-wrapper block, before `dry_mode = ...`:

```python
    from pxh import spark_config
    dry_mode = os.environ.get("PX_DRY", "0") != "0"

    def _night_now() -> bool:
        """Same env-overridable bounds as tool-announce (PX_NIGHT_SILENCE_*)."""
        from zoneinfo import ZoneInfo
        start = int(os.environ.get("PX_NIGHT_SILENCE_START_H",
                                   spark_config.NIGHT_SILENCE_START_H))
        end = int(os.environ.get("PX_NIGHT_SILENCE_END_H",
                                 spark_config.NIGHT_SILENCE_END_H))
        hour = __import__("datetime").datetime.now(ZoneInfo("Australia/Hobart")).hour
        return hour >= start or hour < end

    def _try_nest_route(text: str) -> dict | None:
        """Route SPARK speech to a Nest via tool-announce.

        Returns a payload dict when the route HANDLED the request (ok or
        suppressed — suppression must not fall through to onboard), or None
        to fall back to the onboard path. A failed attempt during night
        silence returns a suppressed payload instead of None: a dead relay
        must not sidestep the night gate (PX_VOICE_URGENT=1 bypasses).
        """
        if dry_mode or not spark_config.ANNOUNCE_ENABLED:
            return None
        if os.environ.get("PX_VOICE_NO_ROUTE") == "1":
            return None                    # voice loop / urgent callers: straight onboard
        if os.environ.get("PX_PERSONA", "") not in ("", "spark"):
            return None

        def _failed() -> dict | None:
            if _night_now() and os.environ.get("PX_VOICE_URGENT") != "1":
                return {"status": "suppressed", "route": "nest",
                        "reason": "night_silence", "text": text}
            return None                    # daytime/urgent → onboard fallback

        try:
            from pxh.speaker_router import resolve_speaker
            target = resolve_speaker()
            if not target:
                return _failed()
            env = os.environ.copy()
            env["PX_ANNOUNCE_TEXT"] = text
            env["PX_ANNOUNCE_TARGETS"] = target
            result = subprocess.run(
                [str(PROJECT_ROOT / "bin" / "tool-announce")], env=env,
                capture_output=True, text=True,
                timeout=spark_config.SPEAKER_ROUTE_TIMEOUT_S)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
        except Exception:
            return _failed()               # relay/HA breakage
        if payload.get("status") == "ok":
            return {"status": "ok", "route": "nest", "text": text,
                    "target": (payload.get("targets") or [None])[0]}
        if payload.get("status") == "suppressed":
            return {"status": "suppressed", "route": "nest",
                    "reason": payload.get("reason", "suppressed"), "text": text}
        return _failed()                   # error / dry / unknown

    routed = _try_nest_route(text)
    if routed is not None:
        log_event("voice", routed)
        if routed["status"] == "ok":
            update_session(fields={"last_action": "tool_voice"},
                           history_entry={"event": "voice", "text": text,
                                          "route": "nest", "dry": False})
        print(json.dumps(routed))
        return 0
```

Then add `"route": "onboard"` to the existing onboard `payload` dict (the one currently built as `{"status": "ok", "dry": dry_mode, "text": text}`).

Mind the existing structure: `dry_mode` is currently assigned later — move that single assignment up rather than assigning twice.

Check the exact names `NIGHT_SILENCE_START_H`/`NIGHT_SILENCE_END_H` in `spark_config.py` before using them in `_night_now()` — mirror however `bin/tool-announce` reads its night bounds.

- [ ] **Step 5: Wire the callers**

`src/pxh/voice_loop.py` — in `validate_action()`'s voice branch, inject `env["PX_VOICE_NO_ROUTE"] = "1"`: the human is at the robot (onboard is the right speaker) and the 30s watchdog would kill a 90s route attempt mid-cast.

`src/pxh/mind.py`:
- `_run_voice` (~line 3136): default `timeout` becomes `spark_config.SPEAKER_ROUTE_TIMEOUT_S + 15` — the greet/comment/weather path is where routing should actually happen, and the old 30s would kill it mid-synth.
- Battery warn (~1581), battery critical (~1554), reflection-offline (~3893): set `env["PX_VOICE_NO_ROUTE"] = "1"` and `env["PX_VOICE_URGENT"] = "1"` — alerts must not spend 90s casting, and must keep speaking onboard at night as today.

Tests (append to `tests/test_mind.py`): `_run_voice`'s effective timeout ≥ `SPEAKER_ROUTE_TIMEOUT_S`; the battery-warn env carries both flags (monkeypatch `subprocess.run`, inspect `env`).

- [ ] **Step 6: Run to verify pass** — `python -m pytest tests/test_tools.py -k "tool_voice or announce" -q && python -m pytest tests/test_mind.py -q`; expected: all pass. Then full suite: `python -m pytest -q -m "not live"`.

- [ ] **Step 7: Commit**

```bash
git add bin/tool-voice src/pxh/spark_config.py src/pxh/mind.py src/pxh/voice_loop.py tests/test_tools.py tests/test_mind.py
git commit -m "feat(voice): route SPARK speech to the nearest Nest, onboard fallback"
```

---

### Task 7: Deploy, live gates, CLAUDE.md

Manual/live steps — evidence before assertions for each.

- [ ] **Step 1: Push and deploy to the Pi**

```bash
git push
ssh pi@192.168.0.236 'cd /home/pi/picar-x-hacking && git pull && sudo systemctl restart px-mind px-api-server'
```

- [ ] **Step 2: Verify HA area names match `SPEAKER_ROOMS` keys**

On the Pi (token in `.env`): `GET /api/states` → check the areas the Nest satellites belong to (or via HA UI). The `SPEAKER_ROOMS` keys must equal the lowercased HA area names — fix the keys if the house says "study" instead of "office".

- [ ] **Step 3: Deploy the HA component**

```bash
scp ha/custom_components/spark_conversation/conversation.py pi@homeassistant.local:/homeassistant/custom_components/spark_conversation/
# restart HA Core from the HA UI (Settings → System → Restart) or:
ssh pi@homeassistant.local 'source /etc/profile.d/homeassistant.sh 2>/dev/null; ha core restart'
```

- [ ] **Step 4: P3 — measure warm synth latency**

```bash
ssh pi@192.168.0.236 'cd /home/pi/picar-x-hacking && time PX_ANNOUNCE_TEXT="warm latency check" bin/tool-announce; time PX_ANNOUNCE_TEXT="warm latency check" bin/tool-announce'
```

Second run should be near-instant (relay cache hit). Record both numbers in the spec's P3 section. If a warm *novel-text* synth is >10s, note it — conversational replies via voice loop will feel slow and `SPEAKER_ROUTE_TIMEOUT_S` may need tuning, but do not redesign here.

- [ ] **Step 5: End-to-end check**

Say something to the Office Mini ("Hey Google, ask SPARK …"), then within 30 min trigger SPARK speech (`PX_TEXT="routing test" PX_PERSONA=spark bin/tool-voice` on the Pi). It must play on the Office Mini, and `state/last_heard.json` must contain `{"room": "office", ...}`.

- [ ] **Step 6: Update CLAUDE.md** — in the Announce Pipeline section, add one short paragraph: tool-voice is now the routing chokepoint (SPARK persona only), router resolution order, `last_heard.json` source, suppression-never-falls-back rule.

- [ ] **Step 7: Commit + push**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-07-31-nest-speaker-routing-design.md
git commit -m "docs: nest routing live — P3 numbers, CLAUDE.md chokepoint notes"
git push
```
