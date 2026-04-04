"""Pre-race hardware validation tests."""
import subprocess
import json
import os


def test_race_check_dry_run(isolated_project):
    """px-race-check --dry-run exits 0 and emits JSON."""
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    r = subprocess.run(
        ["bash", "bin/px-race-check", "--dry-run"],
        capture_output=True, text=True, env=env,
        cwd=env["PROJECT_ROOT"],
    )
    assert r.returncode == 0, f"stderr: {r.stderr}"
    out = json.loads(r.stdout.strip().split("\n")[-1])
    assert "checks" in out
    assert isinstance(out["checks"], dict)
    assert "ready" in out


def test_race_check_reports_missing_calibration(isolated_project, tmp_path):
    """Without calibration file, reports not ready."""
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    # Use a fresh state dir with no calibration file
    empty_state = tmp_path / "empty_state"
    empty_state.mkdir(exist_ok=True)
    env["PX_STATE_DIR"] = str(empty_state)
    r = subprocess.run(
        ["bash", "bin/px-race-check", "--dry-run"],
        capture_output=True, text=True, env=env,
        cwd=env["PROJECT_ROOT"],
    )
    out = json.loads(r.stdout.strip().split("\n")[-1])
    assert out["checks"]["calibration"] == "missing"
    assert out["ready"] is False
