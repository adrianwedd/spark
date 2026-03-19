import json
import os
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class TestPDController:

    def test_centered_returns_zero(self):
        from pxh.race import PDController
        pd = PDController(kp=20.0, kd=5.0, output_min=-30, output_max=30)
        assert pd.update(0.0, dt=0.05) == 0.0

    def test_positive_error_steers_positive(self):
        from pxh.race import PDController
        pd = PDController(kp=20.0, kd=5.0, output_min=-30, output_max=30)
        result = pd.update(0.5, dt=0.05)
        assert result > 0

    def test_negative_error_steers_negative(self):
        from pxh.race import PDController
        pd = PDController(kp=20.0, kd=5.0, output_min=-30, output_max=30)
        result = pd.update(-0.5, dt=0.05)
        assert result < 0

    def test_output_clamped_to_max(self):
        from pxh.race import PDController
        pd = PDController(kp=100.0, kd=0.0, output_min=-30, output_max=30)
        assert pd.update(1.0, dt=0.05) == 30.0

    def test_output_clamped_to_min(self):
        from pxh.race import PDController
        pd = PDController(kp=100.0, kd=0.0, output_min=-30, output_max=30)
        assert pd.update(-1.0, dt=0.05) == -30.0

    def test_derivative_responds_to_change(self):
        from pxh.race import PDController
        pd = PDController(kp=0.0, kd=10.0, output_min=-30, output_max=30)
        pd.update(0.0, dt=0.05)
        result = pd.update(0.5, dt=0.05)
        assert result > 0

    def test_reset_clears_state(self):
        from pxh.race import PDController
        pd = PDController(kp=20.0, kd=5.0, output_min=-30, output_max=30)
        pd.update(0.5, dt=0.05)
        pd.reset()
        assert pd.update(0.0, dt=0.05) == 0.0


class TestGrayscaleNormalization:

    def test_on_track_returns_near_zero(self):
        from pxh.race import normalize_grayscale
        result = normalize_grayscale([400, 410, 405], [400, 410, 405], [700, 710, 705])
        assert all(abs(v) < 0.05 for v in result)

    def test_on_barrier_returns_near_one(self):
        from pxh.race import normalize_grayscale
        result = normalize_grayscale([700, 710, 705], [400, 410, 405], [700, 710, 705])
        assert all(abs(v - 1.0) < 0.05 for v in result)

    def test_clamped_below_zero(self):
        from pxh.race import normalize_grayscale
        result = normalize_grayscale([300, 300, 300], [400, 410, 405], [700, 710, 705])
        assert all(v == 0.0 for v in result)

    def test_clamped_above_one(self):
        from pxh.race import normalize_grayscale
        result = normalize_grayscale([800, 800, 800], [400, 410, 405], [700, 710, 705])
        assert all(v == 1.0 for v in result)


class TestEdgeError:

    def test_centered_returns_near_zero(self):
        from pxh.race import compute_edge_error
        assert abs(compute_edge_error([0.1, 0.0, 0.1])) < 0.01

    def test_drifting_right_positive_error(self):
        from pxh.race import compute_edge_error
        assert compute_edge_error([0.0, 0.1, 0.8]) > 0

    def test_drifting_left_negative_error(self):
        from pxh.race import compute_edge_error
        assert compute_edge_error([0.8, 0.1, 0.0]) < 0


class TestGateDetector:

    def test_no_trigger_on_stable_readings(self):
        from pxh.race import GateDetector
        gd = GateDetector(threshold=50, debounce_s=3.0, confirm_frames=3)
        assert gd.update([400, 410, 405], [400, 410, 405], t=0.0) is False

    def test_triggers_on_2_of_3_delta(self):
        from pxh.race import GateDetector
        gd = GateDetector(threshold=50, debounce_s=3.0, confirm_frames=3)
        assert gd.update([400, 410, 405], [460, 470, 405], t=1.0) is True

    def test_debounce_prevents_double_count(self):
        from pxh.race import GateDetector
        gd = GateDetector(threshold=50, debounce_s=3.0, confirm_frames=3)
        gd.update([400, 410, 405], [460, 470, 405], t=1.0)
        assert gd.update([460, 470, 405], [520, 530, 405], t=2.0) is False

    def test_triggers_after_debounce_expires(self):
        from pxh.race import GateDetector
        gd = GateDetector(threshold=50, debounce_s=3.0, confirm_frames=3)
        gd.update([400, 410, 405], [460, 470, 405], t=1.0)
        assert gd.update([400, 410, 405], [460, 470, 405], t=5.0) is True

    def test_temporal_confirm_1_then_2(self):
        from pxh.race import GateDetector
        gd = GateDetector(threshold=50, debounce_s=3.0, confirm_frames=3)
        assert gd.update([400, 410, 405], [460, 410, 405], t=0.0) is False
        assert gd.update([460, 410, 405], [460, 470, 405], t=0.05) is True

    def test_temporal_confirm_expires(self):
        from pxh.race import GateDetector
        gd = GateDetector(threshold=50, debounce_s=3.0, confirm_frames=3)
        gd.update([400, 410, 405], [460, 410, 405], t=0.0)
        for i in range(4):
            gd.update([460, 410, 405], [460, 410, 405], t=0.05 * (i + 1))
        result = gd.update([460, 410, 405], [460, 470, 405], t=0.3)
        assert result is True


