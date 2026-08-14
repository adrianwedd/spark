"""Tests for pxh.health — per-component daemon health reporting."""
import datetime as dt
import json

import pytest

from pxh import health


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    health._last_success_write.clear()


def _shift(component, seconds):
    """Backdate a component's updated_ts by `seconds` to simulate silence."""
    path = health._component_path(component)
    rec = json.loads(path.read_text())
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    rec["updated_ts"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(rec))


# --- writing ---------------------------------------------------------------

def test_success_creates_record_and_dir(tmp_path):
    health.record_success("px-mind", detail={"ticks": 1})
    rec = json.loads(health._component_path("px-mind").read_text())
    assert rec["component"] == "px-mind"
    assert rec["consecutive_failures"] == 0
    assert rec["success_count"] == 1
    assert rec["last_success_detail"] == {"ticks": 1}
    assert rec["last_success_ts"]


def test_failures_accumulate_then_reset_on_success():
    health.record_failure("px-post", "bluesky auth failed")
    health.record_failure("px-post", "bluesky auth failed")
    rec = json.loads(health._component_path("px-post").read_text())
    assert rec["consecutive_failures"] == 2
    assert rec["failure_count"] == 2
    assert rec["last_error"] == "bluesky auth failed"

    health.record_success("px-post")
    rec = json.loads(health._component_path("px-post").read_text())
    assert rec["consecutive_failures"] == 0
    # The historical failure count and the last error survive the reset —
    # they are the forensic trail.
    assert rec["failure_count"] == 2
    assert rec["last_error"] == "bluesky auth failed"


def test_long_error_is_truncated():
    health.record_failure("px-blog", "x" * 900)
    rec = json.loads(health._component_path("px-blog").read_text())
    assert len(rec["last_error"]) == 500


def test_components_do_not_share_a_file():
    """The whole point of the design: no read-modify-write across writers."""
    health.record_success("px-mind")
    health.record_failure("px-alive", "gpio busy")
    assert json.loads(health._component_path("px-mind").read_text())["consecutive_failures"] == 0
    assert json.loads(health._component_path("px-alive").read_text())["consecutive_failures"] == 1


def test_health_dir_is_writable_by_every_user():
    """px-alive runs as root, px-mind as pi — whoever creates the dir, both must
    be able to mkstemp in it. Sticky bit stops them deleting each other's files."""
    health.record_success("px-mind")
    mode = health.health_dir().stat().st_mode & 0o7777
    assert mode == 0o1777


def test_existing_dir_is_repaired_to_shared_mode(tmp_path):
    """Simulates root having created state/health/ at the default 0755."""
    d = health.health_dir()
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o755)
    health.record_success("px-mind")
    assert health.health_dir().stat().st_mode & 0o7777 == 0o1777


def test_path_traversal_is_neutralised():
    health.record_success("../../etc/evil")
    written = list(health.health_dir().glob("*.json"))
    assert len(written) == 1
    assert written[0].parent == health.health_dir()


def test_success_throttle_suppresses_rapid_writes():
    health.record_success("px-alive", min_interval_s=60)
    health.record_success("px-alive", min_interval_s=60)
    health.record_success("px-alive", min_interval_s=60)
    rec = json.loads(health._component_path("px-alive").read_text())
    assert rec["success_count"] == 1


def test_success_throttle_is_per_component():
    health.record_success("px-alive", min_interval_s=60)
    health.record_success("px-mind", min_interval_s=60)
    assert json.loads(health._component_path("px-alive").read_text())["success_count"] == 1
    assert json.loads(health._component_path("px-mind").read_text())["success_count"] == 1


def test_failures_are_never_throttled():
    """The streak is the signal — dropping any failure corrupts it."""
    for _ in range(5):
        health.record_failure("px-alive", "i2c error")
    rec = json.loads(health._component_path("px-alive").read_text())
    assert rec["consecutive_failures"] == 5


def test_failure_drops_the_throttle_so_recovery_is_visible():
    """A component flapping faster than its throttle window must not read as
    'failing' — the failures would all land while the successes were dropped."""
    health.record_success("px-alive", min_interval_s=60)
    for _ in range(4):
        health.record_failure("px-alive", "i2c error")
        health.record_success("px-alive", min_interval_s=60)  # inside the window
    rec = json.loads(health._component_path("px-alive").read_text())
    assert rec["consecutive_failures"] == 0
    assert rec["failure_count"] == 4
    assert health.read_health()["components"]["px-alive"]["status"] == "ok"


