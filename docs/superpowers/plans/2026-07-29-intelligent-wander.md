# Intelligent Wander (Option B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn px-wander's explore mode into an LLM-guided explorer with a fail-closed cliff guard, honest chassis-fixed sonar sensing, and observations that accumulate into SPARK's memory.

**Architecture:** Extract the 870-line bash-heredoc into an importable `src/pxh/wander.py` (thin `bin/px-wander` launcher), then layer: (1) reflex safety — grayscale cliff guard calibrated per-floor, fail closed; (2) probe-turn scanning replacing the bogus camera-pan sonar sweep; (3) periodic Ollama-on-M5 directives with reactive fallback; (4) post-wander narrative synthesis flowing back through mind.py. Spec: `docs/superpowers/specs/2026-07-29-wander-intelligence-design.md`.

**Tech Stack:** Python 3.11 (system `/usr/bin/python3` at runtime, venv for tests), pytest + `isolated_project` fixture, Ollama HTTP (`/api/generate`), picarx/robot_hat (system site-packages), filelock.

## Global Constraints

- `bin/` scripts run under `/usr/bin/python3`, NOT the venv; `px-env` puts `$PROJECT_ROOT/src` on PYTHONPATH so `pxh.*` imports work there.
- **sudo strips env vars**: tool-wander invokes `sudo -n env PYTHONPATH=… bin/px-wander`; anything crossing that hop MUST be a CLI argument (steps/mode/duration already are; intent must be too).
- `pxh.wander` must be importable without picarx installed and without hardware: no module-level `from picarx import …`; all functions take a `px` handle parameter (race.py pattern). `filelock` import stays guarded by try/except ImportError.
- `PX_DRY=1` skips motion/audio; **default is live when unset**. Every new behavior needs a dry path.
- Tests use the `isolated_project` fixture (`tests/conftest.py:13`); subprocess tests pass `isolated_project["env"]`.
- `get_cliff_status(gm_val_list)` returns True when ANY reading ≤ `cliff_reference[i]` (verified `/home/pi/picar-x/picarx/picarx.py:240` this session; **re-read that file before coding against it** — same caution as the `bpe_model` gotcha).
- Never touch: exploring.json/px-alive handshake, SIGTERM cleanup + I2C fallback, night silence, existing abort gates.
- New tunables live in `src/pxh/spark_config.py` (self-evolution whitelist target).
- Commit after every green task; run `python -m pytest -m "not live"` before each commit; after any test run that could touch Bluesky auth, `sudo systemctl restart px-post`.

## Constants introduced (single source: `wander.py` unless noted)

| Name | Value | Meaning |
|---|---|---|
| `CLIFF_MARGIN` | `0.65` | cliff_ref = floor_ref × margin |
| `CALIBRATION_STALE_S` | `30*24*3600` | staleness → warning only, still valid |
| `REVERSE_S` / `REVERSE_SPEED` | `0.3` / `20` | blind-reverse hard bound |
| `REVERSE_STALL_CM` | `2.0` | forward clearance must grow ≥ this or reverse counts as stall |
| `PROBE_S` | `0.4` | guarded creep per probe |
| `EDGE_ABORT_COUNT` | `2` | edge events → abort wander |
| `OBS_CAP` / `NAV_CAP` | `1000` / `100` | observations.jsonl / exploration.jsonl caps |
| `WANDER_DIRECTIVE_INTERVAL_S` (spark_config) | `20` | directive window cadence |
| `WANDER_DIRECTIVE_TIMEOUT_S` (spark_config) | `5` | LLM call timeout, wheels stopped |

---

## Milestone 1 — Extraction, cliff safety, honest sensing

### Task 1: Extract wander core to `src/pxh/wander.py`

Behavior-preserving move so everything after this is unit-testable.

**Files:**
- Create: `src/pxh/wander.py`
- Modify: `bin/px-wander` (heredoc body → 4-line shim)
- Test: `tests/test_wander.py` (new)

**Interfaces:**
- Produces: `pxh.wander.main(argv: list[str]) -> int` (unchanged CLI); every existing module-level function/constant from the heredoc, now importable.

- [ ] **Step 1: Write the failing import test**

```python
# tests/test_wander.py
"""Unit tests for pxh.wander (extracted from bin/px-wander heredoc)."""
import json
from pxh import wander


def test_wander_module_importable_without_picarx():
    assert callable(wander.main)


def test_best_direction_ignores_none():
    assert wander.best_direction({0: None, 25: 40.0, -25: 10.0}) == (25, 40.0)
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_wander.py -v` → FAIL `ModuleNotFoundError: pxh.wander`

- [ ] **Step 3: Move the heredoc** — copy the Python body of `bin/px-wander` (lines 11–872, everything between `<<'PY'` and `PY`) verbatim into `src/pxh/wander.py`. Only changes:
  - drop the local `clamp` and use `from pxh.utils import clamp`
  - keep the module-level env-var reads (`LOG_DIR`, `LOG_FILE`, `STATE_DIR`, …) exactly as-is — tests monkeypatch the module attributes
  - keep `from picarx import Picarx` inside `main()` (it already is)

  Replace `bin/px-wander`'s heredoc with:

```bash
/usr/bin/python3 - "$@" <<'PY'
import sys
from pxh.wander import main
raise SystemExit(main(sys.argv[1:]))
PY
```

  (Keep the `yield_alive` line and `px-env` sourcing above it untouched.)

- [ ] **Step 4: Verify** — `python -m pytest tests/test_wander.py tests/test_tools.py -k "wander" -v` → all PASS (existing dry-run subprocess tests prove the shim works end-to-end).

- [ ] **Step 5: Commit** — `git add -A && git commit -m "refactor: extract px-wander heredoc into importable pxh.wander"`

---

### Task 2: Cliff calibration (persist, load, staleness warning, CLI)

**Files:**
- Modify: `src/pxh/wander.py`
- Test: `tests/test_wander.py`

**Interfaces:**
- Consumes: `pxh.race.safe_grayscale(px, retries=1) -> list[float] | None`
- Produces:
  - `calibrate_cliff(px, state_dir: Path) -> dict` — reads floor, writes `state_dir/"wander_calibration.json"` atomically, returns the dict; raises `RuntimeError` on grayscale read failure.
  - `load_cliff_calibration(state_dir: Path) -> dict | None` — None if missing/corrupt/invalid; logs a warning (does NOT refuse) when older than `CALIBRATION_STALE_S`.
  - File format: `{"floor_ref": [l,c,r], "cliff_ref": [l*0.65, c*0.65, r*0.65], "ts": "<UTC ISO>"}`
  - CLI: `--calibrate-cliff` on `main()` (runs calibration, prints the JSON, exits 0/1; ignores other mode flags).

- [ ] **Step 1: Write failing tests**