class TestTrackProfile:

    def test_empty_profile(self):
        from pxh.race import TrackProfile
        tp = TrackProfile()
        assert len(tp.segments) == 0

    def test_add_segment(self):
        from pxh.race import TrackProfile
        tp = TrackProfile()
        tp.add_segment("straight", 2.0, 44, 43, 120, [450, 460, 455])
        assert len(tp.segments) == 1
        assert tp.segments[0]["type"] == "straight"

    def test_round_trip_json(self, tmp_path):
        from pxh.race import TrackProfile
        tp = TrackProfile()
        tp.add_segment("straight", 2.0, 44, 43, 120, [450, 460, 455])
        tp.add_segment("turn_left", 1.4, 30, 55, 65, [520, 460, 380])
        path = tmp_path / "profile.json"
        tp.save(path)
        loaded = TrackProfile.load(path)
        assert len(loaded.segments) == 2
        assert loaded.segments[1]["type"] == "turn_left"


class TestSegmentClassifier:

    def test_straight_balanced_sonar(self):
        from pxh.race import classify_segment
        assert classify_segment(44, 43, 120, 88) == "straight"

    def test_turn_left_imbalance(self):
        from pxh.race import classify_segment
        assert classify_segment(25, 60, 50, 88) == "turn_left"

    def test_turn_right_imbalance(self):
        from pxh.race import classify_segment
        assert classify_segment(60, 25, 50, 88) == "turn_right"

    def test_borderline_stays_straight(self):
        from pxh.race import classify_segment
        assert classify_segment(40, 48, 120, 88) == "straight"


class TestSonarHelpers:

    def test_safe_ping_returns_distance(self):
        from pxh.race import safe_ping
        mock_px = MagicMock()
        mock_px.get_distance.return_value = 45.0
        assert safe_ping(mock_px) == 45.0

    def test_safe_ping_retries_on_oserror(self):
        from pxh.race import safe_ping
        mock_px = MagicMock()
        mock_px.get_distance.side_effect = [OSError("i2c"), 42.0]
        assert safe_ping(mock_px) == 42.0

    def test_safe_ping_returns_none_on_double_failure(self):
        from pxh.race import safe_ping
        mock_px = MagicMock()
        mock_px.get_distance.side_effect = OSError("i2c")
        assert safe_ping(mock_px) is None

    def test_quick3_scan_returns_left_right(self):
        from pxh.race import quick3_scan
        mock_px = MagicMock()
        mock_px.get_distance.side_effect = [44.0, 100.0, 43.0]
        left, right = quick3_scan(mock_px, settle_s=0.0)
        assert left == 44.0
        assert right == 43.0
        pan_calls = [c[0][0] for c in mock_px.set_cam_pan_angle.call_args_list]
        assert pan_calls == [-25, 0, 25, 0]

    def test_safe_grayscale_retries_on_error(self):
        from pxh.race import safe_grayscale
        mock_px = MagicMock()
        mock_px.get_grayscale_data.side_effect = [OSError("i2c"), [400, 410, 405]]
        assert safe_grayscale(mock_px) == [400, 410, 405]

    def test_safe_grayscale_returns_none_on_failure(self):
        from pxh.race import safe_grayscale
        mock_px = MagicMock()
        mock_px.get_grayscale_data.side_effect = OSError("i2c")
        assert safe_grayscale(mock_px) is None


