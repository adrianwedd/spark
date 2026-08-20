# Brain handshake recycle-and-kill fixes + exit instrumentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop protocol traffic (handshakes) from driving `spark-brain`/`spark-io` context recycling, stop a failed handshake from killing a session that is merely unvalidated rather than actually wedged, and make the next unexplained `spark-brain` exit observable with hard evidence instead of speculation.

**Architecture:** Three independent changes to `src/pxh/brain_daemon.py` and `bin/px-claude-session`, each landing as its own commit:

1. `run_handshake()` stops incrementing `state.turns` on a successful handshake — `CONTEXT_TURNS` now measures only real caller traffic (`count_turns()`), never supervisor plumbing.
2. `run_handshake()` stops calling `tmux_claude.kill_session()` when `HANDSHAKE_ATTEMPTS` are exhausted. It still clears the validation marker (so `ask_brain` immediately stops routing to the session) and records the failure, but leaves the process and the holder alone. Killing stays the exclusive job of `check_wedge()`, which only acts on a live caller request past its deadline — concrete evidence of a stuck session, not a failed diagnostic.
3. `bin/px-claude-session` stops `exec`ing `claude` directly (which leaves nothing running to observe an exit) and instead runs it as a backgrounded, waited-on child, logging a `start` event (pid, model, Claude CLI version, boot id, cwd, tool count, supervisor instance) the moment it launches and an `exit` event (exit code or signal, duration, a bounded stderr tail) the moment it stops. Records go through `pxh.logging.log_event()`, so they get the same file locking and 5 MB rotation every other daemon log already has, and they live under `logs/`, independent of the tmux pane that just died.

**Tech Stack:** Python (`src/pxh/brain_daemon.py`, `pytest`), Bash (`bin/px-claude-session`), the existing `pxh.logging.log_event` / `pxh.state.rotate_log` logging infrastructure.