```python
import datetime as dt

class FakePx:
    """Minimal picarx stand-in. Scripted grayscale; records calls."""
    def __init__(self, grayscale=None):
        self._gs = list(grayscale or [])
        self.cliff_reference = [500.0, 500.0, 500.0]
        self.calls = []
    def get_grayscale_data(self):
        if not self._gs:
            raise OSError("I2C read failed")
        v = self._gs.pop(0)
        if v is None:
            raise OSError("I2C read failed")
        return list(v)
    def set_cliff_reference(self, value):
        self.cliff_reference = list(value)
    def get_cliff_status(self, gm):  # mirrors picarx.py:240 semantics
        return any(gm[i] <= self.cliff_reference[i] for i in range(3))
    def stop(self): self.calls.append("stop")
    def forward(self, s): self.calls.append(("forward", s))
    def backward(self, s): self.calls.append(("backward", s))
    def set_dir_servo_angle(self, a): self.calls.append(("dir", a))
    def set_cam_pan_angle(self, a): self.calls.append(("pan", a))
    def get_distance(self): return 100.0


def test_calibrate_cliff_writes_reference(tmp_path):
    px = FakePx(grayscale=[[1000.0, 1100.0, 900.0]])
    cal = wander.calibrate_cliff(px, tmp_path)
    assert cal["floor_ref"] == [1000.0, 1100.0, 900.0]
    assert cal["cliff_ref"] == [650.0, 715.0, 585.0]
    on_disk = json.loads((tmp_path / "wander_calibration.json").read_text())
    assert on_disk == cal


def test_calibrate_cliff_read_failure_raises(tmp_path):
    import pytest
    with pytest.raises(RuntimeError):
        wander.calibrate_cliff(FakePx(grayscale=[None, None]), tmp_path)


def test_load_calibration_missing_or_corrupt_is_none(tmp_path):
    assert wander.load_cliff_calibration(tmp_path) is None
    (tmp_path / "wander_calibration.json").write_text("{nope")
    assert wander.load_cliff_calibration(tmp_path) is None


def test_load_calibration_stale_warns_but_loads(tmp_path, monkeypatch):
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)).isoformat()
    (tmp_path / "wander_calibration.json").write_text(json.dumps(
        {"floor_ref": [1000, 1000, 1000], "cliff_ref": [650, 650, 650], "ts": old}))
    warnings = []
    monkeypatch.setattr(wander, "log", lambda m: warnings.append(m))
    cal = wander.load_cliff_calibration(tmp_path)
    assert cal is not None
    assert any("stale" in w.lower() for w in warnings)
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_wander.py -k calibrat -v` → FAIL (AttributeError)

- [ ] **Step 3: Implement in wander.py**

```python
from pxh.race import safe_grayscale

CLIFF_MARGIN = 0.65
CALIBRATION_STALE_S = 30 * 24 * 3600

def calibrate_cliff(px, state_dir: Path) -> dict:
    gs = safe_grayscale(px, retries=2)
    if gs is None:
        raise RuntimeError("grayscale read failed — cannot calibrate")
    cal = {
        "floor_ref": [float(v) for v in gs],
        "cliff_ref": [round(float(v) * CLIFF_MARGIN, 1) for v in gs],
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(state_dir), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(cal, f, indent=2)
    os.chmod(tmp, 0o644)
    os.replace(tmp, str(state_dir / "wander_calibration.json"))
    log(f"cliff calibration saved: floor={cal['floor_ref']} cliff={cal['cliff_ref']}")
    return cal

def load_cliff_calibration(state_dir: Path) -> dict | None:
    path = state_dir / "wander_calibration.json"
    try:
        cal = json.loads(path.read_text(encoding="utf-8"))
        ref = cal["cliff_ref"]
        if not (isinstance(ref, list) and len(ref) == 3):
            return None
        age = (dt.datetime.now(dt.timezone.utc)
               - dt.datetime.fromisoformat(cal["ts"])).total_seconds()
        if age > CALIBRATION_STALE_S:
            log(f"cliff calibration is stale ({age/86400:.0f} days old) — "
                "consider re-running --calibrate-cliff on the current floor")
        return cal
    except Exception:
        return None
```

  In `main()`: add `parser.add_argument("--calibrate-cliff", action="store_true")`; when set (and not dry), construct Picarx, call `calibrate_cliff(px, STATE_DIR)` inside try/except printing `{"status":"ok",...cal}` or `{"status":"error","error":...}`, return before mode dispatch. Dry mode prints `{"status":"ok","dry":true}` without hardware.

- [ ] **Step 4: Verify** — `python -m pytest tests/test_wander.py -v` → PASS
- [ ] **Step 5: Commit** — `git commit -am "feat(wander): per-floor cliff calibration, fail-closed load, staleness warning"`

---

### Task 3: Cliff guard primitives (fail closed)

**Files:**
- Modify: `src/pxh/wander.py`
- Test: `tests/test_wander.py`

**Interfaces:**
- Produces:
  - `class CliffGuard:` — `__init__(self, cliff_ref: list[float])`, attr `edge_events: int`
  - `CliffGuard.check(px) -> str` — `"clear" | "cliff" | "fail"`; `"fail"` (grayscale unreadable) is handled identically to `"cliff"` by callers (fail closed)
  - `guarded_forward(px, guard, speed: int, duration_s: float, slice_s: float = 0.15) -> str` — `"ok" | "edge"`; on cliff/fail: stop, `bounded_reverse`, increment `guard.edge_events`
  - `bounded_reverse(px) -> bool` — ≤`REVERSE_S` at `REVERSE_SPEED`; returns True if stalled (forward sonar clearance grew < `REVERSE_STALL_CM`)

- [ ] **Step 1: Write failing tests**

```python
def _guard():
    return wander.CliffGuard([650.0, 650.0, 650.0])

def test_cliff_guard_detects_drop():
    px = FakePx(grayscale=[[1000, 640, 1000]])   # center ≤ ref → cliff
    assert _guard().check(px) == "cliff"

def test_cliff_guard_clear():
    px = FakePx(grayscale=[[1000, 1000, 1000]])
    assert _guard().check(px) == "clear"

def test_cliff_guard_read_failure_is_fail_closed():
    px = FakePx(grayscale=[None, None, None])    # retries exhausted
    assert _guard().check(px) == "fail"

def test_guarded_forward_stops_and_reverses_on_cliff(monkeypatch):
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[1000]*3, [600]*3] + [[1000]*3]*5)
    px._dist = iter([50.0, 60.0])                # before/after reverse: moved
    px.get_distance = lambda: next(px._dist, 60.0)
    guard = _guard()
    r = wander.guarded_forward(px, guard, speed=30, duration_s=0.5)
    assert r == "edge"
    assert guard.edge_events == 1
    assert "stop" in px.calls
    assert ("backward", wander.REVERSE_SPEED) in px.calls

def test_bounded_reverse_stall_detection(monkeypatch):
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[1000]*3]*5)
    px._dist = iter([50.0, 50.5])                # clearance didn't grow → stall
    px.get_distance = lambda: next(px._dist, 50.5)
    assert wander.bounded_reverse(px) is True
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_wander.py -k "cliff or reverse" -v` → FAIL

- [ ] **Step 3: Implement**

