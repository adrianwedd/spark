"""Autonomous track racing with learning.

Two-phase system: map the track during practice, race with per-lap learning.
See docs/superpowers/specs/2026-03-19-px-race-design.md for full spec.
"""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

from pxh.utils import clamp
from pxh.time import utc_timestamp


class PDController:
    """Proportional-Derivative controller with output clamping."""

    def __init__(self, kp: float, kd: float, output_min: float = -30.0, output_max: float = 30.0):
        self.kp = kp
        self.kd = kd
        self.output_min = output_min
        self.output_max = output_max
        self._prev_error: float | None = None

    def update(self, error: float, dt: float) -> float:
        p = self.kp * error
        if self._prev_error is not None and dt > 0:
            d = self.kd * (error - self._prev_error) / dt
        else:
            d = 0.0
        self._prev_error = error
        return clamp(p + d, self.output_min, self.output_max)

    def reset(self) -> None:
        self._prev_error = None


def normalize_grayscale(readings: list[float], track_ref: list[float], barrier_ref: list[float]) -> list[float]:
    """Normalize grayscale readings to 0.0 (track) - 1.0 (barrier)."""
    result = []
    for raw, t, b in zip(readings, track_ref, barrier_ref):
        span = b - t
        if span == 0:
            result.append(0.0)
        else:
            result.append(clamp((raw - t) / span, 0.0, 1.0))
    return result


def compute_edge_error(gs_norm: list[float]) -> float:
    """Compute edge error from normalized grayscale. Positive = drifting right.

    This is an error signal for the PD controller, not a steering angle.
    The PD controller converts positive error -> negative steering (steer left).
    """
    return gs_norm[2] - gs_norm[0]


class GateDetector:
    """Detect start/finish gate from grayscale deltas.

    Triggers on 2-of-3 sensors showing delta > threshold.
    Temporal confirmation: if only 1 triggers, waits up to confirm_frames
    for a 2nd. Debounce prevents double-counting.
    """

    def __init__(self, threshold: float, debounce_s: float = 3.0, confirm_frames: int = 3):
        self.threshold = threshold
        self.debounce_s = debounce_s
        self.confirm_frames = confirm_frames
        self._last_trigger_t: float = -999.0
        self._pending_count: int = 0
        self._pending_frames: int = 0

    def update(self, prev_gs: list[float], gs: list[float], t: float) -> bool:
        """Check if gate was crossed. Returns True on detection."""
        if (t - self._last_trigger_t) < self.debounce_s:
            self._pending_count = 0
            self._pending_frames = 0
            return False

        triggered_this_frame = sum(
            1 for p, c in zip(prev_gs, gs) if abs(c - p) > self.threshold
        )

        if self._pending_count > 0:
            self._pending_frames += 1
            self._pending_count += triggered_this_frame

            if self._pending_count >= 2:
                self._last_trigger_t = t
                self._pending_count = 0
                self._pending_frames = 0
                return True

            if self._pending_frames > self.confirm_frames:
                self._pending_frames = 0
                self._pending_count = 0

        if triggered_this_frame >= 2:
            self._last_trigger_t = t
            self._pending_count = 0
            self._pending_frames = 0
            return True
        elif triggered_this_frame == 1:
            self._pending_count = 1
            self._pending_frames = 0

        return False


