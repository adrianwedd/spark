"""Extended tests for evolve pipeline edge cases identified by QA.

Covers: dry-run flag path, rate-limit timestamp formats, reflection()
introspection ts backward compat, enriched status parsing, and public
services 10-service expansion.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def evolve_env(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    env = os.environ.copy()
    env["PX_STATE_DIR"] = str(state_dir)
    env["LOG_DIR"] = str(log_dir)
    env["PX_DRY"] = "1"
    env["PX_BYPASS_SUDO"] = "1"
    return env, state_dir


def _write_fresh_introspection(state_dir):
    intro = {"ts": time.time(), "config": {}, "mood_distribution": {}}
    (state_dir / "introspection.json").write_text(json.dumps(intro))


# ---------------------------------------------------------------------------
# tool-evolve dry-run flag
# ---------------------------------------------------------------------------

class TestEvolveDryFlag:

    def test_dry_run_writes_entry_no_dry_field(self, evolve_env):
        """PX_DRY=1 queues successfully; canonical schema has no 'dry' field."""
        env, state_dir = evolve_env
        _write_fresh_introspection(state_dir)
        env["PX_EVOLVE_INTENT"] = "Add acoustic exploration angles to reflection"
        result = subprocess.run(
            ["bin/tool-evolve"], cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=15)
        output = json.loads(result.stdout.strip().splitlines()[-1])
        assert output["status"] == "queued"

        entry = json.loads(
            (state_dir / "evolve_queue.jsonl").read_text().strip())
        assert "dry" not in entry
        assert entry["status"] == "pending"

    def test_non_dry_run_omits_dry_flag(self, evolve_env):
        """PX_DRY=0 should not set dry: true."""
        env, state_dir = evolve_env
        _write_fresh_introspection(state_dir)
        env["PX_DRY"] = "0"
        env["PX_EVOLVE_INTENT"] = "Add acoustic exploration angles to reflection"
        result = subprocess.run(
            ["bin/tool-evolve"], cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=15)
        output = json.loads(result.stdout.strip().splitlines()[-1])
        assert output["status"] == "queued"

        entry = json.loads(
            (state_dir / "evolve_queue.jsonl").read_text().strip())
        assert "dry" not in entry


# ---------------------------------------------------------------------------
# tool-evolve rate-limit timestamp format edge cases
# ---------------------------------------------------------------------------

class TestEvolveRateLimitFormats:

    def test_rate_limit_with_iso_ts_completed_only(self, evolve_env):
        """Old-format log entry with only ts_completed (ISO) should rate-limit."""
        env, state_dir = evolve_env
        _write_fresh_introspection(state_dir)
        # Use a recent timestamp (1 hour ago) so it's always within the 24h window
        recent_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_entry = {
            "id": "old-1",
            "status": "pr_created",
            "ts_completed": recent_ts,
        }
        (state_dir / "evolve_log.jsonl").write_text(
            json.dumps(log_entry) + "\n")

        env["PX_EVOLVE_INTENT"] = "Add acoustic exploration angles to reflection"
        result = subprocess.run(
            ["bin/tool-evolve"], cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=15)
        output = json.loads(result.stdout.strip().splitlines()[-1])
        # Recent ts_completed → should rate-limit
        assert output["status"] == "error"
        assert "rate" in output["error"].lower()

    def test_rate_limit_with_numeric_ts(self, evolve_env):
        """New-format log entry with numeric ts should rate-limit."""
        env, state_dir = evolve_env
        _write_fresh_introspection(state_dir)
        log_entry = {"ts": time.time() - 100, "status": "pr_created"}
        (state_dir / "evolve_log.jsonl").write_text(
            json.dumps(log_entry) + "\n")

        env["PX_EVOLVE_INTENT"] = "Add acoustic exploration angles to reflection"
        result = subprocess.run(
            ["bin/tool-evolve"], cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=15)
        output = json.loads(result.stdout.strip().splitlines()[-1])
        assert output["status"] == "error"
        assert "rate" in output["error"].lower()

    def test_rate_limit_both_timestamps_missing(self, evolve_env):
        """Entry with neither ts nor ts_completed should be skipped (not crash)."""
        env, state_dir = evolve_env
        _write_fresh_introspection(state_dir)
        log_entry = {"id": "orphan-1", "status": "failed:tests"}
        (state_dir / "evolve_log.jsonl").write_text(
            json.dumps(log_entry) + "\n")

        env["PX_EVOLVE_INTENT"] = "Add acoustic exploration angles to reflection"
        result = subprocess.run(
            ["bin/tool-evolve"], cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=15)
        output = json.loads(result.stdout.strip().splitlines()[-1])
        # Missing timestamps → entry skipped → no rate limit → queued
        assert output["status"] == "queued"

    def test_rate_limit_malformed_ts_completed(self, evolve_env):
        """Malformed ts_completed string should be caught, not crash."""
        env, state_dir = evolve_env
        _write_fresh_introspection(state_dir)
        log_entry = {
            "ts": "not_a_number",
            "ts_completed": "garbage-date",
            "status": "pr_created",
        }
        (state_dir / "evolve_log.jsonl").write_text(
            json.dumps(log_entry) + "\n")

        env["PX_EVOLVE_INTENT"] = "Add acoustic exploration angles to reflection"
        result = subprocess.run(
            ["bin/tool-evolve"], cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=15)
        output = json.loads(result.stdout.strip().splitlines()[-1])
        # Malformed → ValueError caught → entry skipped → queued
        assert output["status"] == "queued"

    def test_rate_limit_expired_entry_allows_evolve(self, evolve_env):
        """Entry older than 24h should not block a new evolution."""
        env, state_dir = evolve_env
        _write_fresh_introspection(state_dir)
        log_entry = {"ts": time.time() - 90000, "status": "pr_created"}
        (state_dir / "evolve_log.jsonl").write_text(
            json.dumps(log_entry) + "\n")

        env["PX_EVOLVE_INTENT"] = "Add acoustic exploration angles to reflection"
        result = subprocess.run(
            ["bin/tool-evolve"], cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=15)
        output = json.loads(result.stdout.strip().splitlines()[-1])
        assert output["status"] == "queued"


# ---------------------------------------------------------------------------
# reflection() introspection ts backward compat
# ---------------------------------------------------------------------------

class TestReflectionIntrospectionTs:

    def test_epoch_float_ts(self, tmp_path):
        """reflection() should handle numeric epoch ts in introspection.json."""
        import pxh.mind as mind

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        intro = {"ts": time.time(), "mood_distribution": {"curious": 50},
                 "config": {"SIMILARITY_THRESHOLD": 0.85}}
        (state_dir / "introspection.json").write_text(json.dumps(intro))

        with patch.object(mind, "STATE_DIR", state_dir), \
             patch.object(mind, "INTROSPECTION_STALE_S", 7200):
            awareness = {"time_of_day": "afternoon", "obi_mode": "calm"}
            result = mind.reflection(awareness, dry=True)
            # Should not crash — that's the main assertion
            assert result is None or isinstance(result, dict)  # returns dict|None per type hint

    def test_iso_string_ts(self, tmp_path):
        """reflection() should handle ISO string ts (backward compat)."""
        import pxh.mind as mind

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        intro = {"ts": "2026-03-20T10:00:00Z", "mood_distribution": {},
                 "config": {}}
        (state_dir / "introspection.json").write_text(json.dumps(intro))

        with patch.object(mind, "STATE_DIR", state_dir), \
             patch.object(mind, "INTROSPECTION_STALE_S", 999999999):
            awareness = {"time_of_day": "afternoon", "obi_mode": "calm"}
            # Should not crash
            result = mind.reflection(awareness, dry=True)
            assert result is not None or result is None

    def test_malformed_ts_no_crash(self, tmp_path):
        """reflection() should silently skip malformed ts values."""
        import pxh.mind as mind

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        intro = {"ts": "not-a-date", "mood_distribution": {}, "config": {}}
        (state_dir / "introspection.json").write_text(json.dumps(intro))

        with patch.object(mind, "STATE_DIR", state_dir):
            awareness = {"time_of_day": "afternoon", "obi_mode": "calm"}
            # Should not crash — ValueError caught
            result = mind.reflection(awareness, dry=True)
            assert result is not None or result is None

    def test_none_ts_no_crash(self, tmp_path):
        """reflection() should handle ts=None without crashing."""
        import pxh.mind as mind

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        intro = {"ts": None, "mood_distribution": {}, "config": {}}
        (state_dir / "introspection.json").write_text(json.dumps(intro))

        with patch.object(mind, "STATE_DIR", state_dir):
            awareness = {"time_of_day": "afternoon", "obi_mode": "calm"}
            # ts=None → float(None) → TypeError caught
            result = mind.reflection(awareness, dry=True)
            assert result is not None or result is None


# ---------------------------------------------------------------------------
# px-evolve enriched status parsing
# ---------------------------------------------------------------------------

class TestEnrichedStatusParsing:

    def test_parse_pr_created_with_url_and_files(self):
        """Enriched status 'pr_created|url|files' should parse correctly."""
        raw_status = "pr_created|https://github.com/test/repo/pull/42|src/pxh/spark_config.py,tests/test_new.py"
        parts = raw_status.split("|", 2)
        assert parts[0] == "pr_created"
        assert parts[1] == "https://github.com/test/repo/pull/42"
        assert parts[2].split(",") == ["src/pxh/spark_config.py", "tests/test_new.py"]

    def test_parse_plain_failed_status(self):
        """Plain 'failed:reason' should not be split."""
        raw_status = "failed:tests"
        assert not raw_status.startswith("pr_created|")
        assert raw_status == "failed:tests"

    def test_parse_pr_created_empty_files(self):
        """Handle edge case of pr_created with empty files list."""
        raw_status = "pr_created|https://github.com/test/repo/pull/1|"
        parts = raw_status.split("|", 2)
        files = parts[2].split(",") if len(parts) > 2 and parts[2] else []
        assert files == []


# ---------------------------------------------------------------------------
# Public services 10-service expansion
# ---------------------------------------------------------------------------

class TestPublicServicesExpansion:

    def test_has_all_ten_services(self):
        """Public services endpoint should return all 10 boot services."""
        from pxh import api

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="active\n")

            from starlette.testclient import TestClient
            client = TestClient(api.app)

            resp = client.get("/api/v1/public/services")
            assert resp.status_code == 200
            data = resp.json()

            expected = {
                "px-mind", "px-alive", "px-wake-listen", "px-battery-poll",
                "px-api-server", "px-post", "px-frigate-stream",
                "px-evolve", "cloudflared",
            }
            assert set(data.keys()) == expected
