# Agent Operations Guide

`CLAUDE.md` is the canonical engineering constitution for every coding agent. Read it first and do not duplicate it here.

## Agent Workflow

1. Activate the virtualenv: `source .venv/bin/activate`.
2. Hardware diagnostics (`bin/px-diagnostics`) are only required when the task actually touches hardware.
3. Run targeted local tests (`python -m pytest -k test_name`). The CI pipeline is the full-suite gate; do not run the full suite locally on SPARK.
4. Preserve dirty work: keep unrelated files and staging exact-path as you found them.
5. Continue through environmental limitations when equivalent evidence exists rather than stopping.

## Local Notes

- `PX_BYPASS_SUDO=1` and `LOG_DIR=logs_test` make subprocess tests safe.
- Update operator documentation when a change makes existing operator guidance inaccurate; avoid duplicating constitutional instructions from `CLAUDE.md`.