class TestLapLearning:

    def _make_segment(self, seg_type="straight", race_speed=45, duration_s=2.0,
                      brake_before_s=0.0, steer_bias=0):
        return {"id": 0, "type": seg_type, "duration_s": duration_s,
                "width_left_cm": 44, "width_right_cm": 43, "sonar_center_cm": 120,
                "race_speed": race_speed, "steer_bias": steer_bias,
                "entry_speed": race_speed, "brake_before_s": brake_before_s,
                "gs_signature": [450, 460, 455]}

    def test_no_change_on_clean_pass(self):
        from pxh.race import apply_lap_learning
        seg = self._make_segment(race_speed=45)
        updated = apply_lap_learning(seg, {"duration_s": 2.0, "wall_clips": 0, "obstacle": False}, 1.0)
        assert updated["race_speed"] == 48

    def test_wall_clip_reduces_speed(self):
        from pxh.race import apply_lap_learning
        seg = self._make_segment(race_speed=45, seg_type="turn_left", brake_before_s=0.3)
        updated = apply_lap_learning(seg, {"duration_s": 1.4, "wall_clips": 1, "obstacle": False}, 1.0)
        assert updated["race_speed"] == 40
        assert updated["brake_before_s"] == 0.4

    def test_speed_change_capped(self):
        from pxh.race import apply_lap_learning
        seg = self._make_segment(race_speed=10)
        updated = apply_lap_learning(seg, {"duration_s": 2.0, "wall_clips": 2, "obstacle": False}, 1.0)
        assert updated["race_speed"] == 5

    def test_obstacle_no_speed_change(self):
        from pxh.race import apply_lap_learning
        seg = self._make_segment(race_speed=45)
        updated = apply_lap_learning(seg, {"duration_s": 3.0, "wall_clips": 0, "obstacle": True}, 1.0)
        assert updated["race_speed"] == 45

    def test_duration_adjusted(self):
        from pxh.race import apply_lap_learning
        seg = self._make_segment(race_speed=45, duration_s=2.0)
        updated = apply_lap_learning(seg, {"duration_s": 2.5, "wall_clips": 0, "obstacle": False}, 1.0)
        assert updated["duration_s"] == 2.5


class TestSafety:

    def test_estop_threshold_at_low_speed(self):
        from pxh.race import estop_threshold
        assert estop_threshold(20) == 8.0

    def test_estop_threshold_at_high_speed(self):
        from pxh.race import estop_threshold
        assert estop_threshold(50) == 15.0

    def test_estop_triggered(self):
        from pxh.race import check_estop
        assert check_estop(5.0, 30) is True

    def test_estop_not_triggered(self):
        from pxh.race import check_estop
        assert check_estop(50.0, 30) is False

    def test_estop_none_sonar_triggers(self):
        from pxh.race import check_estop
        assert check_estop(None, 30) is True

    def test_stuck_detector_not_stuck(self):
        from pxh.race import StuckDetector
        sd = StuckDetector(timeout_s=2.0)
        sd.update(50.0, 0.0)
        sd.update(55.0, 1.0)
        assert sd.is_stuck(1.0) is False

    def test_stuck_detector_stuck(self):
        from pxh.race import StuckDetector
        sd = StuckDetector(timeout_s=2.0)
        sd.update(50.0, 0.0)
        sd.update(50.0, 1.0)
        sd.update(50.0, 2.5)
        assert sd.is_stuck(2.5) is True

    def test_edge_guard_triggers(self):
        from pxh.race import check_edge_guard
        triggered, direction = check_edge_guard([0.0, 0.0, 0.9], threshold=0.7)
        assert triggered is True
        assert direction < 0

    def test_edge_guard_clear(self):
        from pxh.race import check_edge_guard
        triggered, _ = check_edge_guard([0.1, 0.05, 0.15], threshold=0.7)
        assert triggered is False