class TrackProfile:
    """Ordered list of track segments with persistence."""

    def __init__(self):
        self.segments: list[dict] = []
        self.map_speed: int = 20
        self.calibration_v: float = 0.0
        self.lap_duration_s: float = 0.0
        self.track_width_cm: float = 0.0
        self.lap_history: list[dict] = []

    def add_segment(self, seg_type: str, duration_s: float, width_left_cm: float,
                    width_right_cm: float, sonar_center_cm: float, gs_signature: list[float]) -> None:
        seg = {
            "id": len(self.segments),
            "type": seg_type,
            "duration_s": round(duration_s, 2),
            "width_left_cm": round(width_left_cm, 1),
            "width_right_cm": round(width_right_cm, 1),
            "sonar_center_cm": round(sonar_center_cm, 1),
            "race_speed": 28 if seg_type.startswith("turn") else 45,
            "steer_bias": 0,
            "entry_speed": 28 if seg_type.startswith("turn") else 45,
            "brake_before_s": 0.3 if seg_type.startswith("turn") else 0.0,
            "gs_signature": [round(v, 1) for v in gs_signature],
        }
        if seg_type.startswith("turn"):
            diff = width_right_cm - width_left_cm
            seg["steer_bias"] = round(clamp(diff * 0.5, -30, 30), 1)
        self.segments.append(seg)

    def save(self, path: Path) -> None:
        data = {
            "mapped_at": utc_timestamp(),
            "map_speed": self.map_speed,
            "calibration_v": self.calibration_v,
            "lap_duration_s": self.lap_duration_s,
            "track_width_cm": self.track_width_cm,
            "segments": self.segments,
            "lap_history": self.lap_history,
        }
        from pxh.state import atomic_write
        atomic_write(path, json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path) -> "TrackProfile":
        data = json.loads(path.read_text())
        tp = cls()
        tp.map_speed = data.get("map_speed", 20)
        tp.calibration_v = data.get("calibration_v", 0.0)
        tp.lap_duration_s = data.get("lap_duration_s", 0.0)
        tp.track_width_cm = data.get("track_width_cm", 0.0)
        tp.segments = data.get("segments", [])
        tp.lap_history = data.get("lap_history", [])
        return tp


def classify_segment(left_cm: float, right_cm: float, center_cm: float, track_width_cm: float) -> str:
    """Classify a sensor reading as straight, turn_left, or turn_right."""
    half_width = track_width_cm / 2
    threshold = half_width * 0.20
    imbalance = abs(left_cm - right_cm)
    center_close = center_cm < track_width_cm

    if imbalance > threshold and center_close:
        return "turn_left" if left_cm < right_cm else "turn_right"
    return "straight"


SERVO_SETTLE_S = 0.15  # 150ms — matches px-wander


def safe_ping(px, retries: int = 1) -> float | None:
    """Read sonar with I2C retry. Returns cm or None."""
    for attempt in range(1 + retries):
        try:
            return px.get_distance()
        except OSError:
            if attempt < retries:
                time.sleep(0.03)
    return None


def safe_grayscale(px, retries: int = 1) -> list[float] | None:
    """Read grayscale with I2C retry. Returns [left, center, right] or None."""
    for attempt in range(1 + retries):
        try:
            return px.get_grayscale_data()
        except OSError:
            if attempt < retries:
                time.sleep(0.01)
    return None


def quick3_scan(px, settle_s: float = SERVO_SETTLE_S) -> tuple[float | None, float | None]:
    """Scan sonar at -25, 0, +25 degrees. Returns (left_cm, right_cm)."""
    readings: dict[int, float | None] = {}
    for angle in (-25, 0, 25):
        px.set_cam_pan_angle(angle)
        if settle_s > 0:
            time.sleep(settle_s)
        readings[angle] = safe_ping(px)
    px.set_cam_pan_angle(0)
    return readings.get(-25), readings.get(25)


MIN_RACE_SPEED = 5
MAX_SPEED_DELTA = 5


def apply_lap_learning(segment: dict, actual: dict, speed_ratio: float) -> dict:
    """Apply per-lap learning to a segment. Returns updated segment copy."""
    seg = dict(segment)
    if speed_ratio > 0:
        seg["duration_s"] = round(actual["duration_s"] / speed_ratio, 2)
    if actual.get("obstacle"):
        return seg
    if actual["wall_clips"] > 0:
        seg["race_speed"] = max(MIN_RACE_SPEED, seg["race_speed"] - MAX_SPEED_DELTA)
        seg["entry_speed"] = min(seg["entry_speed"], seg["race_speed"])
        if seg.get("brake_before_s", 0) > 0:
            seg["brake_before_s"] = round(seg["brake_before_s"] + 0.1, 2)
    else:
        seg["race_speed"] = seg["race_speed"] + 3
        seg["entry_speed"] = seg["race_speed"]
    return seg


def estop_threshold(speed: float) -> float:
    """Speed-dependent e-stop distance in cm."""
    return max(8.0, speed * 0.3)


def check_estop(sonar_cm: float | None, speed: float) -> bool:
    """Check if emergency stop should trigger."""
    if sonar_cm is None:
        return True
    return sonar_cm < estop_threshold(speed)


def check_edge_guard(gs_norm: list[float], threshold: float = 0.7) -> tuple[bool, float]:
    """Check if any grayscale sensor is near barrier.
    Returns (triggered, steer_correction).
    """
    left, _center, right = gs_norm
    if left > threshold:
        return True, 15.0
    if right > threshold:
        return True, -15.0
    return False, 0.0


