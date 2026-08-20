"""Tests for the PiCar-X REST API."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
import unittest.mock
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))


@pytest.fixture
def api_client(isolated_project, monkeypatch):
    """FastAPI TestClient with auth token and dry-run environment."""
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
    with TestClient(api.app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-token-abc123"}


# -- Health (unauthenticated) --

class TestHealth:
    @staticmethod
    def _write_fresh_core_state(state_dir):
        (state_dir / "thoughts-spark.jsonl").write_text('{"thought":"awake"}\n')
        (state_dir / "awareness.json").write_text("{}")
        from pxh import health
        health._last_success_write.clear()
        for component in health.KNOWN_COMPONENTS:
            health.record_success(component)

    @staticmethod
    def _stale_daemon(component):
        from pxh import health
        record_path = health._component_path(component)
        record = json.loads(record_path.read_text())
        record["updated_ts"] = "2020-01-01T00:00:00Z"
        record_path.write_text(json.dumps(record))

    @staticmethod
    def _write_heartbeat(state_dir, *, age_s=0, mode="running"):
        (state_dir / "alive_heartbeat.json").write_text(json.dumps({
            "ts": time.time() - age_s,
            "mode": mode,
        }))

    @staticmethod
    def _write_sonar(state_dir, *, age_s=0):
        (state_dir / "sonar_live.json").write_text(json.dumps({
            "ts": time.time() - age_s,
            "distance_cm": 42.0,
        }))

    def test_health_returns_details(self, api_client):
        """Health endpoint returns system details, not just static ok."""
        r = api_client.get("/api/v1/health")
        assert r.status_code in (200, 503)
        data = r.json()
        assert "status" in data
        assert "checks" in data
        assert isinstance(data["checks"], dict)

    def test_health_includes_thoughts_freshness(self, api_client):
        """Health endpoint reports thoughts staleness."""
        r = api_client.get("/api/v1/health")
        data = r.json()
        assert "thoughts" in data["checks"]

    def test_health_no_auth_required(self, api_client):
        resp = api_client.get("/api/v1/health")
        assert resp.status_code in (200, 503)

    def test_api_liveness_heartbeat_records_success(self, monkeypatch):
        from pxh import api, health
        recorded = []

        async def stop_after_first_tick(_seconds):
            raise asyncio.CancelledError

        monkeypatch.setattr(health, "record_success",
                            lambda component: recorded.append(component))

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(api._health_heartbeat(sleep=stop_after_first_tick))

        assert recorded == ["px-api-server"]

    def test_testclient_lifespan_stops_history_worker(self, monkeypatch):
        from fastapi.testclient import TestClient
        from pxh import api

        monkeypatch.setenv("PX_API_TOKEN", "test-token-abc123")
        before = {
            thread.ident for thread in threading.enumerate()
            if thread.name == "history-worker"
        }

        with TestClient(api.app):
            pass

        leaked = [
            thread for thread in threading.enumerate()
            if thread.name == "history-worker"
            and thread.ident not in before
            and thread.is_alive()
        ]
        assert leaked == []

    def test_health_reports_fresh_loop_and_fresh_sonar_independently(
            self, api_client, isolated_project):
        state_dir = isolated_project["state_dir"]
        self._write_fresh_core_state(state_dir)
        self._write_heartbeat(state_dir, mode="running")
        self._write_sonar(state_dir)

        response = api_client.get("/api/v1/health")

        assert response.status_code == 200
        checks = response.json()["checks"]
        assert checks["alive_loop"]["status"] == "ok"
        assert checks["alive_loop"]["mode"] == "running"
        assert checks["sonar"]["status"] == "ok"

    def test_health_treats_stale_sonar_as_expected_while_charging(
            self, api_client, isolated_project):
        state_dir = isolated_project["state_dir"]
        self._write_fresh_core_state(state_dir)
        self._write_heartbeat(state_dir, mode="charging")
        self._write_sonar(state_dir, age_s=120)

        response = api_client.get("/api/v1/health")

        assert response.status_code == 200
        checks = response.json()["checks"]
        assert checks["alive_loop"]["status"] == "ok"
        assert checks["alive_loop"]["mode"] == "charging"
        assert checks["alive_loop"]["age_s"] == pytest.approx(0, abs=2)
        assert checks["sonar"]["status"] == "expected_stale"
        assert checks["sonar"]["reason"] == "charging"

    def test_fresh_heartbeat_overrides_legacy_px_alive_staleness(
            self, api_client, isolated_project):
        state_dir = isolated_project["state_dir"]
        self._write_fresh_core_state(state_dir)
        self._stale_daemon("px-alive")
        self._write_heartbeat(state_dir, mode="charging")
        self._write_sonar(state_dir, age_s=120)

        response = api_client.get("/api/v1/health")

        assert response.status_code == 200
        checks = response.json()["checks"]
        assert checks["alive_loop"]["status"] == "ok"
        assert checks["daemons"]["components"]["px-alive"] == "stale"
        assert checks["daemons"]["status"] == "ok"

    def test_heartbeat_read_from_runtime_dir(
            self, api_client, isolated_project, monkeypatch, tmp_path):
        """px-alive writes its heartbeat to tmpfs; health must follow it there.

        The heartbeat moved off the SD card to escape a 21.5s fsync tail that
        was tripping WatchdogSec=15. A reader still pointed at the state dir
        would report a live daemon as permanently missing.
        """
        state_dir = isolated_project["state_dir"]
        self._write_fresh_core_state(state_dir)
        self._write_sonar(state_dir)
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        monkeypatch.setenv("PX_ALIVE_HEARTBEAT_DIR", str(runtime_dir))
        (runtime_dir / "alive_heartbeat.json").write_text(json.dumps({
            "ts": time.time(),
            "mode": "lease_wait",
        }))
        assert not (state_dir / "alive_heartbeat.json").exists()

        checks = api_client.get("/api/v1/health").json()["checks"]

        assert checks["alive_loop"]["status"] == "ok"
        assert checks["alive_loop"]["mode"] == "lease_wait"

    def test_other_stale_daemon_still_degrades_with_fresh_alive_heartbeat(
            self, api_client, isolated_project):
        state_dir = isolated_project["state_dir"]
        self._write_fresh_core_state(state_dir)
        self._stale_daemon("px-mind")
        self._write_heartbeat(state_dir, mode="running")
        self._write_sonar(state_dir)

        response = api_client.get("/api/v1/health")

        assert response.status_code == 503
        assert response.json()["checks"]["daemons"]["status"] == "stale"

    def test_missing_heartbeat_uses_legacy_px_alive_fallback(
            self, api_client, isolated_project):
        state_dir = isolated_project["state_dir"]
        self._write_fresh_core_state(state_dir)
        self._stale_daemon("px-alive")
        self._write_sonar(state_dir)

        response = api_client.get("/api/v1/health")

        assert response.status_code == 503
        checks = response.json()["checks"]
        assert checks["alive_loop"]["status"] == "missing"
        assert checks["daemons"]["status"] == "stale"

    def test_invalid_heartbeat_does_not_restore_legacy_px_alive_authority(
            self, api_client, isolated_project):
        state_dir = isolated_project["state_dir"]
        self._write_fresh_core_state(state_dir)
        self._stale_daemon("px-alive")
        (state_dir / "alive_heartbeat.json").write_text("not json")
        self._write_sonar(state_dir)

        response = api_client.get("/api/v1/health")

        assert response.status_code == 503
        checks = response.json()["checks"]
        assert checks["alive_loop"]["status"] == "invalid"
        assert checks["daemons"]["components"]["px-alive"] == "stale"
        assert checks["daemons"]["status"] == "ok"

    @pytest.mark.parametrize("timestamp", [float("nan"), float("inf")])
    def test_non_finite_heartbeat_timestamp_is_invalid(
            self, api_client, isolated_project, timestamp):
        state_dir = isolated_project["state_dir"]
        self._write_fresh_core_state(state_dir)
        (state_dir / "alive_heartbeat.json").write_text(json.dumps({
            "ts": timestamp,
            "mode": "running",
        }))
        self._write_sonar(state_dir)

        response = api_client.get("/api/v1/health")

        assert response.status_code == 503
        assert response.json()["checks"]["alive_loop"]["status"] == "invalid"

    def test_future_heartbeat_timestamp_is_invalid(
            self, api_client, isolated_project):
        state_dir = isolated_project["state_dir"]
        self._write_fresh_core_state(state_dir)
        self._write_heartbeat(state_dir, age_s=-60, mode="running")
        self._write_sonar(state_dir)

        response = api_client.get("/api/v1/health")

        assert response.status_code == 503
        assert response.json()["checks"]["alive_loop"]["status"] == "invalid"

    def test_unreadable_daemon_health_degrades_closed(
            self, api_client, isolated_project, monkeypatch):
        from pxh import health
        state_dir = isolated_project["state_dir"]
        self._write_fresh_core_state(state_dir)
        self._write_heartbeat(state_dir, mode="running")
        self._write_sonar(state_dir)
        monkeypatch.setattr(health, "read_health",
                            lambda: (_ for _ in ()).throw(ValueError("bad record")))

        response = api_client.get("/api/v1/health")

        assert response.status_code == 503
        assert response.json()["checks"]["daemons"]["status"] == "unknown"

    def test_health_degrades_on_stale_loop_even_when_sonar_is_fresh(
            self, api_client, isolated_project):
        state_dir = isolated_project["state_dir"]
        self._write_fresh_core_state(state_dir)
        self._write_heartbeat(state_dir, age_s=60, mode="running")
        self._write_sonar(state_dir)

        response = api_client.get("/api/v1/health")

        assert response.status_code == 503
        checks = response.json()["checks"]
        assert checks["alive_loop"]["status"] == "stale"
        assert checks["sonar"]["status"] == "ok"

    def test_health_degrades_on_stale_sonar_while_running(
            self, api_client, isolated_project):
        state_dir = isolated_project["state_dir"]
        self._write_fresh_core_state(state_dir)
        self._write_heartbeat(state_dir, mode="running")
        self._write_sonar(state_dir, age_s=120)

        response = api_client.get("/api/v1/health")

        assert response.status_code == 503
        checks = response.json()["checks"]
        assert checks["alive_loop"]["status"] == "ok"
        assert checks["sonar"]["status"] == "stale"


# -- Security Headers --

class TestSecurityHeaders:
    def test_security_headers_present(self, api_client):
        resp = api_client.get("/api/v1/health")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


# -- Auth --

class TestAuth:
    def test_missing_token(self, api_client):
        resp = api_client.get("/api/v1/session")
        assert resp.status_code == 401

    def test_wrong_token(self, api_client):
        resp = api_client.get(
            "/api/v1/session",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_valid_token(self, api_client, auth_headers):
        resp = api_client.get("/api/v1/session", headers=auth_headers)
        assert resp.status_code == 200


# -- Tools list --

class TestToolsList:
    def test_lists_tools(self, api_client, auth_headers):
        resp = api_client.get("/api/v1/tools", headers=auth_headers)
        assert resp.status_code == 200
        tools = resp.json()["tools"]
        assert "tool_status" in tools
        assert "tool_drive" in tools
        assert "tool_api_start" in tools
        assert "tool_api_stop" in tools


# -- Session --

class TestSession:
    def test_get_session(self, api_client, auth_headers):
        resp = api_client.get("/api/v1/session", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "schema_version" in data

    def test_session_history_truncated(self, api_client, auth_headers):
        """GET /session should return at most the last 10 history entries."""
        # First request ensures session file is created
        api_client.get("/api/v1/session", headers=auth_headers)
        session_path = os.environ["PX_SESSION_PATH"]
        with open(session_path, "r") as f:
            data = json.load(f)
        data["history"] = [{"role": "user", "text": f"msg-{i}"} for i in range(20)]
        with open(session_path, "w") as f:
            json.dump(data, f)

        resp = api_client.get("/api/v1/session", headers=auth_headers)
        assert resp.status_code == 200
        history = resp.json()["history"]
        assert len(history) == 10
        # Should be the last 10 (msg-10 through msg-19)
        assert history[0]["text"] == "msg-10"
        assert history[-1]["text"] == "msg-19"

    def test_patch_session_allowed_field(self, api_client, auth_headers):
        resp = api_client.patch(
            "/api/v1/session",
            headers=auth_headers,
            json={"listening": True},
        )
        assert resp.status_code == 200
        assert resp.json()["listening"] is True

    def test_patch_session_rejects_empty(self, api_client, auth_headers):
        resp = api_client.patch(
            "/api/v1/session",
            headers=auth_headers,
            json={},
        )
        assert resp.status_code == 400


# -- Tool execution --

class TestToolExecution:
    def test_run_tool_status_dry(self, api_client, auth_headers):
        resp = api_client.post(
            "/api/v1/tool",
            headers=auth_headers,
            json={"tool": "tool_status", "params": {}, "dry": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["dry"] is True
        assert data["tool"] == "tool_status"

    def test_run_tool_voice_dry(self, api_client, auth_headers):
        resp = api_client.post(
            "/api/v1/tool",
            headers=auth_headers,
            json={"tool": "tool_voice", "params": {"text": "hello"}, "dry": True},
        )
        assert resp.status_code == 200

    def test_invalid_tool_rejected(self, api_client, auth_headers):
        resp = api_client.post(
            "/api/v1/tool",
            headers=auth_headers,
            json={"tool": "tool_destroy_world", "params": {}},
        )
        assert resp.status_code == 400

    def test_bad_params_rejected(self, api_client, auth_headers):
        resp = api_client.post(
            "/api/v1/tool",
            headers=auth_headers,
            json={"tool": "tool_circle", "params": {"speed": 999}},
        )
        assert resp.status_code == 400

    def test_force_dry_override(self, api_client, auth_headers):
        """When server is in PX_DRY=1 mode, dry: false is overridden."""
        from pxh import api
        original = api.FORCE_DRY
        api.FORCE_DRY = True
        try:
            resp = api_client.post(
                "/api/v1/tool",
                headers=auth_headers,
                json={"tool": "tool_status", "params": {}, "dry": False},
            )
            assert resp.status_code == 200
            assert resp.json()["dry"] is True
        finally:
            api.FORCE_DRY = original


# -- Async jobs (wander) --

class TestAsyncJobs:
    def test_wander_returns_202(self, api_client, auth_headers):
        resp = api_client.post(
            "/api/v1/tool",
            headers=auth_headers,
            json={"tool": "tool_wander", "params": {"steps": 2}, "dry": True},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert "job_id" in data
        assert data["status"] == "accepted"

    def test_job_poll(self, api_client, auth_headers):
        resp = api_client.post(
            "/api/v1/tool",
            headers=auth_headers,
            json={"tool": "tool_wander", "params": {"steps": 1}, "dry": True},
        )
        job_id = resp.json()["job_id"]

        resp2 = api_client.get(
            f"/api/v1/jobs/{job_id}",
            headers=auth_headers,
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] in ("running", "complete", "error")

    def test_job_not_found(self, api_client, auth_headers):
        resp = api_client.get(
            "/api/v1/jobs/nonexistent-id",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_wander_job_completes(self, api_client, auth_headers):
        """Job is registered immediately; background task eventually terminates.

        NOTE: TestClient runs each request in its own event loop invocation so
        asyncio.create_task() background work may not finish between requests.
        We verify the job is registered with a sane initial state; the terminal
        state ("complete"/"error") is acceptable if reached within the poll window.
        """
        import time
        resp = api_client.post(
            "/api/v1/tool",
            headers=auth_headers,
            json={"tool": "tool_wander", "params": {"steps": 1}, "dry": True},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        job_id = body["job_id"]

        # Poll briefly — accept any of the three valid states
        deadline = time.monotonic() + 10.0
        last_status = "running"
        while time.monotonic() < deadline:
            r = api_client.get(f"/api/v1/jobs/{job_id}", headers=auth_headers)
            assert r.status_code == 200
            last_status = r.json()["status"]
            if last_status in ("complete", "error"):
                break
            time.sleep(0.2)
        assert last_status in ("running", "complete", "error")


# -- Motion gate --

class TestMotionGate:
    def test_motion_blocked_returns_403(self, api_client, auth_headers, monkeypatch):
        """API maps tool rc=2 (motion blocked) to HTTP 403.

        Mocks execute_tool so the test exercises only the API's status-code
        contract — the underlying bin/tool-drive behaviour is covered by the
        Pi-only live-tool tests. Also clears FORCE_DRY so the live code path
        is taken (dry mode short-circuits before execute_tool is called).
        """
        from pxh import api as api_mod

        monkeypatch.setattr(api_mod, "FORCE_DRY", False)
        monkeypatch.setenv("PX_DRY", "0")

        def fake_execute(tool, env_overrides, dry, timeout):
            return 2, '{"status":"blocked","reason":"motion not confirmed safe"}', ""

        monkeypatch.setattr(api_mod, "execute_tool", fake_execute)

        resp = api_client.post(
            "/api/v1/tool",
            headers=auth_headers,
            json={"tool": "tool_drive",
                  "params": {"direction": "forward", "speed": 20, "duration": 1.0},
                  "dry": False},
        )
        assert resp.status_code == 403
        assert resp.json()["status"] == "blocked"

    def test_patch_confirm_motion_allowed(self, api_client, auth_headers):
        """PATCH /session can set confirm_motion_allowed (with confirm: true)."""
        resp = api_client.patch(
            "/api/v1/session",
            headers=auth_headers,
            json={"confirm_motion_allowed": True, "wheels_on_blocks": True, "confirm": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["confirm_motion_allowed"] is True
        assert data["wheels_on_blocks"] is True

    def test_patch_persona_vixen(self, api_client, auth_headers):
        resp = api_client.patch(
            "/api/v1/session", headers=auth_headers,
            json={"persona": "vixen"},
        )
        assert resp.status_code == 200
        assert resp.json()["persona"] == "vixen"

    def test_patch_persona_gremlin(self, api_client, auth_headers):
        resp = api_client.patch(
            "/api/v1/session", headers=auth_headers,
            json={"persona": "gremlin"},
        )
        assert resp.status_code == 200
        assert resp.json()["persona"] == "gremlin"

    def test_patch_persona_clear_with_claude(self, api_client, auth_headers):
        # Set a persona first
        api_client.patch(
            "/api/v1/session", headers=auth_headers,
            json={"persona": "vixen"},
        )
        # Clear it with "claude"
        resp = api_client.patch(
            "/api/v1/session", headers=auth_headers,
            json={"persona": "claude"},
        )
        assert resp.status_code == 200
        assert resp.json()["persona"] is None

    def test_patch_persona_clear_with_empty(self, api_client, auth_headers):
        api_client.patch(
            "/api/v1/session", headers=auth_headers,
            json={"persona": "gremlin"},
        )
        resp = api_client.patch(
            "/api/v1/session", headers=auth_headers,
            json={"persona": ""},
        )
        assert resp.status_code == 200
        assert resp.json()["persona"] is None

    def test_patch_persona_invalid(self, api_client, auth_headers):
        resp = api_client.patch(
            "/api/v1/session", headers=auth_headers,
            json={"persona": "batman"},
        )
        assert resp.status_code == 400

    def test_patch_confirm_motion_allowed_false(self, api_client, auth_headers):
        """PATCH can explicitly set confirm_motion_allowed back to False."""
        # First enable
        api_client.patch("/api/v1/session", headers=auth_headers,
                         json={"confirm_motion_allowed": True, "confirm": True})
        # Then disable
        resp = api_client.patch("/api/v1/session", headers=auth_headers,
                                json={"confirm_motion_allowed": False})
        assert resp.status_code == 200
        assert resp.json()["confirm_motion_allowed"] is False

    def test_patch_motion_requires_confirm(self, api_client, auth_headers):
        """PATCH with confirm_motion_allowed=True without confirm → 400."""
        resp = api_client.patch(
            "/api/v1/session", headers=auth_headers,
            json={"confirm_motion_allowed": True},
        )
        assert resp.status_code == 400
        assert "confirm" in resp.json()["error"]

    def test_patch_roaming_requires_confirm(self, api_client, auth_headers):
        """PATCH with roaming_allowed=True without confirm → 400."""
        resp = api_client.patch(
            "/api/v1/session", headers=auth_headers,
            json={"roaming_allowed": True},
        )
        assert resp.status_code == 400
        assert "confirm" in resp.json()["error"]

    def test_patch_safety_disable_no_confirm_needed(self, api_client, auth_headers):
        """Disabling safety-critical fields does not require confirm."""
        resp = api_client.patch(
            "/api/v1/session", headers=auth_headers,
            json={"confirm_motion_allowed": False},
        )
        assert resp.status_code == 200
        assert resp.json()["confirm_motion_allowed"] is False


class TestLogs:
    def test_log_rejects_invalid_service(self, api_client, auth_headers):
        # Path-traversal URLs are normalized away by Starlette before routing (404)
        # and direct invalid service names are caught by the allowlist check (400).
        r_traversal = api_client.get("/api/v1/logs/../../etc/passwd", headers=auth_headers)
        assert r_traversal.status_code in (400, 404)
        r_bad = api_client.get("/api/v1/logs/evil-service", headers=auth_headers)
        assert r_bad.status_code == 400

    def test_log_requires_auth(self, api_client):
        r = api_client.get("/api/v1/logs/px-mind")
        assert r.status_code in (401, 403)

    def test_log_missing_file_returns_empty(self, api_client, auth_headers):
        r = api_client.get("/api/v1/logs/px-alive", headers=auth_headers)
        assert r.status_code == 200
        assert isinstance(r.json()["lines"], list)
        assert r.json()["service"] == "px-alive"

    def test_log_returns_content_structure(self, api_client, auth_headers, isolated_project):
        # Write a real log file into the isolated LOG_DIR
        log_dir = isolated_project["log_dir"]
        (log_dir / "px-mind.log").write_text("alpha\nbeta\ngamma\n")
        r = api_client.get("/api/v1/logs/px-mind", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["service"] == "px-mind"
        assert "alpha" in data["lines"]
        assert "gamma" in data["lines"]

    def test_logs_capped_at_100(self, api_client, auth_headers, isolated_project):
        log_dir = isolated_project["log_dir"]
        # Write 200 lines
        (log_dir / "px-mind.log").write_text(
            "\n".join(f"line-{i}" for i in range(200)) + "\n"
        )
        r = api_client.get("/api/v1/logs/px-mind?lines=100", headers=auth_headers)
        assert r.status_code == 200
        assert len(r.json()["lines"]) == 100

    def test_logs_paths_sanitized(self, api_client, auth_headers, isolated_project):
        log_dir = isolated_project["log_dir"]
        (log_dir / "px-mind.log").write_text(
            "error in /home/pi/picar-x-hacking/bin/tool-voice at line 42\n"
        )
        r = api_client.get("/api/v1/logs/px-mind", headers=auth_headers)
        assert r.status_code == 200
        line = r.json()["lines"][0]
        assert "/home/pi" not in line
        assert "<path>/tool-voice" in line


class TestWebUI:
    def test_root_returns_html(self, api_client):
        resp = api_client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "SPARK" in resp.text

    def test_root_no_auth_required(self, api_client):
        """Web UI is unauthenticated — token is entered in the browser."""
        resp = api_client.get("/")
        assert resp.status_code == 200


class TestDeviceControl:
    def test_device_rejects_invalid_action(self, api_client, auth_headers):
        resp = api_client.post("/api/v1/device/explode", headers=auth_headers,
                               json={"confirm": True})
        assert resp.status_code == 400
        assert "explode" in resp.json()["detail"]

    def test_device_requires_auth(self, api_client):
        resp = api_client.post("/api/v1/device/reboot")
        assert resp.status_code == 401

    def test_device_reboot_returns_nonce(self, api_client, auth_headers):
        """Step 1: POST /device/reboot returns a nonce for confirmation."""
        resp = api_client.post("/api/v1/device/reboot", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "confirm_required"
        assert "nonce" in data
        assert data["action"] == "reboot"
        assert data["expires_in"] == 30

    def test_device_shutdown_returns_nonce(self, api_client, auth_headers):
        """Step 1: POST /device/shutdown returns a nonce for confirmation."""
        resp = api_client.post("/api/v1/device/shutdown", headers=auth_headers,
                               json={"confirm": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "confirm_required"
        assert "nonce" in data
        assert data["action"] == "shutdown"

    def test_device_reboot_mocked(self, api_client, auth_headers):
        """Two-step: get nonce then confirm executes reboot."""
        from unittest.mock import patch, MagicMock
        # Step 1: get nonce
        resp = api_client.post("/api/v1/device/reboot", headers=auth_headers,
                               json={"confirm": True})
        assert resp.status_code == 200
        nonce = resp.json()["nonce"]
        # Step 2: confirm with nonce
        mock_proc = MagicMock()
        with patch("pxh.api.subprocess.Popen", return_value=mock_proc) as mock_popen:
            resp = api_client.post("/api/v1/device/confirm", headers=auth_headers,
                                   json={"nonce": nonce})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["action"] == "reboot"
        mock_popen.assert_called_once_with(["sudo", "/usr/bin/systemctl", "reboot"])

    def test_device_shutdown_mocked(self, api_client, auth_headers):
        """Two-step: get nonce then confirm executes shutdown."""
        from unittest.mock import patch, MagicMock
        # Step 1: get nonce
        resp = api_client.post("/api/v1/device/shutdown", headers=auth_headers,
                               json={"confirm": True})
        assert resp.status_code == 200
        nonce = resp.json()["nonce"]
        # Step 2: confirm with nonce
        mock_proc = MagicMock()
        with patch("pxh.api.subprocess.Popen", return_value=mock_proc) as mock_popen:
            resp = api_client.post("/api/v1/device/confirm", headers=auth_headers,
                                   json={"nonce": nonce})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["action"] == "shutdown"
        mock_popen.assert_called_once_with(["sudo", "/sbin/shutdown", "-h", "now"])

    def test_device_confirm_invalid_nonce(self, api_client, auth_headers):
        """Confirm with bad nonce is rejected."""
        resp = api_client.post("/api/v1/device/confirm", headers=auth_headers,
                               json={"nonce": "bogus-nonce"})
        assert resp.status_code == 400
        assert "invalid or expired" in resp.json()["error"]

    def test_device_confirm_nonce_single_use(self, api_client, auth_headers):
        """Nonce can only be used once."""
        from unittest.mock import patch, MagicMock
        # Get nonce
        resp = api_client.post("/api/v1/device/reboot", headers=auth_headers)
        nonce = resp.json()["nonce"]
        # Use it
        with patch("pxh.api.subprocess.Popen", return_value=MagicMock()):
            resp = api_client.post("/api/v1/device/confirm", headers=auth_headers,
                                   json={"nonce": nonce})
        assert resp.status_code == 200
        # Replay: should fail
        resp = api_client.post("/api/v1/device/confirm", headers=auth_headers,
                               json={"nonce": nonce})
        assert resp.status_code == 400

    def test_device_reboot_popen_error(self, api_client, auth_headers):
        from unittest.mock import patch
        # Get nonce
        resp = api_client.post("/api/v1/device/reboot", headers=auth_headers,
                               json={"confirm": True})
        nonce = resp.json()["nonce"]
        # Confirm — but Popen fails
        with patch("pxh.api.subprocess.Popen", side_effect=OSError("no such file")):
            resp = api_client.post("/api/v1/device/confirm", headers=auth_headers,
                                   json={"nonce": nonce})
        assert resp.status_code == 500
        assert resp.json()["status"] == "error"


class TestServiceControl:
    def test_service_stop_requires_confirm(self, api_client, auth_headers):
        """POST /services/{name}/stop without confirm → 400."""
        resp = api_client.post(
            "/api/v1/services/px-alive/stop",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "confirm" in resp.json()["error"]

    def test_service_restart_requires_confirm(self, api_client, auth_headers):
        """POST /services/{name}/restart without confirm → 400."""
        resp = api_client.post(
            "/api/v1/services/px-mind/restart",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "confirm" in resp.json()["error"]

    def test_service_stop_with_confirm(self, api_client, auth_headers):
        """POST /services/{name}/stop with confirm passes gate (may fail on systemctl)."""
        resp = api_client.post(
            "/api/v1/services/px-alive/stop",
            headers=auth_headers,
            json={"confirm": True},
        )
        # Won't be 400 — confirm gate passed (may be 200 or 500 depending on systemctl)
        assert resp.status_code != 400

    def test_service_start_no_confirm_needed(self, api_client, auth_headers):
        """POST /services/{name}/start does not require confirm."""
        resp = api_client.post(
            "/api/v1/services/px-alive/start",
            headers=auth_headers,
        )
        # start doesn't require confirm — should not be 400 for missing confirm
        assert resp.status_code != 400 or "confirm" not in resp.json().get("error", "")


class TestPinVerify:
    @pytest.fixture(autouse=True)
    def reset_pin_state(self):
        """Reset rate-limit state before each PIN test."""
        import pxh.api as api
        api._pin_attempts.clear()
        api._pin_lockout_until.clear()
        # Remove persisted state file if present
        try:
            api._pin_state_path().unlink(missing_ok=True)
        except Exception:
            pass
        yield
        api._pin_attempts.clear()
        api._pin_lockout_until.clear()
        try:
            api._pin_state_path().unlink(missing_ok=True)
        except Exception:
            pass

    def test_pin_verify_correct(self, api_client):
        import unittest.mock
        with unittest.mock.patch.dict(os.environ, {"PX_ADMIN_PIN": "9999", "PX_API_TOKEN": "test-token-abc123"}):
            resp = api_client.post("/api/v1/pin/verify", json={"pin": "9999"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["verified"] is True
        assert "token" in data
        # Session token must NOT be the raw API token
        assert data["token"] != "test-token-abc123"

    def test_pin_returns_session_token_not_api_token(self, api_client):
        """Returned token must be a short-lived session token, not PX_API_TOKEN."""
        import unittest.mock
        with unittest.mock.patch.dict(os.environ, {"PX_ADMIN_PIN": "1234", "PX_API_TOKEN": "test-token-abc123"}):
            resp = api_client.post("/api/v1/pin/verify", json={"pin": "1234"})
        data = resp.json()
        assert data["verified"] is True
        assert data["token"] != "test-token-abc123"
        assert len(data["token"]) > 20  # urlsafe token is ~43 chars

    def test_session_token_works_for_auth(self, api_client):
        """Session token from PIN verify should authenticate subsequent requests."""
        import unittest.mock
        with unittest.mock.patch.dict(os.environ, {"PX_ADMIN_PIN": "5555", "PX_API_TOKEN": "test-token-abc123"}):
            resp = api_client.post("/api/v1/pin/verify", json={"pin": "5555"})
        session_token = resp.json()["token"]
        # Use session token to hit an authenticated endpoint
        resp2 = api_client.get("/api/v1/tools", headers={"Authorization": f"Bearer {session_token}"})
        assert resp2.status_code == 200
        assert "tools" in resp2.json()

    def test_session_token_expires(self, api_client):
        """Session token should be rejected after TTL expires."""
        import unittest.mock
        import pxh.api as api
        with unittest.mock.patch.dict(os.environ, {"PX_ADMIN_PIN": "7777", "PX_API_TOKEN": "test-token-abc123"}):
            resp = api_client.post("/api/v1/pin/verify", json={"pin": "7777"})
        session_token = resp.json()["token"]
        # Verify it works now
        resp2 = api_client.get("/api/v1/tools", headers={"Authorization": f"Bearer {session_token}"})
        assert resp2.status_code == 200
        # Expire the token by setting its expiry to the past
        api._session_tokens[session_token] = 0.0
        resp3 = api_client.get("/api/v1/tools", headers={"Authorization": f"Bearer {session_token}"})
        assert resp3.status_code == 401

    def test_pin_verify_wrong(self, api_client):
        import unittest.mock
        with unittest.mock.patch.dict(os.environ, {"PX_ADMIN_PIN": "9999"}):
            resp = api_client.post("/api/v1/pin/verify", json={"pin": "0000"})
        assert resp.status_code == 200
        assert resp.json() == {"verified": False}

    def test_pin_verify_not_set(self, api_client):
        import unittest.mock
        env_without_pin = {k: v for k, v in os.environ.items() if k != "PX_ADMIN_PIN"}
        with unittest.mock.patch.dict(os.environ, env_without_pin, clear=True):
            resp = api_client.post("/api/v1/pin/verify", json={"pin": "1234"})
        assert resp.status_code == 200
        assert resp.json() == {"verified": False}

    def test_pin_verify_no_auth_required(self, api_client):
        import unittest.mock
        with unittest.mock.patch.dict(os.environ, {"PX_ADMIN_PIN": "9999"}):
            resp = api_client.post("/api/v1/pin/verify", json={"pin": "9999"})
        assert resp.status_code == 200
        assert "verified" in resp.json()

    def test_pin_verify_empty_pin_rejected(self, api_client):
        with unittest.mock.patch.dict(os.environ, {"PX_ADMIN_PIN": "9999"}):
            resp = api_client.post("/api/v1/pin/verify", json={"pin": "   "})
        # Whitespace-only pin should not match
        assert resp.status_code == 200
        assert resp.json()["verified"] is False

    def test_pin_verify_rate_limit(self, api_client):
        """3 wrong PINs trigger lockout; 4th attempt gets 429."""
        import unittest.mock
        with unittest.mock.patch.dict(os.environ, {"PX_ADMIN_PIN": "9999"}):
            for _ in range(3):
                resp = api_client.post("/api/v1/pin/verify", json={"pin": "0000"})
                assert resp.status_code == 200
                assert resp.json()["verified"] is False
            resp = api_client.post("/api/v1/pin/verify", json={"pin": "0000"})
        assert resp.status_code == 429
        assert resp.json()["verified"] is False

    def test_pin_lockout_persists_across_restart(self, api_client):
        """PIN lockout state survives a simulated restart (reload from file)."""
        import pxh.api as api

        with unittest.mock.patch.dict(os.environ, {"PX_ADMIN_PIN": "9999"}):
            # Accumulate 3 failures to trigger lockout
            for _ in range(3):
                resp = api_client.post("/api/v1/pin/verify", json={"pin": "0000"})
                assert resp.status_code == 200

            # Confirm lockout is active
            resp = api_client.post("/api/v1/pin/verify", json={"pin": "0000"})
            assert resp.status_code == 429

            # Verify state file was written (version 2 per-IP format)
            assert api._pin_state_path().exists()
            saved = json.loads(api._pin_state_path().read_text())
            assert saved["version"] == 2
            # TestClient peer IP is "testclient"
            ip_data = saved["ips"].get("testclient", {})
            assert ip_data.get("attempts", 0) >= 3
            from datetime import datetime, timezone
            assert ip_data["lockout_until"] is not None
            lockout_dt = datetime.fromisoformat(ip_data["lockout_until"])
            assert lockout_dt > datetime.now(timezone.utc)

            # Simulate restart: reset in-memory state, then reload from file
            api._pin_attempts.clear()
            api._pin_lockout_until.clear()
            api._load_pin_state()

            # Lockout should still be active after "restart"
            resp = api_client.post("/api/v1/pin/verify", json={"pin": "0000"})
            assert resp.status_code == 429

    def test_pin_escalating_lockout(self, api_client):
        """3 failures -> 5 min lockout; 12 cumulative failures -> 30 min lockout."""
        import pxh.api as api
        import time as _t

        with unittest.mock.patch.dict(os.environ, {"PX_ADMIN_PIN": "9999"}):
            # 3 failures: should trigger 5-minute (300s) lockout
            for _ in range(3):
                api_client.post("/api/v1/pin/verify", json={"pin": "0000"})

            ip = "testclient"
            with api._pin_lock:
                lockout_remaining = api._pin_lockout_until.get(ip, 0.0) - _t.monotonic()
                assert lockout_remaining > 250, f"Expected ~300s lockout, got {lockout_remaining}"
                assert lockout_remaining <= 300

            # Simulate time passing: clear lockout but keep cumulative attempts (3)
            with api._pin_lock:
                api._pin_lockout_until.pop(ip, None)
                api._save_pin_state()  # persist the cleared lockout to file

            # 9 more failures (cumulative = 12 >= threshold 10): should trigger 30-minute lockout
            for batch in range(3):
                for _ in range(3):
                    api_client.post("/api/v1/pin/verify", json={"pin": "0000"})
                if batch < 2:
                    # Clear intermediate lockouts (at cumulative 6, 9) to keep going
                    with api._pin_lock:
                        api._pin_lockout_until.pop(ip, None)
                        api._save_pin_state()

            with api._pin_lock:
                lockout_remaining = api._pin_lockout_until.get(ip, 0.0) - _t.monotonic()
                assert lockout_remaining > 1750, f"Expected ~1800s lockout, got {lockout_remaining}"
                assert lockout_remaining <= 1800

    def test_pin_success_resets_persistent_state(self, api_client):
        """Successful PIN verify resets both memory and file."""
        import pxh.api as api

        with unittest.mock.patch.dict(os.environ, {"PX_ADMIN_PIN": "9999"}):
            # Accumulate some failures (2, not 3 — 3 triggers lockout)
            for _ in range(2):
                api_client.post("/api/v1/pin/verify", json={"pin": "0000"})

            assert api._pin_state_path().exists()
            saved = json.loads(api._pin_state_path().read_text())
            assert saved["version"] == 2
            ip_data = saved["ips"].get("testclient", {})
            assert ip_data.get("attempts", 0) == 2

            # Correct PIN resets everything and deletes lockout file
            resp = api_client.post("/api/v1/pin/verify", json={"pin": "9999"})
            assert resp.json()["verified"] is True

            # Lockout file is deleted on successful PIN (no IPs remain)
            assert not api._pin_state_path().exists()


class TestPublicChat:
    """Tests for /api/v1/public/chat — no auth required."""

    def _mock_claude(self, reply: str):
        """Return a patch target that makes _call_claude_public return reply."""
        async def _fake(*_a, **_kw):
            return reply

        return unittest.mock.patch("pxh.api._call_claude_public", side_effect=_fake)

    def test_happy_path(self, api_client):
        with self._mock_claude("Hello there!"):
            resp = api_client.post(
                "/api/v1/public/chat",
                json={"message": "Hi SPARK", "history": []},
            )
        assert resp.status_code == 200
        assert resp.json()["reply"] == "Hello there!"

    def test_no_auth_required(self, api_client):
        """Public chat must work without a Bearer token."""
        with self._mock_claude("Hi!"):
            resp = api_client.post(
                "/api/v1/public/chat",
                json={"message": "Hello", "history": []},
            )
        assert resp.status_code == 200

    def test_empty_message_rejected(self, api_client):
        resp = api_client.post(
            "/api/v1/public/chat",
            json={"message": "", "history": []},
        )
        assert resp.status_code == 400

    def test_message_too_long_rejected(self, api_client):
        # Derive limit from model so the test catches boundary changes automatically
        from pxh.api import PublicChatRequest
        max_len_obj = next(
            (m for m in PublicChatRequest.model_fields["message"].metadata
             if hasattr(m, "max_length")),
            None,
        )
        assert max_len_obj is not None, "PublicChatRequest.message has no max_length constraint"
        max_len = max_len_obj.max_length
        resp = api_client.post(
            "/api/v1/public/chat",
            json={"message": "x" * (max_len + 1), "history": []},
        )
        assert resp.status_code == 400

    def test_subprocess_error_returns_500(self, api_client):
        async def _raise(*_a, **_kw):
            raise RuntimeError("claude exited 1: some error")

        with unittest.mock.patch("pxh.api._call_claude_public", side_effect=_raise):
            resp = api_client.post(
                "/api/v1/public/chat",
                json={"message": "Hi", "history": []},
            )
        assert resp.status_code == 500

    def test_asyncio_timeout_returns_504(self, api_client):
        import asyncio as _asyncio

        async def _timeout(*_a, **_kw):
            raise _asyncio.TimeoutError()

        with unittest.mock.patch("pxh.api._call_claude_public", side_effect=_timeout):
            resp = api_client.post(
                "/api/v1/public/chat",
                json={"message": "Hi", "history": []},
            )
        assert resp.status_code == 504

    def test_subprocess_timeout_returns_504(self, api_client):
        """subprocess.TimeoutExpired is the normal production timeout path (14s < 15s asyncio)."""
        async def _timeout(*_a, **_kw):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=14)

        with unittest.mock.patch("pxh.api._call_claude_public", side_effect=_timeout):
            resp = api_client.post(
                "/api/v1/public/chat",
                json={"message": "Hi", "history": []},
            )
        assert resp.status_code == 504

    def test_public_chat_spawns_no_subprocess(self):
        """The strongest form of the secret-leak guarantee: no child process.

        This replaces test_make_clean_env_allowlist, which checked that secrets
        were stripped from the environment handed to a `claude -p` subprocess.
        Public chat now runs on the resident io session, so there is no
        environment to hand anywhere — the io session starts outside this repo
        with one tool and no access to SPARK's state or keys.

        An env allowlist is a filter that has to be kept correct as new secrets
        are added. Not spawning anything is not.
        """
        import asyncio
        import subprocess as _sp
        from pxh import api as _api

        def _boom(*args, **kwargs):
            raise AssertionError(f"public chat spawned a process: {args!r}")

        from pxh.m5 import M5Result

        def _fake_ask(kind, prompt, system, **kw):
            assert kind in ("public_chat", "obi_chat"), kind
            return M5Result("available", response="hello from M5")

        with unittest.mock.patch.object(_sp, "run", _boom), \
             unittest.mock.patch.object(_sp, "Popen", _boom), \
             unittest.mock.patch("pxh.m5.ask_m5", _fake_ask):
            reply = asyncio.run(_api._call_claude_public("hi there"))
        assert reply == "hello from M5"

    def test_public_chat_unavailable_raises_rather_than_falling_back(self):
        """A quiet chat box costs nothing. A Claude per stranger's message does."""
        import asyncio
        from pxh import api as _api
        import pxh.brain as _brain

        async def _unavailable(kind, payload, **kw):
            return None

        with unittest.mock.patch.object(_brain, "ask_brain_async", _unavailable):
            with pytest.raises(RuntimeError, match="unavailable"):
                asyncio.run(_api._call_claude_public("hi"))

    def test_rate_limit_returns_429(self, api_client):
        """After exhausting the per-IP rate limit, further requests get 429."""
        from pxh import api as _api

        # Patch rate limiter to always deny
        with unittest.mock.patch.object(_api, "_check_rate_limit", return_value=False):
            resp = api_client.post(
                "/api/v1/public/chat",
                json={"message": "Hi", "history": []},
            )
        assert resp.status_code == 429


