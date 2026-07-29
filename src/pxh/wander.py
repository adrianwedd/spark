"""Autonomous wander: drive forward, sweep for best direction, avoid obstacles.

Uses the Picarx handle directly for sonar — no subprocess, no GPIO conflict.
Speaks while exploring if espeak is available and a persona is set.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

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
    """Speak text via espeak if available. Non-blocking (fire and forget)."""
    if not shutil.which("espeak"):
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
        if shutil.which("aplay"):
            cmd = ["aplay", "-q"]
            if device:
                cmd += ["-D", device]
            aplay_env = os.environ.copy()
            aplay_env["PULSE_SERVER"] = "unix:/run/user/1000/pulse/native"
            ap = subprocess.Popen(cmd, stdin=es.stdout, stderr=subprocess.DEVNULL, env=aplay_env)
            es.stdout.close()
            ap.wait()
        else:
            es.wait()
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


def _read_sonar(px) -> float | None:
    try:
        d = px.get_distance()
        if d is None or d < 0:
            return None
        return float(d)
    except Exception:
        return None


def _query_frigate() -> list[dict] | None:
    url = f"{FRIGATE_HOST}/api/events?cameras={FRIGATE_CAMERA}&limit=5&min_score=0.5"
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
    try:
        result = subprocess.run(
            [str(BIN_DIR / "tool-describe-scene")],
            capture_output=True, text=True, check=False,
            env=env, timeout=60,
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


def calibrate_cliff(px, state_dir: Path) -> dict:
    """Read the floor's grayscale signature and persist a cliff calibration.

    Raises RuntimeError if the grayscale sensor can't be read.
    """
    gs = safe_grayscale(px, retries=2)
    if gs is None:
        raise RuntimeError("grayscale read failed — cannot calibrate")
    cal = {
        "floor_ref": [float(v) for v in gs],
        "cliff_ref": [round(float(v) * CLIFF_MARGIN, 1) for v in gs],
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
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
        if not (isinstance(ref, list) and len(ref) == 3):
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


class CliffGuard:
    """Fail-closed cliff detector backed by a calibrated grayscale reference."""

    def __init__(self, cliff_ref: list[float]):
        self.cliff_ref = [float(v) for v in cliff_ref]
        self.edge_events = 0

    def check(self, px) -> str:
        """Return "clear" | "cliff" | "fail". An unreadable sensor ("fail")
        must be treated identically to "cliff" by callers — fail closed."""
        gs = safe_grayscale(px, retries=1)
        if gs is None:
            log("cliff guard: grayscale read failed — treating as cliff (fail closed)")
            return "fail"
        if any(gs[i] <= self.cliff_ref[i] for i in range(3)):
            return "cliff"
        return "clear"


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
            px.stop()
            log(f"cliff guard tripped ({status}) — stop + bounded reverse")
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
            px.set_dir_servo_angle(-steer)
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
    px.set_dir_servo_angle(-second_steer)
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
    args = parser.parse_args(argv)

    mode   = args.mode
    steps  = int(clamp(args.steps, 1, 20))
    dry    = args.dry_run
    quiet  = args.quiet or os.environ.get("PX_WANDER_QUIET", "0") != "0"
    duration = int(clamp(args.duration, 30, 300)) if mode == "explore" else 0

    if args.calibrate_cliff:
        if dry:
            print(json.dumps({"status": "ok", "dry": True}))
            return 0
        try:
            from picarx import Picarx
            px = Picarx()
            cal = calibrate_cliff(px, STATE_DIR)
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

    px = None

    if not dry:
        try:
            from picarx import Picarx
            px = Picarx()
            px.set_cliff_reference(cal["cliff_ref"])
        except Exception as exc:
            log(f"picarx import error: {exc}")
            print(json.dumps({"status": "error", "error": str(exc)}))
            return 1

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
                        log("cliff guard tripped during avoid step")

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
            if not _write_exploring_state(True, pid=os.getpid(),
                                          started=dt.datetime.now(dt.timezone.utc).isoformat()):
                log("explore abort: cannot write exploring.json — px-alive coordination broken")
                print(json.dumps({"status": "error", "error": "exploring.json write failed"}))
                return 1

            meta = _load_exploration_meta()
            meta["last_explore_ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
            meta["total_explorations"] = meta.get("total_explorations", 0) + 1
            _save_exploration_meta(meta)

            log(f"explore start id={explore_id} duration={duration}s")
            if not quiet:
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
                        detections = _query_frigate()
                        if detections is not None:
                            log("explore: Frigate back online")
                            frigate_available = True
                            frigate_labels = [d["label"] for d in detections]
                    elif frigate_available:
                        detections = _query_frigate()
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

                        if not vision_failed and not quiet:
                            speak(desc[:200])

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

            if abort_reason and not quiet and abort_reason != "time limit reached":
                speak(abort_reason)

            interesting_count = sum(1 for o in observations if o.get("interesting"))

            # Update meta with final counts
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
            if not quiet and abort_reason == "time limit reached":
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
        _write_exploring_state(False)
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
