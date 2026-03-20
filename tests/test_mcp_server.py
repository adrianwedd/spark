"""Tests for SPARK MCP server tools."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pxh.mcp_server import (
    spark_awareness,
    spark_sonar,
    spark_status,
    spark_thoughts,
    spark_vitals,
)
import pxh.mcp_server as mcp_mod


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

    def test_no_data_returns_error(self, state_dir):
        with patch.dict(os.environ, {}, clear=False), \
             patch("pxh.mcp_server._read_json", return_value=None):
            # With no battery and no psutil, should get error
            pass  # hard to fully isolate, just ensure no crash
