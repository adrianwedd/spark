"""Autonomous wander: drive forward, sweep for best direction, avoid obstacles.

Uses the Picarx handle directly for sonar — no subprocess, no GPIO conflict.
Speaks while exploring if espeak is available and a persona is set.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

from pxh.gpio_lease import GpioLeaseGuard, GpioLeaseStore
from pxh.race import safe_grayscale
from pxh.utils import clamp

LOG_DIR  = Path(os.environ.get("LOG_DIR",  Path.cwd() / "logs"))
LOG_FILE = Path(os.environ.get("PX_LOG_FILE", LOG_DIR / "px-wander.log"))
PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parent.parent))

OBSTACLE_CM    = 30.0   # stop and re-evaluate if anything closer than this
CLEAR_CM       = 60.0   # "definitely clear" threshold for direction choice
FORWARD_SPEED  = 30
FORWARD_S      = 1.2    # drive this long per step
TURN_SPEED     = 25
TURN_S         = 0.7
PROBE_S        = 0.4

EXPLORE_STEP_TIMEOUT   = 30
# Budget for one tool-describe-scene call: Claude vision (vision.CLAUDE_TIMEOUT,
# 60s) + bounded speech (60s) + ~20s photo/stream headroom = 140s worst case,
# plus 25s slack. Moves WITH vision.CLAUDE_TIMEOUT — the pin in
# test_describe_scene_timeout_has_margin_over_claude checks the surplus, not
# just the sign, so raising one without the other fails the suite by name.
DESCRIBE_SCENE_TIMEOUT = 165
PHOTO_COOLDOWN_S       = 30
DAILY_VISION_CAP       = 50
VISION_FAIL_MAX        = 3
STUCK_THRESHOLD        = 3
BATTERY_STALE_S        = 60
FLUSH_INTERVAL         = 10

STATE_DIR  = Path(os.environ.get("PX_STATE_DIR", PROJECT_ROOT / "state"))
BIN_DIR    = PROJECT_ROOT / "bin"

CLIFF_MARGIN          = 0.65
CALIBRATION_STALE_S   = 30 * 24 * 3600
# Keep the published exploration state current for observers and timeout
# cleanup. GPIO exclusion itself is owned separately by gpio_lease.json.
EXPLORING_REFRESH_S   = 20.0
GRAYSCALE_SETTLE_S    = 3.0
GRAYSCALE_POLL_S      = 0.05

REVERSE_S             = 0.3
REVERSE_SPEED         = 20
REVERSE_STALL_CM      = 2.0
EDGE_ABORT_COUNT      = 2
SENSOR_FAIL_ABORT_COUNT = 3

FRIGATE_HOST   = os.environ.get("PX_FRIGATE_HOST", "http://pi5-hailo:5000")
FRIGATE_CAMERA = os.environ.get("PX_FRIGATE_CAMERA", "picar_x")

FALLBACK_DESCRIPTION = "I couldn't see anything right now."

OBS_CAP = 1000
NAV_CAP = 100


def log(msg: str) -> None:
    ts = dt.datetime.now().isoformat(timespec="seconds")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ml = msg.lower()
    icon = "  "
    if "explore" in ml and ("start" in ml or "complete" in ml):
        icon = "\U0001f9ed"  # 🧭
    elif any(k in ml for k in ("sweep", "sonar", "dist=")):
        icon = "\U0001f4e1"  # 📡
    elif any(k in ml for k in ("step ", "forward", "turned", "reverse")):
        icon = "\U0001f697"  # 🚗
    elif any(k in ml for k in ("obstacle", "stuck", "blocked")):
        icon = "\U0001f6a7"  # 🚧
    elif any(k in ml for k in ("abort", "error", "failed")):
        icon = "⚡"      # ⚡
    elif "frigate" in ml:
        icon = "\U0001f4f7"  # 📷
    elif "vision" in ml or "photo" in ml:
        icon = "\U0001f441️ "  # 👁️
    elif "wander start" in ml or "wander complete" in ml:
        icon = "\U0001f6b6"  # 🚶
    line = f"{ts} {icon} {msg}"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, file=sys.stderr)


def speak(text: str) -> None:
    """Speak text via espeak if available. Non-blocking (fire and forget).

    Requires aplay as well as espeak: espeak is spawned with a pipe that only
    aplay drains, and nothing here waits on it. With no reader the pipe fills
    (~64KB) and espeak blocks on write forever, leaking a stuck process per
    call — so with no sink we synthesize nothing.
    """
    if not (shutil.which("espeak") and shutil.which("aplay")):
        return
    try:
        from robot_hat import enable_speaker
        enable_speaker()
    except Exception:
        try:
            subprocess.run(["/usr/bin/python3", "-c",
                "from robot_hat import enable_speaker; enable_speaker()"],
                capture_output=True, timeout=3)
        except Exception as exc2:
            log(f"enable_speaker fallback failed: {exc2}")
    variant = os.environ.get("PX_VOICE_VARIANT", "en+m3")
    pitch   = os.environ.get("PX_VOICE_PITCH",   "82")
    rate    = os.environ.get("PX_VOICE_RATE",     "150")
    device  = os.environ.get("PX_VOICE_DEVICE",   "")
    try:
        es = subprocess.Popen(
            ["espeak", "-v", variant, "-p", pitch, "-s", rate, "--stdout", text],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        cmd = ["aplay", "-q"]
        if device:
            cmd += ["-D", device]
        aplay_env = os.environ.copy()
        aplay_env["PULSE_SERVER"] = "unix:/run/user/1000/pulse/native"
        # fire-and-forget: never block the drive loop on audio
        subprocess.Popen(cmd, stdin=es.stdout, stderr=subprocess.DEVNULL, env=aplay_env)
        es.stdout.close()
    except Exception as exc:
        log(f"speak error: {exc}")


_sigterm_flag = threading.Event()

def _handle_sigterm(signum, frame):
    _sigterm_flag.set()


def _read_session() -> dict:
    session_path = Path(os.environ.get("PX_SESSION_PATH", STATE_DIR / "session.json"))
    try:
        return json.loads(session_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_battery() -> dict | None:
    battery_path = STATE_DIR / "battery.json"
    try:
        data = json.loads(battery_path.read_text(encoding="utf-8"))
        ts = dt.datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
        age_s = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
        if age_s > BATTERY_STALE_S:
            return None
        return {"pct": int(data["pct"]), "volts": float(data["volts"]),
                "charging": bool(data.get("charging", False))}
    except Exception:
        return None


def _check_abort(session: dict, battery: dict | None, stuck_count: int,
                 start_time: float, duration: int, edge_events: int = 0,
                 sensor_fail_streak: int = 0) -> str | None:
    if _sigterm_flag.is_set():
        return "terminated"
    if not session.get("roaming_allowed", False):
        return "roaming disabled"
    if not session.get("confirm_motion_allowed", False):
        return "motion not allowed"
    if session.get("wheels_on_blocks", False):
        return "wheels on blocks"
    if session.get("listening", False):
        return "someone is talking"
    if battery is None:
        return "battery data stale or missing"
    if battery.get("charging", False):
        return "battery charging"
    if battery["pct"] <= 20:
        return "battery low"
    if edge_events >= EDGE_ABORT_COUNT:
        return "edge events"
    if sensor_fail_streak >= SENSOR_FAIL_ABORT_COUNT:
        return "sonar sensor failure"
    if stuck_count >= STUCK_THRESHOLD:
        return "stuck (3 blocked sweeps)"
    if time.time() - start_time >= duration:
        return "time limit reached"
    return None


# An HC-SR04 echo timeout returns a negative distance, and on angled or soft
# surfaces that is routine rather than a fault. Un-retried, three routine
# timeouts in a row hit SENSOR_FAIL_ABORT_COUNT and end the wander: live run 9
# (2026-08-06) burned steps 4, 5 and 6 inside one second that way, aborting a
# run that was otherwise driving fine. Retries cost a few ms and only happen on
# a bad read, so a streak now means the sensor really is dead.
SONAR_RETRIES = 2
SONAR_RETRY_GAP_S = 0.03


def _read_sonar(px) -> float | None:
    for attempt in range(1 + SONAR_RETRIES):
        try:
            d = px.get_distance()
            if d is not None and d >= 0:
                return float(d)
        except Exception:
            pass
        if attempt < SONAR_RETRIES:
            time.sleep(SONAR_RETRY_GAP_S)
    return None


def _query_frigate(after_epoch: float | None = None) -> list[dict] | None:
    """Fetch recent Frigate detections for the wander camera.

    `after_epoch` bounds the query to events that *started* after that epoch
    (Frigate's `after=` takes epoch seconds). Without it the endpoint returns
    historical events, so a single person detection from hours ago colours
    every step of a run and fires the "new label" photo trigger spuriously.
    Truncated (not rounded) so the window never starts after the run did —
    a late-rounded bound would silently drop detections from the run's first
    half-second.
    """
    url = f"{FRIGATE_HOST}/api/events?cameras={FRIGATE_CAMERA}&limit=5&min_score=0.5"
    if after_epoch is not None:
        url += f"&after={int(after_epoch)}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=4) as resp:
            events = json.loads(resp.read().decode())
        detections = []
        seen = set()
        for evt in events:
            label = evt.get("label", "unknown")
            score = evt.get("top_score") or evt.get("score", 0)
            if label not in seen:
                detections.append({"label": label, "score": round(float(score), 2)})
                seen.add(label)
        return detections
    except Exception as exc:
        log(f"frigate query failed: {exc}")
        return None


def _write_exploring_state(active: bool, pid: int | None = None,
                            started: str | None = None) -> bool:
    """Write exploring.json. Returns True on success, False on failure."""
    data = {"active": active}
    if pid is not None:
        data["pid"] = pid
    if started is not None:
        data["started"] = started
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "exploring.json"
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        # World-readable: wander runs as root but pi-user processes must read
        # this file (tool-wander's timeout SIGTERM path reads the pid;
        # tool-describe-scene's owner-liveness check). mkstemp's 0600 default
        # locks them out with EACCES.
        os.chmod(tmp, 0o644)
        os.replace(tmp, str(path))
        return True
    except Exception as exc:
        log(f"exploring.json write failed: {exc}")
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return False


class _ExploringRefresher(threading.Thread):
    """Rewrite exploring.json every EXPLORING_REFRESH_S while a run is live.

    Observers use this state to identify and stop an orphaned wander process.
    The owner stops this refresher before clearing the state in its finally
    block so an in-flight write cannot resurrect active exploration.
    """

    def __init__(self, pid: int, started: str):
        super().__init__(daemon=True, name="exploring-refresh")
        self._stop_evt = threading.Event()
        self._pid = pid
        # NB: not "_started" — threading.Thread uses that name internally.
        self._started_iso = started

    def run(self) -> None:
        while not self._stop_evt.wait(EXPLORING_REFRESH_S):
            _write_exploring_state(True, pid=self._pid, started=self._started_iso)

    def stop(self) -> None:
        self._stop_evt.set()


def _load_exploration_meta() -> dict:
    path = STATE_DIR / "exploration_meta.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_exploration_meta(meta: dict) -> bool:
    """Write exploration_meta.json. Returns True on success, False on failure."""
    path = STATE_DIR / "exploration_meta.json"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, str(path))
        return True
    except Exception as exc:
        log(f"exploration_meta.json write failed: {exc}")
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return False


def _extract_landmark(description: str) -> str:
    if not description or description == FALLBACK_DESCRIPTION:
        return ""
    first = description.split(".")[0].strip()
    words = first.split()
    if words and words[0].lower() in ("a", "an", "the"):
        words = words[1:]
    return " ".join(words[:6])


def append_jsonl_capped(path: Path, entries: list[dict], cap: int) -> None:
    """Append entries to a jsonl file, keeping only the last `cap` lines.

    FileLock-guarded (best-effort — falls back to no lock if filelock isn't
    installed), and the trim+rewrite is atomic via mkstemp + os.replace so a
    concurrent reader never sees a partially-written file. Any failure during
    the temp-file write cleans up the temp file before re-raising the log
    (mirrors _save_exploration_meta / _write_exploring_state / calibrate_cliff).
    """
    if not entries:
        return
    try:
        from filelock import FileLock
        lock = FileLock(str(path) + ".lock", timeout=5)
    except ImportError:
        lock = None
    try:
        _cm = lock if lock else __import__("contextlib").nullcontext()
        with _cm:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = []
            if path.exists():
                existing = [ln for ln in path.read_text(encoding="utf-8").strip().splitlines() if ln.strip()]
            existing.extend(json.dumps(e) for e in entries)
            if len(existing) > cap:
                existing = existing[-cap:]
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write("\n".join(existing) + "\n")
                os.chmod(tmp, 0o644)
                os.replace(tmp, str(path))
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
    except Exception as exc:
        log(f"append_jsonl_capped error ({path.name}): {exc}")


def _flush_nav_entries(entries: list[dict], explore_id: str) -> None:
    append_jsonl_capped(STATE_DIR / "exploration.jsonl", entries, NAV_CAP)


def _write_observation(entry: dict) -> None:
    append_jsonl_capped(STATE_DIR / "observations.jsonl", [entry], OBS_CAP)


def _call_describe_scene(dry: bool) -> dict:
    env = os.environ.copy()
    env["PX_DRY"] = "1" if dry else "0"
    # Onboard speech only: routed Nest speech can take 90s+ and would blow
    # DESCRIBE_SCENE_TIMEOUT, killing the tool mid-run and charging a
    # spurious vision failure toward VISION_FAIL_MAX.
    env["PX_VOICE_NO_ROUTE"] = "1"
    try:
        result = subprocess.run(
            [str(BIN_DIR / "tool-describe-scene")],
            capture_output=True, text=True, check=False,
            env=env, timeout=DESCRIBE_SCENE_TIMEOUT,
        )
        lines = result.stdout.strip().splitlines()
        if lines:
            return json.loads(lines[-1])
    except Exception as exc:
        log(f"describe_scene error: {exc}")
    return {"status": "error", "description": FALLBACK_DESCRIPTION}


def _auto_remember(text: str) -> None:
    notes_path = STATE_DIR / "notes.jsonl"
    try:
        from filelock import FileLock
        lock = FileLock(str(notes_path) + ".lock", timeout=5)
    except ImportError:
        lock = None
    try:
        _cm = lock if lock else __import__("contextlib").nullcontext()
        with _cm:
            notes_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "note": text[:500],
                "source": "exploration",
            }
            # The only durable writer that records genuine perception: this is
            # what the camera saw, via tool-describe-scene. Still capped below
            # certainty — a vision model can be confidently wrong (#170).
            try:
                from pxh import provenance
                provenance.stamp(entry, "observation", "vision:describe-scene")
            except Exception:
                pass  # an unlabelled landmark beats a lost one

            with notes_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        log(f"auto_remember error: {exc}")


def _check_daily_vision_cap(meta: dict) -> bool:
    today = dt.date.today().isoformat()
    if meta.get("daily_vision_date") != today:
        return True
    return meta.get("daily_vision_calls", 0) < DAILY_VISION_CAP


def _increment_vision_count(meta: dict) -> dict:
    today = dt.date.today().isoformat()
    if meta.get("daily_vision_date") != today:
        meta["daily_vision_date"] = today
        meta["daily_vision_calls"] = 1
    else:
        meta["daily_vision_calls"] = meta.get("daily_vision_calls", 0) + 1
    return meta


def wait_for_grayscale(px, settle_s: float | None = None,
                        poll_s: float | None = None) -> list[float] | None:
    """Block until the grayscale ADC returns a genuine conversion.

    For roughly 0.75s after Picarx() is constructed the robot_hat ADC returns a
    fixed power-on latch rather than a measurement — observed on this hardware
    as [2571, 3085, 3599], an exact arithmetic progression (gaps of 514, 514)
    that three independent physical sensors would never produce. The window is
    wall-clock based, not read-count based: twelve back-to-back reads all return
    the latch, while reads spaced 0.25s apart go live on the fourth.

    Reading inside that window fabricates data, and the latch is high enough to
    clear any sane cliff threshold — so a guard check taken too early reports
    "clear" while the car sits at the edge of a step, and a calibration taken
    too early persists a reference ~5x too high.

    The latch clears PER CHANNEL, not all at once: px-guard-probe measured a
    live idle reading of [2571, 544, 385] — ch0 still on its exact latch value
    while ch1/ch2 had gone live — with ch0 reading 320 seconds later. So the
    reading must be held against every channel, not any: a rule of "differs
    from the baseline" accepts that mixed sample and calibration then persists
    a floor_ref carrying a ~6x-inflated channel, lifting the cliff threshold
    with it until ordinary floor trips the guard.

    Returns the first reading in which every channel has been observed to
    change since the initial sample, or None if the ADC never fully updated
    within `settle_s`. None means "cannot sense" and callers must fail closed,
    exactly as CliffGuard.check does. Tracking each channel's change across the
    whole poll loop (rather than requiring one reading to differ everywhere at
    once) is what keeps an already-partially-live baseline from deadlocking:
    a live channel still moves with sensor noise read to read, so it registers
    its own change and the wait ends normally instead of failing closed.

    settle_s/poll_s read the module attributes at call time (not as captured
    default args) so tests can shorten them without patching time.sleep.
    """
    settle_s = GRAYSCALE_SETTLE_S if settle_s is None else settle_s
    poll_s   = GRAYSCALE_POLL_S if poll_s is None else poll_s
    first = safe_grayscale(px, retries=1)
    changed = [False, False, False]
    deadline = time.monotonic() + settle_s
    while time.monotonic() < deadline:
        if poll_s:
            time.sleep(poll_s)
        gs = safe_grayscale(px, retries=1)
        if gs is None:
            continue
        if first is None:
            # The baseline read itself failed (an I2C error is most likely
            # right after Picarx(), which is exactly when the latch is up).
            # Adopt this reading as the baseline and keep waiting for a
            # CHANGE — returning it here would hand back the very latch this
            # function exists to reject, with no sample to compare against.
            first = gs
            changed = [False, False, False]
            continue
        for i in range(3):
            if gs[i] != first[i]:
                changed[i] = True
        if all(changed):
            return gs
    log("grayscale ADC never left its power-on latch on every channel — "
        "treating as unreadable (fail closed)")
    return None


def calibrate_cliff(px, state_dir: Path, settle_s: float | None = None,
                     poll_s: float | None = None, accumulate: bool = False) -> dict:
    """Read the floor's grayscale signature and persist a cliff calibration.

    Raises RuntimeError if the grayscale sensor can't be read, including the
    case where it only ever returns its power-on latch — persisting that would
    write a fabricated reference, so nothing is written at all.

    A single spot is a poor reference for a real floor. Measured 2026-08-06,
    one channel read ~950 on open lit boards and ~100 over a floorboard gap:
    a threshold derived from the bright spot rejects the dark one as a drop,
    which is what grounded runs 8-10. `accumulate` folds this reading into the
    stored one by keeping the per-channel MINIMUM, so calibrating at several
    spots yields a threshold below the darkest floor SPARK is allowed to meet.

    The trade is deliberate and one-way: the threshold can only fall, so the
    guard grows more permissive with every spot added. Sensitivity to a genuine
    drop is bounded by the darkest floor sampled — never accumulate a reading
    taken over an actual edge.
    """
    gs = wait_for_grayscale(px, settle_s=settle_s, poll_s=poll_s)
    if gs is None:
        raise RuntimeError("grayscale read failed — cannot calibrate")
    floor = [float(v) for v in gs]
    spots = [list(floor)]

    if accumulate:
        prior = load_cliff_calibration(state_dir)
        prior_floor = prior.get("floor_ref") if prior else None
        if (isinstance(prior_floor, list) and len(prior_floor) == 3
                and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        and math.isfinite(v) and v > 0 for v in prior_floor)):
            floor = [min(float(prior_floor[i]), floor[i]) for i in range(3)]
            prior_spots = prior.get("spots")
            if isinstance(prior_spots, list):
                # Cap the history: it is diagnostic context for "how varied is
                # this floor", not an unbounded log in a file read every wander.
                spots = (prior_spots + spots)[-20:]
        else:
            log("accumulate: no usable prior calibration — starting fresh")

    cal = {
        "floor_ref": floor,
        "cliff_ref": [round(v * CLIFF_MARGIN, 1) for v in floor],
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "spots": spots,
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(state_dir), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(cal, f, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, str(state_dir / "wander_calibration.json"))
    except Exception as exc:
        log(f"cliff calibration write failed: {exc}")
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        raise
    log(f"cliff calibration saved: floor={cal['floor_ref']} cliff={cal['cliff_ref']}")
    return cal


def load_cliff_calibration(state_dir: Path) -> dict | None:
    """Load a persisted cliff calibration. None if missing/corrupt/invalid.

    Staleness is a warning only — a stale calibration is still returned.
    A missing file is the normal first-run state and isn't logged; any other
    failure (corrupt JSON, malformed structure) is logged so a real bug
    doesn't silently and invisibly ground autonomous roaming (Task 6 gates on
    this return value).
    """
    path = state_dir / "wander_calibration.json"
    if not path.exists():
        return None
    try:
        cal = json.loads(path.read_text(encoding="utf-8"))
        ref = cal["cliff_ref"]
        # Finite positive numbers only. Python's json parser accepts NaN, and
        # every `reading <= nan` comparison is False — a NaN reference would
        # load "successfully" and silently disarm CliffGuard (fail-open).
        if not (isinstance(ref, list) and len(ref) == 3
                and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        and math.isfinite(v) and v > 0 for v in ref)):
            log(f"cliff calibration invalid (cliff_ref={ref!r}) — ignoring file")
            return None
        age = (dt.datetime.now(dt.timezone.utc)
               - dt.datetime.fromisoformat(cal["ts"])).total_seconds()
        if age > CALIBRATION_STALE_S:
            log(f"cliff calibration is stale ({age/86400:.0f} days old) — "
                "consider re-running --calibrate-cliff on the current floor")
        return cal
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        log(f"cliff calibration load failed: {exc}")
        return None


# Motor load couples transient spikes into the grayscale ADC: measured on
# this hardware 2026-08-06, idle reads sat at 346–364/589–624/386–413 (stable)
# but with motors running single samples dipped as low as 135/222/186 — below
# any usable cliff threshold — while the median over the same window stayed at
# floor level (433/678/435). A single-sample guard therefore trips on the
# first drive slice of every wander (steps_driven=0). A genuine cliff holds
# the sensor low across every sample, so a per-channel median still trips.
GUARD_SAMPLES = 3
GUARD_SAMPLE_GAP_S = 0.01
# Settle between the stationary confirm reads taken after a driving trip.
# Motor noise at FORWARD_SPEED=30 is dense enough that even medians dip below
# threshold (measured 2026-08-06: 16 of 56 in-motion median checks tripped),
# so a trip is confirmed or dismissed with the wheels stopped.
#
# One stationary read is not enough on its own. Run 8 aborted on a confirm of
# 257/79/79 taken on floor reading 718/540 in motion moments earlier and
# 400-900 on the probe minutes later. A settle ladder from 0s to 1.6s
# (bin/px-guard-probe, 36 post-stop reads) put the dips at ~3% of reads
# scattered across every rung — not concentrated at short delays — so a longer
# wait buys nothing and only repetition separates a transient from a drop.
GUARD_CONFIRM_SETTLE_S = 0.05
# A real cliff holds the sensor low indefinitely; a transient does not survive
# being asked three times.
GUARD_CONFIRM_READS = 3

# --- Creep confirm: telling a board gap from a drop by WIDTH, not depth ------
#
# Measured 2026-08-06 on the shed boards. A genuine drop (sensor overhanging a
# desk edge) reads 48. The widest floorboard gap in the same room reads 63.
# Those are not separable by any threshold, and no recalibration helps: both
# are the same physical thing, an absence of return. Depth is the wrong axis.
#
# Width is the right one. A board gap is ~10mm with solid floor on the far
# side; a cliff has no far side. The grayscale bar sits SENSOR_LEAD_MM ahead of
# the front tyre's contact patch, so when the sensor is over a void the wheels
# are still that far short of it — which buys enough room to nudge forward and
# ask whether the void ends.
#
# The cap is the safety property: CREEP_MAX_PULSES * CREEP_MM_PER_PULSE must
# stay well under SENSOR_LEAD_MM, so that even if every pulse reads void the
# tyre never reaches the edge. Pinned by
# test_creep_budget_cannot_reach_the_tyre_contact_patch.
CREEP_SPEED = 12
CREEP_PULSE_S = 0.04
# 10 pulses at (CREEP_SPEED, CREEP_PULSE_S) measured 37mm of travel by tape.
CREEP_MM_PER_PULSE = 3.7
# 5 pulses = 18.5mm: clears a 10mm gap with room over, and is half the lead.
CREEP_MAX_PULSES = 5
# Sensor bar to front tyre contact patch, measured by tape 2026-08-06.
SENSOR_LEAD_MM = 37.0


class CliffGuard:
    """Fail-closed cliff detector backed by a calibrated grayscale reference."""

    def __init__(self, cliff_ref: list[float]):
        self.cliff_ref = [float(v) for v in cliff_ref]
        self.edge_events = 0
        self.last_median: list[float] | None = None
        self.trip_channels: list[int] = []

    def check(self, px) -> str:
        """Return "clear" | "cliff" | "fail". An unreadable sensor ("fail")
        must be treated identically to "cliff" by callers — fail closed.

        Medians GUARD_SAMPLES reads to reject motor-noise spikes (see above).
        Partial sample sets still sense (a lone successful read decides);
        only a fully unreadable window returns "fail"."""
        samples = []
        for i in range(GUARD_SAMPLES):
            gs = safe_grayscale(px, retries=1)
            if gs is not None:
                samples.append(gs)
            if i + 1 < GUARD_SAMPLES and GUARD_SAMPLE_GAP_S:
                time.sleep(GUARD_SAMPLE_GAP_S)
        if not samples:
            log("cliff guard: grayscale read failed — treating as cliff (fail closed)")
            return "fail"
        med = [statistics.median(col) for col in zip(*samples)]
        # Remember the deciding numbers. Three separate live runs aborted with
        # steps_driven=0 and a log line that said only "tripped", leaving the
        # readings — the one thing that identifies WHICH channel and by how
        # much — to be re-derived by hand from the hardware afterwards.
        self.last_median = med
        if any(med[i] <= self.cliff_ref[i] for i in range(3)):
            self.trip_channels = [i for i in range(3) if med[i] <= self.cliff_ref[i]]
            return "cliff"
        self.trip_channels = []
        return "clear"

    def describe_last(self) -> str:
        """Readings vs thresholds for the most recent check, for logging."""
        if self.last_median is None:
            return "no reading"
        med = "/".join(f"{v:.0f}" for v in self.last_median)
        ref = "/".join(f"{v:.0f}" for v in self.cliff_ref)
        chans = ",".join("LCR"[i] for i in self.trip_channels) or "none"
        return f"median={med} ref={ref} tripped={chans}"


def creep_confirm(px, guard: CliffGuard) -> str:
    """Nudge forward in bounded increments to measure how WIDE a void is.

    Returns "clear" if floor came back within the creep budget (a crossable
    board gap) or "cliff" if the void outlasted it. Only ever called with the
    wheels stopped and a *stationary-confirmed* void underneath, never on a
    "fail" — an unreadable sensor must not be answered by driving forward.

    The total distance travelled is bounded by CREEP_MAX_PULSES, and that
    bound is what makes probing a possible cliff safe: the budget is half the
    sensor's lead over the tyres, so the wheels cannot reach the edge even in
    the worst case where every single pulse reads void.
    """
    for i in range(CREEP_MAX_PULSES):
        px.forward(CREEP_SPEED)
        time.sleep(CREEP_PULSE_S)
        px.stop()
        time.sleep(GUARD_CONFIRM_SETTLE_S)
        travelled = (i + 1) * CREEP_MM_PER_PULSE
        if guard.check(px) == "clear":
            log(f"cliff guard: void ended after {travelled:.0f}mm of creep — "
                f"board gap, not a drop ({guard.describe_last()})")
            return "clear"
    budget = CREEP_MAX_PULSES * CREEP_MM_PER_PULSE
    log(f"cliff guard: void persisted through {budget:.0f}mm of creep — "
        f"treating as a real drop ({guard.describe_last()})")
    return "cliff"


def bounded_reverse(px) -> bool:
    """Reverse for up to REVERSE_S at REVERSE_SPEED. Returns True if the
    escape stalled (forward sonar clearance grew less than REVERSE_STALL_CM)."""
    before = _read_sonar(px)
    px.backward(REVERSE_SPEED)
    time.sleep(REVERSE_S)
    px.stop()
    after = _read_sonar(px)
    # An unreadable sonar reading (before or after is None) deliberately
    # counts as "not stalled" here, even though this looks like it inverts
    # CliffGuard.check's fail-closed convention. The physical safety
    # guarantee doesn't come from this accounting — it comes from
    # guard.check() re-verifying grayscale before every slice, including
    # the very next one after this reverse. Failing closed here instead
    # would let routine sonar dropouts (common on soft/angled surfaces)
    # inflate edge_events and abort otherwise-fine wanders via
    # EDGE_ABORT_COUNT. Reviewed and intentionally kept as-is.
    if before is not None and after is not None:
        if (after - before) < REVERSE_STALL_CM:
            log(f"reverse stall: clearance {before:.0f}→{after:.0f}cm — edge-event equivalent")
            return True
    return False


def guarded_forward(px, guard: CliffGuard, speed: int, duration_s: float,
                     slice_s: float = 0.15) -> str:
    """Drive forward in slices, checking the cliff guard before every slice
    (including the first — a wander that starts at the desk edge never
    moves). Returns "ok" | "edge"."""
    remaining = duration_s
    while remaining > 0:
        status = guard.check(px)
        if status != "clear":
            in_motion = guard.describe_last()
            px.stop()
            # In-motion trips are ~29% motor-noise phantoms even after the
            # median (see GUARD_CONFIRM_SETTLE_S). The stop above is the
            # safety reaction and is unconditional; whether to reverse/abort
            # is decided by a stationary re-read, which noise cannot reach.
            # A real cliff keeps the sensor low with the wheels stopped.
            confirms = []
            for _ in range(GUARD_CONFIRM_READS):
                time.sleep(GUARD_CONFIRM_SETTLE_S)
                status = guard.check(px)
                confirms.append(guard.describe_last())
                if status == "clear":
                    break
            if status == "clear":
                log("cliff guard: in-motion trip dismissed by stationary re-read "
                    f"(transient) — in-motion {in_motion}, stationary "
                    + "; ".join(confirms))
                continue
            # A stationary-confirmed void is still ambiguous on a board floor:
            # gap 63 vs drop 48, indistinguishable by threshold. Measure its
            # width before spending an edge event on it. Only for "cliff" —
            # "fail" means the sensor is unreadable, and the answer to that is
            # never to drive forward.
            if status == "cliff" and creep_confirm(px, guard) == "clear":
                log("cliff guard: in-motion trip resolved as a crossable gap "
                    f"— in-motion {in_motion}, stationary " + "; ".join(confirms))
                continue
            log(f"cliff guard tripped ({status}) — stop + bounded reverse "
                f"— in-motion {in_motion}, stationary " + "; ".join(confirms))
            # Cliff + stalled escape deliberately counts as TWO edge events:
            # at EDGE_ABORT_COUNT=2 a single cornered-against-a-cliff moment
            # aborts the whole wander. That is the intended behavior (no
            # escape room = the abort case) — do not "fix" the double count
            # in either direction without a spec change.
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
            # Retrace the probe arc: reversing with the SAME steer angle
            # follows the same circular path backward (bicycle model:
            # heading rate = v/L * tan(steer); negating v alone undoes the
            # rotation). Reversing with the MIRRORED angle negates both
            # terms, which doubles the heading change instead of undoing it.
            px.set_dir_servo_angle(steer)
            stalled = bounded_reverse(px)
            px.set_dir_servo_angle(0)
            if stalled:
                guard.edge_events += 1
                return ("edge", 0.0)
    # Falling out of the loop means the chassis sits at the end of the SECOND
    # probe arc. If that side won, the label already matches — return as-is.
    if best[0] == order[1] and best[1] >= OBSTACLE_CM:
        return best
    # Otherwise (committing to the first side, or blocked) the honest-label
    # invariant requires physically recovering: arc back the same way as the
    # mid-probe recovery, then re-execute the first arc when committing.
    second_steer = -30 if order[1] == "left" else 30
    px.set_dir_servo_angle(second_steer)   # same-steer reverse retraces the arc
    stalled = bounded_reverse(px)
    px.set_dir_servo_angle(0)
    if stalled:
        guard.edge_events += 1
        return ("edge", 0.0)
    if best[1] < OBSTACLE_CM:
        return ("blocked", best[1])
    first_steer = -30 if best[0] == "left" else 30
    px.set_dir_servo_angle(first_steer)
    result = guarded_forward(px, guard, TURN_SPEED, PROBE_S)
    px.set_dir_servo_angle(0)
    if result != "ok":
        return ("edge", 0.0)
    return best


def run_explore_step(px, guard: CliffGuard, state: dict) -> dict:
    """One navigation step shared by both avoid and explore modes.

    `state` carries forced_turn/stuck_count/sensor_fail_streak/steps_completed/
    explore_id across calls — the caller owns and persists it between steps.
    Does no steering of its own: probe_turn already guarantees the chassis
    matches the label it returns.
    """
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Autonomous wander loop")
    parser.add_argument("--steps",    type=int,   default=int(os.environ.get("PX_WANDER_STEPS", "5")))
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--quiet",    action="store_true", help="Don't speak while wandering")
    parser.add_argument("--mode",     type=str,   default="avoid", choices=["avoid", "explore"])
    parser.add_argument("--duration", type=int,   default=180, help="Explore mode duration in seconds")
    parser.add_argument("--calibrate-cliff", action="store_true",
                         help="Read the floor's grayscale signature and persist a cliff calibration")
    parser.add_argument("--accumulate", action="store_true",
                         help="Fold this reading into the stored calibration, keeping the "
                              "per-channel minimum (calibrate several spots on a varied floor)")
    args = parser.parse_args(argv)

    mode   = args.mode
    steps  = int(clamp(args.steps, 1, 20))
    # PX_DRY=1 must mean dry everywhere, not only when the caller remembered
    # to translate it into --dry-run (tool-wander does; direct invocations
    # historically did not).
    dry    = args.dry_run or os.environ.get("PX_DRY", "0") != "0"
    quiet  = args.quiet or os.environ.get("PX_WANDER_QUIET", "0") != "0"
    duration = int(clamp(args.duration, 30, 300)) if mode == "explore" else 0

    if args.calibrate_cliff:
        if dry:
            print(json.dumps({"status": "ok", "dry": True}))
            return 0
        try:
            from picarx import Picarx
            px = Picarx()
            cal = calibrate_cliff(px, STATE_DIR, accumulate=args.accumulate)
            print(json.dumps({"status": "ok", **cal}))
            return 0
        except Exception as exc:
            log(f"calibrate-cliff error: {exc}")
            print(json.dumps({"status": "error", "error": str(exc)}))
            return 1

    cal = load_cliff_calibration(STATE_DIR)
    if not dry and cal is None:
        print(json.dumps({"status": "blocked",
            "reason": "cliff guard not calibrated — run px-wander --calibrate-cliff"}))
        return 2

    # Session motion gate applies to BOTH modes (explore re-checks it every
    # step via _check_abort, but avoid mode had no session check at all —
    # a direct live px-wander run must not bypass confirm_motion_allowed).
    if not dry and not _read_session().get("confirm_motion_allowed", False):
        print(json.dumps({"status": "blocked", "reason": "motion not confirmed safe"}))
        return 2

    px = None
    refresher = None
    gpio_guard = None

    if not dry:
        # Establish hardware ownership BEFORE constructing Picarx: yield_alive
        # already killed px-alive in the launcher, systemd relaunches it 15s
        # later. The lease is distinct from exploration state and can only be
        # refreshed or released by this generation's token.
        gpio_guard = GpioLeaseGuard(GpioLeaseStore(STATE_DIR), "wander")
        if not gpio_guard.acquire():
            log("abort: GPIO is leased by another live owner")
            print(json.dumps({"status": "error", "error": "GPIO already leased"}))
            return 1
        os.environ["PX_GPIO_LEASE_ID"] = gpio_guard.lease.lease_id
        started_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        if not _write_exploring_state(True, pid=os.getpid(), started=started_iso):
            gpio_guard.release()
            os.environ.pop("PX_GPIO_LEASE_ID", None)
            log("abort: cannot publish exploration state")
            print(json.dumps({"status": "error", "error": "exploring.json write failed"}))
            return 1
        refresher = _ExploringRefresher(os.getpid(), started_iso)
        refresher.start()

    if not dry:
        try:
            from picarx import Picarx
            px = Picarx()
            px.set_cliff_reference(cal["cliff_ref"])
        except Exception as exc:
            log(f"picarx import error: {exc}")
            print(json.dumps({"status": "error", "error": str(exc)}))
            refresher.stop()
            refresher.join(timeout=2)
            _write_exploring_state(False)
            gpio_guard.release()
            os.environ.pop("PX_GPIO_LEASE_ID", None)
            return 1

        # The ADC returns a power-on latch for ~0.75s after Picarx(). That latch
        # sits far ABOVE any calibrated cliff threshold, so a guard check taken
        # inside the window returns "clear" on fabricated data — which would
        # defeat guarded_forward's "never moves at the desk edge" guarantee on
        # the very first slice. Both loops reach their first guard check well
        # inside 0.75s, so block here until the sensor is demonstrably live.
        if wait_for_grayscale(px) is None:
            log("explore abort: grayscale ADC not live — refusing to move")
            print(json.dumps({"status": "blocked",
                "reason": "grayscale sensor not live — cannot guard against cliffs"}))
            refresher.stop()
            refresher.join(timeout=2)
            _write_exploring_state(False)
            gpio_guard.release()
            os.environ.pop("PX_GPIO_LEASE_ID", None)
            return 2

    guard = CliffGuard([0, 0, 0] if dry else cal["cliff_ref"])

    obstacles_avoided = 0
    steps_driven      = 0

    try:
        if mode == "avoid":
            log(f"wander start steps={steps} dry={dry}")

            nav_state = {"forced_turn": None, "stuck_count": 0, "sensor_fail_streak": 0,
                         "steps_completed": 0, "explore_id": "avoid"}

            for step in range(steps):
                log(f"step {step + 1}/{steps}")

                if dry:
                    log("dry: forward 1.2s")
                    time.sleep(0.1)
                    steps_driven += 1
                    continue

                nav_state["steps_completed"] = step + 1
                entry = run_explore_step(px, guard, nav_state)
                action = entry["action"]
                log(f"step action={action} sonar={entry['sonar_cm']}")

                if action == "sensor_fail":
                    log("sonar sensor failure — stopping")
                    break
                elif action == "forward":
                    steps_driven += 1
                    if not quiet and step == 0:
                        speak("Exploring!")
                else:
                    obstacles_avoided += 1
                    if action == "blocked":
                        log("blocked all around — reversing")
                        if not quiet:
                            speak("Hmm, I'm stuck. Backing up.")
                    elif action in ("turned_left", "turned_right"):
                        side = action.split("_", 1)[1]
                        log(f"obstacle ahead — turned {side}")
                        if not quiet:
                            speak(f"Something's in the way, going {side}.")
                    elif action == "edge_event":
                        log(f"cliff guard tripped during avoid step — {guard.describe_last()}")

            result = {
                "status": "ok",
                "steps": steps,
                "steps_driven": steps_driven,
                "obstacles_avoided": obstacles_avoided,
                "dry": dry,
            }
            log(f"wander complete driven={steps_driven} avoided={obstacles_avoided}")
            if not dry and not quiet and obstacles_avoided == 0:
                speak("All done!")
            print(json.dumps(result))
            return 0

        elif mode == "explore":
            signal.signal(signal.SIGTERM, _handle_sigterm)
            explore_id = f"e-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
            start_time = time.time()
            # Exploration state + GPIO lease are already established above.

            if not dry:
                # Dry runs must not consume the real 20-min explore cooldown
                # or inflate lifetime counters.
                meta = _load_exploration_meta()
                meta["last_explore_ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
                meta["total_explorations"] = meta.get("total_explorations", 0) + 1
                _save_exploration_meta(meta)

            log(f"explore start id={explore_id} duration={duration}s")
            if not quiet and not dry:
                speak("Time to explore!")

            nav_buffer = []
            observations = []
            last_photo_time = 0.0
            seen_labels = set()
            frigate_available = True
            frigate_warned = False
            vision_fail_streak = 0
            photos_disabled = False
            abort_reason = None
            explore_state = {"forced_turn": None, "stuck_count": 0, "sensor_fail_streak": 0,
                              "steps_completed": 0, "explore_id": explore_id}

            try:
                while True:
                    step_start = time.time()

                    # Abort check (FIRST)
                    session = _read_session()
                    battery = _read_battery()
                    abort_reason = _check_abort(session, battery, explore_state["stuck_count"],
                                                start_time, duration, guard.edge_events,
                                                explore_state["sensor_fail_streak"])
                    if abort_reason:
                        log(f"explore abort: {abort_reason}")
                        break

                    explore_state["steps_completed"] += 1
                    log(f"explore step {explore_state['steps_completed']}")

                    if dry:
                        log("dry: explore step (simulated)")
                        nav_entry = {
                            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                            "type": "nav",
                            "explore_id": explore_id,
                            "action": "forward",
                            "sonar_cm": 200.0,
                            "steps_from_start": explore_state["steps_completed"],
                            "frigate_labels": [],
                        }
                        nav_buffer.append(nav_entry)
                        if len(nav_buffer) >= FLUSH_INTERVAL:
                            _flush_nav_entries(nav_buffer, explore_id)
                            nav_buffer.clear()
                        time.sleep(0.1)
                        continue

                    entry = run_explore_step(px, guard, explore_state)
                    if entry["action"] == "forward":
                        steps_driven += 1
                    elif entry["action"] in ("blocked", "turned_left", "turned_right", "edge_event"):
                        obstacles_avoided += 1

                    # Frigate query (retry every FLUSH_INTERVAL steps after failure)
                    frigate_labels = []
                    if not frigate_available and explore_state["steps_completed"] % FLUSH_INTERVAL == 0:
                        detections = _query_frigate(after_epoch=start_time)
                        if detections is not None:
                            log("explore: Frigate back online")
                            frigate_available = True
                            frigate_labels = [d["label"] for d in detections]
                    elif frigate_available:
                        detections = _query_frigate(after_epoch=start_time)
                        if detections is None:
                            if not frigate_warned:
                                log("explore: Frigate offline — sonar-only navigation")
                                if not quiet:
                                    speak("My object detection is offline. I'll explore by sonar.")
                                frigate_warned = True
                            frigate_available = False
                        else:
                            frigate_labels = [d["label"] for d in detections]
                    entry["frigate_labels"] = frigate_labels

                    nav_buffer.append(entry)
                    if len(nav_buffer) >= FLUSH_INTERVAL:
                        _flush_nav_entries(nav_buffer, explore_id)
                        nav_buffer.clear()

                    # Curiosity trigger (photo) — only "new label" and "object < 100cm"
                    # triggers for now; heading-based triggers are gone with heading
                    # tracking (replaced by Task 11's directive-window logic).
                    now = time.time()
                    should_photo = False
                    photo_reason = ""

                    if not photos_disabled and not dry:
                        meta = _load_exploration_meta()
                        if not _check_daily_vision_cap(meta):
                            if not photos_disabled:
                                log("explore: daily vision cap reached — photos disabled")
                                photos_disabled = True
                        elif (now - last_photo_time) >= PHOTO_COOLDOWN_S:
                            new_labels = set(frigate_labels) - seen_labels
                            if new_labels:
                                should_photo = True
                                photo_reason = f"new label: {', '.join(new_labels)}"

                            if not should_photo:
                                sonar_cm = entry["sonar_cm"]
                                if sonar_cm is not None and sonar_cm < 100:
                                    should_photo = True
                                    photo_reason = f"object at {sonar_cm:.0f}cm"

                    if should_photo:
                        log(f"explore: photo trigger — {photo_reason}")
                        scene = _call_describe_scene(dry)
                        desc = scene.get("description", FALLBACK_DESCRIPTION)
                        vision_failed = (desc == FALLBACK_DESCRIPTION)

                        if vision_failed:
                            vision_fail_streak += 1
                            if vision_fail_streak >= VISION_FAIL_MAX:
                                log("explore: 3 consecutive vision failures — photos disabled")
                                photos_disabled = True
                        else:
                            vision_fail_streak = 0
                            last_photo_time = now
                            seen_labels.update(frigate_labels)

                            meta = _load_exploration_meta()
                            meta = _increment_vision_count(meta)
                            _save_exploration_meta(meta)

                        landmark = _extract_landmark(desc) if not vision_failed else ""
                        interesting = False
                        if not vision_failed:
                            if "person" in frigate_labels:
                                interesting = True
                            else:
                                prev_descs = " ".join(o.get("description", "") for o in observations)
                                desc_words = set(desc.lower().split()[:10])
                                prev_words = set(prev_descs.lower().split())
                                if len(desc_words - prev_words) > 3:
                                    interesting = True

                        obs_entry = {
                            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                            "type": "observation",
                            "explore_id": explore_id,
                            "sonar_cm": entry["sonar_cm"],
                            "frigate_labels": frigate_labels,
                            "description": desc,
                            "landmark": landmark,
                            "interesting": interesting,
                            "vision_failed": vision_failed,
                            "steps_from_start": explore_state["steps_completed"],
                        }
                        _write_observation(obs_entry)
                        observations.append(obs_entry)

                        # No speak() here: tool-describe-scene already voices
                        # the description via tool-voice — speaking it again
                        # onboard played every observation twice.

                        if interesting and not vision_failed:
                            _auto_remember(f"While exploring: {desc[:300]}")

                    elapsed = time.time() - step_start
                    if elapsed > EXPLORE_STEP_TIMEOUT:
                        log(f"explore: step took {elapsed:.1f}s (>{EXPLORE_STEP_TIMEOUT}s) — continuing")

                    time.sleep(0.2)

            except KeyboardInterrupt:
                abort_reason = "keyboard interrupt"
                log("explore: keyboard interrupt")

            # Flush remaining nav entries
            _flush_nav_entries(nav_buffer, explore_id)

            if abort_reason and not quiet and not dry and abort_reason != "time limit reached":
                speak(abort_reason)

            interesting_count = sum(1 for o in observations if o.get("interesting"))

            # Update meta with final counts (dry must not touch live meta)
            if not dry:
                meta = _load_exploration_meta()
                meta["total_observations"] = meta.get("total_observations", 0) + len(observations)
                _save_exploration_meta(meta)

            result = {
                "status": "ok",
                "mode": "explore",
                "explore_id": explore_id,
                "steps_completed": explore_state["steps_completed"],
                "steps_driven": steps_driven,
                "obstacles_avoided": obstacles_avoided,
                "observations": len(observations),
                "interesting": interesting_count,
                "frigate_available": frigate_available,
                "abort_reason": abort_reason,
                "dry": dry,
            }
            log(f"explore complete: {json.dumps(result)}")
            if not quiet and not dry and abort_reason == "time limit reached":
                speak("All done exploring!")
            print(json.dumps(result))
            return 0

    except KeyboardInterrupt:
        log("aborted")
        return 130
    except Exception as exc:
        log(f"runtime error: {exc}")
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1
    finally:
        if refresher is not None:
            refresher.stop()
            # Join before clearing the file so an in-flight refresh can't
            # resurrect active:true after the False write below.
            refresher.join(timeout=2)
        if not dry:
            _write_exploring_state(False)
            if gpio_guard is not None:
                gpio_guard.release()
                os.environ.pop("PX_GPIO_LEASE_ID", None)
        if px is not None:
            try:
                px.stop()
                px.set_dir_servo_angle(0)
                px.set_cam_pan_angle(0)
                px.set_cam_tilt_angle(0)
                px.close()
            except Exception as exc:
                log(f"motor cleanup failed ({exc}) — attempting I2C fallback")
                try:
                    import smbus2
                    bus = smbus2.SMBus(1)
                    bus.write_byte_data(0x40, 0xFD, 0x10)  # ALL_LED_OFF
                    bus.close()
                except Exception as exc2:
                    log(f"I2C fallback also failed: {exc2}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