```python
REVERSE_S = 0.3
REVERSE_SPEED = 20
REVERSE_STALL_CM = 2.0
EDGE_ABORT_COUNT = 2

class CliffGuard:
    def __init__(self, cliff_ref: list[float]):
        self.cliff_ref = [float(v) for v in cliff_ref]
        self.edge_events = 0

    def check(self, px) -> str:
        gs = safe_grayscale(px, retries=1)
        if gs is None:
            log("cliff guard: grayscale read failed — treating as cliff (fail closed)")
            return "fail"
        if any(gs[i] <= self.cliff_ref[i] for i in range(3)):
            return "cliff"
        return "clear"

def bounded_reverse(px) -> bool:
    before = _read_sonar(px)
    px.backward(REVERSE_SPEED)
    time.sleep(REVERSE_S)
    px.stop()
    after = _read_sonar(px)
    if before is not None and after is not None:
        if (after - before) < REVERSE_STALL_CM:
            log(f"reverse stall: clearance {before:.0f}→{after:.0f}cm — edge-event equivalent")
            return True
    return False

def guarded_forward(px, guard: CliffGuard, speed: int, duration_s: float,
                    slice_s: float = 0.15) -> str:
    remaining = duration_s
    while remaining > 0:
        status = guard.check(px)
        if status != "clear":
            px.stop()
            log(f"cliff guard tripped ({status}) — stop + bounded reverse")
            if bounded_reverse(px):
                guard.edge_events += 1   # stall during escape counts too
            guard.edge_events += 1
            return "edge"
        px.forward(speed)
        step = min(slice_s, remaining)
        time.sleep(step)
        remaining -= step
    px.stop()
    return "ok"
```

  Note: guard is checked before the FIRST slice too — a wander that starts at the desk edge never moves.

- [ ] **Step 4: Verify** — `python -m pytest tests/test_wander.py -v` → PASS
- [ ] **Step 5: Commit** — `git commit -am "feat(wander): fail-closed cliff guard, sliced guarded_forward, bounded stall-detecting reverse"`

---

### Task 4: Probe-turn scanning; delete camera-pan sweep and fake compass

**Files:**
- Modify: `src/pxh/wander.py`
- Test: `tests/test_wander.py`

**Interfaces:**
- Produces: `probe_turn(px, guard, prefer: str = "left") -> tuple[str, float]` — returns `("left"|"right", clearance_cm)` on success, `("blocked", best_cm)` if both sides < `OBSTACLE_CM`, `("edge", 0.0)` if the guard/stall tripped.
- Deletes: `sweep_distances`, `read_dist`, `_sweep_sonar`, `best_direction`, `_heading_label`, `turn_accumulator` usage, `TURN_K`, `SWEEP_ANGLES`. Single surviving sonar reader: `_read_sonar(px)`.
- Nav entry schema (both modes, dry and live) becomes: `{"ts", "type": "nav", "explore_id", "action", "sonar_cm": float|None, "steps_from_start", "frigate_labels"}` — no `heading_estimate`, no `sonar_readings` dict.

- [ ] **Step 1: Write failing tests**

```python
def test_probe_turn_picks_clearer_side(monkeypatch):
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[1000]*3]*20)
    # left probe reads 20cm (blocked), arc-back ok, right probe reads 90cm → commit right
    px._dist = iter([20.0, 40.0, 55.0, 90.0])
    px.get_distance = lambda: next(px._dist, 90.0)
    side, clearance = wander.probe_turn(px, _guard(), prefer="left")
    assert side == "right" and clearance == 90.0
    assert ("dir", 30) in px.calls and ("dir", -30) in px.calls

def test_probe_turn_edge_aborts_probe(monkeypatch):
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[600]*3] * 5)     # cliff on first probe creep
    px._dist = iter([50.0, 55.0])
    px.get_distance = lambda: next(px._dist, 55.0)
    guard = _guard()
    side, _ = wander.probe_turn(px, guard, prefer="left")
    assert side == "edge"
    assert guard.edge_events >= 1

def test_sweep_helpers_are_gone():
    for name in ("sweep_distances", "_sweep_sonar", "read_dist", "_heading_label"):
        assert not hasattr(wander, name)
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_wander.py -k "probe or sweep_helpers" -v` → FAIL

- [ ] **Step 3: Implement**

