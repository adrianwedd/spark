# px-mind Extraction: Bash Heredoc to src/pxh/mind.py

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract all Python code from bin/px-mind's bash heredoc into src/pxh/mind.py, making it a proper importable module with IDE support and normal test imports.

**Architecture:** Single-file extraction. All 56 functions and globals move to src/pxh/mind.py unchanged. bin/px-mind becomes a thin bash wrapper that sources px-env and .env then calls python -m pxh.mind. Tests change from the heredoc extraction hack to normal from-imports. Zero behaviour change.

**Tech Stack:** Python 3.11, bash, pytest

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| src/pxh/mind.py | CREATE | All Python code from the heredoc (3200+ lines, 56 functions, all globals) |
| bin/px-mind | REWRITE | Thin bash wrapper: source px-env + .env, call python -m pxh.mind |
| tests/test_mind_utils.py | MODIFY | Replace heredoc extraction hack with normal from-imports |
| tests/test_px_mind.py | NO CHANGE | Runs bin/px-mind as subprocess, still works |

---

## Chunk 1: Extract Python code to src/pxh/mind.py

### Task 1: Create src/pxh/mind.py from heredoc

**Files:**
- Create: src/pxh/mind.py
- Modify: bin/px-mind

- [ ] **Step 1: Extract the Python code from the heredoc**

  Read bin/px-mind, find everything between the heredoc start marker and the closing PY line. This is the Python source. Write it verbatim to src/pxh/mind.py.

  The code starts with the module docstring and ends with the if-name-main block.

  CRITICAL: Do not modify any Python code during extraction. Copy it exactly as-is.

- [ ] **Step 2: Verify src/pxh/mind.py is syntactically valid**

  Run: python -c "import ast; ast.parse(open('src/pxh/mind.py').read()); print('OK')"
  Expected: OK

- [ ] **Step 3: Rewrite bin/px-mind as thin wrapper**

  Replace the entire content of bin/px-mind with a bash script that:
  1. Sources px-env
  2. Sources .env if present (for PX_HA_TOKEN etc.)
  3. Runs: python -m pxh.mind with all args forwarded

- [ ] **Step 4: Verify bin/px-mind --dry-run still works**

  Run: PX_DRY=1 PX_BYPASS_SUDO=1 bin/px-mind --dry-run 2>&1 | tail -5
  Expected: dry-run output with "starting pid=..." and "dry-run complete"

- [ ] **Step 5: Commit extraction**

  git add src/pxh/mind.py bin/px-mind
  git commit -m "refactor: extract px-mind Python code to src/pxh/mind.py (#78)"

---

## Chunk 2: Update test imports

### Task 2: Replace heredoc extraction hack with normal imports

**Files:**
- Modify: tests/test_mind_utils.py

- [ ] **Step 1: Replace the import block**

  The file currently reads bin/px-mind as text, extracts the heredoc, creates stub modules, and runs the code to get a _MIND dict. Replace ALL of this with normal from-imports:

  from pxh.mind import (
      compute_obi_mode,
      filter_battery,
      _daytime_action_hint,
      _fetch_frigate_presence,
      # ... all other functions and constants used by tests
  )

  Check every _MIND["..."] reference in the file and convert to a direct import.

- [ ] **Step 2: Update battery state reset helper**

  Replace _MIND["_battery_glitch_count"] = 0 style resets with:
  import pxh.mind as _mind_module
  _mind_module._battery_glitch_count = 0

- [ ] **Step 3: Run all mind tests**

  Run: .venv/bin/python -m pytest tests/test_mind_utils.py tests/test_px_mind.py -v
  Expected: All 117+ tests pass

- [ ] **Step 4: Run full test suite**

  Run: .venv/bin/python -m pytest tests/ -q --ignore=tests/test_tools_live.py
  Expected: 430+ tests pass

- [ ] **Step 5: Commit test updates**

  git add tests/test_mind_utils.py
  git commit -m "refactor: update mind tests to import from pxh.mind directly"

---

## Chunk 3: Verify and deploy

### Task 3: Integration verification

- [ ] **Step 1: Verify systemd service still works**

  Run: sudo systemctl restart px-mind && sleep 10 && systemctl is-active px-mind
  Expected: active

  Run: tail -5 logs/px-mind.log
  Expected: Normal startup log

- [ ] **Step 2: Verify dry-run cycles complete**

  Run: PX_BYPASS_SUDO=1 timeout 30 bin/px-mind --dry-run 2>&1 | grep -c 'thought:'
  Expected: 3

- [ ] **Step 3: Push and close issue**

  git push
  gh issue close 78 --comment "Implemented: px-mind extracted to src/pxh/mind.py"

- [ ] **Step 4: Restart px-post**

  sudo systemctl restart px-post
