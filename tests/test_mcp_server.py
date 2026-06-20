"""Tests for SPARK MCP server tools."""
import json
from unittest.mock import patch

import pytest

mcp_mod = pytest.importorskip("pxh.mcp_server", reason="mcp module not installed")
spark_awareness = mcp_mod.spark_awareness
spark_sonar = mcp_mod.spark_sonar
spark_status = mcp_mod.spark_status
spark_thoughts = mcp_mod.spark_thoughts
spark_vitals = mcp_mod.spark_vitals


@pytest.fixture
def state_dir(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    with patch.object(mcp_mod, "STATE_DIR", sd):
        yield sd


class TestSparkStatus:
    def test_returns_session_fields(self, state_dir):
        session = {
            "persona": "spark",
            "listening": True,
            "confirm_motion_allowed": True,
            "roaming_allowed": False,
            "last_action": "greet",
            "robot_name": "SPARK",
            "history": [{"ts": "x", "text": "secret"}],
        }
        (state_dir / "session.json").write_text(json.dumps(session))
        result = json.loads(spark_status())
        assert result["persona"] == "spark"
        assert result["listening"] is True
        assert "history" not in result  # should not leak history

    def test_missing_session(self, state_dir):
        result = json.loads(spark_status())
        assert "error" in result


class TestSparkThoughts:
    def test_returns_recent_thoughts(self, state_dir):
        lines = [json.dumps({"thought": f"thought {i}", "mood": "curious"})
                 for i in range(15)]
        (state_dir / "thoughts-spark.jsonl").write_text("\n".join(lines) + "\n")
        result = json.loads(spark_thoughts(count=5))
        assert len(result) == 5
        assert result[-1]["thought"] == "thought 14"

    def test_empty_thoughts(self, state_dir):
        result = json.loads(spark_thoughts())
        assert "message" in result

    def test_count_clamped(self, state_dir):
        lines = [json.dumps({"thought": f"t{i}"}) for i in range(5)]
        (state_dir / "thoughts-spark.jsonl").write_text("\n".join(lines) + "\n")
        result = json.loads(spark_thoughts(count=100))
        assert len(result) == 5  # only 5 exist


class TestSparkAwareness:
    def test_returns_awareness(self, state_dir):
        awareness = {"time_of_day": "afternoon", "obi_mode": "calm", "sonar_cm": 42}
        (state_dir / "awareness.json").write_text(json.dumps(awareness))
        result = json.loads(spark_awareness())
        assert result["obi_mode"] == "calm"

    def test_missing_awareness(self, state_dir):
        result = json.loads(spark_awareness())
        assert "error" in result


class TestSparkSonar:
    def test_returns_sonar(self, state_dir):
        sonar = {"cm": 35.2, "source": "px-alive", "ts": "2026-03-20T10:00:00Z"}
        (state_dir / "sonar_live.json").write_text(json.dumps(sonar))
        result = json.loads(spark_sonar())
        assert result["cm"] == 35.2

    def test_missing_sonar(self, state_dir):
        result = json.loads(spark_sonar())
        assert "error" in result


class TestSparkVitals:
    def test_returns_battery_when_available(self, state_dir):
        battery = {"volts": 8.1, "pct": 95, "charging": False, "ts": "x"}
        (state_dir / "battery.json").write_text(json.dumps(battery))
        result = json.loads(spark_vitals())
        assert result["battery"]["volts"] == 8.1
        assert result["battery"]["charging"] is False

    def test_returns_ram_via_psutil(self, state_dir):
        result = json.loads(spark_vitals())
        # psutil should be available in test env
        assert "ram_mb" in result or "error" in result

    def test_no_battery_still_returns_ram(self, state_dir):
        # No battery.json, but psutil available → should return ram data
        result = json.loads(spark_vitals())
        assert "battery" not in result
        assert "ram_mb" in result


class TestSparkListTools:
    def test_spark_list_tools_covers_all(self):
        from pxh import mcp_server
        from pxh.voice_loop import ALLOWED_TOOLS
        listed = mcp_server.spark_list_tools()
        assert set(listed) >= ALLOWED_TOOLS


class TestSparkRunTool:
    def test_spark_run_tool_dry(self, monkeypatch):
        from pxh import mcp_server
        captured = {}
        monkeypatch.setattr(mcp_server, "execute_tool",
            lambda tool, env, dry, timeout=None: captured.update(
                {"tool": tool, "dry": dry}) or (0, '{"status":"ok"}', ""))
        out = mcp_server.spark_run_tool("tool_status", {})
        assert captured["tool"] == "tool_status"
        assert captured["dry"] is True   # safe default
        assert out["returncode"] == 0

    def test_spark_run_tool_rejects_unknown(self, monkeypatch):
        from pxh import mcp_server
        out = mcp_server.spark_run_tool("tool_evil", {})
        assert out["status"] == "error"

    def test_spark_run_tool_blocked_on_motion(self, monkeypatch):
        from pxh import mcp_server
        monkeypatch.setattr(mcp_server, "execute_tool",
            lambda tool, env, dry, timeout=None: (2, "", "motion blocked"))
        out = mcp_server.spark_run_tool("tool_status", {}, dry=False)
        assert out["status"] == "blocked"
        assert out["returncode"] == 2

    def test_spark_run_tool_none_params(self, monkeypatch):
        from pxh import mcp_server
        captured = {}
        monkeypatch.setattr(mcp_server, "execute_tool",
            lambda tool, env, dry, timeout=None: captured.update({"tool": tool}) or (0, "{}", ""))
        out = mcp_server.spark_run_tool("tool_status")   # no params arg — exercises params=None -> {}
        assert out["status"] == "ok"
        assert captured["tool"] == "tool_status"


class TestResourceSession:
    def test_resource_session_reads_state(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
        (tmp_path / "session.json").write_text('{"persona":"spark"}')
        import importlib
        import pxh.mcp_server as m
        importlib.reload(m)
        assert '"persona"' in m.resource_session()

    def test_resource_thoughts_reads_state(self, state_dir):
        lines = [json.dumps({"thought": f"thought {i}", "mood": "curious"})
                 for i in range(15)]
        (state_dir / "thoughts-spark.jsonl").write_text("\n".join(lines) + "\n")
        result = mcp_mod.resource_thoughts()
        assert '"thought"' in result
        parsed = json.loads(result)
        assert len(parsed) == 15

    def test_resource_notes_reads_state(self, state_dir):
        lines = [json.dumps({"note": f"note {i}"}) for i in range(5)]
        (state_dir / "notes-spark.jsonl").write_text("\n".join(lines) + "\n")
        result = mcp_mod.resource_notes()
        assert '"note"' in result
        parsed = json.loads(result)
        assert len(parsed) == 5