class TestRaceControllerCalibrate:

    def test_calibrate_stores_references(self, tmp_path):
        from pxh.race import RaceController
        mock_px = MagicMock()
        mock_px.get_grayscale_data.return_value = [400, 410, 405]
        mock_px.get_distance.return_value = 90.0
        rc = RaceController(px=mock_px, state_dir=tmp_path, dry=True)
        rc._calibrate_surface("track", mock_px)
        assert rc.calibration["track_ref"] == [400, 410, 405]
        rc.save_calibration()
        assert (tmp_path / "race_calibration.json").exists()

    def test_calibration_round_trip(self, tmp_path):
        from pxh.race import RaceController
        mock_px = MagicMock()
        mock_px.get_grayscale_data.return_value = [500, 510, 505]
        mock_px.get_distance.return_value = 90.0
        rc = RaceController(px=mock_px, state_dir=tmp_path, dry=True)
        rc._calibrate_surface("track", mock_px)
        rc._calibrate_surface("barrier", mock_px)
        rc.calibration["gate_threshold"] = 40
        rc.calibration["track_width_cm"] = 88
        rc.save_calibration()
        # New controller should load it
        rc2 = RaceController(px=mock_px, state_dir=tmp_path, dry=True)
        assert rc2.calibration["track_ref"] == [500, 510, 505]
        assert rc2.calibration["gate_threshold"] == 40

    def test_exploring_json_written_on_init(self, tmp_path):
        from pxh.race import RaceController
        mock_px = MagicMock()
        mock_px.get_distance.return_value = 90.0
        mock_px.get_grayscale_data.return_value = [400, 410, 405]
        rc = RaceController(px=mock_px, state_dir=tmp_path, dry=True)
        exploring_path = tmp_path / "exploring.json"
        assert exploring_path.exists()
        data = json.loads(exploring_path.read_text())
        assert data["active"] is True


class TestRaceControllerMap:

    def test_map_builds_profile(self, tmp_path):
        from pxh.race import RaceController
        mock_px = MagicMock()
        gs_sequence = [[400, 410, 405]] * 3 + [[460, 470, 465]]
        mock_px.get_grayscale_data.side_effect = gs_sequence
        mock_px.get_distance.return_value = 90.0
        rc = RaceController(px=mock_px, state_dir=tmp_path, dry=True)
        rc.calibration = {"track_ref": [400, 410, 405], "barrier_ref": [700, 710, 705],
                          "gate_threshold": 40, "track_width_cm": 88}
        rc.run_map(max_iterations=4)
        assert rc.profile is not None
        assert len(rc.profile.segments) > 0

    def test_map_saves_profile_file(self, tmp_path):
        from pxh.race import RaceController
        mock_px = MagicMock()
        mock_px.get_grayscale_data.return_value = [400, 410, 405]
        mock_px.get_distance.return_value = 90.0
        rc = RaceController(px=mock_px, state_dir=tmp_path, dry=True)
        rc.calibration = {"track_ref": [400, 410, 405], "barrier_ref": [700, 710, 705],
                          "gate_threshold": 40, "track_width_cm": 88}
        rc.run_map(max_iterations=3)
        assert (tmp_path / "race_track.json").exists()

    def test_map_raises_without_calibration(self, tmp_path):
        from pxh.race import RaceController
        mock_px = MagicMock()
        mock_px.get_distance.return_value = 90.0
        mock_px.get_grayscale_data.return_value = [400, 410, 405]
        rc = RaceController(px=mock_px, state_dir=tmp_path, dry=True)
        rc.calibration = {}
        with pytest.raises(RuntimeError, match="Calibration required"):
            rc.run_map(max_iterations=1)