```python
PROBE_S = 0.4

def probe_turn(px, guard: CliffGuard, prefer: str = "left") -> tuple[str, float]:
    """Physically probe ±30° for the clearer path. Sonar is chassis-fixed, so
    the only honest way to measure a direction is to point the chassis at it."""
    order = ["left", "right"] if prefer == "left" else ["right", "left"]
    best = ("blocked", 0.0)
    for i, side in enumerate(order):
        steer = -30 if side == "left" else 30
        px.set_dir_servo_angle(steer)
        result = guarded_forward(px, guard, TURN_SPEED, PROBE_S)
        px.set_dir_servo_angle(0)
        if result != "ok":
            return ("edge", 0.0)
        d = _read_sonar(px) or 0.0
        log(f"probe {side}: {d:.0f}cm")
        if d >= CLEAR_CM:
            return (side, d)          # good enough — commit without probing the other side
        if d > best[1]:
            best = (side, d)
        if i == 0:                     # arc back before trying the other side
            px.set_dir_servo_angle(-steer)
            stalled = bounded_reverse(px)
            px.set_dir_servo_angle(0)
            if stalled:
                guard.edge_events += 1
                return ("edge", 0.0)
    if best[1] < OBSTACLE_CM:
        return ("blocked", best[1])
    return best
```

  Then delete `sweep_distances`, `read_dist`, `_sweep_sonar`, `best_direction`, `_heading_label`, `SWEEP_ANGLES`, `TURN_K`, and every `turn_accumulator` reference. `_extract_landmark`, `speak`, meta/observation writers stay. (The explore/avoid loops still reference deleted names after this step — that's Task 5; run only the wander unit tests here, not the subprocess tests.)

- [ ] **Step 4: Verify** — `python -m pytest tests/test_wander.py -k "probe or sweep_helpers or cliff or calibrat" -v` → PASS
- [ ] **Step 5: Commit** — `git commit -am "feat(wander): probe-turn scanning; remove camera-pan sweep and fake compass"`

---

### Task 5: Rewire both wander loops onto guard + probe

**Files:**
- Modify: `src/pxh/wander.py` (`main()` avoid + explore loops)
- Test: `tests/test_wander.py`, existing `tests/test_tools.py` wander tests

**Interfaces:**
- Consumes: Tasks 2–4 primitives.
- Produces: `run_explore_step(px, guard, state) -> dict` — one navigation step, returns the nav entry; `state` is a plain dict carrying `forced_turn: str|None`, `stuck_count: int`, `sensor_fail_streak: int`, `steps_completed: int`, `explore_id: str`. Both loops (avoid + explore) call the same navigation core; the old duplicated navigate blocks are deleted.
- Explore live path: `main()` refuses live explore without calibration → `{"status":"blocked","reason":"cliff guard not calibrated — run px-wander --calibrate-cliff"}`, rc 2. Dry explore runs without calibration (no motion to guard). `CliffGuard` is constructed from `load_cliff_calibration(STATE_DIR)["cliff_ref"]` and `px.set_cliff_reference(cal["cliff_ref"])` is also applied.
- Abort additions to `_check_abort`: `guard.edge_events >= EDGE_ABORT_COUNT` → `"edge events"`; `sensor_fail_streak >= 3` → `"sonar sensor failure"`.

- [ ] **Step 1: Write failing tests**

```python
def test_explore_step_forward_when_clear(monkeypatch):
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[1000]*3]*10)
    px.get_distance = lambda: 120.0
    state = {"forced_turn": None, "stuck_count": 0, "sensor_fail_streak": 0,
             "steps_completed": 1, "explore_id": "e-test"}
    entry = wander.run_explore_step(px, _guard(), state)
    assert entry["action"] == "forward"
    assert entry["sonar_cm"] == 120.0
    assert "heading_estimate" not in entry

def test_explore_step_probes_when_blocked(monkeypatch):
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[1000]*3]*20)
    px._dist = iter([15.0, 80.0])            # blocked ahead; left probe clear
    px.get_distance = lambda: next(px._dist, 80.0)
    state = {"forced_turn": None, "stuck_count": 0, "sensor_fail_streak": 0,
             "steps_completed": 1, "explore_id": "e-test"}
    entry = wander.run_explore_step(px, _guard(), state)
    assert entry["action"] == "turned_left"
    assert state["stuck_count"] == 0

def test_explore_live_requires_calibration(isolated_project):
    """Live explore (bypass-sudo, no calibration file) is blocked, rc 2."""
    from pxh.state import default_state
    state = default_state()
    state["confirm_motion_allowed"] = True
    state["roaming_allowed"] = True
    isolated_project["session_path"].write_text(json.dumps(state))
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "0"
    import subprocess
    r = subprocess.run(["bin/px-wander", "--mode", "explore", "--duration", "30"],
                       capture_output=True, text=True, env=env, cwd=str(wander.PROJECT_ROOT))
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload["status"] == "blocked"
    assert "calibrat" in payload["reason"]
```

  (The third test never reaches Picarx construction: the calibration check runs before the import, so it's safe on a dev box too — order the check accordingly.)

- [ ] **Step 2: Run to verify failure** — FAIL (`run_explore_step` missing; live run reaches picarx import error instead of blocked)

- [ ] **Step 3: Implement.** In `main()` explore branch, before Picarx construction and before `_write_exploring_state`:

```python
cal = load_cliff_calibration(STATE_DIR)
if not dry and cal is None:
    print(json.dumps({"status": "blocked",
        "reason": "cliff guard not calibrated — run px-wander --calibrate-cliff"}))
    return 2
```

  After Picarx construction (live): `px.set_cliff_reference(cal["cliff_ref"])`; `guard = CliffGuard(cal["cliff_ref"])` (dry: `guard = CliffGuard([0, 0, 0])` — never consulted because dry never moves). Navigation core:

```python
def run_explore_step(px, guard: CliffGuard, state: dict) -> dict:
    d = _read_sonar(px)
    if d is None:
        state["sensor_fail_streak"] += 1
        action = "sensor_fail"
    else:
        state["sensor_fail_streak"] = 0
        if d < OBSTACLE_CM or state["forced_turn"]:
            prefer = state["forced_turn"] or "left"
            state["forced_turn"] = None
            side, clearance = probe_turn(px, guard, prefer=prefer)
            if side == "blocked":
                state["stuck_count"] += 1
                bounded_reverse(px)
                action = "blocked"
            elif side == "edge":
                action = "edge_event"
            else:
                state["stuck_count"] = 0
                action = f"turned_{side}"
        else:
            result = guarded_forward(px, guard, FORWARD_SPEED, FORWARD_S)
            action = "forward" if result == "ok" else "edge_event"
    return {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "type": "nav",
        "explore_id": state["explore_id"],
        "action": action,
        "sonar_cm": d,
        "steps_from_start": state["steps_completed"],
        "frigate_labels": [],
    }
```

  Explore loop body replaces the old sweep/navigate block with: abort check (now also `edge events` / `sensor_fail_streak` reasons) → `entry = run_explore_step(...)` → frigate query fills `entry["frigate_labels"]` → nav buffer → curiosity block (drop `current_heading`/`last_photo_heading`; the heading-based photo triggers are replaced in Task 10's window logic — until then keep only the `new label` and `object < 100cm` triggers, using `entry["sonar_cm"]`). Dry explore emits the new schema (`"sonar_cm": 200.0`). Avoid mode: replace its sweep/turn block with the same `run_explore_step` navigation core (guard constructed the same way; avoid mode ALSO refuses live without calibration — same fail-closed rule, same message). Observation entries lose `heading_estimate`.

- [ ] **Step 4: Verify** — `python -m pytest tests/test_wander.py tests/test_tools.py -k "wander" -v` → PASS (update any test_tools assertions that referenced the old schema/heading; the roaming-gate and listening-abort tests must still pass unchanged).
- [ ] **Step 5: Full dry suite** — `python -m pytest -m "not live" -q` → no new failures (known pre-existing: test_memory dedup).
- [ ] **Step 6: Commit** — `git commit -am "feat(wander): guard+probe navigation core in both modes; live explore fail-closed on calibration"`

---

### Task 6: Arm gating in mind.py (`_can_explore` requires calibration)

**Files:**
- Modify: `src/pxh/mind.py:1207` (`_can_explore`)
- Test: `tests/test_px_mind.py` (or wherever `_can_explore` tests live — `grep -rn "_can_explore" tests/`)

**Interfaces:**
- Consumes: `pxh.wander.load_cliff_calibration(state_dir)` (import inside the function to keep mind.py's import graph light).
- Produces: `_can_explore` returns False when calibration is missing/corrupt; stale calibration only logs (wander.py already logs it on load).

- [ ] **Step 1: Write failing test**

```python
def test_can_explore_requires_cliff_calibration(tmp_path, monkeypatch):
    from pxh import mind
    monkeypatch.setattr(mind, "STATE_DIR", tmp_path)
    session = {"roaming_allowed": True, "confirm_motion_allowed": True,
               "wheels_on_blocks": False, "listening": False}
    awareness = {"battery": {"pct": 80, "charging": False}}
    assert mind._can_explore(session, awareness) is False   # no calibration file
    (tmp_path / "wander_calibration.json").write_text(json.dumps({
        "floor_ref": [1000, 1000, 1000], "cliff_ref": [650, 650, 650],
        "ts": "2026-07-29T00:00:00+00:00"}))
    assert mind._can_explore(session, awareness) is True    # no cooldown file yet
```

- [ ] **Step 2: Run to verify failure** — first assert fails (returns True today with no meta file)

- [ ] **Step 3: Implement** — in `_can_explore`, after the battery checks and before the cooldown check:

```python
    # Fail closed: autonomous roaming requires a calibrated cliff reference.
    from pxh.wander import load_cliff_calibration
    if load_cliff_calibration(STATE_DIR) is None:
        return False
```

  Note: `load_cliff_calibration` monkeypatch-resolves `STATE_DIR` via the argument, so mind tests patching `mind.STATE_DIR` work; wander.py's own module `STATE_DIR` is not consulted here.

- [ ] **Step 4: Verify** — `python -m pytest tests/test_px_mind.py tests/test_mind.py -v` → PASS
- [ ] **Step 5: Commit** — `git commit -am "feat(mind): explore self-dispatch requires calibrated cliff reference (fail closed)"`

---

## Milestone 2 — Memory that accumulates, freshness, cleanup

### Task 7: Split observations into `state/observations.jsonl`; update all consumers

**Files:**
- Modify: `src/pxh/wander.py` (`_flush_nav_entries`, `_write_observation` → one helper), `src/pxh/mind.py:1998` (awareness `recent_exploration`), `src/pxh/mind.py:2714` (explore hints), `src/pxh/voice_loop.py:474` (context injection)
- Test: `tests/test_wander.py`, mind/voice-loop test files that cover those blocks (`grep -rn "recent_exploration\|exploration.jsonl" tests/`)

**Interfaces:**
- Produces: `append_jsonl_capped(path: Path, entries: list[dict], cap: int) -> None` in wander.py — FileLock-guarded (guarded import), appends, trims to last `cap` lines via mkstemp+os.replace, chmod 0o644. Constants `OBS_CAP = 1000`, `NAV_CAP = 100`.
- Nav entries → `exploration.jsonl` (cap `NAV_CAP`); observations → `observations.jsonl` (cap `OBS_CAP`). All three consumers read `observations.jsonl` and no longer filter `type == "observation"` from mixed lines (but keep the filter for robustness during transition).

- [ ] **Step 1: Write failing tests**

```python
def test_append_jsonl_capped_trims(tmp_path):
    p = tmp_path / "observations.jsonl"
    for batch in range(3):
        wander.append_jsonl_capped(p, [{"n": batch * 10 + i} for i in range(10)], cap=15)
    lines = [json.loads(l) for l in p.read_text().strip().splitlines()]
    assert len(lines) == 15
    assert lines[-1] == {"n": 29}

def test_observation_goes_to_observations_file(tmp_path, monkeypatch):
    monkeypatch.setattr(wander, "STATE_DIR", tmp_path)
    wander._write_observation({"type": "observation", "landmark": "a red chair"})
    assert (tmp_path / "observations.jsonl").exists()
    assert not (tmp_path / "exploration.jsonl").exists()
```

  And for mind.py (in its test file):

```python
def test_awareness_reads_observations_file(tmp_path, monkeypatch):
    from pxh import mind
    monkeypatch.setattr(mind, "STATE_DIR", tmp_path)
    obs = {"type": "observation", "landmark": "bookshelf corner",
           "heading_estimate": "", "interesting": True, "vision_failed": False}
    (tmp_path / "observations.jsonl").write_text(json.dumps(obs) + "\n")
    # nav spam in the OLD file must not shadow observations anymore
    (tmp_path / "exploration.jsonl").write_text(
        "\n".join(json.dumps({"type": "nav", "action": "forward"}) for _ in range(100)) + "\n")
    recent = mind._recent_exploration_observations()   # extracted helper, see Step 3
    assert recent and recent[0]["landmark"] == "bookshelf corner"
```

- [ ] **Step 2: Run to verify failure** — FAIL

- [ ] **Step 3: Implement.** wander.py: write `append_jsonl_capped` (body = current `_flush_nav_entries` locking/trim logic, generalized); `_flush_nav_entries(entries, explore_id)` → `append_jsonl_capped(STATE_DIR / "exploration.jsonl", entries, NAV_CAP)`; `_write_observation(entry)` → `append_jsonl_capped(STATE_DIR / "observations.jsonl", [entry], OBS_CAP)`. mind.py: extract the awareness block at :1998 into `_recent_exploration_observations() -> list[dict]` reading `STATE_DIR / "observations.jsonl"` last 20 lines (keeps the `type`/`vision_failed` filters); awareness code calls it. Explore-hints block at :2714 reads `observations.jsonl` last 10. voice_loop.py:474: `exploration_file = state_dir / "observations.jsonl"` (variable rename to `observations_file`), same filtering.

- [ ] **Step 4: Verify** — targeted files + `python -m pytest -m "not live" -q` → no new failures.
- [ ] **Step 5: Commit** — `git commit -am "feat(wander): observations.jsonl split (cap 1000) so nav telemetry can't evict memories"`

---

### Task 8: Frigate freshness, duplicate meta write, non-blocking speak

**Files:**
- Modify: `src/pxh/wander.py` (`_query_frigate`, `speak`), `src/pxh/mind.py:~3149-3159` (delete duplicate meta write)
- Test: `tests/test_wander.py`, mind dispatch test file

**Interfaces:**
- Produces: `_query_frigate(after_epoch: float | None = None)` — appends `&after={int(after_epoch)}` when given; explore loop passes `start_time`. `speak(text)` no longer waits on aplay (drop `ap.wait()` / `es.wait()`; keep Popen fire-and-forget).
- Deletes: mind.py's "Update exploration_meta (establishes cooldown)" block — px-wander's start-of-run write (currently `wander.py`, the `meta["last_explore_ts"] = …` right after the exploring.json handshake) is the single writer. Accepted residual (documented in spec §5): launch-failure re-dispatch next cycle; those paths are quick-exit and motionless.

- [ ] **Step 1: Write failing tests**

```python
def test_query_frigate_passes_after(monkeypatch):
    seen = {}
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"[]"
    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        return FakeResp()
    monkeypatch.setattr(wander.urllib.request, "urlopen", fake_urlopen)
    wander._query_frigate(after_epoch=1753747200.9)
    assert "after=1753747200" in seen["url"]
```

  mind side (dispatch test — follow the existing explore-dispatch test pattern, `grep -rn "PX_WANDER_MODE" tests/`):

```python
def test_mind_dispatch_does_not_write_cooldown(tmp_path, monkeypatch):
    # arrange the dispatch path with subprocess mocked; then:
    assert not (tmp_path / "exploration_meta.json").exists()
```

- [ ] **Step 2: Run to verify failure** — FAIL
- [ ] **Step 3: Implement** — `_query_frigate` gains the parameter and URL suffix; explore loop calls `_query_frigate(after_epoch=start_time)`. Delete the mind.py block from the comment `# Update exploration_meta (establishes cooldown)` through its `except…pass` (keep the yield_alive/wait code above and the subprocess launch below untouched). In `speak()`, replace `ap.wait()` with nothing (Popen returned and abandoned; add `# fire-and-forget: never block the drive loop on audio`) and drop the else-branch `es.wait()`.
- [ ] **Step 4: Verify** — targeted tests + full dry suite → PASS/no new failures.
- [ ] **Step 5: Commit** — `git commit -am "fix(wander): fresh-only frigate events, single cooldown writer, non-blocking speech"`

---

### Task 9: Stop pytest leaking into real logs

**Files:**
- Modify: `tests/conftest.py`
- Test: verification by line-count comparison (this is harness hygiene, not app behavior)

**Interfaces:**
- Produces: autouse fixture patching module-level `LOG_FILE` attributes of already-imported `pxh.mind` / `pxh.wander` to tmp_path. Root cause: `mind.py:56-57` resolves `LOG_FILE` from env **at import time**; direct-call tests (e.g. `tests/test_mind.py` `_drive_reflection`) then write "explore injection: action enum not found", synthetic thoughts, etc. into the real `logs/px-mind.log`.

- [ ] **Step 1: Capture baseline** — `wc -l logs/px-mind.log` (note the number).
- [ ] **Step 2: Implement**

```python
@pytest.fixture(autouse=True)
def _isolate_module_log_files(tmp_path, monkeypatch):
    """mind.py/wander.py resolve LOG_FILE from env at import time; direct-call
    tests would otherwise write into the repo's real logs/."""
    for mod_name in ("pxh.mind", "pxh.wander"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "LOG_FILE"):
            monkeypatch.setattr(mod, "LOG_FILE",
                                tmp_path / f"{mod_name.rsplit('.', 1)[-1]}.log")
```

- [ ] **Step 3: Verify** — `python -m pytest tests/test_mind.py tests/test_px_mind.py tests/test_wander.py -q` then `wc -l logs/px-mind.log` → line count unchanged from Step 1.
- [ ] **Step 4: Commit** — `git commit -am "test: quarantine module-level LOG_FILE so pytest can't write real logs"`

---

## Milestone 3 — LLM in the loop

### Task 10: Directive client (test seam + Ollama backend + cap)

**Files:**
- Modify: `src/pxh/wander.py`, `src/pxh/spark_config.py`
- Test: `tests/test_wander.py`

**Interfaces:**
- Produces (spark_config): `WANDER_DIRECTIVE_INTERVAL_S = 20`, `WANDER_DIRECTIVE_TIMEOUT_S = 5`, `WANDER_DIRECTIVE_PROMPT` (template with `{intent}`, `{observations}`, `{telemetry}`), `WANDER_SYNTHESIS_PROMPT` (template with `{intent}`, `{observations}`).
- Produces (wander.py):
  - `VALID_DIRECTIVES = {"continue", "turn_left", "turn_right", "investigate", "photograph", "done"}`
  - `get_directive(intent: str, observations: list[dict], telemetry: str) -> dict | None` — `{"directive": ..., "reason": str}` or None on ANY failure (timeout, HTTP error, unparseable, invalid enum). Backend: `PX_WANDER_LLM_CMD` env (shell command, prompt on stdin, JSON on stdout — test seam, `CODEX_CHAT_CMD` pattern) else POST `{PX_OLLAMA_HOST|http://M5:11434}/api/generate` with `{"model": PX_WANDER_LLM_MODEL|PX_OLLAMA_MODEL|"qwen3:4b", "prompt": …, "stream": False, "options": {"num_predict": 120}}` (mind.py:2146-2166 shape), timeout `WANDER_DIRECTIVE_TIMEOUT_S`.
  - `_extract_json_object(text: str) -> dict | None` — first `{…}` via brace matching.

- [ ] **Step 1: Write failing tests**

```python
def _fake_llm_cmd(tmp_path, payload: str) -> str:
    script = tmp_path / "fake_llm.sh"
    script.write_text(f"#!/bin/sh\ncat > /dev/null\nprintf '%s' '{payload}'\n")
    script.chmod(0o755)
    return str(script)

def test_get_directive_via_cmd_seam(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_WANDER_LLM_CMD",
        _fake_llm_cmd(tmp_path, '{"directive": "turn_left", "reason": "sound to the left"}'))
    d = wander.get_directive("find the bookshelf", [], "forward 80cm clear")
    assert d == {"directive": "turn_left", "reason": "sound to the left"}

def test_get_directive_invalid_enum_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_WANDER_LLM_CMD",
        _fake_llm_cmd(tmp_path, '{"directive": "fly", "reason": "wheee"}'))
    assert wander.get_directive("x", [], "y") is None

def test_get_directive_garbage_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_WANDER_LLM_CMD", _fake_llm_cmd(tmp_path, "I think you should"))
    assert wander.get_directive("x", [], "y") is None

def test_get_directive_timeout_is_none(tmp_path, monkeypatch):
    script = tmp_path / "slow.sh"
    script.write_text("#!/bin/sh\nsleep 10\n")
    script.chmod(0o755)
    monkeypatch.setenv("PX_WANDER_LLM_CMD", str(script))
    monkeypatch.setattr(wander.spark_config, "WANDER_DIRECTIVE_TIMEOUT_S", 1)
    assert wander.get_directive("x", [], "y") is None
```

- [ ] **Step 2: Run to verify failure** — FAIL
- [ ] **Step 3: Implement.** spark_config additions (prompts kept short — this runs on a small local model):

```python
WANDER_DIRECTIVE_INTERVAL_S = 20
WANDER_DIRECTIVE_TIMEOUT_S = 5
WANDER_DIRECTIVE_PROMPT = """You are SPARK, a small robot exploring your home.
Your intention: {intent}
Recent observations: {observations}
Telemetry: {telemetry}
Reply with ONLY this JSON: {{"directive": "continue|turn_left|turn_right|investigate|photograph|done", "reason": "short"}}"""
WANDER_SYNTHESIS_PROMPT = """You are SPARK, a small robot who just finished exploring.
Your intention was: {intent}
What you observed: {observations}
Write 1-3 first-person sentences about what you found and how it felt. Warm, specific, no lists."""
```

  wander.py: `from pxh import spark_config` at module top (pure constants, safe under root python3 — verify with `sudo -n /usr/bin/python3 -c "import pxh.spark_config"` once at implementation) and `import shlex` (the test-seam command is split with `shlex.split`, never `shell=True`). Client:

```python
VALID_DIRECTIVES = {"continue", "turn_left", "turn_right", "investigate", "photograph", "done"}

def _extract_json_object(text: str) -> dict | None:
    start = text.find("{")
    depth = 0
    for i in range(start, len(text)) if start >= 0 else []:
        if text[i] == "{": depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None

def _call_wander_llm(prompt: str, timeout: float) -> str | None:
    cmd = os.environ.get("PX_WANDER_LLM_CMD")
    try:
        if cmd:
            r = subprocess.run(shlex.split(cmd), input=prompt, capture_output=True,
                               text=True, timeout=timeout)
            return r.stdout if r.returncode == 0 else None
        host = os.environ.get("PX_OLLAMA_HOST", "http://M5:11434")
        model = (os.environ.get("PX_WANDER_LLM_MODEL")
                 or os.environ.get("PX_OLLAMA_MODEL") or "qwen3:4b")
        body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                           "options": {"num_predict": 120}}).encode()
        req = urllib.request.Request(f"{host}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()).get("response", "")
    except Exception as exc:
        log(f"wander LLM call failed: {exc}")
        return None

def get_directive(intent: str, observations: list[dict], telemetry: str) -> dict | None:
    obs_text = "; ".join(o.get("landmark") or o.get("description", "")[:60]
                         for o in observations[-3:]) or "nothing yet"
    prompt = spark_config.WANDER_DIRECTIVE_PROMPT.format(
        intent=intent or "explore and see what you find",
        observations=obs_text, telemetry=telemetry)
    raw = _call_wander_llm(prompt, spark_config.WANDER_DIRECTIVE_TIMEOUT_S)
    if not raw:
        return None
    parsed = _extract_json_object(raw)
    if not parsed or parsed.get("directive") not in VALID_DIRECTIVES:
        return None
    return {"directive": parsed["directive"], "reason": str(parsed.get("reason", ""))[:200]}
```

- [ ] **Step 4: Verify** — `python -m pytest tests/test_wander.py -k directive -v` → PASS
- [ ] **Step 5: Commit** — `git commit -am "feat(wander): LLM directive client with PX_WANDER_LLM_CMD seam, M5 Ollama backend"`

---

### Task 11: Directive integration — wheels stopped, capped, cooldown-respecting

**Files:**
- Modify: `src/pxh/wander.py` (explore loop)
- Test: `tests/test_wander.py`

**Interfaces:**
- Consumes: Task 10 client; Task 5 loop `state` dict.
- Produces, inside the explore loop after the nav entry and BELOW the abort check (spec: time-limit stays above directive handling):
  - Directive window fires when `time.time() - last_directive_ts >= WANDER_DIRECTIVE_INTERVAL_S` OR an observation was just written, AND `directives_used < duration // WANDER_DIRECTIVE_INTERVAL_S + 2`.
  - `px.stop()` is called BEFORE `get_directive` (never drive on a stale decision).
  - Mapping: `continue` → nothing; `turn_left`/`turn_right` → `state["forced_turn"] = "left"/"right"` (consumed by `run_explore_step`, which routes it through `probe_turn` — still cliff-guarded, so safety vetoes bad directives by construction); `investigate` → one `guarded_forward(px, guard, 15, 0.3)` + `photo_requested = True`; `photograph` → `photo_requested = True`; `done` → `abort_reason = "directive_done"`, break.
  - **`photo_requested` is only a trigger candidate**: the existing curiosity block gates it behind `_check_daily_vision_cap` AND `PHOTO_COOLDOWN_S`, exactly like the other triggers — a chatty model cannot burn the camera (review note #2).
  - Each directive appends `{"type": "nav", "action": f"directive_{d}", "reason": …}` to the nav buffer for the narrative trail.

- [ ] **Step 1: Write failing tests**

```python
def _fast_explore(monkeypatch, tmp_path, llm_payload, duration=2):
    """Run a short live-ish explore with FakePx via a wander.run_explore() seam."""
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    monkeypatch.setattr(wander, "STATE_DIR", tmp_path)
    monkeypatch.setattr(wander, "_query_frigate", lambda after_epoch=None: [])
    monkeypatch.setattr(wander, "_call_describe_scene",
                        lambda dry: {"status": "ok", "description": "A tidy desk."})
    monkeypatch.setenv("PX_WANDER_LLM_CMD", _fake_llm_cmd(tmp_path, llm_payload))
    ...

def test_directive_done_ends_wander(tmp_path, monkeypatch):
    result = _fast_explore(monkeypatch, tmp_path,
                           '{"directive": "done", "reason": "seen enough"}')
    assert result["abort_reason"] == "directive_done"

def test_wheels_stopped_before_directive_call(tmp_path, monkeypatch):
    # FakePx.calls must contain "stop" at an index before the LLM cmd runs;
    # instrument by having the fake LLM script touch a marker file and
    # asserting px.calls[-1] == "stop" at that moment via a wrapper.
    ...

def test_photograph_directive_respects_cooldown(tmp_path, monkeypatch):
    # PHOTO_COOLDOWN_S monkeypatched high; two directive windows both say
    # "photograph"; _call_describe_scene call-count must be <= 1.
    ...

def test_directive_cap(tmp_path, monkeypatch):
    # duration=40 → cap = 40//20 + 2 = 4; LLM always answers "continue";
    # count invocations via a counting wrapper on get_directive; assert <= 4.
    ...
```

  To make these testable, Step 3 extracts the explore loop body from `main()` into `run_explore(px, guard, args, intent) -> dict` (returns the result payload; `main()` explore branch becomes: gates → calibration → meta/exploring writes → `run_explore(...)` → cleanup). Fill in the four `...` bodies against that seam — each is arrange (FakePx with generous grayscale/sonar iterators) → `wander.run_explore(px, guard, argparse.Namespace(duration=…, quiet=True, dry=False, steps=5), intent="test")` → assert.

- [ ] **Step 2: Run to verify failure** — FAIL (`run_explore` doesn't exist)
- [ ] **Step 3: Implement** — extract `run_explore`, add the directive window per the Interfaces block:

```python
    # inside the explore loop, after nav entry + curiosity block
    now = time.time()
    directive_cap = duration // spark_config.WANDER_DIRECTIVE_INTERVAL_S + 2
    if ((now - last_directive_ts >= spark_config.WANDER_DIRECTIVE_INTERVAL_S
         or observation_written)
            and directives_used < directive_cap):
        px.stop()                       # never drive on a stale decision
        telemetry = f"forward {entry['sonar_cm'] or '?'}cm, last action {entry['action']}, " \
                    f"{guard.edge_events} edge events"
        d = get_directive(intent, observations, telemetry)
        last_directive_ts = now
        directives_used += 1
        if d:
            log(f"directive: {d['directive']} — {d['reason']}")
            nav_buffer.append({"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                               "type": "nav", "explore_id": explore_id,
                               "action": f"directive_{d['directive']}",
                               "reason": d["reason"]})
            if d["directive"] in ("turn_left", "turn_right"):
                state["forced_turn"] = d["directive"].removeprefix("turn_")
            elif d["directive"] == "investigate":
                guarded_forward(px, guard, 15, 0.3)
                photo_requested = True
            elif d["directive"] == "photograph":
                photo_requested = True
            elif d["directive"] == "done":
                abort_reason = "directive_done"
                break
```

  In the curiosity block, add `photo_requested` as a trigger candidate INSIDE the cooldown+cap gate (then reset it to False regardless of whether the photo fired — a denied request does not queue).

- [ ] **Step 4: Verify** — `python -m pytest tests/test_wander.py -v` → PASS; full dry suite → no new failures.
- [ ] **Step 5: Commit** — `git commit -am "feat(wander): directive loop — wheels-stopped LLM windows, capped, cooldown-respecting photos"`

---

### Task 12: Intent plumbing + narrative synthesis + docs

**Files:**
- Modify: `src/pxh/wander.py` (`--intent`, synthesis), `bin/tool-wander` (env→CLI bridge), `src/pxh/voice_loop.py:675` (`tool_wander` validation), `src/pxh/mind.py` explore dispatch (~3160) + post-thought (~3200), `docs/prompts/spark-voice-system.md`, `docs/prompts/claude-voice-system.md`, `docs/prompts/codex-voice-system.md`, `docs/prompts/persona-gremlin.md`, `docs/prompts/persona-vixen.md`
- Test: `tests/test_wander.py`, `tests/test_tools.py`, voice_loop validation tests (`grep -n "tool_wander" tests/test_voice_loop*.py`)

**Interfaces:**
- `px-wander --intent "text"` (argparse, default `""`; sanitized: `intent = " ".join(raw.split())[:200]`).
- voice_loop `validate_action` `tool_wander` branch: optional `intent` param → `sanitized["PX_WANDER_INTENT"] = str(params.get("intent", ""))[:200]`.
- tool-wander: reads `PX_WANDER_INTENT` env (set by voice_loop/mind — no sudo yet at that point) and appends `["--intent", intent]` to the sudo command (**CLI because sudo strips env**).
- mind.py dispatch: `explore_env["PX_WANDER_INTENT"] = thought_text[:200]` where `thought_text` is the reflection thought that chose `explore`.
- Synthesis in `run_explore` (live, observations non-empty, after the loop): `narrative = synthesize_narrative(intent, observations)` → result JSON gains `"narrative": str | None`. `synthesize_narrative` uses `_call_wander_llm(spark_config.WANDER_SYNTHESIS_PROMPT.format(...), timeout=10)`, returns stripped text ≤400 chars or None.
- tool-wander forwards `narrative` (add to the forwarded-keys tuple).
- mind.py post-exploration thought: use `explore_result.get("narrative")` as the thought text when present (fallback: current hardcoded strings); `salience = min(0.8, 0.4 + 0.1 * explore_result.get("interesting", 0))`; mood `"curious"`.
- Docs: in all five prompt files, extend the `tool_wander` line: `optional intent: "why you're exploring" (string, ≤200 chars) — shapes where the robot goes and what it reports back`.

- [ ] **Step 1: Write failing tests**

```python
def test_synthesize_narrative(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_WANDER_LLM_CMD",
        _fake_llm_cmd(tmp_path, "I found the sunlit corner by the bookshelf and lingered there."))
    n = wander.synthesize_narrative("find the light",
        [{"landmark": "sunlit bookshelf", "description": "A sunlit bookshelf."}])
    assert "bookshelf" in n

def test_tool_wander_intent_becomes_cli_arg(isolated_project):
    """tool-wander passes PX_WANDER_INTENT through as --intent (sudo strips env)."""
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_WANDER_INTENT"] = "look for  the\ncat"
    stdout = run_tool(["bin/tool-wander"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "ok"     # dry avoid-mode run accepts intent silently

def test_validate_action_wander_intent():
    from pxh.voice_loop import validate_action
    tool, sanitized = validate_action(
        {"tool": "tool_wander", "params": {"steps": 3, "mode": "explore",
                                           "duration": 60, "intent": "chase the sunbeam"}})
    assert sanitized["PX_WANDER_INTENT"] == "chase the sunbeam"
```

  mind side: extend the dispatch test to assert `PX_WANDER_INTENT` lands in the subprocess env, and a post-thought test asserting `append_thought` receives the narrative text when the mocked tool output contains `"narrative"`.

- [ ] **Step 2: Run to verify failure** — FAIL
- [ ] **Step 3: Implement** per the Interfaces block. tool-wander bridge (after the mode/duration args):

```python
    intent = " ".join(os.environ.get("PX_WANDER_INTENT", "").split())[:200]
    if intent:
        command.extend(["--intent", intent])
```

  wander.py synthesis:

```python
def synthesize_narrative(intent: str, observations: list[dict]) -> str | None:
    if not observations:
        return None
    obs_text = "; ".join((o.get("description") or o.get("landmark", ""))[:100]
                         for o in observations[-6:])
    raw = _call_wander_llm(spark_config.WANDER_SYNTHESIS_PROMPT.format(
        intent=intent or "just wandering", observations=obs_text), timeout=10)
    if not raw:
        return None
    text = " ".join(raw.split())[:400]
    return text or None
```

- [ ] **Step 4: Verify** — targeted tests + full dry suite → PASS / no new failures.
- [ ] **Step 5: Update the five prompt docs** (one-line edit each, wording above).
- [ ] **Step 6: Commit** — `git commit -am "feat(wander): intent plumbing across the sudo hop, post-wander narrative synthesis"`

---

## Milestone 4 — Gate flip (LAST, after on-floor proof)

### Task 13: Kill-switch semantics + docs + live checklist

**Files:**
- Modify: `state/session.template.json` (`roaming_allowed: true`), `CLAUDE.md` (wander/roaming section), `docs/superpowers/specs/2026-07-29-wander-intelligence-design.md` (mark IMPLEMENTED)
- Test: existing suite (template change is exercised by `default_state()` tests — update any that assert `roaming_allowed is False`)

- [ ] **Step 1: Flip the template** — `"roaming_allowed": true` in `state/session.template.json`; run `grep -rn "roaming_allowed" tests/` and update assertions that encoded the old default (the tool-gate test writes its own session, so it still passes).
- [ ] **Step 2: CLAUDE.md** — replace the wander mentions in the safety section with: roaming_allowed is a kill switch (default true); autonomous explore additionally requires `state/wander_calibration.json` (fail closed — `px-wander --calibrate-cliff` on the actual floor); note the lost default state explicitly ("voice motion OK, autonomous roaming off" now requires setting the kill switch manually); cliff guard is surface-dependent, recalibrate when the floor changes.
- [ ] **Step 3: Run full dry suite** — `python -m pytest -m "not live"` → green (minus known test_memory dedup); `sudo systemctl restart px-post`.
- [ ] **Step 4: Commit** — `git commit -am "feat(wander): roaming_allowed becomes kill switch — arming is calibration-gated"`
- [ ] **Step 5: MANUAL live checklist (human present, not automated):**
  1. `bin/px-wander --calibrate-cliff` with the robot on the actual floor
  2. `curl http://M5:11434/api/tags` from the Pi (directive backend reachable)
  3. Supervised `bin/px-wander --mode explore --duration 60` on the floor, hand near the robot
  4. Watch one edge approach: confirm stop + reverse (place a dark mat as a pseudo-cliff if no safe ledge)
  5. Only then: `PATCH /api/v1/session {"roaming_allowed": true, "confirm": true}` on the live session (it may already be true from the template on next reset — verify with `GET /api/v1/session`)
  6. Watch the first autonomous px-mind-dispatched explore end-to-end; check `state/observations.jsonl` and the narrative thought in `state/thoughts-spark.jsonl`

---

## Self-review notes (completed)

- **Spec coverage:** §1 safety → Tasks 2,3,5,6,13; §2 sensing → Tasks 4,5; §3 LLM → Tasks 10,11,12; §4 memory → Tasks 7,8; §5 cleanup → Tasks 1,8,9; rollout order preserved (gate flip last). Review note #1 (staleness = warning) → Task 2; note #2 (photo cooldown) → Task 11.
- **Type consistency:** `CliffGuard.check` returns `"clear"|"cliff"|"fail"` everywhere; `run_explore_step` consumes `state["forced_turn"]` set by Task 11; `get_directive`/`synthesize_narrative` both route through `_call_wander_llm`.
- **Known risk:** Task 11's test seam requires extracting `run_explore` — the largest mechanical step; do it as a pure extraction first, then add the directive window.
