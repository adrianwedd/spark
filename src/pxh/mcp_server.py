"""SPARK MCP server — read-only and actionable tools for Claude Code dev sessions.

Exposes SPARK's live state as MCP tools so Claude Code on the Pi (or remote)
can inspect session, thoughts, sonar, awareness, and vitals without going
through the REST API or voice loop.

Phase 1: 5 read-only tools (status, thoughts, awareness, sonar, vitals).
Phase 2: spark_list_tools + spark_run_tool — run any SPARK tool via the voice
loop's validate_action/execute_tool pipeline. dry=True by default (safe); set
dry=False to actuate. Motion still gated by confirm_motion_allowed in session
state (rc==2 → status "blocked").
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from pxh.voice_loop import validate_action, execute_tool, VoiceLoopError
from pxh.schemas import TOOL_SCHEMAS

PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parent.parent.parent))
STATE_DIR = Path(os.environ.get("PX_STATE_DIR", PROJECT_ROOT / "state"))


mcp = FastMCP(
    "spark",
    instructions=(
        "SPARK is a PiCar-X robot with a three-layer cognitive architecture. "
        "Use spark_status/thoughts/awareness/sonar/vitals to read live state. "
        "Use spark_list_tools to see all available tools, then spark_run_tool "
        "to execute them. spark_run_tool defaults to dry=True (safe simulation); "
        "pass dry=False to actuate. Motion tools are additionally gated by "
        "confirm_motion_allowed in session state (rc==2 → status 'blocked')."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict | list | None:
    """Read a JSON file, returning None if missing or corrupt."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _read_jsonl_tail(path: Path, n: int = 10) -> list[dict]:
    """Read last n entries from a JSONL file."""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries[-n:]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def spark_status() -> str:
    """Get SPARK's current session state — persona, listening, motion allowed, last action, robot name."""
    data = _read_json(STATE_DIR / "session.json")
    if data is None:
        return json.dumps({"error": "session.json not found"})
    # Return key fields only, not the full history
    summary = {
        "persona": data.get("persona", ""),
        "listening": data.get("listening", False),
        "confirm_motion_allowed": data.get("confirm_motion_allowed", False),
        "roaming_allowed": data.get("roaming_allowed", False),
        "last_action": data.get("last_action", ""),
        "robot_name": data.get("robot_name", ""),
    }
    return json.dumps(summary, indent=2)


@mcp.tool()
def spark_thoughts(count: int = 10) -> str:
    """Get SPARK's recent thoughts from the cognitive loop.

    Args:
        count: Number of recent thoughts to return (1-50, default 10).
    """
    count = max(1, min(50, count))
    thoughts = _read_jsonl_tail(STATE_DIR / "thoughts-spark.jsonl", n=count)
    if not thoughts:
        return json.dumps({"message": "no thoughts yet"})
    return json.dumps(thoughts, indent=2)


@mcp.tool()
def spark_awareness() -> str:
    """Get SPARK's Layer 1 awareness state — sonar, time of day, Obi mode, weather, battery, presence."""
    data = _read_json(STATE_DIR / "awareness.json")
    if data is None:
        return json.dumps({"error": "awareness.json not found — px-mind may not be running"})
    return json.dumps(data, indent=2)


@mcp.tool()
def spark_sonar() -> str:
    """Get the latest sonar reading (distance in cm, source, timestamp)."""
    data = _read_json(STATE_DIR / "sonar_live.json")
    if data is None:
        return json.dumps({"error": "sonar_live.json not found — px-alive may not be running"})
    return json.dumps(data, indent=2)


@mcp.tool()
def spark_vitals() -> str:
    """Get system vitals — CPU temp, RAM, battery voltage/percentage, charging state."""
    result = {}

    # Battery
    battery = _read_json(STATE_DIR / "battery.json")
    if battery:
        result["battery"] = {
            "volts": battery.get("volts"),
            "pct": battery.get("pct"),
            "charging": battery.get("charging"),
        }

    # CPU temp
    try:
        temp_path = Path("/sys/class/thermal/thermal_zone0/temp")
        if temp_path.exists():
            result["cpu_temp_c"] = int(temp_path.read_text().strip()) / 1000
    except (ValueError, OSError):
        pass

    # RAM
    try:
        import psutil
        mem = psutil.virtual_memory()
        result["ram_mb"] = round(mem.used / 1024 / 1024)
        result["ram_pct"] = mem.percent
    except ImportError:
        pass

    if not result:
        return json.dumps({"error": "no vitals data available"})
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Resources: spark:// URIs for Claude Code sessions
# ---------------------------------------------------------------------------

@mcp.resource("spark://session")
def resource_session() -> str:
    """Current session.json."""
    return json.dumps(_read_json(STATE_DIR / "session.json") or {}, indent=2)


@mcp.resource("spark://thoughts")
def resource_thoughts() -> str:
    """Recent SPARK thoughts."""
    return json.dumps(_read_jsonl_tail(STATE_DIR / "thoughts-spark.jsonl", 20), indent=2)


@mcp.resource("spark://notes")
def resource_notes() -> str:
    """SPARK long-term notes."""
    return json.dumps(_read_jsonl_tail(STATE_DIR / "notes-spark.jsonl", 20), indent=2)


# ---------------------------------------------------------------------------
# Phase 2: Actionable tools — full-tool exposure via voice loop dispatch
# ---------------------------------------------------------------------------

@mcp.tool()
def spark_list_tools() -> dict:
    """List every SPARK tool and its parameter schema."""
    return TOOL_SCHEMAS


@mcp.tool()
def spark_run_tool(tool: str, params: dict | None = None, dry: bool = True) -> dict:
    """Run a SPARK tool. dry=True (default) simulates; set dry=False to actuate.
    Motion tools still require confirm_motion_allowed in session state.

    Args:
        tool: Tool name (e.g. "tool_status", "tool_forward").
        params: Optional dict of tool parameters.
        dry: If True (default), simulate without side effects. Pass False to actuate.
    """
    try:
        validated_tool, env = validate_action({"tool": tool, "params": params or {}})
    except VoiceLoopError as exc:
        return {"status": "error", "error": str(exc)}
    rc, out, err = execute_tool(validated_tool, env, dry)
    status = "ok" if rc == 0 else ("blocked" if rc == 2 else "error")
    return {"status": status, "returncode": rc,
            "stdout": out[-4096:], "stderr": err[-2048:], "dry": dry}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    mcp.run()


if __name__ == "__main__":
    main()