**Spec:** This plan is a deliberate, audited reversal of parts of `docs/superpowers/specs/2026-08-17-brain-handshake-validation-design.md` §2.3 step 7 and §2.7. That document is left as the historical record of the original design; the reversal's rationale is documented in code comments at the point of change, per this repo's established convention (see e.g. `src/pxh/brain.py:97-104`'s own comment about a prior trust-direction bug).

## Global Constraints

- CI runs `pytest -m "not live"`. Every test added here must pass under that marker set and must not touch the real `state/`, `logs/`, or tmux socket — use the existing `_mailbox` / `fake_tmux` fixtures in `tests/test_brain_daemon.py`, and `isolated_project` for the one subprocess-level test.
- No comments explaining *what* code does — only *why*, matching this file's existing style. Every removed behavior gets a comment at the point of removal explaining why, in the register already used throughout `brain_daemon.py` and `brain.py`.
- Do not touch `docs/prompts/spark-*.md` — none of these changes require a prompt edit, and editing those needs a `kill-session` against the live robot, which is out of scope for this plan.
- Do not restart the live `px-brain` service or the `spark-brain`/`spark-io` tmux sessions as part of this plan — that is a deployment step for a human to take deliberately once this lands, not something to do autonomously while implementing it.
- Each task ends with a commit. Do not leave the tree dirty between tasks.

---

### Task 1: Stop handshake turns from driving context recycle

**Files:**
- Modify: `src/pxh/brain_daemon.py:391-403` (inside `run_handshake`'s success branch)
- Modify: `src/pxh/brain_daemon.py:29-38` (module docstring, item 6 — one added sentence)
- Test: `tests/test_brain_daemon.py` (new test near `test_turns_are_counted_once_per_request`)

**Interfaces:**
- Consumes: `brain_daemon.CONTEXT_TURNS` (existing module constant), `brain_daemon.SessionState.turns` (existing field), `brain_daemon.run_handshake(state, reason) -> bool` (existing function, signature unchanged), `brain_daemon.maybe_recycle(state, now_local) -> None` (existing function, signature unchanged).
- Produces: no new names. `state.turns` after this task no longer increments on a successful handshake — later tasks and any other caller must not assume it does.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_brain_daemon.py`, directly after `test_turns_are_counted_once_per_request` (around line 405):

```python
def test_twenty_successful_handshakes_alone_never_trigger_a_recycle(
        fake_tmux, _fast_handshake, monkeypatch):
    """CONTEXT_TURNS bounds cognitive-context growth from real work. A
    2026-08-20 audit found handshakes counted toward it too (spec §2.7),
    so a session doing nothing but answering health checks could age into
    a context recycle on protocol traffic alone. This pins the fix at the
    behavioural level, not just the counter: even after CONTEXT_TURNS
    successful handshakes, maybe_recycle must not inject a recycle turn."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject", _echo_when_nudged(fake_tmux, session))

    state = _state(session)
    for _ in range(brain_daemon.CONTEXT_TURNS):
        assert brain_daemon.run_handshake(state, "no_marker") is True

    assert state.turns == 0, "a handshake must never increment turns"

    now_local = datetime(2026, 8, 20, 12, 0, tzinfo=brain_daemon.HOBART)
    state.last_recycle_day = now_local.strftime("%Y-%m-%d")  # nightly not due
    injected_before = len(fake_tmux.injected)
    brain_daemon.maybe_recycle(state, now_local)
    assert len(fake_tmux.injected) == injected_before, \
        "maybe_recycle must not inject a journal+/clear turn on handshake traffic alone"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_brain_daemon.py::test_twenty_successful_handshakes_alone_never_trigger_a_recycle -v`
Expected: FAIL — `state.turns == 0` fails because `state.turns == 20` (each handshake incremented it).

- [ ] **Step 3: Remove the turn-counting from a successful handshake**

In `src/pxh/brain_daemon.py`, inside `run_handshake`, replace:

```python
                brain.cleanup_request(session, request_id)
                health.record_success(state.component,
                                      detail={"model": model, "attempt": attempt})
                _log("handshake_ok", session=session, attempt=attempt, model=model)
                # Handshakes count toward CONTEXT_TURNS too (spec §2.7).
                # count_turns() cannot see this turn on its own — current.json
                # is written and removed entirely inside this function, after
                # count_turns() already ran for this tick — so it is counted
                # here instead.
                state.turns += 1
                # The quiet window's job is done: the thing it was protecting
                # (a slow recycle turn) is over, one way or another, by the
                # time a handshake succeeds. Clearing it here means the window
                # cannot outlive the recycle it exists to protect.
                state.last_recycle_at = 0.0
                return True
```

with:

```python
                brain.cleanup_request(session, request_id)
                health.record_success(state.component,
                                      detail={"model": model, "attempt": attempt})
                _log("handshake_ok", session=session, attempt=attempt, model=model)
                # Deliberately NOT state.turns += 1. The original design (spec
                # §2.7, docs/superpowers/specs/2026-08-17-brain-handshake-
                # validation-design.md) counted a handshake as a turn like any
                # other, reasoning that it is a real turn of context. A
                # 2026-08-20 audit of session recreations found the practical
                # effect instead: a session doing nothing but answering health
                # checks ages into a context recycle on protocol traffic
                # alone. CONTEXT_TURNS exists to bound growth from real work
                # (see count_turns(), which only counts caller requests); a
                # handshake is supervisor plumbing and must not spend that
                # budget. Pinned by
                # test_twenty_successful_handshakes_alone_never_trigger_a_recycle.
                #
                # The quiet window's job is done: the thing it was protecting
                # (a slow recycle turn) is over, one way or another, by the
                # time a handshake succeeds. Clearing it here means the window
                # cannot outlive the recycle it exists to protect.
                state.last_recycle_at = 0.0
                return True
```

- [ ] **Step 4: Update the module docstring's item 6**

In `src/pxh/brain_daemon.py`, near the top of the file, find item 6 of the numbered list:

```
6. **Validate** — one handshake per tick, to the session that has waited
   longest, because the glyph cannot tell us a session can answer. Health
   success is recorded only for a session whose marker says `validated`,
   never on the strength of the prompt glyph — a permission dialog renders
   the glyph too, which is exactly how a session that could not answer a
   single request used to report `ok` indefinitely. A tmux server restart
   mid-handshake is self-recovering and needs no special handling: the
   session disappears, the next tick reads `session_absent`, recreates,
   sweeps and handshakes with a fresh id. Stated so nobody adds machinery
   for it.
```

Replace with (adds one sentence at the end):

```
6. **Validate** — one handshake per tick, to the session that has waited
   longest, because the glyph cannot tell us a session can answer. Health
   success is recorded only for a session whose marker says `validated`,
   never on the strength of the prompt glyph — a permission dialog renders
   the glyph too, which is exactly how a session that could not answer a
   single request used to report `ok` indefinitely. A tmux server restart
   mid-handshake is self-recovering and needs no special handling: the
   session disappears, the next tick reads `session_absent`, recreates,
   sweeps and handshakes with a fresh id. Stated so nobody adds machinery
   for it. A handshake does not count toward context-recycle turns, and a
   failed one does not kill the session — see the comments in
   `run_handshake` for both.
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_brain_daemon.py::test_twenty_successful_handshakes_alone_never_trigger_a_recycle -v`
Expected: PASS

- [ ] **Step 6: Run the full brain_daemon suite to check for regressions**

Run: `python -m pytest tests/test_brain_daemon.py -v`
Expected: all PASS (no existing test asserted `state.turns` increases on a successful handshake, so nothing else should move)

- [ ] **Step 7: Commit**

```bash
git add src/pxh/brain_daemon.py tests/test_brain_daemon.py
git commit -m "$(cat <<'EOF'
fix(brain): stop handshake turns from driving context recycle

CONTEXT_TURNS is meant to bound cognitive-context growth from real
caller traffic. A 2026-08-20 audit of session recreations found that
because a successful handshake also incremented state.turns (spec
§2.7), a session answering nothing but health checks could still age
into a full context recycle. Handshakes are supervisor plumbing, not
work; count_turns() already counts real requests independently.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01TpGKTMigEnof73AvgQpX8D
EOF
)"
```

---

### Task 2: A failed handshake must not kill a previously-live session

**Files:**
- Modify: `src/pxh/brain_daemon.py:405-418` (`run_handshake`'s attempts-exhausted branch)
- Test: `tests/test_brain_daemon.py:526-539` (rename + rewrite)
- Test: `tests/test_brain_daemon.py:558-576` (rename, body unchanged)
- Test: `tests/test_brain_daemon.py:669-685` (rename + rewrite)

**Interfaces:**
- Consumes: `tmux_claude.kill_session(spec) -> bool` (existing, no longer called from this branch), `brain.clear_validation_marker(session) -> None` (existing, still called), `brain.session_state(session) -> str` returning one of `brain.VALIDATED` / `VALIDATING` / `NO_MARKER` / `SESSION_ABSENT` (existing).
- Produces: after this task, a session whose handshake fails all `HANDSHAKE_ATTEMPTS` reports `brain.session_state(session) == brain.NO_MARKER` (not `SESSION_ABSENT`), remains in `tmux_claude`'s live session set, and its `SessionState.holder` is left attached and alive.

- [ ] **Step 1: Write the failing tests**

In `tests/test_brain_daemon.py`, replace the test at lines 526-539:

```python
def test_a_silent_session_is_escaped_retried_then_killed(fake_tmux, _fast_handshake, monkeypatch):
    """The handshake does its own escalation. It cannot rely on check_wedge,
    which clears itself the moment the glyph is back — exactly the case a
    permission dialog produces."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, answer=False))

    assert brain_daemon.run_handshake(_state(session), "no_marker") is False
    assert (session, "Escape") in fake_tmux.keys
    assert session in fake_tmux.killed
    assert brain.session_state(session) == brain.SESSION_ABSENT
```

with:

```python
def test_a_silent_session_is_escaped_and_retried_but_not_killed(
        fake_tmux, _fast_handshake, monkeypatch):
    """The handshake still does its own per-attempt escalation — Escape
    between retries — but exhausting HANDSHAKE_ATTEMPTS no longer kills a
    session that is merely unvalidated. A 2026-08-20 audit found this kill
    branch caused 6 of 19 session recreations on its own; a failed
    handshake is proof the session cannot currently be trusted, not proof
    the process must die. Only check_wedge, on a real stuck caller
    request, may kill."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, answer=False))

    assert brain_daemon.run_handshake(_state(session), "no_marker") is False
    assert (session, "Escape") in fake_tmux.keys
    assert session not in fake_tmux.killed
    assert brain.session_state(session) == brain.NO_MARKER
```

Then replace the test at (now-shifted, search by name) `test_a_handshake_after_a_kill_uses_a_fresh_id`:

```python
def test_a_handshake_after_a_kill_uses_a_fresh_id(fake_tmux, _fast_handshake, monkeypatch):
    """The sweep and the new handshake would otherwise handle one id in both
    roles — one moving it to dead/, the other waiting on it."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, answer=False))
    first = _state(session)
    fake_tmux.sessions.add(session)
    brain_daemon.run_handshake(first, "no_marker")
    first_ids = {re.search(r"/([0-9a-f-]{36})\.json", t).group(1)
                 for _, t in fake_tmux.injected if "NEW REQUEST" in t}

    fake_tmux.injected.clear()
    fake_tmux.sessions.add(session)
    brain_daemon.run_handshake(_state(session), "no_marker")
    second_ids = {re.search(r"/([0-9a-f-]{36})\.json", t).group(1)
                  for _, t in fake_tmux.injected if "NEW REQUEST" in t}
    assert first_ids.isdisjoint(second_ids)
```

with (rename only — body and behaviour are unchanged, since `uuid4()` is fresh per call regardless of whether the prior session was killed; the old name and docstring implied the freshness depended on a kill that no longer happens):

```python
def test_two_failed_handshakes_use_a_fresh_id_each_time(fake_tmux, _fast_handshake, monkeypatch):
    """Each run_handshake call mints its own uuid4 regardless of whether the
    previous attempt's session survived — it does now, see
    test_a_silent_session_is_escaped_and_retried_but_not_killed."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, answer=False))
    first = _state(session)
    fake_tmux.sessions.add(session)
    brain_daemon.run_handshake(first, "no_marker")
    first_ids = {re.search(r"/([0-9a-f-]{36})\.json", t).group(1)
                 for _, t in fake_tmux.injected if "NEW REQUEST" in t}

    fake_tmux.injected.clear()
    fake_tmux.sessions.add(session)
    brain_daemon.run_handshake(_state(session), "no_marker")
    second_ids = {re.search(r"/([0-9a-f-]{36})\.json", t).group(1)
                  for _, t in fake_tmux.injected if "NEW REQUEST" in t}
    assert first_ids.isdisjoint(second_ids)
```

Then replace the test at (search by name) `test_a_killed_handshake_stops_and_drops_the_holder`:

```python
def test_a_killed_handshake_stops_and_drops_the_holder(fake_tmux, _fast_handshake, monkeypatch):
    """The holder is attached to a session that no longer exists after a kill.
    Leaving it in place would leak a client attached to nothing."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, answer=False))

    state = _state(session)
    holder = _FakeHolder(brain.spec_for_session(session))
    holder.start()
    state.holder = holder

    assert brain_daemon.run_handshake(state, "no_marker") is False
    assert holder.alive() is False
    assert state.holder is None
```

with:

```python
def test_a_failed_handshake_leaves_the_holder_attached(fake_tmux, _fast_handshake, monkeypatch):
    """A failed handshake no longer kills the session, so the holder must
    not be stopped either — it still has something to hold."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, answer=False))

    state = _state(session)
    holder = _FakeHolder(brain.spec_for_session(session))
    holder.start()
    state.holder = holder

    assert brain_daemon.run_handshake(state, "no_marker") is False
    assert holder.alive() is True
    assert state.holder is holder
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_brain_daemon.py -k "not_killed or fresh_id_each_time or leaves_the_holder_attached" -v`
Expected: FAIL — the current code still kills the session and drops the holder, so `session not in fake_tmux.killed` and `holder.alive() is True` both fail.

- [ ] **Step 3: Stop killing the session on a failed handshake**

In `src/pxh/brain_daemon.py`, inside `run_handshake`, replace:

```python
        # Attempts exhausted. Kill it: the next tick sees session_absent,
        # recreates, sweeps, and handshakes with a new id.
        health.record_failure(
            state.component,
            f"handshake failed after {brain.HANDSHAKE_ATTEMPTS} attempts")
        _log("handshake_failed", session=session,
             attempts=brain.HANDSHAKE_ATTEMPTS, request=request_id)
        brain.clear_validation_marker(session)
        brain.cleanup_request(session, request_id)
        tmux_claude.kill_session(spec)
        if state.holder is not None:
            state.holder.stop()
            state.holder = None  # attached to a session that no longer exists
        return False
```

with:

```python
        # Attempts exhausted. Earlier this killed the session outright — see
        # docs/superpowers/specs/2026-08-17-brain-handshake-validation-
        # design.md §2.3 step 7 — on the theory that a session failing to
        # answer a handshake is broken and should be replaced. A 2026-08-20
        # audit of session recreations found 6 of 19 traced to exactly this
        # branch: a slow or momentarily confused session, not a dead one,
        # killed by its own diagnostic. A failed handshake is proof the
        # session is not currently trustable, not proof the process must
        # die.
        #
        # So: clear the marker — every reader now sees no_marker, and
        # ask_brain will not route to this session — and record the
        # failure, but leave the session and its holder alone. The next
        # tick's handshake_reason() sees no_marker and tries again on its
        # own schedule (§2.3's third trigger, unaffected by this change).
        # If the session really is wedged rather than merely unvalidated,
        # check_wedge() is the path that kills it — on the concrete
        # evidence of a live caller request past its deadline, not on a
        # diagnostic ping failing twice. Pinned by
        # test_a_silent_session_is_escaped_and_retried_but_not_killed and
        # test_a_failed_handshake_leaves_the_holder_attached.
        health.record_failure(
            state.component,
            f"handshake failed after {brain.HANDSHAKE_ATTEMPTS} attempts")
        _log("handshake_failed", session=session,
             attempts=brain.HANDSHAKE_ATTEMPTS, request=request_id)
        brain.clear_validation_marker(session)
        brain.cleanup_request(session, request_id)
        return False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_brain_daemon.py -k "not_killed or fresh_id_each_time or leaves_the_holder_attached" -v`
Expected: PASS

- [ ] **Step 5: Run the full brain_daemon suite to check for regressions**

Run: `python -m pytest tests/test_brain_daemon.py -v`
Expected: all PASS. In particular re-check `test_a_write_failure_does_not_orphan_the_inbox_entry` (line ~625) — it asserts `run_handshake(...) is False` and inbox emptiness only, not killing, so it is unaffected. `spec` is still used earlier in `run_handshake` (for `ensure_session`, `inject`, `send_key`) so removing its one remaining use in the deleted branch does not make it unused — confirm with a lint/import pass in the next step.

- [ ] **Step 6: Confirm `spec` and `tmux_claude` are still used elsewhere in the file**

Run: `grep -n "tmux_claude\." src/pxh/brain_daemon.py | wc -l`
Expected: a number greater than 1 (the module is still used by `start_session`, `check_wedge`, `run_handshake`'s earlier lines, etc.) — this is a sanity check, not a new constraint; no code change expected here.

- [ ] **Step 7: Commit**

```bash
git add src/pxh/brain_daemon.py tests/test_brain_daemon.py
git commit -m "$(cat <<'EOF'
fix(brain): a failed handshake unvalidates a session, no longer kills it

A 2026-08-20 audit of spark-brain's session recreations found 6 of 19
traced to run_handshake killing the session after HANDSHAKE_ATTEMPTS
were exhausted. A failed handshake proves the session is not
currently trustable, not that the process is dead — the diagnostic
was killing the thing it was diagnosing.

Now a failed handshake clears the validation marker (so ask_brain
falls back immediately) and records the failure, but leaves the
session and its holder running. Only check_wedge kills a session now,
and only on the concrete evidence of a live caller request stuck past
its deadline.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01TpGKTMigEnof73AvgQpX8D
EOF
)"
```

---

### Task 3: Instrument `bin/px-claude-session` to record every exit

**Files:**
- Modify: `bin/px-claude-session:116-131` (replace the tail of the script — everything from the nesting `unset` line through the final `exec`)
- Test: Create `tests/test_claude_session_launcher.py`

**Interfaces:**
- Consumes: `pxh.logging.log_event(name: str, payload: Mapping[str, Any]) -> None` (existing — used unmodified, called as `log_event("claude-session", {...})`), `PX_CLAUDE_BIN`, `PX_CLAUDE_TMUX_PROMPT`, `PX_STATE_DIR`, `LOG_DIR` (existing env overrides the script and `pxh.logging` already honour).
- Produces: `logs/tool-claude-session.log` — one JSON line per `start` event and one per `exit` event, each carrying (at least) `event`, `session`, `pid`, `model`, `claude_version`, `boot_id`, `start_ts`; the `exit` event additionally carries `end_ts`, `duration_s`, `exit_code` (present XOR `signal`), and `stderr_tail`. Also `state/brain/<session>/last_stderr.log` — the current run's full stderr, truncated at the start of each new launch. Later tasks/callers do not depend on any of these names beyond what's stated here.

**Risk note for the reviewer:** this task removes the script's `exec "$CLAUDE_BIN" ...` tail and replaces it with a background-and-wait wrapper. That is a deliberate, necessary change — `exec` replaces the process, so nothing is left running to observe how or when Claude stopped, which is exactly why `spark-brain` could vanish 12 times with "no repo-owned kill path." The new form is the standard shell idiom for "supervise a foreground TTY child and observe its exit" (no job control is enabled, so the backgrounded child stays in the script's own process group and the terminal's foreground process group — it does not lose access to the pty). This is a repo-wide production launcher (`spark-brain` and `spark-io` both use it); do not restart the live sessions to pick this up as part of this task — that is a deliberate follow-up for a human, once this is reviewed.

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_session_launcher.py`:

```python
"""Tests for bin/px-claude-session's exit instrumentation.

spark-brain has vanished from tmux 12 times with no repo-owned kill path.
`exec`ing claude directly left nothing running to observe how or when it
stopped. These tests drive the real script against a fake `claude` binary
(no tmux, no real Claude Code) and check what lands in the log.
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "px-claude-session"


def _write_fake_claude(path: Path, *, exit_code=None, signal=None) -> None:
    assert (exit_code is None) != (signal is None), "pick exactly one"
    body = [
        "#!/usr/bin/env bash",
        'if [[ "${1:-}" == "--version" ]]; then',
        '    echo "fake-claude 9.9.9"',
        "    exit 0",
        "fi",
        'echo "fake claude stdout"',
        'echo "boom stderr line 1" >&2',
        'echo "boom stderr line 2" >&2',
    ]
    if signal is not None:
        body.append(f"kill -{signal} $$")
        body.append("sleep 5")  # never reached if the signal is delivered
    else:
        body.append(f"exit {exit_code}")
    path.write_text("\n".join(body) + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run_launcher(tmp_path, fake_claude, *, session="spark-brain"):
    log_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    log_dir.mkdir()
    state_dir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("system prompt with {{TOOL_BRAIN_REPLY}} in it\n")

    env = os.environ.copy()
    env["LOG_DIR"] = str(log_dir)
    env["PX_STATE_DIR"] = str(state_dir)
    env["PX_CLAUDE_BIN"] = str(fake_claude)
    env["PX_CLAUDE_TMUX_PROMPT"] = str(prompt)
    env["PX_BRAIN_SESSION"] = session

    result = subprocess.run(
        [str(LAUNCHER)], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=30,
    )
    log_path = log_dir / "tool-claude-session.log"
    events = []
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))
    return result, events


def test_a_normal_exit_records_pid_start_and_exit(tmp_path):
    fake_claude = tmp_path / "fake-claude"
    _write_fake_claude(fake_claude, exit_code=3)

    result, events = _run_launcher(tmp_path, fake_claude)

    assert result.returncode == 3
    assert "boom stderr line 1" in result.stderr, \
        "stderr must still reach the terminal live, not only the log"

    starts = [e for e in events if e.get("event") == "start"]
    exits = [e for e in events if e.get("event") == "exit"]
    assert len(starts) == 1 and len(exits) == 1

    start, exit_ = starts[0], exits[0]
    assert start["session"] == "spark-brain"
    assert start["claude_version"] == "fake-claude 9.9.9"
    assert isinstance(start["pid"], int) and start["pid"] > 0
    assert start["boot_id"]

    assert exit_["pid"] == start["pid"]
    assert exit_["exit_code"] == 3
    assert "signal" not in exit_
    assert exit_["duration_s"] >= 0
    assert "boom stderr line 1" in exit_["stderr_tail"]
    assert "boom stderr line 2" in exit_["stderr_tail"]


def test_a_signal_kill_is_recorded_as_a_signal_not_an_exit_code(tmp_path):
    fake_claude = tmp_path / "fake-claude"
    _write_fake_claude(fake_claude, signal="TERM")

    result, events = _run_launcher(tmp_path, fake_claude)

    assert result.returncode == 128 + 15  # SIGTERM
    exits = [e for e in events if e.get("event") == "exit"]
    assert len(exits) == 1
    assert exits[0]["signal"] == 15
    assert "exit_code" not in exits[0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_claude_session_launcher.py -v`
Expected: FAIL — `logs/tool-claude-session.log` does not exist yet (the current script never writes it).

- [ ] **Step 3: Replace the tail of `bin/px-claude-session`**

In `bin/px-claude-session`, replace everything from the nesting-`unset` line through the final `exec` (the current lines 116-131):

```bash
# Allow nesting when launched from inside a Claude Code session.
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT 2>/dev/null || true

# Render {{TOOL_BRAIN_REPLY}} into the prompt, so the text telling the session
# how to reply and the rule deciding whether it may are the same string. The
# prompt cannot hardcode the path (it varies by checkout) and cannot be
# relative (see above), so a placeholder is the only spelling that is right in
# both places. Bash substitution, not sed: PROJECT_ROOT is a path, and every
# sed delimiter is a character a path may legally contain.
PROMPT_TEXT="$(cat "$PROMPT_FILE")"
PROMPT_TEXT="${PROMPT_TEXT//\{\{TOOL_BRAIN_REPLY\}\}/$TOOL_BRAIN_REPLY}"

exec "$CLAUDE_BIN" \
    --model "$MODEL" \
    --allowedTools "${TOOLS[@]}" \
    --append-system-prompt "$PROMPT_TEXT"
```

with:

```bash
# Allow nesting when launched from inside a Claude Code session.
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT 2>/dev/null || true

# Render {{TOOL_BRAIN_REPLY}} into the prompt, so the text telling the session
# how to reply and the rule deciding whether it may are the same string. The
# prompt cannot hardcode the path (it varies by checkout) and cannot be
# relative (see above), so a placeholder is the only spelling that is right in
# both places. Bash substitution, not sed: PROJECT_ROOT is a path, and every
# sed delimiter is a character a path may legally contain.
PROMPT_TEXT="$(cat "$PROMPT_FILE")"
PROMPT_TEXT="${PROMPT_TEXT//\{\{TOOL_BRAIN_REPLY\}\}/$TOOL_BRAIN_REPLY}"

# ── Exit instrumentation ───────────────────────────────────────────────────
#
# spark-brain has vanished from tmux with no repo-owned kill path 12 times in
# one audit. `exec`ing claude (the old ending of this script) makes that
# unanswerable by construction: once claude replaces this process there is
# nothing left running to notice how or when it stopped, and tmux just closes
# the pane. This keeps a thin wrapper alive across the child's whole
# lifetime instead, so an exit — however it happens — lands a record that
# survives the pane's own death. Do not go back to a bare `exec` here.
CLAUDE_VERSION="$("$CLAUDE_BIN" --version 2>/dev/null | head -n1 || echo unknown)"
BOOT_ID="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)"
# /proc/uptime, not `date`: this host has no RTC and timesyncd has stepped
# its wall clock by tens of minutes mid-session (see
# brain_daemon._read_boot_id). A duration from two `date` reads can be wrong
# in either direction across a step; uptime cannot step.
START_UPTIME_S="$(awk '{print $1}' /proc/uptime 2>/dev/null || echo 0)"
START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"

_log_claude_session_event() {
    # Every dynamic value travels through an env var, never through the
    # heredoc text itself — a stderr tail is arbitrary process output and may
    # contain quotes, `$`, backticks or control bytes that would otherwise be
    # re-interpreted by bash or corrupt hand-built JSON. python does the
    # escaping, and log_event() gives this the same locking and 5 MB
    # rotation every other daemon log already has, in
    # logs/tool-claude-session.log.
    PXCS_EVENT="$1" /usr/bin/python3 <<'PYEOF' || true
import os

try:
    from pxh.logging import log_event

    def _num(name):
        val = os.environ.get(name)
        if val in (None, ""):
            return None
        try:
            return float(val) if "." in val else int(val)
        except ValueError:
            return val

    payload = {
        "event": os.environ.get("PXCS_EVENT"),
        "session": os.environ.get("PXCS_SESSION"),
        "pid": _num("PXCS_PID"),
        "model": os.environ.get("PXCS_MODEL"),
        "claude_version": os.environ.get("PXCS_CLAUDE_VERSION"),
        "cwd": os.environ.get("PXCS_CWD"),
        "tool_count": _num("PXCS_TOOL_COUNT"),
        "boot_id": os.environ.get("PXCS_BOOT_ID"),
        "supervisor_instance": os.environ.get("PXCS_SUPERVISOR_INSTANCE") or None,
        "supervisor_boot_id": os.environ.get("PXCS_SUPERVISOR_BOOT_ID") or None,
        "start_ts": os.environ.get("PXCS_START_TS"),
        "end_ts": os.environ.get("PXCS_END_TS"),
        "duration_s": _num("PXCS_DURATION_S"),
        "exit_code": _num("PXCS_EXIT_CODE"),
        "signal": _num("PXCS_SIGNAL"),
        "stderr_tail": os.environ.get("PXCS_STDERR_TAIL") or None,
    }
    log_event("claude-session", {k: v for k, v in payload.items() if v is not None})
except Exception:  # noqa: BLE001 - instrumentation must never break the launch
    pass
PYEOF
}

STDERR_LOG="${PX_STATE_DIR:-$PROJECT_ROOT/state}/brain/$BRAIN_SESSION/last_stderr.log"
mkdir -p "$(dirname "$STDERR_LOG")" 2>/dev/null || true
: > "$STDERR_LOG" 2>/dev/null || true

# Backgrounded, then waited on: this is what makes $! the real claude PID and
# $? its real exit status (128+signal if a signal killed it), instead of
# `exec` replacing this process outright. Stderr is teed rather than
# redirected outright so a human attached to the pane still sees it live;
# only a copy goes to disk. No job control is enabled in this script, so the
# backgrounded child stays in this process's own group and the terminal's
# foreground group — it does not lose the pty.
set +e
"$CLAUDE_BIN" \
    --model "$MODEL" \
    --allowedTools "${TOOLS[@]}" \
    --append-system-prompt "$PROMPT_TEXT" \
    2> >(tee -a "$STDERR_LOG" >&2) &
CLAUDE_PID=$!

PXCS_SESSION="$BRAIN_SESSION" \
PXCS_PID="$CLAUDE_PID" \
PXCS_MODEL="$MODEL" \
PXCS_CLAUDE_VERSION="$CLAUDE_VERSION" \
PXCS_CWD="$PWD" \
PXCS_TOOL_COUNT="${#TOOLS[@]}" \
PXCS_BOOT_ID="$BOOT_ID" \
PXCS_SUPERVISOR_INSTANCE="${PX_BRAIN_SUPERVISOR_INSTANCE:-}" \
PXCS_SUPERVISOR_BOOT_ID="${PX_BRAIN_SUPERVISOR_BOOT_ID:-}" \
PXCS_START_TS="$START_TS" \
    _log_claude_session_event start

wait "$CLAUDE_PID"
CLAUDE_STATUS=$?
wait  # let the stderr tee subshell finish flushing before we read its file
# `set -e` is deliberately NOT restored below this point. Everything from
# here on is instrumentation racing to log an exit that already happened;
# the one thing that must not happen is this script dying to an errexit
# from e.g. a transient `awk`/`date` hiccup before it reaches the final
# `exit "$CLAUDE_STATUS"` — that would silently swap a real exit code for
# whatever bash does on an uncaught errexit, defeating the entire point.

END_UPTIME_S="$(awk '{print $1}' /proc/uptime 2>/dev/null || echo 0)"
END_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
DURATION_S="$(awk -v a="$START_UPTIME_S" -v b="$END_UPTIME_S" 'BEGIN{printf "%.3f", b-a}' 2>/dev/null || echo 0)"

EXIT_CODE=""
SIGNAL=""
if [[ "$CLAUDE_STATUS" -gt 128 ]]; then
    SIGNAL=$((CLAUDE_STATUS - 128))
else
    EXIT_CODE="$CLAUDE_STATUS"
fi

STDERR_TAIL="$(tail -c 4000 "$STDERR_LOG" 2>/dev/null || true)"

PXCS_SESSION="$BRAIN_SESSION" \
PXCS_PID="$CLAUDE_PID" \
PXCS_MODEL="$MODEL" \
PXCS_CLAUDE_VERSION="$CLAUDE_VERSION" \
PXCS_BOOT_ID="$BOOT_ID" \
PXCS_SUPERVISOR_INSTANCE="${PX_BRAIN_SUPERVISOR_INSTANCE:-}" \
PXCS_SUPERVISOR_BOOT_ID="${PX_BRAIN_SUPERVISOR_BOOT_ID:-}" \
PXCS_START_TS="$START_TS" \
PXCS_END_TS="$END_TS" \
PXCS_DURATION_S="$DURATION_S" \
PXCS_EXIT_CODE="$EXIT_CODE" \
PXCS_SIGNAL="$SIGNAL" \
PXCS_STDERR_TAIL="$STDERR_TAIL" \
    _log_claude_session_event exit

exit "$CLAUDE_STATUS"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_claude_session_launcher.py -v`
Expected: PASS

- [ ] **Step 5: Run it once by hand to see the log shape**

Run:
```bash
bash -n bin/px-claude-session   # syntax check
```
Expected: no output (clean parse). This catches a stray quoting mistake before it reaches a live tmux pane.

- [ ] **Step 6: Run the full non-live suite to check for regressions**

Run: `python -m pytest -m "not live"`
Expected: same pass/fail counts as the pre-existing baseline (see `docs/testing.md`), plus the new tests passing. This script is not imported by any Python module, so no other test should reference it — a regression here would only show up in `tests/test_claude_session_launcher.py` itself or in a `test_brain_envelope.py` check on prompt-file content (unaffected, since the prompt files themselves are untouched).

- [ ] **Step 7: Commit**

```bash
git add bin/px-claude-session tests/test_claude_session_launcher.py
git commit -m "$(cat <<'EOF'
instrument(brain): log every px-claude-session exit before changing anything else

spark-brain vanished from tmux 12 times with no repo-owned kill path.
The launcher used to exec claude directly, which replaces the process
— once that happens nothing is left running to notice how or when it
stopped, and tmux just closes the pane.

px-claude-session now backgrounds claude and waits on it instead,
logging a start event (pid, model, Claude CLI version, boot id, cwd,
tool count, supervisor instance) the moment it launches and an exit
event (exit code or signal, duration measured via /proc/uptime since
this host's wall clock steps, a bounded stderr tail) the moment it
stops — via the existing locked, rotated pxh.logging.log_event(), in
logs/tool-claude-session.log, independent of the tmux pane. Does not
touch the live spark-brain/spark-io sessions; picking this up needs a
deliberate restart, not part of this change.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01TpGKTMigEnof73AvgQpX8D
EOF
)"
```

---

### Task 4: Carry the supervisor's identity into the launcher's environment

**Files:**
- Modify: `src/pxh/brain_daemon.py` (imports line, and `start_session`)
- Test: `tests/test_brain_daemon.py` (new test near `test_a_holder_is_attached_to_every_session`)

**Interfaces:**
- Consumes: `brain_daemon._INSTANCE_ID: str`, `brain_daemon._BOOT_ID: str` (existing module-level values), `tmux_claude.SessionSpec` (existing frozen dataclass with an `env: dict[str, str]` field), `dataclasses.replace` (stdlib).
- Produces: `start_session()` now calls `tmux_claude.ensure_session()` with a `spec` whose `env` includes `PX_BRAIN_SUPERVISOR_INSTANCE` and `PX_BRAIN_SUPERVISOR_BOOT_ID`. `bin/px-claude-session` (Task 3) already reads these two env vars if present — this task is what actually sets them for the resident sessions.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_brain_daemon.py`, directly after `test_a_holder_is_attached_to_every_session`:

```python
def test_start_session_passes_supervisor_identity_to_the_launcher_env(fake_tmux, monkeypatch):
    """The launcher's own exit log can only name which supervisor started it
    if the supervisor tells it — useful when comparing spark-brain against
    spark-io after a death, since both run under the same supervisor
    process and the same tmux server."""
    captured = {}
    real_ensure = tmux_claude.ensure_session

    def _capture(timeout_s=None, spec=None):
        captured["spec"] = spec
        return real_ensure(timeout_s=timeout_s, spec=spec)

    monkeypatch.setattr(tmux_claude, "ensure_session", _capture)
    state = _state(brain.BRAIN_SESSION)
    assert brain_daemon.start_session(state) is True

    env = captured["spec"].env
    assert env["PX_BRAIN_SUPERVISOR_INSTANCE"] == brain_daemon._INSTANCE_ID
    assert env["PX_BRAIN_SUPERVISOR_BOOT_ID"] == brain_daemon._BOOT_ID
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_brain_daemon.py::test_start_session_passes_supervisor_identity_to_the_launcher_env -v`
Expected: FAIL — `KeyError: 'PX_BRAIN_SUPERVISOR_INSTANCE'` (the spec's `env` does not carry it yet).

- [ ] **Step 3: Pass the supervisor's identity through `spec.env`**

In `src/pxh/brain_daemon.py`, update the import line:

```python
from dataclasses import dataclass, field
```

to:

```python
from dataclasses import dataclass, field, replace
```

Then, inside `start_session`, replace:

```python
def start_session(state: SessionState,
                  now_local: datetime | None = None) -> bool:
    """Bring one session up and attach its holder. Idempotent per tick."""
    spec = brain.spec_for_session(state.name)
    brain.ensure_mailbox(state.name)
```

with:

```python
def start_session(state: SessionState,
                  now_local: datetime | None = None) -> bool:
    """Bring one session up and attach its holder. Idempotent per tick."""
    spec = brain.spec_for_session(state.name)
    # Only matters at actual session creation — tmux_claude.ensure_session
    # only applies -e env args on new-session, not on an idempotent recheck
    # — but harmless to set unconditionally. Lets px-claude-session's exit
    # log (bin/px-claude-session) name which supervisor process and boot
    # started it, which is exactly the axis a comparison between spark-brain
    # and spark-io needs after a death.
    spec = replace(spec, env={
        **spec.env,
        "PX_BRAIN_SUPERVISOR_INSTANCE": _INSTANCE_ID,
        "PX_BRAIN_SUPERVISOR_BOOT_ID": _BOOT_ID,
    })
    brain.ensure_mailbox(state.name)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_brain_daemon.py::test_start_session_passes_supervisor_identity_to_the_launcher_env -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest -m "not live"`
Expected: same baseline as Task 3's Step 6, plus this new test passing.

- [ ] **Step 6: Commit and push**

```bash
git add src/pxh/brain_daemon.py tests/test_brain_daemon.py
git commit -m "$(cat <<'EOF'
feat(brain): tell the launcher which supervisor instance started it

Threads brain_daemon's own _INSTANCE_ID/_BOOT_ID into the tmux session
env (PX_BRAIN_SUPERVISOR_INSTANCE/PX_BRAIN_SUPERVISOR_BOOT_ID) so
px-claude-session's exit log can record which supervisor process
started the session that later exited — useful when comparing
spark-brain (12 unexplained deaths) against spark-io (zero) after the
next one.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01TpGKTMigEnof73AvgQpX8D
EOF
)"
git push
```

---

## After this plan lands

This plan only changes the checked-out repository. It does not:

- restart `px-brain.service` or recreate the live `spark-brain`/`spark-io` tmux sessions — the currently-running Claude processes were launched under the old `exec`-based script and will not pick up the instrumentation until they are recreated;
- speculate about *why* `spark-brain` has been exiting — Task 3 only makes the next exit observable.

Once a real `spark-brain` exit happens under the new launcher, `grep '"event": "exit"' logs/tool-claude-session.log | tail` (or the equivalent `jq` filter) gives the report the original request asked for: time, session, pid, exit code/signal, stderr tail, version, and — by joining against `logs/tool-brain-daemon.log`'s `handshake_failed`/`recycle`/`wedge_kill` events around the same timestamp — the preceding request kind. That correlation is deliberately left as a manual `grep`/`jq` step rather than new code: there is exactly one death to look at once this lands, and building a joiner before seeing real data would be designing against a guess.