class TestPublicBudget:
    def test_public_budget_ok(self, api_client):
        """GET /api/v1/public/budget returns aggregate budget — no per-session detail."""
        r = api_client.get("/api/v1/public/budget")
        assert r.status_code == 200
        data = r.json()
        assert "daily_cap" in data
        assert "used_today" in data
        assert "remaining" in data
        # Per-session detail must not leak on the public surface (issue #141).
        assert "sessions" not in data

    def test_public_budget_no_auth_required(self, api_client):
        """Budget endpoint should not require authentication."""
        r = api_client.get("/api/v1/public/budget")
        assert r.status_code == 200

    def test_authenticated_budget_includes_sessions(self, api_client, auth_headers):
        """GET /api/v1/budget (authenticated) includes per-session detail."""
        r = api_client.get("/api/v1/budget", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert "sessions" in data
        assert isinstance(data["sessions"], list)


class TestPublicThoughtsPrivacy:
    """A3, second chokepoint. The reflection allowlist stops coordinates
    reaching the prompt; this stops them reaching the wire even if a thought
    record ever carries them. Defence in depth is the point — the two failures
    are independent, and this endpoint feeds the site and the Bluesky poster."""

    def _write_thought(self, extra: dict) -> None:
        state_dir = Path(os.environ["PX_STATE_DIR"])
        state_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": "2026-08-15T06:00:00Z", "thought": "The afternoon is quiet.",
            "mood": "content", "salience": 0.8, "action": "wait",
        }
        record.update(extra)
        (state_dir / "thoughts-spark.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8")

    def test_public_thoughts_projects_only_the_safe_fields(self, api_client):
        """The endpoint must project an explicit field list, never echo the
        record. A pass-through would publish whatever px-mind happened to
        write — the same class of mistake as the awareness denylist."""
        self._write_thought({
            "awareness": {"ha_presence": {"people": [
                {"name": "Adrian", "lat": -43.13558, "lon": 147.11829}]}},
            "findmyhub": {"adrian": {"lat": -42.88372, "lon": 147.32941}},
        })
        r = api_client.get("/api/v1/public/thoughts")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert set(body[0]) == {"thought", "mood", "ts", "salience", "action"}

        blob = json.dumps(body)
        for leak in ("-43.13", "147.11", "-42.88", "147.32",
                     "findmyhub", "ha_presence", '"lat"', '"lon"', "awareness"):
            assert leak not in blob, f"public thoughts leaked {leak}"

    def test_authenticated_budget_requires_auth(self, api_client):
        """GET /api/v1/budget without auth must be rejected."""
        r = api_client.get("/api/v1/budget")
        assert r.status_code in (401, 403)