def test_reporting_never_raises(monkeypatch):
    """A broken health store must not be able to kill the daemon it reports on."""
    monkeypatch.setattr(health, "_write_record",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    health.record_success("px-mind")   # must not raise
    health.record_failure("px-mind", "boom")


# --- status derivation -----------------------------------------------------

def test_ok_after_recent_success():
    health.record_success("px-mind")
    assert health.read_health()["components"]["px-mind"]["status"] == "ok"


def test_degraded_below_threshold():
    health.record_success("px-mind")
    health.record_failure("px-mind", "ollama timeout")
    assert health.read_health()["components"]["px-mind"]["status"] == "degraded"


def test_failing_at_threshold():
    for _ in range(health.FAIL_THRESHOLD):
        health.record_failure("px-mind", "ollama timeout")
    entry = health.read_health()["components"]["px-mind"]
    assert entry["status"] == "failing"
    assert entry["consecutive_failures"] == health.FAIL_THRESHOLD


def test_stale_when_silent_past_its_window():
    health.record_success("px-mind")
    _shift("px-mind", health.STALE_AFTER_S["px-mind"] + 60)
    entry = health.read_health()["components"]["px-mind"]
    assert entry["status"] == "stale"
    assert entry["age_s"] > health.STALE_AFTER_S["px-mind"]


def test_stale_window_is_per_component():
    """px-blog runs daily; the px-mind window must not condemn it."""
    health.record_success("px-blog")
    _shift("px-blog", health.STALE_AFTER_S["px-mind"] + 60)
    assert health.read_health()["components"]["px-blog"]["status"] == "ok"


def test_failing_outranks_stale():
    for _ in range(health.FAIL_THRESHOLD):
        health.record_failure("px-mind", "boom")
    _shift("px-mind", 99999)
    assert health.read_health()["components"]["px-mind"]["status"] == "failing"


def test_missing_when_never_reported():
    assert health.read_health()["components"]["px-mind"]["status"] == "missing"


def test_corrupt_record_reads_as_missing():
    health.record_success("px-mind")
    health._component_path("px-mind").write_text("{not json")
    assert health.read_health()["components"]["px-mind"]["status"] == "missing"


def test_unparseable_timestamp_degrades_instead_of_claiming_freshness():
    health.record_success("px-mind")
    path = health._component_path("px-mind")
    rec = json.loads(path.read_text())
    rec["updated_ts"] = "not-a-timestamp"
    path.write_text(json.dumps(rec))
    entry = health.read_health()["components"]["px-mind"]
    assert entry["status"] == "degraded"
    assert "age_s" not in entry


def test_malformed_failure_count_degrades_instead_of_aborting_aggregate():
    health.record_success("px-mind")
    path = health._component_path("px-mind")
    rec = json.loads(path.read_text())
    rec["consecutive_failures"] = "not-a-count"
    path.write_text(json.dumps(rec))

    result = health.read_health()

    assert result["components"]["px-mind"]["status"] == "degraded"
    assert result["overall"] != "ok"


# --- aggregation -----------------------------------------------------------

def test_every_known_component_appears():
    assert set(health.read_health()["components"]) >= set(health.KNOWN_COMPONENTS)


def test_unknown_component_still_surfaces():
    health.record_success("px-experiment")
    assert "px-experiment" in health.read_health()["components"]


def test_overall_is_the_worst_component():
    for name in health.KNOWN_COMPONENTS:
        health.record_success(name)
    assert health.read_health()["overall"] == "ok"

    health.record_failure("px-post", "boom")
    result = health.read_health()
    assert result["overall"] == "degraded"
    assert result["unhealthy"] == ["px-post"]


# --- summary ---------------------------------------------------------------

def test_summary_is_empty_when_healthy():
    for name in health.KNOWN_COMPONENTS:
        health.record_success(name)
    assert health.summarize() == ""


def test_summary_names_the_problem():
    for name in health.KNOWN_COMPONENTS:
        health.record_success(name)
    for _ in range(health.FAIL_THRESHOLD):
        health.record_failure("px-post", "bluesky auth failed")
    summary = health.summarize()
    assert "px-post" in summary
    assert "bluesky auth failed" in summary


def test_summary_reports_silence_in_minutes():
    for name in health.KNOWN_COMPONENTS:
        health.record_success(name)
    _shift("px-mind", 1800)
    assert "silent for 30 min" in health.summarize()


def test_summary_reports_never_reported():
    assert "never reported" in health.summarize()
