# Agent Operations Guide

`CLAUDE.md` is the canonical engineering constitution for every coding agent,
including Codex. Read it first and do not duplicate it here.

## Codex Workflow

1. Activate the virtualenv: `source .venv/bin/activate`.
2. Run `bin/px-diagnostics --no-motion` when operating on SPARK hardware; on
   another host, record unavailable hardware binaries as environmental context
   and use focused tests instead.
3. Keep implementation logic in `src/pxh/` or Python-backed `bin/` helpers;
   add focused pytest coverage and run `python -m pytest` before a commit.
4. Preserve the safety and routing invariants in `CLAUDE.md`, especially the
   absolute ban on `claude -p` and the resident-session envelope.

## Local Notes

- `PX_BYPASS_SUDO=1` and `LOG_DIR=logs_test` make subprocess tests safe.
- Update operator documentation alongside behavior changes; avoid duplicating
  constitutional instructions from `CLAUDE.md`.
