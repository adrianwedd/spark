# Frigate Person Detection — SPARK Awareness Integration

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Feed Hailo/Frigate person-detection events into SPARK's awareness loop so `compute_obi_mode()` knows whether a person is actually present, and px-alive turns toward the detected person instead of blindly facing forward.

**Architecture:** Three changes to existing files — no new modules. px-mind polls `http://pi5-hailo.local:5000/api/events` in Layer 1 (awareness_tick), writes `state/frigate_presence.json`, and `compute_obi_mode()` weights the detection above sonar/ambient heuristics. px-alive reads the same file on proximity trigger to compute a directional pan angle.

**Tech Stack:** Python stdlib `urllib.request` (no new deps), Frigate 0.14+ event API, existing `atomic_write` / state-file pattern already used by sonar_live.json.

---

## Frigate API confirmed schema (picar_x camera)

```
GET http://pi5-hailo.local:5000/api/events?label=person&camera=picar_x&limit=10&after=<unix_float>
```

Each event (relevant fields):
```json
{
  "end_time": 1773314256.91,
  "data": {
    "box":   [x, y, w, h],
    "score": 0.82,
    "top_score": 0.80,
    "average_estimated_speed": 0.0,
    "velocity_angle": 0.0,
    "path_data": [[[x_center, y_center], unix_timestamp], ...]
  }
}
```

`after` filters on `end_time`. **`end_time` is `null` for in-progress events** — use `e.get("end_time") or 0` in `max()`. `box` is `[x, y, width, height]` normalized 0-1; x_center = `box[0] + box[2]/2`.

---

## File Map

