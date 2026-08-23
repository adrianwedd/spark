#!/usr/bin/env python3
"""Delegated-agent authority invariant (issue #281).

A delegated/research agent must be mechanically unable to run shell
commands, write or edit files, restart production services, touch
GPIO/audio hardware, or spawn further agents — even if its prompt,
reasoning, or instructions are wrong, adversarial, or simply mistaken.

Prompt wording is not the boundary. Claude Code enforces subagent tool
access at the dispatch layer: a subagent type whose `.claude/agents/*.md`
frontmatter declares a `tools:` allowlist literally cannot call anything
outside it, regardless of what the model decides to do mid-turn.
`spark-investigator` is that allowlist. This guard checks the one thing
that actually matters — that the dangerous tools stay out of the list —
so a future edit that "just adds Bash back for convenience" fails CI
instead of silently reopening the hole a prior delegated agent fell
through (see docs/architecture/agent-authority.md).

Does not check sudoers or OS-level process/user separation — that is a
separate, still-open layer of #281, not this script's job.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_FILE = REPO_ROOT / ".claude" / "agents" / "spark-investigator.md"

# Any of these in the agent's tools list defeats the boundary: Bash/Write/
# Edit/NotebookEdit reach the filesystem or a shell; Agent can spawn a
# fresh subagent with a wider allowlist than this one.
FORBIDDEN_TOOLS = {"Bash", "Write", "Edit", "NotebookEdit", "Agent"}


def _parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        raise ValueError("no frontmatter block")
    end = text.index("\n---", 3)
    block = text[3:end]
    fields = {}
    for line in block.splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def check(agent_file: Path | None = None) -> list:
    path = agent_file if agent_file is not None else AGENT_FILE
    violations = []
    if not path.exists():
        return [f"{path} does not exist — the restricted investigation agent type is missing"]

    fields = _parse_frontmatter(path.read_text())
    tools_raw = fields.get("tools")
    if not tools_raw:
        return [
            f"{path}: no `tools:` field — an agent definition with no `tools:` "
            "field inherits every tool from the parent session, the opposite "
            "of a restriction"
        ]

    tools = {t.strip() for t in re.split(r"[,\[\]]", tools_raw) if t.strip()}
    hit = tools & FORBIDDEN_TOOLS
    if hit:
        violations.append(
            f"{path}: tools list grants {sorted(hit)} — spark-investigator must "
            "never have shell, write, edit, or agent-spawning access (issue #281)"
        )
    return violations


if __name__ == "__main__":
    found = check()
    if found:
        print(f"{len(found)} agent-authority violation(s):")
        for v in found:
            print(f"  {v}")
        sys.exit(1)
    print("spark-investigator tool allowlist OK")