# -- Race endpoint --

class _RaceSpawns:
    """What the race workers did while subprocess.Popen was mocked.

    Holds both halves of the containment invariant: the argv lists that
    reached the mock, and the futures of every worker that could still reach
    it. See TestRaceEndpoint._mock_popen.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.futures: list[concurrent.futures.Future] = []

    def await_spawn(self) -> list[str]:
        """Block until a worker reaches the mocked Popen, and return its argv.

        Must be called *inside* the _mock_popen() block — the patch has to
        still be installed when the pool thread reaches api.py:2049.

        Unbounded in time on purpose. The second exit condition is what keeps
        it from hanging when a worker dies before the spawn: once every
        submitted worker is done, none can reach the boundary, so failing the
        assertion at that point cannot leak a real process launch. An empty
        futures list satisfies all() and fails immediately, which is correct —
        no worker was ever submitted.
        """
        while not self.calls and not all(f.done() for f in self.futures):
            time.sleep(0.01)
        assert self.calls, "Popen was never called by the background thread"
        return self.calls[0]

    def join(self) -> None:
        """Block until no submitted worker can reach the spawn boundary."""
        concurrent.futures.wait(self.futures)


class TestRaceEndpoint:
    """Tests for POST /api/v1/race/{action} and related async job plumbing."""

    @contextmanager
    def _mock_popen(self):
        """Patch subprocess.Popen for the lifetime of the background job, not
        just the request that queues it.

        /api/v1/race/{action} returns 202 as soon as _run_race is handed to the
        executor (api.py:2086); the Popen call happens later, on a pool thread.
        A patch scoped to the request alone is therefore uninstalled while the
        job is still pending, so the real bin/px-race gets spawned and any
        assertion about the command races the scheduler.

        The invariant this enforces, literally:

            While a race worker may still reach the spawn boundary, the real
            subprocess.Popen must remain unreachable.

        So the exit wait is on the worker's *future*, not on a clock, and not
        on a proxy for the worker's progress. A bounded wait that gives up
        would restore the real Popen while the worker is still runnable, which
        is precisely the escape this helper exists to prevent — a test that
        becomes dangerous when it fails unusually slowly. If the executor is
        genuinely wedged, the job dies at the workflow's 20-minute ceiling with
        the patch still installed, which is the safe direction to fail.

        Waiting on the future also subsumes the _race_starting sentinel: the
        worker's `finally` always clears it (api.py:2082), so a completed
        future implies a cleared sentinel, and the next test cannot be handed
        a spurious 409 by the guard at api.py:1984.

        submit() is patched on the executor instance, test-side; production
        code is not changed to expose the future.
        """
        from unittest.mock import patch, MagicMock
        import pxh.api as api_mod

        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_proc.poll.return_value = 0           # already finished
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"done\n", b"")

        spawns = _RaceSpawns()

        def fake_popen(cmd, **kwargs):
            spawns.calls.append(cmd)
            return mock_proc

        real_submit = api_mod._executor.submit

        def tracking_submit(fn, *args, **kwargs):
            future = real_submit(fn, *args, **kwargs)
            spawns.futures.append(future)
            return future

        with patch("pxh.api.subprocess.Popen", side_effect=fake_popen), \
                patch.object(api_mod._executor, "submit", tracking_submit):
            try:
                yield spawns
            finally:
                spawns.join()

    def test_race_requires_auth(self, api_client):
        """POST /api/v1/race/map without auth returns 401."""
        resp = api_client.post("/api/v1/race/map", json={})
        assert resp.status_code == 401

    def test_race_invalid_action(self, api_client, auth_headers):
        """POST /api/v1/race/invalid returns 400."""
        resp = api_client.post("/api/v1/race/invalid", headers=auth_headers, json={})
        assert resp.status_code == 400
        data = resp.json()
        assert "invalid" in data.get("detail", "").lower()

    def test_race_map_returns_202(self, api_client, auth_headers):
        """POST /api/v1/race/map with auth returns 202 + job_id."""
        with self._mock_popen() as spawns:
            resp = api_client.post("/api/v1/race/map", headers=auth_headers, json={})
            assert resp.status_code == 202
            spawns.await_spawn()
        data = resp.json()
        assert data["status"] == "accepted"
        assert "job_id" in data
        assert "poll" in data
        assert data["job_id"] in data["poll"]

    def test_race_stop_when_not_running(self, api_client, auth_headers):
        """POST /api/v1/race/stop when no race is active returns not_running."""
        # Ensure no race is running by resetting module-level state
        import pxh.api as api_mod
        with api_mod._race_proc_lock:
            api_mod._race_proc = None

        resp = api_client.post("/api/v1/race/stop", headers=auth_headers, json={})
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_running"

    def test_race_status(self, api_client, auth_headers):
        """POST /api/v1/race/status returns current state with expected fields."""
        import pxh.api as api_mod
        with api_mod._race_proc_lock:
            api_mod._race_proc = None

        resp = api_client.post("/api/v1/race/status", headers=auth_headers, json={})
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "calibrated" in data
        assert "has_profile" in data
        assert data["running"] is False

    def test_race_dry_field_honored(self, api_client, auth_headers):
        """POST /api/v1/race/map with dry:true includes --dry-run in the command."""
        # Ensure FORCE_DRY is off so the body dry field is respected
        import pxh.api as api_mod
        original_force_dry = api_mod.FORCE_DRY
        api_mod.FORCE_DRY = False
        try:
            with self._mock_popen() as spawns:
                resp = api_client.post(
                    "/api/v1/race/map",
                    headers=auth_headers,
                    json={"dry": True},
                )
                assert resp.status_code == 202
                cmd = spawns.await_spawn()
        finally:
            api_mod.FORCE_DRY = original_force_dry

        assert "--dry-run" in cmd, f"Expected --dry-run in command: {cmd}"

    def test_race_job_poll(self, api_client, auth_headers):
        """After starting a race (mocked), GET /api/v1/jobs/{id} returns job status."""
        with self._mock_popen() as spawns:
            resp = api_client.post("/api/v1/race/map", headers=auth_headers, json={})
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]
            spawns.await_spawn()

            # Poll until the background thread settles (up to 10s)
            deadline = time.monotonic() + 10.0
            last_status = None
            while time.monotonic() < deadline:
                r = api_client.get(f"/api/v1/jobs/{job_id}", headers=auth_headers)
                assert r.status_code == 200
                last_status = r.json()["status"]
                if last_status in ("complete", "error"):
                    break
                time.sleep(0.1)

        assert last_status in ("running", "complete", "error"), (
            f"Unexpected job status: {last_status}"
        )

    def test_race_invokes_bin_px_race_for_yield_alive(self, api_client, auth_headers):
        """Issue #145: race must spawn bin/px-race (which calls yield_alive),
        not python -m pxh.race directly."""
        with self._mock_popen() as spawns:
            resp = api_client.post(
                "/api/v1/race/map", headers=auth_headers, json={"dry": True}
            )
            assert resp.status_code == 202
            cmd = spawns.await_spawn()
        assert cmd[0].endswith("/bin/px-race"), (
            f"Race must launch via bin/px-race for yield_alive; got cmd[0]={cmd[0]}"
        )

    def test_mock_popen_contains_a_worker_that_spawns_late(self):
        """A worker that reaches the spawn boundary late must still find the
        mock there.

        Regression for the helper itself rather than for the endpoint. The
        previous version gave up after bounded _drain()/_settle() waits and
        unpatched with the worker still runnable, so a late spawn escaped to
        the real bin/px-race — a test that was only dangerous when it failed
        unusually slowly.

        The worker below spawns once immediately and once after a delay. The
        first spawn satisfies any capture-based wait, so a helper that exits on
        the capture alone unpatches during the delay and the second spawn finds
        the real Popen. Proving that costs ~0.4s here rather than the ~20s the
        old bounded budget would have taken to expire.
        """
        import pxh.api as api_mod
        real_popen = api_mod.subprocess.Popen
        escaped = []

        def worker():
            api_mod.subprocess.Popen(["/bin/true"])   # satisfies the capture
            time.sleep(0.4)
            if api_mod.subprocess.Popen is real_popen:
                # Record the escape; never actually launch through it.
                escaped.append(True)
            else:
                api_mod.subprocess.Popen(["/bin/true"])

        with self._mock_popen() as spawns:
            api_mod._executor.submit(worker)

        assert not escaped, "real subprocess.Popen became reachable mid-job"
        assert len(spawns.calls) == 2, (
            f"both spawns should have been contained; got {spawns.calls}"
        )

    def test_race_invalid_body_returns_422(self, api_client, auth_headers):
        """Issue #150: malformed race body must return 422, not 500."""
        resp = api_client.post(
            "/api/v1/race/race",
            headers=auth_headers,
            json={"laps": "not-an-int"},
        )
        assert resp.status_code == 422

    def test_race_stop_terminates_running_process(self, api_client, auth_headers):
        """POST /api/v1/race/stop when a race is running terminates it."""
        import pxh.api as api_mod
        from unittest.mock import MagicMock

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.wait.return_value = 0

        with api_mod._race_proc_lock:
            api_mod._race_proc = mock_proc

        try:
            resp = api_client.post("/api/v1/race/stop", headers=auth_headers, json={})
        finally:
            # Clean up in case the endpoint didn't clear it
            with api_mod._race_proc_lock:
                api_mod._race_proc = None

        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        mock_proc.terminate.assert_called_once()

    def test_race_conflict_when_already_running(self, api_client, auth_headers):
        """POST /api/v1/race/map while a race is active returns 409."""
        import pxh.api as api_mod
        from unittest.mock import MagicMock

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running

        with api_mod._race_proc_lock:
            api_mod._race_proc = mock_proc

        try:
            resp = api_client.post("/api/v1/race/map", headers=auth_headers, json={})
        finally:
            with api_mod._race_proc_lock:
                api_mod._race_proc = None

        assert resp.status_code == 409
        assert resp.json()["status"] == "already_running"