| File | Change |
|------|--------|
| `bin/px-mind` | Add FRIGATE_* constants; add `_fetch_frigate_presence()`; call in `awareness_tick()`; update `compute_obi_mode()` |
| `bin/px-alive` | Add `_pan_from_frigate()`; read `frigate_presence.json` in proximity-react block |
| `state/frigate_presence.json` | New runtime state file (gitignored via state/*.json) |
| `tests/test_mind_utils.py` | Add tests for `_fetch_frigate_presence` and updated `compute_obi_mode` |
| `tests/test_alive_frigate.py` | New — tests for pan-angle calculation |
| `.env` | Add PX_FRIGATE_HOST |
| `CLAUDE.md` | Add env vars to table |

---

## Chunk 1: Frigate client + awareness_tick wiring + tests

### Task 1: Add Frigate constants and `_fetch_frigate_presence()` to px-mind

**Files:**
- Modify: `bin/px-mind` (Python heredoc) — add after the `AMBIENT_STALE_S` constant (~line 95)
- Test: `tests/test_mind_utils.py` — extend existing file

Context: `bin/px-mind` is a bash script wrapping a Python heredoc. Tests exec the Python block using `_load_mind_helpers()`. Follow the same pattern as `read_sonar()` for network-with-fallback.

- [ ] **Step 1: Write failing tests for `_fetch_frigate_presence`**

Add to `tests/test_mind_utils.py` (after existing imports):

```python
import json as _json
import time as _time
import urllib.error
from unittest.mock import MagicMock, patch


def _make_frigate_event(score=0.75, x=0.2, y=0.1, w=0.3, h=0.8,
                        speed=0.0, vel_angle=0.0, end_time=None):
    return {
        "end_time": end_time or _time.time() - 5,
        "data": {
            "box": [x, y, w, h],
            "score": score, "top_score": score,
            "average_estimated_speed": speed,
            "velocity_angle": vel_angle,
            "path_data": [[[x + w / 2, y + h / 2], _time.time() - 5]],
        },
    }


def _mock_urlopen(events):
    body = _json.dumps(events).encode()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=body)))
    cm.__exit__ = MagicMock(return_value=False)
    return cm


_fetch_frigate_presence = _MIND["_fetch_frigate_presence"]


def test_frigate_returns_presence_when_event_recent():
    events = [_make_frigate_event(score=0.80, x=0.2, w=0.3)]  # x_center = 0.35
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(events)):
        result = _fetch_frigate_presence(dry=False)
    assert result is not None
    assert result["person_present"] is True
    assert abs(result["x_center"] - 0.35) < 0.01
    assert result["score"] == pytest.approx(0.80, abs=0.01)


def test_frigate_returns_none_on_connection_error():
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        result = _fetch_frigate_presence(dry=False)
    assert result is None


def test_frigate_returns_none_on_timeout():
    import socket
    with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        result = _fetch_frigate_presence(dry=False)
    assert result is None


def test_frigate_empty_events_no_person():
    with patch("urllib.request.urlopen", return_value=_mock_urlopen([])):
        result = _fetch_frigate_presence(dry=False)
    assert result is not None
    assert result["person_present"] is False


def test_frigate_dry_run_skips_network():
    with patch("urllib.request.urlopen", side_effect=AssertionError("must not call")):
        result = _fetch_frigate_presence(dry=True)
    assert result is None


def test_frigate_filters_low_confidence():
    events = [_make_frigate_event(score=0.45)]
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(events)):
        result = _fetch_frigate_presence(dry=False)
    assert result["person_present"] is False


def test_frigate_reports_event_count():
    events = [_make_frigate_event(score=0.80), _make_frigate_event(score=0.75)]
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(events)):
        result = _fetch_frigate_presence(dry=False)
    assert result["event_count"] == 2
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_mind_utils.py -k "frigate" -v
```

Expected: errors — `_fetch_frigate_presence` not defined.

- [ ] **Step 3: Add constants and `_fetch_frigate_presence()` to `bin/px-mind`**

After the `AMBIENT_STALE_S` line (~line 95), add:

```python
FRIGATE_HOST      = os.environ.get("PX_FRIGATE_HOST", "http://pi5-hailo.local:5000")
FRIGATE_CAMERA    = os.environ.get("PX_FRIGATE_CAMERA", "picar_x")
FRIGATE_WINDOW_S  = 90      # look for events in the last 90 seconds
FRIGATE_MIN_SCORE = 0.60    # ignore detections below this confidence
FRIGATE_TIMEOUT_S = 2       # short timeout — must not stall the awareness loop
FRIGATE_FILE      = STATE_DIR / "frigate_presence.json"
```

After `read_sonar()`, add:

```python
def _fetch_frigate_presence(dry: bool = False) -> dict | None:
    """Query Frigate for recent person detections. Returns presence dict or None on any error.

    None means Frigate is unreachable — caller falls back to sonar/ambient heuristics.
    dry=True returns None immediately without any network call.
    """
    if dry:
        return None
    import socket
    import urllib.request

    since = time.time() - FRIGATE_WINDOW_S
    url = (
        f"{FRIGATE_HOST}/api/events"
        f"?label=person&camera={FRIGATE_CAMERA}"
        f"&limit=20&after={since:.3f}"
    )
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=FRIGATE_TIMEOUT_S) as resp:
            events = json.loads(resp.read())
    except Exception:
        return None  # network down, mDNS timeout, JSON parse error, etc.

    qualifying = [
        e for e in events
        if e.get("data", {}).get("score", 0) >= FRIGATE_MIN_SCORE
    ]

    if not qualifying:
        return {
            "person_present": False,
            "event_count": 0,
            "score": None,
            "x_center": None,
            "speed": None,
            "velocity_angle": None,
            "ts": utc_timestamp(),
        }

    # end_time is None for in-progress events — treat as 0 so max() doesn't TypeError
    best = max(qualifying, key=lambda e: e.get("end_time") or 0)
    box = best["data"].get("box") or []
    # box format confirmed as [x, y, width, height] (normalized 0-1) per user docs.
    # x_center = x + width/2. If Frigate ever switches to [xmin,ymin,xmax,ymax],
    # use (box[0]+box[2])/2 instead — verify against path_data[0][0][0].
    x_center = round(box[0] + box[2] / 2, 3) if len(box) == 4 else None

    return {
        "person_present": True,
        "event_count": len(qualifying),
        "score": round(best["data"].get("score", 0), 3),
        "x_center": x_center,
        "speed": best["data"].get("average_estimated_speed"),
        "velocity_angle": best["data"].get("velocity_angle"),
        "ts": utc_timestamp(),
    }
```

- [ ] **Step 4: Run tests — expect pass**

```bash
python -m pytest tests/test_mind_utils.py -k "frigate" -v
```

Expected: all 7 frigate tests PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/px-mind tests/test_mind_utils.py
git commit -m "feat(mind): add _fetch_frigate_presence - Frigate API client for person detection"
```

---

### Task 2: Wire Frigate into `awareness_tick` and update `compute_obi_mode`

**Files:**
- Modify: `bin/px-mind` — `awareness_tick()` and `compute_obi_mode()`
- Test: `tests/test_mind_utils.py`

- [ ] **Step 1: Write failing tests for updated `compute_obi_mode`**

Add to `tests/test_mind_utils.py`:

```python
# ---------------------------------------------------------------------------
# compute_obi_mode with Frigate data
# ---------------------------------------------------------------------------

def _fp_present(x_center=0.5, score=0.80, count=1):
    return {"person_present": True, "event_count": count,
            "score": score, "x_center": x_center, "speed": 0.0, "velocity_angle": 0.0}

def _fp_absent():
    return {"person_present": False, "event_count": 0,
            "score": None, "x_center": None, "speed": None, "velocity_angle": None}


def test_obi_mode_calm_from_frigate_without_close_sonar():
    """Frigate detects person → calm, even if sonar shows person far away."""
    awareness = {"ambient_sound": {"level": "quiet"}, "sonar_cm": 200,
                 "frigate": _fp_present()}
    assert compute_obi_mode(awareness, hour_override=10) == "calm"


def test_obi_mode_active_from_frigate_multiple_events():
    """Multiple Frigate detections in window → active."""
    awareness = {"ambient_sound": {"level": "quiet"}, "sonar_cm": 200,
                 "frigate": _fp_present(count=3)}
    assert compute_obi_mode(awareness, hour_override=10) == "active"


def test_obi_mode_not_absent_when_frigate_sees_person_at_night():
    """Frigate detects person at night → not absent."""
    awareness = {"ambient_sound": {"level": "silent"}, "sonar_cm": 90,
                 "frigate": _fp_present()}
    assert compute_obi_mode(awareness, hour_override=2) != "absent"


def test_obi_mode_absent_when_frigate_online_but_no_person_at_night():
    """Frigate is online, reports no person, nighttime → absent."""
    awareness = {"ambient_sound": {"level": "silent"}, "sonar_cm": 90,
                 "frigate": _fp_absent()}
    assert compute_obi_mode(awareness, hour_override=2) == "absent"


def test_obi_mode_sonar_fallback_when_frigate_offline():
    """No frigate key → original sonar/ambient logic unchanged."""
    awareness = {"ambient_sound": {"level": "quiet"}, "sonar_cm": 25}
    assert compute_obi_mode(awareness, hour_override=10) == "calm"
```

- [ ] **Step 2: Run — expect FAIL**

```bash
python -m pytest tests/test_mind_utils.py -k "frigate" -v
```

Expected: new `compute_obi_mode` tests fail.

- [ ] **Step 3: Update `awareness_tick` to call Frigate**

In `awareness_tick()`, right after `sonar_cm = read_sonar(dry)`:

```python
    frigate = _fetch_frigate_presence(dry)
```

In the awareness dict construction, add `"frigate": frigate` after `"sonar_cm"`.

After `atomic_write(AWARENESS_FILE, ...)`, write the Frigate state file:

```python
    if frigate is not None:
        atomic_write(FRIGATE_FILE, json.dumps(frigate, indent=2))
```

Update the log line to include Frigate status:

```python
    if frigate is None:
        frigate_str = "offline"
    elif frigate.get("person_present"):
        frigate_str = "person"
    else:
        frigate_str = "no-person"
    log(f"awareness: transitions={transitions} sonar={sonar_cm:.0f}cm frigate={frigate_str} period={time_period}")
```

- [ ] **Step 4: Update `compute_obi_mode`**

Replace the function body:

```python
def compute_obi_mode(awareness: dict, hour_override: int | None = None) -> str:
    """Infer Obi's state from Frigate detections + ambient sound + sonar + time."""
    hour = hour_override if hour_override is not None else dt.datetime.now(AEDT).hour
    ambient_level = (awareness.get("ambient_sound") or {}).get("level", "unknown")
    sonar_cm = awareness.get("sonar_cm")
    frigate = awareness.get("frigate") or {}
    is_day = OBI_DAY_START <= hour < OBI_DAY_END

    frigate_present = frigate.get("person_present", False)
    frigate_count   = frigate.get("event_count", 0)

    close      = sonar_cm is not None and sonar_cm < 35
    very_close = sonar_cm is not None and sonar_cm < 20

    # Fast sonar signal: physically very close + loud
    if very_close and ambient_level == "loud":
        return "possibly-overloaded"

    # Frigate is authoritative when present
    if frigate_present:
        if frigate_count >= 3 or ambient_level == "loud":
            return "active"
        return "calm"

    # Night + quiet + Frigate online but no detection → absent
    if not is_day and ambient_level in ("silent", "quiet"):
        if "frigate" in awareness:   # Frigate replied: no one there
            return "absent"
        if not close:                # Frigate offline, trust sonar
            return "absent"

    # Sonar/ambient fallback (Frigate offline or daytime no-detection)
    if ambient_level == "unknown":
        return "unknown"
    if close and is_day:
        return "active" if ambient_level == "loud" else "calm"
    return "calm"
```

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/test_mind_utils.py -v
```

Expected: all tests PASS (original 5 + new 5 + 7 frigate client tests).

- [ ] **Step 6: Commit**

```bash
git add bin/px-mind tests/test_mind_utils.py
git commit -m "feat(mind): wire Frigate detection into awareness_tick and compute_obi_mode"
```

---

## Chunk 2: px-alive directional gaze

### Task 3: px-alive turns toward detected person

**Files:**
- Modify: `bin/px-alive`
- Create: `tests/test_alive_frigate.py`

Context: `bin/px-alive` follows the same bash-wrapping-Python-heredoc pattern as px-mind. The proximity-react block is at ~line 313. Currently eases to `(pan=0, tilt=5)`. We extract pan-angle logic into a pure function `_pan_from_frigate()` so it's testable. Camera FOV ~80° horizontal. PiCarX pan convention: **positive = left, negative = right** (confirmed from face-tracking code: `new_pan = cur_pan - int(err_x * gain)` where positive err_x = face right of centre). Formula: `clamp(int((0.5 - x_center) * 80), -40, 40)`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_alive_frigate.py`:

```python
"""Tests for px-alive directional gaze toward Frigate-detected person."""
from __future__ import annotations
import os, sys, types
from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).parent.parent


def _load_alive_helpers():
    src = (PROJECT_ROOT / "bin" / "px-alive").read_text()
    start = src.index("<<'PY'\n") + len("<<'PY'\n")
    end   = src.rindex("\nPY\n")
    py_src = src[start:end]

    stub_keys = ("pxh", "pxh.logging", "pxh.time",
                 "picarx", "robot_hat", "vilib")
    saved = {k: sys.modules.get(k) for k in stub_keys + ("pxh.state",)}
    for k in stub_keys:
        sys.modules[k] = types.ModuleType(k)
    stubs_state = types.ModuleType("pxh.state")
    stubs_state.load_session = lambda: {}
    sys.modules["pxh.state"] = stubs_state  # explicit, not overwritten by loop

    env_patch = {"PROJECT_ROOT": str(PROJECT_ROOT),
                 "LOG_DIR": str(PROJECT_ROOT / "logs"),
                 "PX_STATE_DIR": str(PROJECT_ROOT / "state")}
    old_env = {k: os.environ.get(k) for k in env_patch}
    for k, v in env_patch.items():
        os.environ[k] = v

    globs: dict = {"__file__": str(PROJECT_ROOT / "bin" / "px-alive")}
    try:
        exec(compile(py_src, "bin/px-alive", "exec"), globs)  # noqa: S102
    finally:
        for k, old_mod in saved.items():
            sys.modules.pop(k, None) if old_mod is None else sys.modules.update({k: old_mod})
        for k, old_v in old_env.items():
            os.environ.pop(k, None) if old_v is None else os.environ.update({k: old_v})
    return globs


_ALIVE = _load_alive_helpers()
_pan_from_frigate = _ALIVE["_pan_from_frigate"]


def test_pan_center():
    assert _pan_from_frigate({"person_present": True, "x_center": 0.5}) == 0

def test_pan_right():
    # Person right of frame (x=0.8) → negative pan (picarx: positive=left, negative=right)
    assert _pan_from_frigate({"person_present": True, "x_center": 0.8}) < 0

def test_pan_left():
    # Person left of frame (x=0.2) → positive pan
    assert _pan_from_frigate({"person_present": True, "x_center": 0.2}) > 0

def test_pan_clamped_max():
    # Extreme left (x=0.0) → clamped to +40 max
    assert _pan_from_frigate({"person_present": True, "x_center": 0.0}) <= 40

def test_pan_clamped_min():
    # Extreme right (x=1.0) → clamped to -40 min
    assert _pan_from_frigate({"person_present": True, "x_center": 1.0}) >= -40

def test_pan_no_detection():
    assert _pan_from_frigate({"person_present": False, "x_center": None}) == 0

def test_pan_none_input():
    assert _pan_from_frigate(None) == 0
```

- [ ] **Step 2: Run — expect FAIL**

```bash
python -m pytest tests/test_alive_frigate.py -v
```

Expected: errors — `_pan_from_frigate` not in px-alive.

- [ ] **Step 3: Add `FRIGATE_FILE` and `_pan_from_frigate()` to `bin/px-alive`**

After the `SONAR_LIVE_FILE` constant:

```python
FRIGATE_FILE = STATE_DIR / "frigate_presence.json"
```

Add before the `ease()` function:

```python
def _pan_from_frigate(presence: dict | None) -> int:
    """Pan angle (degrees) toward Frigate-detected person. Returns 0 if no detection.
    x_center 0=left 1=right, 0.5=centre. Clamped to +-40 degrees.
    """
    if not presence or not presence.get("person_present"):
        return 0
    x = presence.get("x_center")
    if x is None:
        return 0
    return max(-40, min(40, int((0.5 - x) * 80)))  # positive=left, negative=right (picarx convention)
```

- [ ] **Step 4: Update proximity-react to use directional pan**

Replace in the proximity-react block (~line 314):

```python
                            log(f"proximity react: object at {dist:.1f}cm — facing forward")
                            ease(get_px(), cur_pan, cur_tilt, 0, 5, 0.5)
                            cur_pan, cur_tilt = 0, 5
```

With:

```python
                            try:
                                _fp = json.loads(FRIGATE_FILE.read_text())
                            except Exception:
                                _fp = None
                            target_pan = _pan_from_frigate(_fp)
                            log(f"proximity react: {dist:.1f}cm — facing pan={target_pan}")
                            ease(get_px(), cur_pan, cur_tilt, target_pan, 5, 0.5)
                            cur_pan, cur_tilt = target_pan, 5
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_alive_frigate.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 6: Full suite — confirm no regressions**

```bash
python -m pytest -m "not live" -v
```

Expected: all non-live tests pass.

- [ ] **Step 7: Add env vars to `.env` and `CLAUDE.md`**

In `.env`, add:
```
PX_FRIGATE_HOST=http://pi5-hailo.local:5000
```

In `CLAUDE.md` Key Environment Variables table, add:
```
| `PX_FRIGATE_HOST`   | Frigate API base URL (default: `http://pi5-hailo.local:5000`) |
| `PX_FRIGATE_CAMERA` | Frigate camera name (default: `picar_x`) |
```

- [ ] **Step 8: Commit**

```bash
git add bin/px-alive tests/test_alive_frigate.py .env CLAUDE.md
git commit -m "feat(alive): directional gaze toward Frigate-detected person on proximity trigger"
```

---

## Manual smoke test

After all tasks pass:

```bash
# Confirm Frigate reachable and returns events
curl -s "http://pi5-hailo.local:5000/api/events?label=person&camera=picar_x&limit=1" | python3 -m json.tool

# Restart px-mind, wait one cycle, check state
sudo systemctl restart px-mind && sleep 70
cat state/frigate_presence.json
tail -5 logs/px-mind.log | grep frigate

# Walk past camera — check awareness log shows "frigate=person"
# then check px-alive log shows non-zero pan on next proximity trigger
```
