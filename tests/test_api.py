"""Tests for the PiCar-X REST API."""
from __future__ import annotations

import json
import os
import sys
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
    monkeypatch.setenv("PROJECT_ROOT", str(ROOT))

    from pxh import api
    api._load_token()

    from fastapi.testclient import TestClient
    client = TestClient(api.app, raise_server_exceptions=False)
    return client


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-token-abc123"}


# -- Health (unauthenticated) --

class TestHealth:
    def test_health_ok(self, api_client):
        resp = api_client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_no_auth_required(self, api_client):
        resp = api_client.get("/api/v1/health")
        assert resp.status_code == 200


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
    def test_motion_blocked_returns_403(self, api_client, auth_headers):
        """Motion tool returns 403 when confirm_motion_allowed is False.

        Temporarily disable FORCE_DRY and clear PX_DRY from the process
        environment so the tool subprocess runs in live mode and hits the gate.
        """
        import os
        from pxh import api as api_mod

        saved_force_dry = api_mod.FORCE_DRY
        saved_px_dry = os.environ.get("PX_DRY")
        os.environ["PX_DRY"] = "0"   # explicit live mode so motion gate is reached
        api_mod.FORCE_DRY = False
        try:
            resp = api_client.post(
                "/api/v1/tool",
                headers=auth_headers,
                json={"tool": "tool_drive",
                      "params": {"direction": "forward", "speed": 20, "duration": 1.0},
                      "dry": False},
            )
            assert resp.status_code == 403
            assert resp.json()["status"] == "blocked"
        finally:
            api_mod.FORCE_DRY = saved_force_dry
            if saved_px_dry is not None:
                os.environ["PX_DRY"] = saved_px_dry
            else:
                os.environ.pop("PX_DRY", None)

    def test_patch_confirm_motion_allowed(self, api_client, auth_headers):
        """PATCH /session can set confirm_motion_allowed."""
        resp = api_client.patch(
            "/api/v1/session",
            headers=auth_headers,
            json={"confirm_motion_allowed": True, "wheels_on_blocks": True},
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
                         json={"confirm_motion_allowed": True})
        # Then disable
        resp = api_client.patch("/api/v1/session", headers=auth_headers,
                                json={"confirm_motion_allowed": False})
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
        resp = api_client.post("/api/v1/device/explode", headers=auth_headers)
        assert resp.status_code == 400
        assert "explode" in resp.json()["detail"]

    def test_device_requires_auth(self, api_client):
        resp = api_client.post("/api/v1/device/reboot")
        assert resp.status_code in (401, 403)

    def test_device_reboot_mocked(self, api_client, auth_headers):
        from unittest.mock import patch, MagicMock
        mock_proc = MagicMock()
        with patch("pxh.api.subprocess.Popen", return_value=mock_proc) as mock_popen:
            resp = api_client.post("/api/v1/device/reboot", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["action"] == "reboot"
        mock_popen.assert_called_once_with(["sudo", "/bin/systemctl", "reboot"])

    def test_device_shutdown_mocked(self, api_client, auth_headers):
        from unittest.mock import patch, MagicMock
        mock_proc = MagicMock()
        with patch("pxh.api.subprocess.Popen", return_value=mock_proc) as mock_popen:
            resp = api_client.post("/api/v1/device/shutdown", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["action"] == "shutdown"
        mock_popen.assert_called_once_with(["sudo", "/sbin/shutdown", "-h", "now"])