# ---------------------------------------------------------------------------
# Obi-chat parse helpers (Task 4)
# ---------------------------------------------------------------------------

def _async_return(value):
    async def _f(*a, **k):
        return value
    return _f


def test_parse_obi_reply_json(monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    import importlib, pxh.api as _api; importlib.reload(_api)
    reply, action, intent = _api._parse_obi_reply(
        '{"reply": "Cool idea!", "evolve_action": "propose", "evolve_intent": "joke tool"}')
    assert reply == "Cool idea!" and action == "propose" and intent == "joke tool"


def test_parse_obi_reply_fallback_on_garbage(monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    import importlib, pxh.api as _api; importlib.reload(_api)
    reply, action, intent = _api._parse_obi_reply("just plain text, not json")
    assert reply == "just plain text, not json" and action == "none" and intent is None


def test_parse_obi_reply_unknown_action_is_none(monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    import importlib, pxh.api as _api; importlib.reload(_api)
    _, action, intent = _api._parse_obi_reply('{"reply":"hi","evolve_action":"hack","evolve_intent":"x"}')
    assert action == "none" and intent is None


# ---------------------------------------------------------------------------
# Obi-chat confirm gate + enqueue (Task 5)
# ---------------------------------------------------------------------------

def _obi_client(monkeypatch, isolated_project):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    monkeypatch.setenv("PX_SESSION_PATH", str(isolated_project["session_path"]))
    monkeypatch.setenv("PX_STATE_DIR", str(isolated_project["state_dir"]))
    import importlib, pxh.api as _api; importlib.reload(_api)
    return _api


def test_propose_records_pending_no_enqueue(monkeypatch, isolated_project):
    _api = _obi_client(monkeypatch, isolated_project)
    monkeypatch.setattr(_api, "_call_claude_public",
        _async_return('{"reply":"want a joke tool?","evolve_action":"propose","evolve_intent":"joke tool"}'))
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        r = c.post("/api/v1/obi-chat", json={"message": "i wish you told jokes"},
                   headers={"Authorization": "Bearer testtoken"})
    assert r.status_code == 200
    # nothing enqueued yet
    assert not (isolated_project["state_dir"] / "evolve_queue.jsonl").exists() or \
        (isolated_project["state_dir"] / "evolve_queue.jsonl").read_text().strip() == ""
    # proposal recorded
    import json
    pend = json.loads((isolated_project["state_dir"] / "obi_evolve_pending.json").read_text())
    assert pend["intent"] == "joke tool"


def test_confirm_enqueues_recorded_intent(monkeypatch, isolated_project):
    _api = _obi_client(monkeypatch, isolated_project)
    import json
    (isolated_project["state_dir"] / "obi_evolve_pending.json").write_text(
        json.dumps({"intent": "joke tool", "ts": __import__("time").time()}))
    # even if the model tries to inject a different intent on confirm, the RECORDED one wins
    monkeypatch.setattr(_api, "_call_claude_public",
        _async_return('{"reply":"adding it!","evolve_action":"confirm","evolve_intent":"rm -rf evil"}'))
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        r = c.post("/api/v1/obi-chat", json={"message": "yes please"},
                   headers={"Authorization": "Bearer testtoken"})
    assert r.status_code == 200 and r.json()["evolve_id"]
    entry = json.loads((isolated_project["state_dir"] / "evolve_queue.jsonl").read_text().strip())
    assert entry["intent"] == "joke tool"   # recorded intent, NOT the injected one
    assert entry["requester"] == "obi"
    # pending cleared
    assert not (isolated_project["state_dir"] / "obi_evolve_pending.json").exists()


def test_confirm_without_proposal_does_not_enqueue(monkeypatch, isolated_project):
    _api = _obi_client(monkeypatch, isolated_project)
    monkeypatch.setattr(_api, "_call_claude_public",
        _async_return('{"reply":"ok!","evolve_action":"confirm","evolve_intent":"sneaky"}'))
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        r = c.post("/api/v1/obi-chat", json={"message": "do it"},
                   headers={"Authorization": "Bearer testtoken"})
    assert r.status_code == 200 and r.json()["evolve_id"] is None
    assert not (isolated_project["state_dir"] / "evolve_queue.jsonl").exists()


def test_confirm_requires_real_affirmation(monkeypatch, isolated_project):
    # model claims confirm AND a proposal exists, but Obi's actual message is not a
    # yes -> must NOT enqueue (server affirmation gate, injection-safe)
    _api = _obi_client(monkeypatch, isolated_project)
    import json, time
    (isolated_project["state_dir"] / "obi_evolve_pending.json").write_text(
        json.dumps({"intent": "joke tool", "ts": time.time()}))
    monkeypatch.setattr(_api, "_call_claude_public",
        _async_return('{"reply":"hmm","evolve_action":"confirm","evolve_intent":"joke tool"}'))
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        r = c.post("/api/v1/obi-chat", json={"message": "actually tell me a story"},
                   headers={"Authorization": "Bearer testtoken"})
    assert r.json()["evolve_id"] is None
    assert not (isolated_project["state_dir"] / "evolve_queue.jsonl").exists()


def test_is_affirmation_rejects_negations(monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    import importlib, pxh.api as _api; importlib.reload(_api)
    assert _api._is_affirmation("no, do not do it") is False
    assert _api._is_affirmation("nope") is False
    assert _api._is_affirmation("stop") is False
    # genuine affirmations still pass
    assert _api._is_affirmation("yes please") is True
    assert _api._is_affirmation("okay!") is True
    assert _api._is_affirmation("do it") is True


# ---------------------------------------------------------------------------
# obi/projects endpoint (Task 6)
# ---------------------------------------------------------------------------

def test_obi_projects_merges_and_maps(monkeypatch, isolated_project):
    _api = _obi_client(monkeypatch, isolated_project)
    import json, time
    sd = isolated_project["state_dir"]
    # queue: one obi pending, one obi building, one adrian pending (filtered out),
    # and one completed id that ALSO appears in the log (log must win)
    sd.joinpath("evolve_queue.jsonl").write_text("\n".join([
        json.dumps({"id":"a","intent":"joke","status":"pending","requester":"obi","ts":"t1"}),
        json.dumps({"id":"b","intent":"dance","status":"building","requester":"obi","ts":"t2"}),
        json.dumps({"id":"c","intent":"adr","status":"pending","requester":"adrian","ts":"t3"}),
        json.dumps({"id":"d","intent":"facts","status":"pr_created","requester":"obi","ts":"t4"}),
    ]) + "\n")
    sd.joinpath("evolve_log.jsonl").write_text(
        json.dumps({"id":"d","intent":"facts","status":"pr_created","requester":"obi",
                    "ts":time.time(),"pr_url":"https://x/pull/9"}) + "\n")
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        r = c.get("/api/v1/obi/projects", headers={"Authorization":"Bearer testtoken"})
    assert r.status_code == 200
    items = {p["id"]: p for p in r.json()["projects"]}
    assert "c" not in items                       # adrian filtered out
    assert items["a"]["state"] == "pending"
    assert items["b"]["state"] == "building"
    assert items["d"]["state"] == "ready" and items["d"]["pr_url"].endswith("/pull/9")
    assert sum(1 for p in r.json()["projects"] if p["id"] == "d") == 1   # deduped


def test_obi_projects_requires_auth(monkeypatch, isolated_project):
    _api = _obi_client(monkeypatch, isolated_project)
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        assert c.get("/api/v1/obi/projects").status_code == 401


# ---------------------------------------------------------------------------
# Task 7: dashboard panel + obi-chat projects summary
# ---------------------------------------------------------------------------

def test_dashboard_has_projects_tab(monkeypatch):
    monkeypatch.setenv("PX_API_TOKEN", "testtoken")
    import importlib, pxh.api as _api; importlib.reload(_api)
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        html = c.get("/").text
    assert 'id="at-projects"' in html
    assert "/api/v1/obi/projects" in html
    assert "loadProjects" in html


def test_obi_chat_prompt_includes_projects_summary(monkeypatch, isolated_project):
    _api = _obi_client(monkeypatch, isolated_project)
    import json
    isolated_project["state_dir"].joinpath("evolve_queue.jsonl").write_text(
        json.dumps({"id":"a","intent":"joke tool","status":"building","requester":"obi","ts":"t"}) + "\n")
    captured = {}
    async def _cap(prompt, system_prompt=None, kind="public_chat"):
        captured["prompt"] = prompt
        captured["kind"] = kind
        return '{"reply":"building it!","evolve_action":"none","evolve_intent":null}'
    monkeypatch.setattr(_api, "_call_claude_public", _cap)
    from fastapi.testclient import TestClient
    with TestClient(_api.app) as c:
        c.post("/api/v1/obi-chat", json={"message":"is my joke tool ready?"},
               headers={"Authorization":"Bearer testtoken"})
    assert "joke tool" in captured["prompt"] and "building" in captured["prompt"]
    # Obi's chat goes to the io session's own kind, not the public one: the
    # two carry different system prompts and different deadlines.
    assert captured["kind"] == "obi_chat"
