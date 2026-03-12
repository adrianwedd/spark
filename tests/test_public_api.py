"""Tests for unauthenticated public read-only API endpoints."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))


@pytest.fixture
def public_client(isolated_project, monkeypatch):
    """TestClient with env set up for public endpoint tests."""
    monkeypatch.setenv("PX_API_TOKEN", "test-token-abc123")
    monkeypatch.setenv("PX_DRY", "1")
    monkeypatch.setenv("PX_BYPASS_SUDO", "1")
    monkeypatch.setenv("PX_VOICE_DEVICE", "null")
    monkeypatch.setenv("PX_SESSION_PATH", str(isolated_project["session_path"]))
    monkeypatch.setenv("LOG_DIR", str(isolated_project["log_dir"]))
    monkeypatch.setenv("PX_STATE_DIR", str(isolated_project["state_dir"]))
    monkeypatch.setenv("PROJECT_ROOT", str(ROOT))

    from pxh import api
    api._load_token()

    from fastapi.testclient import TestClient
    return TestClient(api.app, raise_server_exceptions=False)


@pytest.fixture
def state_dir(isolated_project):
    return isolated_project["state_dir"]


class TestPublicStatus:
    def test_returns_200(self, public_client):
        resp = public_client.get("/api/v1/public/status")
        assert resp.status_code == 200

    def test_no_auth_required(self, public_client):
        resp = public_client.get("/api/v1/public/status")
        assert resp.status_code == 200

    def test_null_fields_when_no_thoughts_file(self, public_client, state_dir):
        # No thoughts file written — all thought fields should be null
        resp = public_client.get("/api/v1/public/status")
        data = resp.json()
        assert data["mood"] is None
        assert data["last_thought"] is None
        assert data["last_action"] is None

    def test_reads_persona_scoped_thoughts(self, public_client, state_dir):
        # Write a thought to thoughts-spark.jsonl (persona-scoped)
        thought = {
            "ts": "2026-03-12T04:00:00Z",
            "thought": "Test thought",
            "mood": "curious",
            "action": "comment",
            "salience": 0.5,
        }
        thoughts_file = state_dir / "thoughts-spark.jsonl"
        thoughts_file.write_text(json.dumps(thought) + "\n")

        # Write session with persona=spark
        session_file = state_dir / "session.json"
        session_file.write_text(json.dumps({"persona": "spark", "listening": False}))

        resp = public_client.get("/api/v1/public/status")
        data = resp.json()
        assert data["mood"] == "curious"
        assert data["last_thought"] == "Test thought"
        assert data["persona"] == "spark"

    def test_falls_back_to_unscoped_thoughts(self, public_client, state_dir):
        # No persona in session — should read thoughts.jsonl
        thought = {"ts": "2026-03-12T04:00:00Z", "thought": "Generic", "mood": "content"}
        (state_dir / "thoughts.jsonl").write_text(json.dumps(thought) + "\n")
        resp = public_client.get("/api/v1/public/status")
        assert resp.json()["mood"] == "content"

    def test_missing_fields_in_thought_return_null(self, public_client, state_dir):
        # Thought entry with no mood field
        (state_dir / "thoughts.jsonl").write_text('{"ts": "2026-03-12T04:00:00Z"}\n')
        resp = public_client.get("/api/v1/public/status")
        assert resp.json()["mood"] is None

    def test_has_required_keys(self, public_client):
        resp = public_client.get("/api/v1/public/status")
        data = resp.json()
        for key in ("persona", "mood", "last_thought", "last_action", "salience", "ts", "listening"):
            assert key in data, f"missing key: {key}"


class TestPublicVitals:
    def test_returns_200(self, public_client):
        resp = public_client.get("/api/v1/public/vitals")
        assert resp.status_code == 200

    def test_no_auth_required(self, public_client):
        resp = public_client.get("/api/v1/public/vitals")
        assert resp.status_code == 200

    def test_has_required_keys(self, public_client):
        resp = public_client.get("/api/v1/public/vitals")
        data = resp.json()
        for key in ("cpu_pct", "ram_pct", "cpu_temp_c", "battery_pct", "disk_pct", "ts"):
            assert key in data, f"missing key: {key}"

    def test_battery_null_when_file_missing(self, public_client, state_dir):
        # No battery.json — battery_pct should be null
        resp = public_client.get("/api/v1/public/vitals")
        assert resp.json()["battery_pct"] is None

    def test_reads_battery_pct_from_file(self, public_client, state_dir):
        battery_file = state_dir / "battery.json"
        battery_file.write_text(json.dumps({"pct": 72, "volts": 8.1, "charging": False}))
        resp = public_client.get("/api/v1/public/vitals")
        assert resp.json()["battery_pct"] == 72

    def test_cpu_temp_null_when_thermal_zone_absent(self, public_client, monkeypatch):
        # Simulate thermal zone file missing
        monkeypatch.setattr("pxh.api._THERMAL_ZONE", Path("/nonexistent/thermal_zone0/temp"))
        resp = public_client.get("/api/v1/public/vitals")
        assert resp.json()["cpu_temp_c"] is None

    def test_psutil_failure_returns_null_cpu_ram_disk(self, public_client, monkeypatch):
        # Simulate psutil unavailable — cpu/ram/disk should be null, not a 500
        import builtins, sys
        monkeypatch.delitem(sys.modules, 'psutil', raising=False)
        real_import = builtins.__import__
        def mock_import(name, *args, **kwargs):
            if name == 'psutil':
                raise ImportError("psutil not available")
            return real_import(name, *args, **kwargs)
        monkeypatch.setattr(builtins, '__import__', mock_import)
        resp = public_client.get("/api/v1/public/vitals")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cpu_pct"] is None
        assert data["ram_pct"] is None
        assert data["disk_pct"] is None
        assert "ts" in data  # still returns a timestamp

    def test_ts_is_iso_string(self, public_client):
        resp = public_client.get("/api/v1/public/vitals")
        ts = resp.json()["ts"]
        assert ts is not None
        assert "T" in ts  # ISO format check


class TestPublicSonar:
    def test_returns_200(self, public_client):
        resp = public_client.get("/api/v1/public/sonar")
        assert resp.status_code == 200

    def test_no_auth_required(self, public_client):
        resp = public_client.get("/api/v1/public/sonar")
        assert resp.status_code == 200

    def test_unavailable_when_file_missing(self, public_client, state_dir):
        # No sonar_live.json — should return unavailable
        resp = public_client.get("/api/v1/public/sonar")
        data = resp.json()
        assert data["source"] == "unavailable"
        assert data["sonar_cm"] is None
        assert data["age_seconds"] is None

    def test_reads_sonar_from_file(self, public_client, state_dir):
        sonar_file = state_dir / "sonar_live.json"
        sonar_file.write_text(json.dumps({
            "ts": time.time(),  # fresh
            "distance_cm": 55.2,
        }))
        resp = public_client.get("/api/v1/public/sonar")
        data = resp.json()
        assert data["source"] == "sonar_live"
        assert data["sonar_cm"] == pytest.approx(55.2, abs=0.1)
        assert isinstance(data["age_seconds"], int)

    def test_stale_sonar_returns_unavailable(self, public_client, state_dir):
        old_ts = time.time() - 120  # 2 minutes ago — stale
        sonar_file = state_dir / "sonar_live.json"
        sonar_file.write_text(json.dumps({"ts": old_ts, "distance_cm": 30.0}))
        resp = public_client.get("/api/v1/public/sonar")
        assert resp.json()["source"] == "unavailable"

    def test_has_required_keys(self, public_client):
        resp = public_client.get("/api/v1/public/sonar")
        data = resp.json()
        for key in ("sonar_cm", "age_seconds", "source"):
            assert key in data, f"missing key: {key}"


class TestPublicAwareness:
    def test_returns_200(self, public_client):
        resp = public_client.get("/api/v1/public/awareness")
        assert resp.status_code == 200

    def test_no_auth_required(self, public_client):
        resp = public_client.get("/api/v1/public/awareness")
        assert resp.status_code == 200

    def test_has_required_keys(self, public_client):
        resp = public_client.get("/api/v1/public/awareness")
        data = resp.json()
        for key in ("obi_mode", "person_present", "frigate_score",
                    "ambient_level", "ambient_rms", "weather",
                    "minutes_since_speech", "time_period", "ts"):
            assert key in data, f"missing key: {key}"

    def test_null_fields_when_no_awareness_file(self, public_client, state_dir):
        # No awareness.json → all fields null, no 500
        resp = public_client.get("/api/v1/public/awareness")
        data = resp.json()
        assert data["obi_mode"] is None
        assert data["person_present"] is None    # null when Frigate absent (hides indicator)
        assert data["weather"] is None

    def test_flattened_projection_from_awareness_file(self, public_client, state_dir):
        awareness = {
            "ts": "2026-03-13T01:00:00Z",
            "obi_mode": "calm",
            "time_period": "night",
            "minutes_since_speech": 4.0,
            "frigate": {
                "person_present": True,
                "score": 0.74,
                "event_count": 1,
            },
            "ambient_sound": {"rms": 340, "level": "quiet"},
            "weather": {
                "temp_C": 14.2,
                "wind_kmh": 12,
                "humidity_pct": 68,
                "summary": "Cloudy",
            },
        }
        (state_dir / "awareness.json").write_text(json.dumps(awareness))
        resp = public_client.get("/api/v1/public/awareness")
        data = resp.json()
        assert data["obi_mode"] == "calm"
        assert data["person_present"] is True
        assert abs(data["frigate_score"] - 0.74) < 0.01
        assert data["ambient_rms"] == 340
        assert data["ambient_level"] == "quiet"
        assert data["minutes_since_speech"] == pytest.approx(4.0, abs=0.1)
        assert data["time_period"] == "night"
        assert data["ts"] == "2026-03-13T01:00:00Z"

    def test_temp_c_lowercase_normalised(self, public_client, state_dir):
        # awareness.json stores temp_C (uppercase); endpoint must normalise to temp_c
        awareness = {
            "weather": {"temp_C": 14.2, "wind_kmh": 12, "humidity_pct": 68, "summary": "Cloudy"},
        }
        (state_dir / "awareness.json").write_text(json.dumps(awareness))
        resp = public_client.get("/api/v1/public/awareness")
        data = resp.json()
        assert data["weather"] is not None
        assert "temp_c" in data["weather"]
        assert abs(data["weather"]["temp_c"] - 14.2) < 0.01
        assert "temp_C" not in data["weather"]

    def test_person_present_null_when_frigate_key_absent(self, public_client, state_dir):
        # awareness.json with no frigate key → Frigate offline → null hides indicator
        (state_dir / "awareness.json").write_text(json.dumps({"obi_mode": "absent"}))
        data = public_client.get("/api/v1/public/awareness").json()
        assert data["person_present"] is None

    def test_person_present_null_when_frigate_is_none(self, public_client, state_dir):
        # awareness.json with frigate: null (Frigate offline) → null hides indicator
        (state_dir / "awareness.json").write_text(json.dumps({"frigate": None}))
        data = public_client.get("/api/v1/public/awareness").json()
        assert data["person_present"] is None

    def test_no_500_on_json_array_awareness_file(self, public_client, state_dir):
        # awareness.json is a JSON array (corrupted) → 200 with null fields, no 500
        (state_dir / "awareness.json").write_text(json.dumps([{"obi_mode": "calm"}]))
        resp = public_client.get("/api/v1/public/awareness")
        assert resp.status_code == 200
        assert resp.json()["obi_mode"] is None

    def test_no_500_on_non_json_awareness_file(self, public_client, state_dir):
        # awareness.json is garbage bytes → 200 with null fields, no 500
        (state_dir / "awareness.json").write_text("not valid json {{{")
        resp = public_client.get("/api/v1/public/awareness")
        assert resp.status_code == 200
        assert resp.json()["obi_mode"] is None

    def test_no_500_when_weather_is_string(self, public_client, state_dir):
        # weather field is a string not a dict → treated as null, no 500
        (state_dir / "awareness.json").write_text(json.dumps({"weather": "Cloudy"}))
        resp = public_client.get("/api/v1/public/awareness")
        assert resp.status_code == 200
        assert resp.json()["weather"] is None

    def test_weather_null_when_weather_key_absent(self, public_client, state_dir):
        (state_dir / "awareness.json").write_text(json.dumps({"obi_mode": "calm"}))
        data = public_client.get("/api/v1/public/awareness").json()
        assert data["weather"] is None

    def test_nested_null_for_missing_subkeys(self, public_client, state_dir):
        # ambient_sound present but missing level → null for that subfield
        awareness = {"ambient_sound": {"rms": 200}}
        (state_dir / "awareness.json").write_text(json.dumps(awareness))
        data = public_client.get("/api/v1/public/awareness").json()
        assert data["ambient_rms"] == 200
        assert data["ambient_level"] is None


class TestPublicHistory:
    def test_returns_200(self, public_client):
        resp = public_client.get("/api/v1/public/history")
        assert resp.status_code == 200

    def test_no_auth_required(self, public_client):
        resp = public_client.get("/api/v1/public/history")
        assert resp.status_code == 200

    def test_returns_list(self, public_client):
        resp = public_client.get("/api/v1/public/history")
        assert isinstance(resp.json(), list)

    def test_endpoint_reads_from_ring_buffer(self, public_client):
        from pxh import api as _api
        # Pre-populate the buffer directly (bypasses background thread)
        with _api._history_lock:
            _api._history_buf.clear()
            _api._history_buf.append({
                "ts": "2026-03-13T00:00:00Z", "cpu_pct": 25.0, "ram_pct": 40.0,
                "cpu_temp_c": 52.0, "battery_pct": 80, "sonar_cm": 45.2, "ambient_rms": 340,
            })
        resp = public_client.get("/api/v1/public/history")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["cpu_pct"] == pytest.approx(25.0, abs=0.1)
        assert data[0]["sonar_cm"] == pytest.approx(45.2, abs=0.1)

    def test_maxlen_60_enforced(self, public_client):
        from pxh import api as _api
        with _api._history_lock:
            _api._history_buf.clear()
            for i in range(70):
                _api._history_buf.append({"ts": f"t{i:03}", "cpu_pct": float(i)})
        resp = public_client.get("/api/v1/public/history")
        data = resp.json()
        # deque(maxlen=60) keeps the last 60
        assert len(data) == 60
        assert data[0]["ts"] == "t010"   # oldest remaining
        assert data[-1]["ts"] == "t069"  # newest

    def test_collect_sample_sonar_null_when_stale(self, state_dir, monkeypatch):
        import time as _time
        # Write a stale sonar file (> 60s old)
        old_ts = _time.time() - 120
        (state_dir / "sonar_live.json").write_text(
            json.dumps({"ts": old_ts, "distance_cm": 30.0})
        )
        monkeypatch.setenv("PX_STATE_DIR", str(state_dir))
        from pxh import api as _api
        sample = _api._collect_history_sample(state_dir)
        assert sample["sonar_cm"] is None

    def test_collect_sample_sonar_present_when_fresh(self, state_dir, monkeypatch):
        import time as _time
        fresh_ts = _time.time() - 5
        (state_dir / "sonar_live.json").write_text(
            json.dumps({"ts": fresh_ts, "distance_cm": 55.0})
        )
        monkeypatch.setenv("PX_STATE_DIR", str(state_dir))
        from pxh import api as _api
        sample = _api._collect_history_sample(state_dir)
        assert sample["sonar_cm"] == pytest.approx(55.0, abs=0.1)

    def test_collect_sample_has_required_fields(self, state_dir, monkeypatch):
        monkeypatch.setenv("PX_STATE_DIR", str(state_dir))
        from pxh import api as _api
        sample = _api._collect_history_sample(state_dir)
        for field in ("ts", "cpu_pct", "cpu_temp_c", "ram_pct", "disk_pct", "battery_pct",
                      "sonar_cm", "ambient_rms", "weather_temp_c", "wind_kmh", "humidity_pct",
                      "tokens_in", "tokens_out", "salience"):
            assert field in sample, f"missing field: {field}"

    def test_collect_sample_weather_fields_from_awareness(self, state_dir, monkeypatch):
        awareness = {"weather": {"temp_C": 18.5, "wind_kmh": 22, "humidity_pct": 71}}
        (state_dir / "awareness.json").write_text(json.dumps(awareness))
        monkeypatch.setenv("PX_STATE_DIR", str(state_dir))
        from pxh import api as _api
        sample = _api._collect_history_sample(state_dir)
        assert sample["weather_temp_c"] == pytest.approx(18.5, abs=0.1)
        assert sample["wind_kmh"] == 22
        assert sample["humidity_pct"] == 71

    def test_collect_sample_zero_temp_c_not_dropped(self, state_dir, monkeypatch):
        """0°C is a valid reading — falsy `or` guard must not drop it."""
        awareness = {"weather": {"temp_C": 0, "wind_kmh": 5, "humidity_pct": 80}}
        (state_dir / "awareness.json").write_text(json.dumps(awareness))
        monkeypatch.setenv("PX_STATE_DIR", str(state_dir))
        from pxh import api as _api
        sample = _api._collect_history_sample(state_dir)
        assert sample["weather_temp_c"] == 0

    def test_collect_sample_weather_null_when_absent(self, state_dir, monkeypatch):
        (state_dir / "awareness.json").write_text(json.dumps({"obi_mode": "calm"}))
        monkeypatch.setenv("PX_STATE_DIR", str(state_dir))
        from pxh import api as _api
        sample = _api._collect_history_sample(state_dir)
        assert sample["weather_temp_c"] is None
        assert sample["wind_kmh"] is None
        assert sample["humidity_pct"] is None


class TestPublicServices:
    def test_returns_200_without_auth(self, public_client):
        resp = public_client.get("/api/v1/public/services")
        assert resp.status_code == 200

    def test_returns_dict_not_list(self, public_client):
        data = public_client.get("/api/v1/public/services").json()
        assert isinstance(data, dict), "should be dict, not list"

    def test_has_all_five_services(self, public_client):
        data = public_client.get("/api/v1/public/services").json()
        for svc in ("px-mind", "px-alive", "px-wake-listen",
                    "px-battery-poll", "px-api-server"):
            assert svc in data, f"missing service: {svc}"

    def test_values_are_valid_status_strings(self, public_client):
        valid = {"active", "activating", "failed", "inactive", "unknown"}
        data = public_client.get("/api/v1/public/services").json()
        for svc, status in data.items():
            assert status in valid, f"{svc} has invalid status: {status!r}"

    def test_existing_auth_services_endpoint_requires_auth(self, public_client):
        # Auth-required endpoint at /api/v1/services must still require auth
        resp = public_client.get("/api/v1/services")
        assert resp.status_code == 401

    def test_existing_auth_services_returns_list_shape(self, public_client):
        resp = public_client.get(
            "/api/v1/services",
            headers={"Authorization": "Bearer test-token-abc123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "services" in data
        assert isinstance(data["services"], list)