class StuckDetector:
    """Detect if the car is stuck (sonar unchanged for timeout_s)."""

    def __init__(self, timeout_s: float = 2.0, tolerance_cm: float = 3.0):
        self.timeout_s = timeout_s
        self.tolerance_cm = tolerance_cm
        self._last_change_t: float = 0.0
        self._last_cm: float | None = None

    def update(self, sonar_cm: float | None, t: float) -> None:
        if sonar_cm is None:
            return
        if self._last_cm is None or abs(sonar_cm - self._last_cm) > self.tolerance_cm:
            self._last_cm = sonar_cm
            self._last_change_t = t

    def is_stuck(self, t: float) -> bool:
        return (t - self._last_change_t) > self.timeout_s

    def reset(self) -> None:
        self._last_cm = None
        self._last_change_t = time.time()


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ─── RaceController constants ────────────────────────────────────────────────

DIR_MAX = 30
MAP_SPEED = 20
OBSTACLE_SPEED = 25
QUICK3_INTERVAL_S = 1.5
TRANSITION_ZONE_S = 0.3
LAP_TIMEOUT_S = 60.0
BATTERY_CACHE_S = 15.0
TELEMETRY_INTERVAL_S = 0.5


class RaceController:
    """Two-phase autonomous track racer: map then race with per-lap learning."""

    def __init__(self, px=None, state_dir: Path | None = None,
                 dry: bool = False, max_speed: int = 50,
                 install_signals: bool = True):
        self.px = px
        self.dry = dry
        self.max_speed = int(clamp(max_speed, MIN_RACE_SPEED, 60))

        if state_dir is None:
            project_root = Path(os.environ.get("PROJECT_ROOT",
                                               Path(__file__).resolve().parents[2]))
            state_dir = Path(os.environ.get("PX_STATE_DIR", project_root / "state"))
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.profile: TrackProfile | None = None
        self.calibration: dict = {}
        self._stop_flag = False

        # Battery cache
        self._battery_cache: dict | None = None
        self._battery_cache_t: float = 0.0

        # Load existing calibration if present
        cal_path = self.state_dir / "race_calibration.json"
        if cal_path.exists():
            try:
                self.calibration = json.loads(cal_path.read_text())
            except Exception:
                self.calibration = {}

        # Load existing profile if present
        profile_path = self.state_dir / "race_track.json"
        if profile_path.exists():
            try:
                self.profile = TrackProfile.load(profile_path)
            except Exception:
                self.profile = None

        # Install signal handlers (skip when used as a library, e.g. from API server)
        if install_signals:
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)

        # Mark as exploring so px-alive stays away (will be cleared in _handle_signal / run cleanup)
        self._set_exploring(True)

    # ─── Signal handling ──────────────────────────────────────────────────────

    def _handle_signal(self, signum, frame) -> None:
        self._stop_flag = True
        if self.px is not None and not self.dry:
            try:
                self.px.stop()
                self.px.set_dir_servo_angle(0)
            except Exception:
                pass
        self._set_exploring(False)

    # ─── Calibration ──────────────────────────────────────────────────────────

    def _calibrate_surface(self, name: str, px) -> None:
        """Read grayscale and store as calibration reference for surface `name`."""
        gs = safe_grayscale(px) if not self.dry else px.get_grayscale_data()
        if gs is None:
            gs = [0.0, 0.0, 0.0]
        self.calibration[f"{name}_ref"] = gs

    def save_calibration(self) -> None:
        """Persist calibration to state/race_calibration.json."""
        from pxh.state import atomic_write
        path = self.state_dir / "race_calibration.json"
        atomic_write(path, json.dumps(self.calibration, indent=2))

    # ─── State helpers ────────────────────────────────────────────────────────

    def _write_live_telemetry(self, data: dict) -> None:
        from pxh.state import atomic_write
        path = self.state_dir / "race_live.json"
        try:
            atomic_write(path, json.dumps(data, indent=2))
        except Exception:
            pass

    def _read_battery_voltage(self) -> float | None:
        """Return battery voltage, caching for BATTERY_CACHE_S seconds."""
        now = time.time()
        if (now - self._battery_cache_t) < BATTERY_CACHE_S and self._battery_cache is not None:
            return self._battery_cache.get("volts")
        path = self.state_dir / "battery.json"
        try:
            data = json.loads(path.read_text())
            self._battery_cache = data
            self._battery_cache_t = now
            return float(data.get("volts", 0.0))
        except Exception:
            return None

    def _set_exploring(self, active: bool) -> None:
        from pxh.state import atomic_write
        path = self.state_dir / "exploring.json"
        try:
            atomic_write(path, json.dumps({"active": active, "pid": os.getpid()}))
        except Exception:
            pass

    def _append_race_log(self, entry: dict) -> None:
        path = self.state_dir / "race_log.jsonl"
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    # ─── Mapping phase ────────────────────────────────────────────────────────

    def run_map(self, max_iterations: int = 0) -> None:
        """Mapping lap: drive at MAP_SPEED, sweep sonar, build TrackProfile."""
        if not self.calibration:
            raise RuntimeError("Calibration required before mapping. Run --calibrate first.")

        if not self.dry:
            try:
                from pxh.state import load_session
                sess = load_session()
                if not sess.get("confirm_motion_allowed", False):
                    raise RuntimeError("Motion not allowed — enable confirm_motion_allowed in session")
            except ImportError:
                pass

        self._set_exploring(True)

        track_ref = self.calibration.get("track_ref", [400, 410, 405])
        barrier_ref = self.calibration.get("barrier_ref", [700, 710, 705])
        track_width_cm = float(self.calibration.get("track_width_cm", 88))

        samples: list[dict] = []
        lap_start = time.time()
        gate_detector = GateDetector(
            threshold=self.calibration.get("gate_threshold", 50),
            debounce_s=3.0,
        )
        prev_gs = track_ref[:]
        iteration = 0

        while not self._stop_flag:
            iteration += 1
            if max_iterations and iteration > max_iterations:
                break

            now = time.time()

            # Sonar 3-point sweep
            if self.dry:
                left_cm = right_cm = center_cm = 90.0
                # Still call set_cam_pan_angle in dry mode via mock
                for angle in (-25, 0, 25):
                    self.px.set_cam_pan_angle(angle)
                self.px.set_cam_pan_angle(0)
            else:
                left_cm, right_cm = quick3_scan(self.px)
                center_cm = safe_ping(self.px)
                if center_cm is None:
                    center_cm = 90.0
                if left_cm is None:
                    left_cm = 90.0
                if right_cm is None:
                    right_cm = 90.0

            # Grayscale
            if self.dry:
                gs_raw = self.px.get_grayscale_data()
            else:
                gs_raw = safe_grayscale(self.px) or track_ref[:]

            seg_type = classify_segment(left_cm, right_cm, center_cm, track_width_cm)

            samples.append({
                "t": now - lap_start,
                "type": seg_type,
                "left_cm": left_cm,
                "right_cm": right_cm,
                "center_cm": center_cm,
                "gs_raw": gs_raw,
            })

            # Gate detection
            if gate_detector.update(prev_gs, gs_raw, now) and iteration > 3:
                # Lap complete — stop if max_iterations not driving us
                if not max_iterations:
                    break
            prev_gs = gs_raw[:]

            # Drive at MAP_SPEED (skip in dry mode)
            if not self.dry and self.px is not None:
                self.px.set_dir_servo_angle(0)
                self.px.forward(MAP_SPEED)
                time.sleep(0.1)

        if not self.dry and self.px is not None:
            self.px.stop()
            self.px.set_dir_servo_angle(0)

        lap_duration = time.time() - lap_start
        self.profile = self._compress_samples(samples, track_width_cm)
        self.profile.lap_duration_s = round(lap_duration, 2)
        self.profile.map_speed = MAP_SPEED
        self.profile.track_width_cm = track_width_cm

        profile_path = self.state_dir / "race_track.json"
        self.profile.save(profile_path)

    def _compress_samples(self, samples: list[dict], track_width_cm: float) -> TrackProfile:
        """Convert raw map samples into a TrackProfile with merged segments."""
        profile = TrackProfile()
        profile.track_width_cm = track_width_cm

        if not samples:
            return profile

        # Group consecutive samples of same type into segments
        seg_start_t = samples[0]["t"]
        seg_type = samples[0]["type"]
        seg_samples: list[dict] = [samples[0]]

        def flush_segment(s_type: str, s_list: list[dict], end_t: float) -> None:
            if not s_list:
                return
            dur = end_t - s_list[0]["t"]
            if dur < 0.05:
                dur = 0.05
            left_avg = _avg([s["left_cm"] for s in s_list])
            right_avg = _avg([s["right_cm"] for s in s_list])
            center_avg = _avg([s["center_cm"] for s in s_list])
            gs_sig = [_avg([s["gs_raw"][i] for s in s_list]) for i in range(3)]
            profile.add_segment(s_type, dur, left_avg, right_avg, center_avg, gs_sig)

        for sample in samples[1:]:
            if sample["type"] == seg_type:
                seg_samples.append(sample)
            else:
                flush_segment(seg_type, seg_samples, sample["t"])
                seg_type = sample["type"]
                seg_samples = [sample]

        if seg_samples:
            end_t = seg_samples[-1]["t"] + 0.1
            flush_segment(seg_type, seg_samples, end_t)

        return profile

    # ─── Race phase ───────────────────────────────────────────────────────────

    def run_race(self, max_laps: int = 0, max_iterations: int = 0) -> None:
        """Race loop with per-lap learning, safety checks, and telemetry."""
        if self.profile is None or not self.profile.segments:
            raise RuntimeError("No track profile loaded. Run --map first.")
        if not self.calibration:
            raise RuntimeError("Calibration required. Run --calibrate first.")

        if not self.dry:
            try:
                from pxh.state import load_session
                sess = load_session()
                if not sess.get("confirm_motion_allowed", False):
                    raise RuntimeError("Motion not allowed — enable confirm_motion_allowed in session")
            except ImportError:
                pass

        self._set_exploring(True)

        track_ref = self.calibration.get("track_ref", [400, 410, 405])
        barrier_ref = self.calibration.get("barrier_ref", [700, 710, 705])

        # PD controllers
        pd_edge = PDController(kp=-20.0, kd=-5.0, output_min=-DIR_MAX, output_max=DIR_MAX)
        pd_sonar = PDController(kp=0.5, kd=0.2, output_min=-DIR_MAX, output_max=DIR_MAX)
        stuck_detector = StuckDetector(timeout_s=2.0)
        gate_detector = GateDetector(
            threshold=self.calibration.get("gate_threshold", 50),
            debounce_s=3.0,
        )

        # Warmup: reset pan to 0
        if not self.dry and self.px is not None:
            self.px.set_cam_pan_angle(0)
            time.sleep(SERVO_SETTLE_S)

        segments = list(self.profile.segments)
        n_segs = len(segments)
        seg_idx = 0
        seg_enter_t = time.time()
        laps_completed = 0
        lap_start_t = time.time()
        prev_gs = track_ref[:]
        last_sonar_cm: float | None = None
        last_quick3_t = 0.0
        last_telemetry_t = 0.0
        sonar_blend_age = 0.0  # seconds since last quick3 scan
        sonar_correction: float = 0.0
        prev_loop_t = time.time()
        iteration = 0

        # Per-lap tracking
        lap_seg_actuals: list[dict] = []
        current_seg_actual: dict = {
            "duration_s": 0.0,
            "wall_clips": 0,
            "obstacle": False,
        }
        seg_enter_t_actual = time.time()
        finish_after_lap = False

        while not self._stop_flag:
            iteration += 1
            if max_iterations and iteration > max_iterations:
                break

            now = time.time()
            dt = max(now - prev_loop_t, 0.001)
            prev_loop_t = now

            # ── Grayscale read ────────────────────────────────────────────────
            if self.dry:
                gs_raw = self.px.get_grayscale_data()
            else:
                gs_raw = safe_grayscale(self.px) or prev_gs[:]

            gs_norm = normalize_grayscale(gs_raw, track_ref, barrier_ref)

            # ── Gate detection ────────────────────────────────────────────────
            if gate_detector.update(prev_gs, gs_raw, now) and iteration > 3:
                laps_completed += 1
                lap_dur = now - lap_start_t
                lap_start_t = now

                # Apply learning to all segments from this lap
                cal_v = self.calibration.get("calibration_v", 0.0)
                current_v = self._read_battery_voltage()
                if cal_v > 0 and current_v and current_v > 0:
                    speed_ratio = current_v / cal_v
                else:
                    speed_ratio = 1.0
                new_segments = []
                for i, (seg, actual) in enumerate(zip(segments, lap_seg_actuals)):
                    updated = apply_lap_learning(seg, actual, speed_ratio)
                    # Cap at max_speed
                    updated["race_speed"] = min(updated["race_speed"], self.max_speed)
                    updated["entry_speed"] = min(updated["entry_speed"], self.max_speed)
                    new_segments.append(updated)
                # Pad any unvisited segments
                for seg in segments[len(lap_seg_actuals):]:
                    new_segments.append(seg)
                segments = new_segments
                self.profile.segments = segments
                self.profile.lap_history.append({
                    "lap": laps_completed,
                    "duration_s": round(lap_dur, 2),
                })
                self.profile.save(self.state_dir / "race_track.json")

                self._append_race_log({
                    "event": "lap",
                    "lap": laps_completed,
                    "duration_s": round(lap_dur, 2),
                })

                lap_seg_actuals = []
                pd_edge.reset()
                pd_sonar.reset()

                if max_laps and laps_completed >= max_laps:
                    break

                if finish_after_lap:
                    break

            prev_gs = gs_raw[:]

            # ── Current segment ───────────────────────────────────────────────
            seg = segments[seg_idx % n_segs]
            seg_elapsed = now - seg_enter_t
            seg_duration = seg["duration_s"]
            target_speed = seg["race_speed"]

            # Transition blending: slow down entering a turn
            time_to_end = seg_duration - seg_elapsed
            if time_to_end < TRANSITION_ZONE_S and seg_elapsed < TRANSITION_ZONE_S:
                # Both entry and exit zones — just use target
                pass
            elif seg_elapsed < TRANSITION_ZONE_S:
                # Entry zone — ramp from entry_speed to race_speed
                alpha = seg_elapsed / TRANSITION_ZONE_S
                target_speed = seg["entry_speed"] + alpha * (seg["race_speed"] - seg["entry_speed"])
            elif time_to_end < TRANSITION_ZONE_S and time_to_end > 0:
                # Exit zone — ramp to next segment entry speed
                next_seg = segments[(seg_idx + 1) % n_segs]
                alpha = 1.0 - (time_to_end / TRANSITION_ZONE_S)
                target_speed = seg["race_speed"] + alpha * (next_seg["entry_speed"] - seg["race_speed"])

            target_speed = clamp(target_speed, MIN_RACE_SPEED, self.max_speed)

            # ── Sonar read + quick3 centering ─────────────────────────────────
            if self.dry:
                raw_sonar = self.px.get_distance()
            else:
                raw_sonar = safe_ping(self.px)

            last_sonar_cm = raw_sonar if raw_sonar is not None else last_sonar_cm
            stuck_detector.update(raw_sonar, now)

            # Quick3 periodic scan for sonar centering
            if (now - last_quick3_t) >= QUICK3_INTERVAL_S:
                if self.dry:
                    # In dry mode just mark that we scanned
                    sonar_blend_age = 0.0
                    left_cm = right_cm = 90.0
                    self.px.set_cam_pan_angle(-25)
                    self.px.set_cam_pan_angle(0)
                    self.px.set_cam_pan_angle(25)
                    self.px.set_cam_pan_angle(0)
                else:
                    left_cm, right_cm = quick3_scan(self.px)
                    if left_cm is None:
                        left_cm = 90.0
                    if right_cm is None:
                        right_cm = 90.0
                    sonar_blend_age = 0.0
                    center_error = right_cm - left_cm  # positive = closer on right = drift left
                    sonar_correction = pd_sonar.update(center_error, dt)
                last_quick3_t = now
            else:
                sonar_blend_age = now - last_quick3_t

            # Age-weighted blend: sonar correction decays to 0 over 2s
            sonar_weight = max(0.0, 1.0 - sonar_blend_age / 2.0)
            blended_sonar = sonar_correction * sonar_weight

            # ── Safety checks ─────────────────────────────────────────────────

            # E-stop
            if check_estop(last_sonar_cm, target_speed):
                if not self.dry and self.px is not None:
                    self.px.stop()
                current_seg_actual["obstacle"] = True
                self._append_race_log({"event": "estop", "sonar_cm": last_sonar_cm,
                                       "speed": target_speed, "iter": iteration})
                # Reverse 0.3s then continue at OBSTACLE_SPEED
                if not self.dry and self.px is not None:
                    self.px.backward(OBSTACLE_SPEED)
                time.sleep(0.3 if not self.dry else 0.01)
                if not self.dry and self.px is not None:
                    self.px.stop()
                target_speed = OBSTACLE_SPEED
                if not self.dry and self.px is not None:
                    self.px.forward(int(target_speed))

            # Stuck detection
            if stuck_detector.is_stuck(now) and not self.dry:
                if self.px is not None:
                    self.px.backward(OBSTACLE_SPEED)
                    time.sleep(0.5)
                    self.px.stop()
                stuck_detector.reset()  # reset reference
                self._append_race_log({"event": "stuck", "iter": iteration})

            # Edge guard
            edge_triggered, edge_correction = check_edge_guard(gs_norm)
            if edge_triggered:
                current_seg_actual["wall_clips"] = current_seg_actual.get("wall_clips", 0) + 1

            # Battery check
            volts = self._read_battery_voltage()
            if volts is not None and volts < 7.0:
                self._append_race_log({"event": "battery_low", "volts": volts})
                finish_after_lap = True

            # Lap timeout
            if (now - lap_start_t) > LAP_TIMEOUT_S:
                self._append_race_log({"event": "lap_timeout", "elapsed": now - lap_start_t})
                break

            # ── Steering computation ──────────────────────────────────────────

            edge_error = compute_edge_error(gs_norm)
            edge_steer = pd_edge.update(edge_error, dt)

            if edge_triggered:
                steer = edge_correction
            else:
                steer = edge_steer + blended_sonar + seg.get("steer_bias", 0)

            steer = clamp(steer, -DIR_MAX, DIR_MAX)

            # ── Actuate ───────────────────────────────────────────────────────
            if not self.dry and self.px is not None:
                self.px.set_dir_servo_angle(int(steer))
                self.px.forward(int(target_speed))

            # ── Segment advancement ───────────────────────────────────────────
            # Speed ratio: actual_speed / map_speed gives time compression factor
            speed_ratio = target_speed / MAP_SPEED if MAP_SPEED else 1.0
            effective_duration = seg_duration / speed_ratio if speed_ratio > 0 else seg_duration

            if seg_elapsed >= effective_duration:
                # Finalize actual for this segment
                current_seg_actual["duration_s"] = round(now - seg_enter_t_actual, 2)
                lap_seg_actuals.append(current_seg_actual)
                current_seg_actual = {"duration_s": 0.0, "wall_clips": 0, "obstacle": False}
                seg_enter_t_actual = now

                # Sonar signature matching: check if next segment makes sense
                seg_idx = (seg_idx + 1) % n_segs
                seg_enter_t = now
                seg = segments[seg_idx % n_segs]
                pd_edge.reset()

            # ── Live telemetry (every 0.5s) ───────────────────────────────────
            if (now - last_telemetry_t) >= TELEMETRY_INTERVAL_S:
                last_telemetry_t = now
                self._write_live_telemetry({
                    "ts": time.time(),
                    "ts_iso": utc_timestamp(),
                    "lap": laps_completed,
                    "seg_idx": seg_idx % n_segs,
                    "seg_type": seg.get("type"),
                    "speed": int(target_speed),
                    "steer": round(steer, 1),
                    "sonar_cm": last_sonar_cm,
                    "edge_error": round(edge_error, 3),
                    "edge_triggered": edge_triggered,
                    "dry": self.dry,
                })

        # ── Cleanup ───────────────────────────────────────────────────────────
        if finish_after_lap:
            self._append_race_log({"event": "battery_stop", "laps": laps_completed})
        if not self.dry and self.px is not None:
            self.px.stop()
            self.px.set_dir_servo_angle(0)
            self.px.set_cam_pan_angle(0)
        self._set_exploring(False)


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main(argv=None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Autonomous track racing")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--calibrate", action="store_true",
                            help="Interactive surface calibration")
    mode_group.add_argument("--map", action="store_true",
                            help="Run mapping lap to build track profile")
    mode_group.add_argument("--race", action="store_true",
                            help="Race using saved track profile")
    mode_group.add_argument("--status", action="store_true",
                            help="Print profile summary and exit")
    parser.add_argument("--laps", type=int, default=0,
                        help="Number of laps to race (0 = unlimited)")
    parser.add_argument("--max-speed", type=int, default=50,
                        help="Maximum race speed (default 50, cap 60)")
    parser.add_argument("--max-iterations", type=int, default=0,
                        help="Hard iteration cap for map/race (0 = unlimited; for testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Use mock hardware; skip motion and I2C")

    if argv is None:
        argv = sys.argv[1:]
    args = parser.parse_args(argv)

    max_speed = int(clamp(args.max_speed, MIN_RACE_SPEED, 60))
    dry = args.dry_run

    # Resolve state dir
    project_root = Path(os.environ.get("PROJECT_ROOT",
                                       Path(__file__).resolve().parents[2]))
    state_dir = Path(os.environ.get("PX_STATE_DIR", project_root / "state"))

    # ── --status ──────────────────────────────────────────────────────────────
    if args.status:
        profile_path = state_dir / "race_track.json"
        if not profile_path.exists():
            print("No profile found — run --map first.")
            return 0
        tp = TrackProfile.load(profile_path)
        print(f"Track profile: {len(tp.segments)} segments, "
              f"lap_duration={tp.lap_duration_s}s, "
              f"track_width={tp.track_width_cm}cm")
        for seg in tp.segments:
            print(f"  [{seg['id']}] {seg['type']:12s} dur={seg['duration_s']}s "
                  f"speed={seg['race_speed']} steer_bias={seg['steer_bias']}")
        laps = tp.lap_history
        if laps:
            print(f"Lap history ({len(laps)} laps):")
            for lh in laps[-5:]:
                print(f"  lap {lh['lap']}: {lh['duration_s']}s")
        return 0

    # ── Live hardware setup ───────────────────────────────────────────────────
    if dry:
        from unittest.mock import MagicMock
        px = MagicMock()
        px.get_distance.return_value = 90.0
        px.get_grayscale_data.return_value = [400, 410, 405]
    else:
        try:
            from picarx import Picarx
            px = Picarx()
        except Exception as exc:
            print(f"ERROR: cannot init Picarx: {exc}", file=sys.stderr)
            return 1

    rc = RaceController(px=px, state_dir=state_dir, dry=dry, max_speed=max_speed)

    try:
        # ── --calibrate ───────────────────────────────────────────────────────
        if args.calibrate:
            print("=== Surface Calibration ===")
            print("Place the car on the track surface.")
            input("Press Enter when ready to sample track surface... ")
            rc._calibrate_surface("track", px)
            print(f"Track ref: {rc.calibration.get('track_ref')}")

            print("Place the car on the barrier/wall surface (or tape).")
            input("Press Enter when ready to sample barrier surface... ")
            rc._calibrate_surface("barrier", px)
            print(f"Barrier ref: {rc.calibration.get('barrier_ref')}")

            track_ref = rc.calibration.get("track_ref", [400, 400, 400])
            barrier_ref = rc.calibration.get("barrier_ref", [700, 700, 700])
            avg_track = _avg(track_ref)
            avg_barrier = _avg(barrier_ref)
            rc.calibration["gate_threshold"] = round(abs(avg_barrier - avg_track) * 0.4, 1)

            try:
                width_str = input("Track width in cm (default 88): ").strip()
                rc.calibration["track_width_cm"] = float(width_str) if width_str else 88.0
            except ValueError:
                rc.calibration["track_width_cm"] = 88.0

            # Capture battery voltage for compensation
            volts = None
            battery_path = state_dir / "battery.json"
            try:
                bdata = json.loads(battery_path.read_text())
                volts = float(bdata.get("volts", 0.0))
            except Exception:
                pass
            if volts and volts > 0:
                rc.calibration["calibration_v"] = round(volts, 2)

            rc.save_calibration()
            print(f"Calibration saved to {state_dir / 'race_calibration.json'}")
            return 0

        # ── --map ─────────────────────────────────────────────────────────────
        elif args.map:
            print("Starting mapping lap...")
            rc.run_map(max_iterations=args.max_iterations)
            if rc.profile:
                print(f"Mapping complete: {len(rc.profile.segments)} segments, "
                      f"lap={rc.profile.lap_duration_s}s")
            return 0

        # ── --race ────────────────────────────────────────────────────────────
        elif args.race:
            laps = max(0, args.laps)
            print(f"Starting race: laps={'unlimited' if laps == 0 else laps}, "
                  f"max_speed={max_speed}")
            rc.run_race(max_laps=laps, max_iterations=args.max_iterations)
            print("Race complete.")
            return 0

        else:
            parser.print_help()
            return 1

    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    finally:
        rc._set_exploring(False)
        if not dry and px is not None:
            try:
                px.stop()
                px.set_dir_servo_angle(0)
                px.set_cam_pan_angle(0)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
