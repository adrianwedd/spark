"""Delegated-agent authority invariant (issue #281).

A delegated/research agent must be mechanically unable to touch production
systemd, GPIO, live audio, or state — not merely instructed not to. See
tools/check_investigator_agent.py and docs/architecture/agent-authority.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import check_investigator_agent as guard  # noqa: E402


def test_spark_investigator_has_no_dangerous_tools():
    violations = guard.check()
    assert violations == [], "\n".join(violations)


def test_spark_investigator_declares_a_tools_allowlist():
    fields = guard._parse_frontmatter(guard.AGENT_FILE.read_text())
    assert fields.get("tools"), "spark-investigator must declare an explicit tools: allowlist"


def test_canary_forbidden_tool_is_detected(tmp_path):
    agent_file = tmp_path / "spark-investigator.md"
    agent_file.write_text("---\nname: spark-investigator\ntools: Read, Bash\n---\nbody\n")

    violations = guard.check(agent_file)

    assert violations and "Bash" in violations[0]


def test_canary_missing_tools_field_is_detected(tmp_path):
    agent_file = tmp_path / "spark-investigator.md"
    agent_file.write_text("---\nname: spark-investigator\n---\nbody\n")

    violations = guard.check(agent_file)

    assert violations


def test_canary_missing_file_is_detected(tmp_path):
    violations = guard.check(tmp_path / "does-not-exist.md")

    assert violations


def test_canary_safe_allowlist_is_not_flagged(tmp_path):
    agent_file = tmp_path / "spark-investigator.md"
    agent_file.write_text(
        "---\nname: spark-investigator\ntools: Read, Grep, Glob, WebSearch, WebFetch\n---\nbody\n"
    )

    violations = guard.check(agent_file)

    assert violations == []