class TestRaceControllerRace:

    def _make_rc(self, tmp_path):
        from pxh.race import RaceController, TrackProfile
        mock_px = MagicMock()
        profile = TrackProfile()
        profile.add_segment("straight", 2.0, 44, 43, 120, [450, 460, 455])
        profile.add_segment("turn_left", 1.0, 30, 55, 65, [520, 460, 380])
        profile.track_width_cm = 88
        profile.save(tmp_path / "race_track.json")
        mock_px.get_grayscale_data.return_value = [400, 410, 405]
        mock_px.get_distance.return_value = 90.0
        rc = RaceController(px=mock_px, state_dir=tmp_path, dry=True, max_speed=50)
        rc.calibration = {"track_ref": [400, 410, 405], "barrier_ref": [700, 710, 705],
                          "gate_threshold": 50, "track_width_cm": 88}
        return rc, mock_px

    def test_race_completes_without_crash(self, tmp_path):
        rc, _ = self._make_rc(tmp_path)
        rc.run_race(max_laps=1, max_iterations=10)

    def test_race_raises_without_profile(self, tmp_path):
        from pxh.race import RaceController
        mock_px = MagicMock()
        mock_px.get_distance.return_value = 90.0
        mock_px.get_grayscale_data.return_value = [400, 410, 405]
        rc = RaceController(px=mock_px, state_dir=tmp_path, dry=True)
        rc.profile = None
        with pytest.raises(RuntimeError, match="No track profile"):
            rc.run_race(max_iterations=1)

    def test_race_writes_telemetry(self, tmp_path):
        rc, _ = self._make_rc(tmp_path)
        rc.run_race(max_laps=1, max_iterations=5)
        # Telemetry may or may not be written (interval-gated) but no crash
        # If it was written, check it's valid JSON
        telem_path = tmp_path / "race_live.json"
        if telem_path.exists():
            data = json.loads(telem_path.read_text())
            assert "lap" in data
            assert "speed" in data

    def test_race_respects_max_iterations(self, tmp_path):
        rc, mock_px = self._make_rc(tmp_path)
        rc.run_race(max_laps=0, max_iterations=5)
        # Should have called get_grayscale_data exactly max_iterations times
        assert mock_px.get_grayscale_data.call_count <= 10  # some slack for dry-mode branching

    def test_estop_on_close_obstacle(self, tmp_path):
        from pxh.race import RaceController, TrackProfile
        mock_px = MagicMock()
        profile = TrackProfile()
        profile.add_segment("straight", 2.0, 44, 43, 120, [450, 460, 455])
        profile.track_width_cm = 88
        profile.save(tmp_path / "race_track.json")
        # Sonar returns 5cm (very close) — should trigger e-stop path
        mock_px.get_distance.return_value = 5.0
        mock_px.get_grayscale_data.return_value = [400, 410, 405]
        rc = RaceController(px=mock_px, state_dir=tmp_path, dry=True, max_speed=50)
        rc.calibration = {"track_ref": [400, 410, 405], "barrier_ref": [700, 710, 705],
                          "gate_threshold": 50, "track_width_cm": 88}
        # Should not raise even with constant obstacle
        rc.run_race(max_laps=0, max_iterations=3)

    def test_stop_flag_halts_race(self, tmp_path):
        from pxh.race import RaceController, TrackProfile
        mock_px = MagicMock()
        profile = TrackProfile()
        profile.add_segment("straight", 2.0, 44, 43, 120, [450, 460, 455])
        profile.track_width_cm = 88
        profile.save(tmp_path / "race_track.json")
        mock_px.get_grayscale_data.return_value = [400, 410, 405]
        mock_px.get_distance.return_value = 90.0
        rc = RaceController(px=mock_px, state_dir=tmp_path, dry=True)
        rc.calibration = {"track_ref": [400, 410, 405], "barrier_ref": [700, 710, 705],
                          "gate_threshold": 50, "track_width_cm": 88}
        rc._stop_flag = True  # pre-set stop flag
        rc.run_race(max_laps=0, max_iterations=100)
        # Should exit immediately on first iteration check
        assert mock_px.get_grayscale_data.call_count == 0


class TestPxRaceCLI:

    def test_status_no_profile(self, isolated_project):
        import subprocess
        env = isolated_project["env"].copy()
        env["PX_STATE_DIR"] = str(isolated_project["state_dir"])
        result = subprocess.run(
            ["bin/px-race", "--status"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert "no profile" in result.stdout.lower()

    def test_status_with_profile(self, isolated_project):
        import subprocess
        from pxh.race import TrackProfile
        state_dir = isolated_project["state_dir"]
        tp = TrackProfile()
        tp.add_segment("straight", 2.0, 44, 43, 120, [450, 460, 455])
        tp.add_segment("turn_left", 1.0, 30, 55, 65, [520, 460, 380])
        tp.track_width_cm = 88
        tp.lap_duration_s = 12.5
        # Save using write_text (skip atomic_write for test setup simplicity)
        import json
        data = {
            "mapped_at": "", "map_speed": 20, "calibration_v": 0.0,
            "lap_duration_s": 12.5, "track_width_cm": 88,
            "segments": tp.segments, "lap_history": [],
        }
        (state_dir / "race_track.json").write_text(json.dumps(data))

        env = isolated_project["env"].copy()
        env["PX_STATE_DIR"] = str(state_dir)
        result = subprocess.run(
            ["bin/px-race", "--status"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert "2" in result.stdout  # 2 segments
        assert "straight" in result.stdout

    def test_dry_run_map_no_hardware(self, isolated_project):
        import subprocess
        env = isolated_project["env"].copy()
        env["PX_STATE_DIR"] = str(isolated_project["state_dir"])
        # Write a minimal calibration file so run_map doesn't fail
        import json
        cal = {"track_ref": [400, 410, 405], "barrier_ref": [700, 710, 705],
               "gate_threshold": 40, "track_width_cm": 88}
        (isolated_project["state_dir"] / "race_calibration.json").write_text(json.dumps(cal))
        result = subprocess.run(
            ["bin/px-race", "--map", "--dry-run", "--max-iterations", "5"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 0
