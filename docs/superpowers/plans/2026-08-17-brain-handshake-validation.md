# Brain Handshake Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before any caller is allowed to use a resident Claude session, the supervisor sends it one real request and requires one real reply — so "the session can answer" becomes a proven fact on disk instead of an inference from rendered terminal output.

**Architecture:** A per-session marker file (`state/brain/<session>/validation.json`) records the outcome of a real round trip through `bin/tool-brain-reply`. `brain.session_state()` derives one of four states from that marker at read time, never storing the verdict. `px-brain` owns the handshake; `ask_brain` only reads the marker, before and after taking the single-flight lock. The prompt glyph `❯` is demoted from "the session can answer" to "the pane is accepting input" at every one of its seven call sites.

**Tech Stack:** Python 3.11 (`src/pxh/`), bash tool wrappers (`bin/`), pytest, `filelock` (per-session single-flight), stdlib `fcntl` (supervisor single-instance), tmux 3.3a, Claude Code CLI.

**Spec:** `docs/superpowers/specs/2026-08-17-brain-handshake-validation-design.md` — read it before starting. Every task below cites the section it implements; the spec carries the reasoning, this plan carries the steps.

## Global Constants (exact values, copied from the spec)

| Constant | Value | Where |
|---|---|---|
| `brain.STARTUP_CEILING_S` | `tmux_claude.STARTUP_TIMEOUT_S` (45.0) — **the same object, not a copy** | §2.6 |
| `brain.SETTLE_S` | `2.0` | §2.6 |
| `brain.HANDSHAKE_TIMEOUT_S` | `60.0` | §2.6 |
| `brain.HANDSHAKE_ATTEMPTS` | `2` | §2.6 |
| `brain.VALIDATION_CEILING_S` | `0.6 * min(health.STALE_AFTER_S["px-brain"], health.STALE_AFTER_S["px-brain-io"])` = 180 | §2.6 |
| marker file mode | `0o644` — single writer (`pi`), every other process reads | §2.1 |
| mailbox dir mode | `0o1777` — **unchanged, do not touch** | brain.py docstring |
| lock file mode | `0o666` — **unchanged, do not touch** | brain.py docstring |
| states | `"validated"`, `"validating"`, `"no_marker"`, `"session_absent"` — these exact strings reach logs and `px-brain-status` | §2.2 |
| handshake kind | `"handshake"` | §2.7 |

## Global Constraints

- **`ask_brain()` returns `None` on every failure and never raises.** No task may introduce an exception path. `None` means "fall back to the Ollama tiers".
- **Health reporting never raises.** `health.record_*` is best-effort by design; never wrap it in logic that can fail.
- **`tick()` never raises.** A supervisor that dies supervises nothing. Every new call inside `tick()` goes inside the existing `try`/`except Exception` or gets its own.
- **There is exactly one spelling of `tool-brain-reply` and it is absolute** (`brain.TOOL_BRAIN_REPLY`). Never write a literal `tool-brain-reply` into a prompt, an allowlist, or a nudge. Prompts use the `{{TOOL_BRAIN_REPLY}}` placeholder, substituted by `bin/px-claude-session`.
- **Do not "tighten" the 1777 mailbox directories or the 0666 lock file.** Different uids write them. The new marker is different: 0644, one writer.
- **Never inject into a busy pane**, and never inject from a caller — only the supervisor injects lifecycle keystrokes now.
- All time-of-day logic uses `ZoneInfo("Australia/Hobart")`.
- `bin/px-brain` runs under `/usr/bin/python3`, **not** the venv. Anything it imports must be stdlib or already vendored in system site-packages. This is why the supervisor guard is `fcntl`, not `filelock`.
- Run the full suite (`python -m pytest -q`) before each commit. Existing brain tests must stay green; several are updated by name in the tasks below.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/pxh/brain.py` | marker read/write, `session_state()`, budget constants, `ask_brain`'s two checks, narrow sweep. Loses `_switch_model`, `_wait_ready`, the `model` marker. | 1, 2, 5 |
| `src/pxh/brain_daemon.py` | the handshake, trigger selection + fairness, locked recycles/model changes, deadline-aware `_is_idle`, `fcntl` guard, health conditional on validation | 3, 4, 5, 6, 7, 8 |
| `src/pxh/tmux_claude.py` | `pane_ready()` docstring correction only | 8 |
| `bin/px-brain` | non-zero exit when another supervisor holds the guard | 7 |
| `bin/px-brain-status` | **new** — derived state, model, attempt, marker age, free space | 9 |
| `docs/prompts/spark-brain-system.md` | how to answer a `handshake` request | 3 |
| `docs/prompts/spark-io-system.md` | same paragraph, same placeholder | 3 |
| `tests/test_brain.py` | marker, states, budget, caller-side checks, glyph absence | 1, 2, 8 |
| `tests/test_brain_daemon.py` | handshake, fairness, sweeps, idle predicate, recycle ordering, guard | 3–7, 9 |
| `tests/test_brain_live.py` | **new** — `@pytest.mark.live` io boundary, run on the Pi | 10 |

---

### Task 1: The validation marker and `session_state()`

Implements §2.1, §2.2, §2.6. Pure read/write plus derivation — no tmux injection yet, no supervisor changes. After this task the marker exists and nothing writes it in production; that is intentional and keeps the task independently reviewable.

**Files:**
- Modify: `src/pxh/brain.py` (add after `model_marker_path`, around `:209`)
- Test: `tests/test_brain.py` (append a new section)

**Interfaces:**
- Consumes: `health.STALE_AFTER_S`, `tmux_claude.STARTUP_TIMEOUT_S`, `tmux_claude.session_exists`, `spec_for_session`, `atomic_write`, `utc_timestamp`
- Produces:
  - `brain.validation_path(session: str) -> Path`
  - `brain.read_validation_marker(session: str) -> dict[str, Any] | None`
  - `brain.write_validation_marker(session: str, *, state: str, request_id: str, model: str, attempt: int) -> bool`
  - `brain.clear_validation_marker(session: str) -> None`
  - `brain.session_state(session: str, model: str | None = None) -> str`
  - `brain.configured_model(session: str) -> str`
  - constants `STARTUP_CEILING_S`, `SETTLE_S`, `HANDSHAKE_TIMEOUT_S`, `HANDSHAKE_ATTEMPTS`, `VALIDATION_CEILING_S`, `VALIDATED`, `VALIDATING`, `NO_MARKER`, `SESSION_ABSENT`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_brain.py`:

```python
# ---------------------------------------------------------------------------
# Validation marker and derived state (§2.1, §2.2, §2.6)
# ---------------------------------------------------------------------------

@pytest.fixture
def _session_present(monkeypatch):
    """tmux has the session. Says nothing about whether it can answer."""
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)


@pytest.fixture
def _session_missing(monkeypatch):
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: False)


def test_a_marker_absent_on_a_live_session_is_no_marker(_mailbox, _session_present):
    """The loud state: the session is up and nothing is handshaking it."""
    assert brain.session_state(brain.BRAIN_SESSION) == brain.NO_MARKER


def test_no_session_at_all_is_its_own_state(_mailbox, _session_missing):
    """`session_absent` and `no_marker` are two different repairs — px-brain is
    down, versus the session is up and cannot answer."""
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=1)
    assert brain.session_state(brain.BRAIN_SESSION) == brain.SESSION_ABSENT, \
        "a marker must never outvote tmux — a dead supervisor cannot leave a lying validated behind"


def test_a_fresh_validating_marker_is_quiet(_mailbox, _session_present):
    """`validating` covers every boot and every nightly recycle. An alarm that
    fires on healthy operation several times a day is un-taught within a week."""
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validating",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=1)
    assert brain.session_state(brain.BRAIN_SESSION) == brain.VALIDATING


def test_a_stale_validating_marker_degrades_to_no_marker(_mailbox, _session_present, monkeypatch):
    """A supervisor killed mid-handshake leaves exactly this, and the repair is
    the same as any other 'nobody is working on it'."""
    monkeypatch.setattr(brain, "VALIDATION_CEILING_S", 0.05)
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validating",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=1)
    time.sleep(0.1)
    assert brain.session_state(brain.BRAIN_SESSION) == brain.NO_MARKER


def test_a_validated_marker_is_validated(_mailbox, _session_present):
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=2)
    assert brain.session_state(brain.BRAIN_SESSION) == brain.VALIDATED


def test_a_caller_naming_a_model_the_marker_does_not_carry_is_not_validated(
        _mailbox, _session_present):
    """A session's model is a property of the session. One caller must not be
    able to retune the mind out from under the next one, so it falls back."""
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=1)
    assert brain.session_state(brain.BRAIN_SESSION, model="claude-opus-4-6") != brain.VALIDATED
    assert brain.session_state(brain.BRAIN_SESSION,
                               model="claude-haiku-4-5-20251001") == brain.VALIDATED


def test_a_caller_that_names_no_model_accepts_the_session_default(_mailbox, _session_present):
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=1)
    assert brain.session_state(brain.BRAIN_SESSION) == brain.VALIDATED


def test_a_corrupt_marker_reads_as_no_marker(_mailbox, _session_present):
    """Unparseable is not validated. The one direction this may fail is quiet."""
    brain.validation_path(brain.BRAIN_SESSION).write_text("{not json")
    assert brain.session_state(brain.BRAIN_SESSION) == brain.NO_MARKER


def test_the_marker_is_single_writer_readable_not_world_writable(_mailbox, _session_present):
    """The 1777 reasoning for the mailbox does not transfer: one writer, and
    write permission for uids that never write would let a confused caller
    forge a validated marker."""
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=1)
    mode = brain.validation_path(brain.BRAIN_SESSION).stat().st_mode & 0o777
    assert mode == 0o644, f"marker mode is {oct(mode)}, must be 0o644 (readable by all, writable by pi)"


def test_validation_budget_fits_inside_the_staleness_window():
    """The bound that has to hold (§2.6), read from the modules rather than from
    literals: if someone adds a second glyph wait, the identity below stops
    describing the code and this test is what forces the conversation."""
    from pxh import health

    assert brain.STARTUP_CEILING_S is tmux_claude.STARTUP_TIMEOUT_S, \
        "there is ONE glyph wait per session start and it lives in ensure_session"
    ceiling = 0.6 * min(health.STALE_AFTER_S["px-brain"],
                        health.STALE_AFTER_S["px-brain-io"])
    assert brain.VALIDATION_CEILING_S == ceiling
    budget = (brain.STARTUP_CEILING_S + brain.SETTLE_S
              + brain.HANDSHAKE_ATTEMPTS * brain.HANDSHAKE_TIMEOUT_S)
    assert budget <= brain.VALIDATION_CEILING_S, (
        f"{budget}s of validation exceeds the {brain.VALIDATION_CEILING_S}s ceiling; the fix is "
        "a state machine that advances one step per tick, not a bigger number")


def test_the_configured_model_default_matches_the_launcher(monkeypatch):
    """brain.configured_model() and bin/px-claude-session must agree on the
    default, or the supervisor sees a permanent model mismatch and re-handshakes
    a healthy session forever."""
    monkeypatch.delenv("PX_CLAUDE_TMUX_MODEL", raising=False)
    launcher = (ROOT / "bin" / "px-claude-session").read_text()
    assert f'MODEL="${{PX_CLAUDE_TMUX_MODEL:-{brain.configured_model(brain.BRAIN_SESSION)}}}"' in launcher
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_brain.py -k "marker or validat or budget or configured_model" -v`
Expected: FAIL — `AttributeError: module 'pxh.brain' has no attribute 'validation_path'`

- [ ] **Step 3: Write the implementation**

In `src/pxh/brain.py`, add `health` to the imports at the top (no circularity — `health.py` imports only `state` and `time`):

```python
from . import health, tmux_claude
```

Add after the `MAX_REPLY_BYTES` block (~`:125`):

```python
# --------------------------------------------------------------------------
# Validation budget (§2.6)
# --------------------------------------------------------------------------

# The glyph wait that `ensure_session()` already performs internally. This is
# the SAME object, not a copy of the number: there is one glyph wait per session
# start, it lives inside ensure_session, and this term accounts for it. An
# earlier draft waited again in the supervisor and spent the same 45s twice.
STARTUP_CEILING_S = tmux_claude.STARTUP_TIMEOUT_S

# Let the pane finish drawing before typing into it.
SETTLE_S = float(os.environ.get("PX_BRAIN_SETTLE_S", "2"))

# One first turn: read a small JSON file, run one Bash tool. Generous for that,
# because a first turn pays model warm-up and permission evaluation.
HANDSHAKE_TIMEOUT_S = float(os.environ.get("PX_BRAIN_HANDSHAKE_TIMEOUT_S", "60"))
HANDSHAKE_ATTEMPTS = int(os.environ.get("PX_BRAIN_HANDSHAKE_ATTEMPTS", "2"))

# Total time one validation may consume. NOT derived from systemd: px-brain is
# Type=simple with no TimeoutStartSec and no WatchdogSec, so systemd has no
# slowness timeout to breach. What binds is that tick() walks both sessions in
# one thread, so time spent validating one is time the other is not getting a
# health write. The 0.6 leaves margin for the rest of the tick.
VALIDATION_CEILING_S = 0.6 * min(health.STALE_AFTER_S["px-brain"],
                                 health.STALE_AFTER_S["px-brain-io"])

# The four states. These exact strings reach log lines and px-brain-status,
# because the vocabulary a human uses to describe the fault should be the
# vocabulary the tool prints.
VALIDATED = "validated"
VALIDATING = "validating"
NO_MARKER = "no_marker"
SESSION_ABSENT = "session_absent"

# Unlike the mailbox directories, this file has exactly one writer — the
# supervisor, always `pi` — and every other process only reads it. Handing
# write permission to uids that never write would let a confused caller forge a
# `validated` marker for a session that cannot answer, which is the exact claim
# this design exists to make unforgeable.
_MARKER_MODE = 0o644

# The model the launcher gives a session when nothing overrides it. Must stay
# equal to bin/px-claude-session's own default, which
# test_the_configured_model_default_matches_the_launcher pins.
DEFAULT_TMUX_MODEL = "claude-haiku-4-5-20251001"
```

Add after `current_path()` (~`:207`), replacing nothing yet — `model_marker_path` stays until Task 2:

```python
def validation_path(session: str) -> Path:
    return session_dir(session) / "validation.json"


def configured_model(session: str) -> str:
    """The model this session was launched (or last switched) to.

    Read from the environment the launcher reads, so the supervisor's idea of
    the configured model and the session's actual model come from one source.
    """
    return os.environ.get("PX_CLAUDE_TMUX_MODEL", DEFAULT_TMUX_MODEL)


def read_validation_marker(session: str) -> dict[str, Any] | None:
    """The marker as written, or None if absent or unreadable.

    Reads are lenient in one direction only: anything we cannot parse is absent,
    never validated.
    """
    try:
        data = json.loads(validation_path(session).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def write_validation_marker(session: str, *, state: str, request_id: str,
                            model: str, attempt: int) -> bool:
    """Record the outcome of a handshake. Returns False if it could not land."""
    if not ensure_mailbox(session):
        return False
    marker = {
        "state": state,
        "request_id": request_id,
        "model": model,
        "attempt": attempt,
        "updated_at": utc_timestamp(),
    }
    path = validation_path(session)
    try:
        atomic_write(path, json.dumps(marker, indent=2))
    except OSError:
        return False
    try:
        # atomic_write's mkstemp yields 0600, which every reader but the writer
        # would get EACCES on. 0644 is the mode; the chmod is how it gets there.
        os.chmod(path, _MARKER_MODE)
    except OSError:
        pass
    return True


def clear_validation_marker(session: str) -> None:
    """Delete the marker. Every reader now sees `no_marker`."""
    try:
        validation_path(session).unlink()
    except OSError:
        pass


def _marker_age_s(marker: dict[str, Any]) -> float:
    """Seconds since the marker was written; +inf if it does not say."""
    stamp = marker.get("updated_at")
    if not isinstance(stamp, str):
        return float("inf")
    try:
        import datetime as _dt

        written = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if written.tzinfo is None:
            written = written.replace(tzinfo=_dt.timezone.utc)
        return (_dt.datetime.now(_dt.timezone.utc) - written).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def session_state(session: str, model: str | None = None) -> str:
    """Derive whether a session may be trusted with a request, at read time.

    Never stored — same discipline as health.py, and for the same reason: a dead
    supervisor must not be able to leave a lying `validated` behind. `model` is
    optional and usually omitted; a caller that accepts the session's own model
    asks only whether the session can answer at all.
    """
    if not tmux_claude.session_exists(spec_for_session(session)):
        return SESSION_ABSENT
    marker = read_validation_marker(session)
    if marker is None:
        return NO_MARKER
    state = marker.get("state")
    if state == VALIDATED:
        if model and marker.get("model") != model:
            return NO_MARKER
        return VALIDATED
    if state == VALIDATING and _marker_age_s(marker) <= VALIDATION_CEILING_S:
        return VALIDATING
    # A stale `validating` marker means a supervisor died mid-handshake. The
    # repair is the same as any other "nobody is working on it".
    return NO_MARKER
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_brain.py -k "marker or validat or budget or configured_model" -v`
Expected: PASS (11 tests)

Then the whole file, to prove nothing regressed: `python -m pytest tests/test_brain.py -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxh/brain.py tests/test_brain.py
git commit -m "feat(brain): a validation marker, and four states derived from it

Nothing writes the marker in production yet. This is the vocabulary the
rest of the handshake is written in: one file per session recording that a
real round trip happened, and a read-time derivation so a dead supervisor
cannot leave a lying validated behind."
```

---

### Task 2: `ask_brain` reads the marker instead of the pane

Implements §3 (both checks), §2.5's removal of per-request `/model`, and glyph sites 3 and 4 from §3.1. After this task no caller consults the glyph and no caller injects `/model`.

**Files:**
- Modify: `src/pxh/brain.py` — remove `_wait_ready` (`:348-354`), `_switch_model` (`:357-366`), `_read_model_marker` (`:334-338`), `_write_model_marker` (`:341-345`), `model_marker_path` (`:209-210`); rewrite `ask_brain` (`:417-511`)
- Modify: `src/pxh/brain_daemon.py:154-157` — drop `validation.json` on a fresh session instead of the removed `model` marker
- Test: `tests/test_brain.py`, `tests/test_brain_daemon.py:164` (existing test renamed and rewritten)

**Interfaces:**
- Consumes: `brain.session_state`, `brain.VALIDATED`, `brain.clear_validation_marker` (Task 1)
- Produces: `ask_brain` unchanged in signature — `model` is now a *filter* on the marker, not a switch instruction. `brain.nudge_line(session, request_id)` and `brain.collect_reply(session, request_id)` and `brain.cleanup_request(session, request_id)` are now public (the supervisor needs all three in Task 3).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_brain.py`:

```python
# ---------------------------------------------------------------------------
# Callers read the marker, never the pane (§3)
# ---------------------------------------------------------------------------

def _validate(session, model=None):
    """Mark a session as having answered a handshake."""
    brain.write_validation_marker(session, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model=model or brain.configured_model(session),
                                  attempt=1)


def test_an_unvalidated_session_is_not_injected_into(_mailbox, monkeypatch):
    """The pane may look perfect — a permission dialog renders a prompt glyph.
    Injecting anyway is how a caller times out against a session that was never
    going to answer."""
    injected = []
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)
    monkeypatch.setattr(tmux_claude, "pane_ready", lambda spec=None: True)
    monkeypatch.setattr(tmux_claude, "inject",
                        lambda text, spec=None: injected.append(text) or True)

    assert brain.ask_brain("research", {"q": "why"}, timeout_s=1) is None
    assert injected == [], "a caller must never inject into an unvalidated session"
    assert not list(brain.inbox_dir(brain.BRAIN_SESSION).glob("*.json")), \
        "and must not leave a request file behind either"


def test_the_pre_lock_check_is_fast_and_takes_no_lock(_mailbox, monkeypatch):
    """The common case during startup. A caller that queued behind the
    supervisor's lock would burn LOCK_WAIT_S to learn what the marker already
    said."""
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)
    monkeypatch.setattr(brain, "_lock_for", lambda session: pytest.fail(
        "the pre-lock check must return before the lock is touched"))
    assert brain.ask_brain("research", {"q": "why"}, timeout_s=1) is None


def test_the_post_lock_recheck_prevents_a_confident_wrong_answer(_mailbox, _live_pane, monkeypatch):
    """A caller can pass the pre-lock check, block on acquire(), and wake on the
    far side of the supervisor's /clear. Injecting then produces a confident
    answer generated with no context — which is worse than an error, because
    nothing downstream can tell."""
    session = brain.BRAIN_SESSION
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)
    _validate(session)

    real_lock_for = brain._lock_for

    def _invalidating_lock(sess):
        lock = real_lock_for(sess)
        real_acquire = lock.acquire

        def _acquire(*args, **kwargs):
            result = real_acquire(*args, **kwargs)
            brain.clear_validation_marker(sess)  # the supervisor's /clear lands
            return result

        lock.acquire = _acquire
        return lock

    monkeypatch.setattr(brain, "_lock_for", _invalidating_lock)

    assert brain.ask_brain("research", {"q": "why"}, timeout_s=1) is None
    assert _live_pane == [], \
        "the symptom being prevented is a confident wrong answer, not an error"


def test_a_validated_session_is_used(_mailbox, _live_pane, monkeypatch):
    """The positive control: without this, every test above would pass against a
    version of ask_brain that never works at all."""
    session = brain.BRAIN_SESSION
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)
    _validate(session)

    def _fake_brain():
        request_id = _pending_id(session)
        _reply_via_tool(session, request_id, {"verdict": "yes"})

    worker = threading.Thread(target=_fake_brain, daemon=True)
    worker.start()
    reply = brain.ask_brain("research", {"q": "why"}, timeout_s=15)
    worker.join(timeout=10)
    assert reply is not None and reply["reply"] == {"verdict": "yes"}


def test_a_caller_never_injects_a_model_switch(_mailbox, _live_pane, monkeypatch):
    """A session's model is a property of the session. Switching it per request
    retunes the mind out from under the next caller."""
    session = brain.BRAIN_SESSION
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)
    _validate(session, model="claude-haiku-4-5-20251001")

    assert brain.ask_brain("research", {"q": "why"},
                           timeout_s=1, model="claude-opus-4-6") is None
    assert not any("/model" in text for text in _live_pane), \
        "ask_brain must fall back on a model mismatch, never inject /model"


def test_every_rolled_out_kind_matches_the_session_model():
    """A kind whose model differs from the session default would fall back on
    every single call, forever, silently. Failing here is how PX_BRAIN_KINDS
    gets widened deliberately rather than by accident."""
    from pxh import claude_session

    default_kinds = ("research", "compose", "post_qa")
    for kind in default_kinds:
        model = claude_session._DEFAULT_MODELS.get(kind)
        if model is None:
            continue  # post_qa has no claude_session entry; it names no model
        assert model == brain.configured_model(brain.session_for_kind(kind)), (
            f"{kind} asks for {model} but its session runs "
            f"{brain.configured_model(brain.session_for_kind(kind))}")


def test_brain_module_no_longer_consults_the_glyph():
    """Glyph sites 3 and 4 (§3.1). The pane is for humans; callers read the
    marker. A test rather than a comment, so the pair cannot quietly come back."""
    source = (ROOT / "src" / "pxh" / "brain.py").read_text()
    assert "pane_ready" not in source, \
        "brain.py must not consult the prompt glyph — readiness is the marker"
```

Rewrite the existing daemon test in `tests/test_brain_daemon.py` (currently `test_the_model_marker_is_dropped_on_a_fresh_session`, `:164-171`):

```python
def test_validation_is_dropped_on_a_fresh_session(fake_tmux):
    """The marker describes a session that no longer exists. Trusting it would
    send the next request into a session that has never answered anything."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="x",
                                  model="claude-haiku-4-5-20251001", attempt=1)
    brain_daemon.start_session(_state(session))
    assert not brain.validation_path(session).exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_brain.py -k "unvalidated or pre_lock or post_lock or validated_session or model_switch or rolled_out or glyph" tests/test_brain_daemon.py::test_validation_is_dropped_on_a_fresh_session -v`
Expected: FAIL — `test_brain_module_no_longer_consults_the_glyph` fails on the current source; the caller tests fail because `ask_brain` still injects regardless of the marker.

- [ ] **Step 3: Write the implementation**

In `src/pxh/brain.py`, delete `model_marker_path`, `_read_model_marker`, `_write_model_marker`, `_wait_ready` and `_switch_model` entirely. Rename `_collect_reply` → `collect_reply`, `_cleanup` → `cleanup_request`, `_nudge_line` → `nudge_line` (the supervisor needs all three), updating their call sites in this file.

Then replace the body of `ask_brain` between `session = session_for_kind(kind)` and `deadline = time.time() + timeout_s` with:

```python
    session = session_for_kind(kind)
    if timeout_s is None:
        timeout_s = float(deadline_for_kind(kind))

    if not ensure_mailbox(session):
        _log("brain_unavailable", kind=kind, session=session,
             reason="mailbox not writable")
        return None

    # Fast path, before the lock. During startup this is the common case, and a
    # caller that queued behind the supervisor's lock would spend LOCK_WAIT_S
    # learning what the marker already said.
    state = session_state(session, model=model)
    if state != VALIDATED:
        _log("brain_unavailable", kind=kind, session=session,
             reason="session not validated", state=state)
        return None

    lock = _lock_for(session)
    if lock is None:
        _log("brain_unavailable", kind=kind, session=session,
             reason="filelock unavailable")
        return None

    started = time.monotonic()
    try:
        lock.acquire(timeout=LOCK_WAIT_S)
    except (FileLockTimeout, OSError):
        _log("brain_busy", kind=kind, session=session,
             waited_s=round(time.monotonic() - started, 2))
        return None
    _relax_lock_mode(session)

    request_id = str(uuid.uuid4())
    try:
        # Re-derive on the far side of the lock. The check above is
        # check-then-act across a lock boundary, and the window is not
        # theoretical: the supervisor holds this same lock for a model change or
        # a recycle, so a caller can pass the check, block here, and wake up
        # after a `/clear`. A slow supervisor costs ten seconds; a quick one
        # gets a real request injected into a session that has just forgotten
        # its identity prompt, and a confident answer produced with no context.
        # Only this second check tells those apart.
        state = session_state(session, model=model)
        if state != VALIDATED:
            _log("brain_unavailable", kind=kind, session=session,
                 reason="invalidated while waiting for the lock", state=state)
            return None

        spec = spec_for_session(session)
        deadline = time.time() + timeout_s
```

The rest of `ask_brain` (request dict, `atomic_write` pair, `record_request`, `inject`, poll loop, `finally`) is unchanged except that `_collect_reply`/`_cleanup`/`_nudge_line` are now spelled without the underscore. Note what is gone: `ensure_session`, `_wait_ready`, and the `_switch_model` branch. Starting sessions is the supervisor's job, and `tmux_claude.inject` already calls `ensure_session` idempotently for the degenerate case.

In `src/pxh/brain_daemon.py`, replace the model-marker unlink at `:154-157`:

```python
    if not existed:
        # A fresh session inherits nothing: sweep whatever the old one left, and
        # drop the validation marker so nothing trusts a round trip that a
        # session which no longer exists once completed. Deleting it leaves the
        # session at `no_marker`, which is what makes the next tick handshake it.
        swept = brain.sweep_pending(state.name)
        brain.clear_validation_marker(state.name)
        state.turns = 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_brain.py tests/test_brain_daemon.py -q`
Expected: PASS. Two existing tests in `test_brain.py` reference the removed model-switch path — `test_a_busy_pane_is_never_injected_into` (`:154`) and `test_model_is_only_switched_when_it_actually_changes` (`:233`). Delete the second (the behaviour it pins no longer exists; §2.5 removes it rather than redesigning it) and rewrite the first as `test_an_unvalidated_session_is_not_injected_into`, which is already in Step 1 — so delete `test_a_busy_pane_is_never_injected_into` too. State both deletions in the commit message.

Run: `python -m pytest -q` → PASS (a `claude_session` test may reference `model=`; the signature is unchanged, so it should not).

- [ ] **Step 5: Commit**

```bash
git add src/pxh/brain.py src/pxh/brain_daemon.py tests/test_brain.py tests/test_brain_daemon.py
git commit -m "feat(brain): callers read the validation marker, not the pane

ask_brain checks the marker twice — once before the lock as a fast path,
and once after acquiring it, because the supervisor holds the same lock for
a /clear. Without the second check a caller can wake on the far side of a
context reset and inject a real request into a session that has forgotten
its identity prompt, then return a confident answer produced with no
context. A wasted ten seconds is a performance bug; that is not.

Per-request /model is removed rather than redesigned (spec 2.5), taking
_switch_model, _wait_ready and the model marker with it. Deletes
test_model_is_only_switched_when_it_actually_changes and
test_a_busy_pane_is_never_injected_into, whose behaviours are gone and
replaced respectively."
```

---

### Task 3: The handshake

Implements §2.3 steps 2–7, §2.4, §2.7. The supervisor sends one real request through the real reply tool and requires one real reply.

**Files:**
- Modify: `src/pxh/brain_daemon.py` (add `run_handshake` and `_await_handshake_reply` after `start_session`, ~`:171`)
- Modify: `docs/prompts/spark-brain-system.md`, `docs/prompts/spark-io-system.md`
- Test: `tests/test_brain_daemon.py`

**Interfaces:**
- Consumes: `brain.write_validation_marker`, `brain.clear_validation_marker`, `brain.read_validation_marker`, `brain.nudge_line`, `brain.collect_reply`, `brain.cleanup_request`, `brain.configured_model`, `brain.HANDSHAKE_TIMEOUT_S`, `brain.HANDSHAKE_ATTEMPTS`, `brain.SETTLE_S`, `brain._lock_for`, `brain.record_request`
- Produces: `brain_daemon.run_handshake(state: SessionState, reason: str) -> bool` where `reason` is `"no_marker"` or `"model_change"`; `brain_daemon.HANDSHAKE_POLL_S`; `SessionState.last_validation_attempt: float`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_brain_daemon.py`:

```python
# ---------------------------------------------------------------------------
# The handshake — one real request, one real reply (§2.3)
# ---------------------------------------------------------------------------

@pytest.fixture
def _fast_handshake(monkeypatch):
    """Real logic, no waiting."""
    monkeypatch.setattr(brain, "SETTLE_S", 0.0)
    monkeypatch.setattr(brain, "HANDSHAKE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(brain, "HANDSHAKE_ATTEMPTS", 2)
    monkeypatch.setattr(brain_daemon, "HANDSHAKE_POLL_S", 0.01)


def _echo_when_nudged(fake_tmux, session, answer=True, wrong_echo=False):
    """Answer the handshake the way a real session does: read the inbox, write
    the outbox. Installed as the fake tmux's inject hook."""
    def _inject(text, spec=None):
        fake_tmux.injected.append((fake_tmux._name(spec), text))
        if not answer or "NEW REQUEST" not in text:
            return True
        entries = list(brain.inbox_dir(session).glob("*.json"))
        if not entries:
            return True
        request = json.loads(entries[0].read_text())
        echo = "not-the-nonce" if wrong_echo else request["payload"]["echo"]
        brain.outbox_dir(session).mkdir(parents=True, exist_ok=True)
        (brain.outbox_dir(session) / f"{request['id']}.json").write_text(
            json.dumps({"id": request["id"], "status": "ok",
                        "reply": {"echo": echo}}))
        return True
    return _inject


def test_a_round_trip_writes_a_validated_marker(fake_tmux, _fast_handshake, monkeypatch):
    """The whole point: `validated` means a real reply came back through the
    real channel, not that a prompt glyph appeared."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject", _echo_when_nudged(fake_tmux, session))

    assert brain_daemon.run_handshake(_state(session), "no_marker") is True
    marker = brain.read_validation_marker(session)
    assert marker["state"] == "validated"
    assert marker["model"] == brain.configured_model(session)
    assert brain.session_state(session) == brain.VALIDATED


def test_the_handshake_leaves_no_request_behind(fake_tmux, _fast_handshake, monkeypatch):
    """A leftover inbox entry pins _is_idle() and blocks every later recycle."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject", _echo_when_nudged(fake_tmux, session))

    brain_daemon.run_handshake(_state(session), "no_marker")
    assert not list(brain.inbox_dir(session).glob("*.json"))
    assert not brain.current_path(session).exists()


def test_a_reply_with_the_wrong_nonce_does_not_validate(fake_tmux, _fast_handshake, monkeypatch):
    """Echoing the nonce is what stops a stale reply from a previous handshake
    validating the current one."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, wrong_echo=True))

    assert brain_daemon.run_handshake(_state(session), "no_marker") is False
    assert brain.session_state(session) != brain.VALIDATED


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


def test_a_retry_re_nudges_the_same_request_id(fake_tmux, _fast_handshake, monkeypatch):
    """Two live requests for one handshake is two turns billed for one answer.
    The session may simply have been slow."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, answer=False))

    brain_daemon.run_handshake(_state(session), "no_marker")
    nudges = [text for _, text in fake_tmux.injected if "NEW REQUEST" in text]
    assert len(nudges) == 2, "HANDSHAKE_ATTEMPTS nudges, not more"
    ids = {re.search(r"/([0-9a-f-]{36})\.json", text).group(1) for text in nudges}
    assert len(ids) == 1, f"a retry must re-nudge the same id, saw {ids}"


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


def test_handshakes_are_metered_like_any_other_request(fake_tmux, _fast_handshake, monkeypatch):
    """They are real Claude turns and cost real money, and a spike in the count
    is the visible symptom of a session restart-looping."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject", _echo_when_nudged(fake_tmux, session))

    brain_daemon.run_handshake(_state(session), "no_marker")
    assert brain.meter_summary()["by_kind"].get("handshake") == 1


def test_the_marker_says_validating_while_the_handshake_runs(fake_tmux, _fast_handshake, monkeypatch):
    """`validating` is a normal state covering every boot and nightly recycle.
    A reader that saw `no_marker` there would alarm several times a day."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    seen = []

    def _inject(text, spec=None):
        seen.append(brain.read_validation_marker(session))
        return _echo_when_nudged(fake_tmux, session)(text, spec)

    monkeypatch.setattr(tmux_claude, "inject", _inject)
    brain_daemon.run_handshake(_state(session), "no_marker")
    assert seen[0]["state"] == "validating"
    assert seen[0]["attempt"] == 1


def test_a_caller_holding_the_lock_defers_the_handshake(fake_tmux, _fast_handshake, monkeypatch):
    """Injecting mid-turn splices two prompts into one and produces a
    plausible-looking wrong answer. There is another tick in ten seconds."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(brain, "LOCK_WAIT_S", 0.05)
    held = brain._lock_for(session)
    held.acquire()
    try:
        assert brain_daemon.run_handshake(_state(session), "no_marker") is False
        assert not any("NEW REQUEST" in t for _, t in fake_tmux.injected)
    finally:
        held.release()


@pytest.mark.parametrize("prompt", ["spark-brain-system.md", "spark-io-system.md"])
def test_both_prompts_explain_the_handshake_with_the_placeholder(prompt):
    """A session that does not know how to answer a handshake cannot be
    validated, and the placeholder is the only spelling that is right in both
    the prompt and the allowlist."""
    text = (ROOT / "docs" / "prompts" / prompt).read_text()
    assert "handshake" in text.lower()
    assert "payload.echo" in text
    assert "tool-brain-reply" not in text.replace("{{TOOL_BRAIN_REPLY}}", ""), \
        "the reply tool is named only via the placeholder"
```

Add the imports `re` and, at the top of `tests/test_brain_daemon.py`, `ROOT`:

```python
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_brain_daemon.py -k "handshake or round_trip or nonce or silent_session or re_nudges or fresh_id or metered or validating or prompts_explain" -v`
Expected: FAIL — `AttributeError: module 'pxh.brain_daemon' has no attribute 'run_handshake'`

- [ ] **Step 3: Write the implementation**

In `src/pxh/brain_daemon.py`, add `uuid` to the imports and this constant next to `WEDGE_GRACE_S`:

```python
# How often to check the outbox during a handshake.
HANDSHAKE_POLL_S = float(os.environ.get("PX_BRAIN_HANDSHAKE_POLL_S", "0.25"))
```

Add `last_validation_attempt: float = 0.0` to `SessionState`, with a comment:

```python
    # When this session last had a handshake attempted (monotonic). Validation
    # goes to whoever has waited longest — see _validate_one in tick().
    last_validation_attempt: float = 0.0
```

Add after `start_session`:

```python
def _await_handshake_reply(session: str, request_id: str, nonce: str,
                           timeout_s: float) -> bool:
    """Wait for one reply and require it to echo the nonce."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        reply = brain.collect_reply(session, request_id)
        if reply is not None:
            body = reply.get("reply")
            return isinstance(body, dict) and body.get("echo") == nonce
        time.sleep(HANDSHAKE_POLL_S)
    return False


def run_handshake(state: SessionState, reason: str) -> bool:
    """Send one real request and require one real reply. Returns success.

    This is not a ping. It is `tool-brain-reply` executing under the real
    permission rules, from the real cwd, with the real allowlist — the same path
    every subsequent request takes. A success proves the allowlist spelling, the
    system prompt's placeholder substitution, the mailbox permissions and
    Claude's own onboarding state all line up, and each of those has broken once.

    `reason` is "no_marker" (aged or freshly created) or "model_change".
    """
    session = state.name
    state.last_validation_attempt = time.monotonic()

    # Narrow sweep: exactly the file the aged marker names, and nothing else.
    # See §2.3 step 1 — this records the orphan of a supervisor that died
    # mid-handshake, which nothing else will ever claim.
    if reason == "no_marker":
        marker = brain.read_validation_marker(session) or {}
        stale_id = marker.get("request_id")
        if isinstance(stale_id, str) and stale_id:
            brain.sweep_one(session, stale_id)
    brain.clear_validation_marker(session)

    lock = brain._lock_for(session)
    if lock is None:
        health.record_failure(state.component, "filelock unavailable")
        return False
    try:
        lock.acquire(timeout=brain.LOCK_WAIT_S)
    except Exception:  # noqa: BLE001 - filelock's Timeout, or an OSError
        # A caller is mid-turn. Injecting now splices two prompts into one; the
        # next tick is ten seconds away.
        _log("handshake_deferred", session=session, reason="lock busy")
        return False

    spec = brain.spec_for_session(session)
    model = brain.configured_model(session)
    request_id = str(uuid.uuid4())
    nonce = str(uuid.uuid4())
    try:
        # ensure_session already polls for the glyph internally, up to its own
        # STARTUP_TIMEOUT_S. Do not wait again here — that spends the same
        # budget twice (§2.6). It returns True on session_exists() alone when
        # the prompt never appeared, and that is fine: the glyph is a
        # best-effort hint about when to start typing, and the handshake below
        # is the authoritative readiness test.
        if not tmux_claude.ensure_session(spec=spec):
            health.record_failure(state.component,
                                  tmux_claude.last_error() or "session did not start")
            return False
        time.sleep(brain.SETTLE_S)

        # One deadline for the whole handshake, so a retry does not leave the
        # request looking abandoned to _is_idle() or check_wedge().
        deadline = time.time() + brain.HANDSHAKE_ATTEMPTS * brain.HANDSHAKE_TIMEOUT_S
        try:
            atomic_write(brain.inbox_dir(session) / f"{request_id}.json",
                         json.dumps({"id": request_id, "kind": "handshake",
                                     "payload": {"echo": nonce},
                                     "deadline": deadline,
                                     "created_at": utc_timestamp()}, indent=2))
            atomic_write(brain.current_path(session),
                         json.dumps({"id": request_id, "kind": "handshake",
                                     "deadline": deadline}, indent=2))
        except OSError as exc:
            health.record_failure(state.component, f"handshake write failed: {exc}")
            return False

        for attempt in range(1, brain.HANDSHAKE_ATTEMPTS + 1):
            brain.write_validation_marker(session, state=brain.VALIDATING,
                                          request_id=request_id, model=model,
                                          attempt=attempt)
            if attempt > 1:
                # Escape whatever the last attempt left in the input box. Alone,
                # never followed by Enter — that submits a stray turn.
                tmux_claude.send_key("Escape", spec=spec)
            brain.record_request("handshake")
            if not tmux_claude.inject(brain.nudge_line(session, request_id), spec=spec):
                health.record_failure(state.component,
                                      tmux_claude.last_error() or "handshake inject failed")
                continue
            if _await_handshake_reply(session, request_id, nonce,
                                      brain.HANDSHAKE_TIMEOUT_S):
                brain.write_validation_marker(session, state=brain.VALIDATED,
                                              request_id=request_id, model=model,
                                              attempt=attempt)
                brain.cleanup_request(session, request_id)
                health.record_success(state.component,
                                      detail={"model": model, "attempt": attempt})
                _log("handshake_ok", session=session, attempt=attempt, model=model)
                return True

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
    finally:
        try:
            lock.release()
        except (RuntimeError, OSError):
            pass
```

Add `brain.sweep_one` to `src/pxh/brain.py`, next to `sweep_pending`:

```python
def sweep_one(session: str, request_id: str) -> bool:
    """Move exactly one named inbox entry to dead/. Returns whether it moved.

    The narrow sweep (§2.3 step 1). Unlike `sweep_pending` this names its target
    rather than globbing, which is what makes it safe to run without the
    single-flight lock: there is no discovery step that could pick up a request
    written by someone still waiting on it. It exists to record the orphan of a
    supervisor that died mid-handshake — the replacement handshake mints a fresh
    id, so nothing else will ever claim that file.
    """
    if not ensure_mailbox(session):
        return False
    entry = inbox_dir(session) / f"{request_id}.json"
    try:
        entry.replace(dead_dir(session) / entry.name)
        return True
    except OSError:
        return False
```

Add to **both** prompts. In `docs/prompts/spark-brain-system.md`, after the "## Requests" section; in `docs/prompts/spark-io-system.md`, after "## How a turn works":

```markdown
## Handshake requests

A request whose `kind` is `handshake` is the supervisor checking that this
session can answer at all — that the reply tool runs, that this prompt arrived,
that nothing is sitting on a permission dialog. Answer it immediately by echoing
`payload.echo` straight back, and do nothing else:

    {{TOOL_BRAIN_REPLY}} <id> '{"echo": "<the value of payload.echo>"}'

No other tools, no commentary, no work. Until it lands, every daemon that would
have asked this session for something is falling back to a smaller local model.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_brain_daemon.py -q`
Expected: PASS

Run: `python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxh/brain.py src/pxh/brain_daemon.py docs/prompts/ tests/test_brain_daemon.py
git commit -m "feat(brain): prove a session can answer before trusting it

run_handshake sends one real request through the real reply tool, under the
real permission rules, and requires one real reply echoing a nonce. That is
the only thing in the system that tests the allowlist spelling, the prompt
substitution, the mailbox permissions and Claude's onboarding state
together — each of which has broken once on its own.

Escalation is the handshake's own: check_wedge clears itself whenever the
glyph is back, which is exactly what a permission dialog renders. Retries
re-nudge the same id, because a second request would bill two turns for one
answer; a kill mints a fresh one, because the sweep and the new handshake
must not handle one id in both roles."
```

---

### Task 4: Triggers, fairness, and health that tracks validation

Implements §2.3's three triggers (collapsed into two code paths — creation deletes the marker, so creation and aging both present as `no_marker`), §2.6's one-per-tick and longest-waited rules, and glyph site 7 from §3.1.

**Files:**
- Modify: `src/pxh/brain_daemon.py` — add `handshake_reason` and `_validate_one`; rewrite `tick` (`:266-282`)
- Test: `tests/test_brain_daemon.py`

**Interfaces:**
- Consumes: `run_handshake` (Task 3), `brain.session_state`, `brain.read_validation_marker`, `brain.configured_model`
- Produces: `brain_daemon.handshake_reason(state: SessionState) -> str | None`; `brain_daemon._validate_one(live: list[SessionState]) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# ---------------------------------------------------------------------------
# Triggers and fairness (§2.3, §2.6)
# ---------------------------------------------------------------------------

def test_no_marker_on_a_live_session_is_repaired_without_a_human(fake_tmux, monkeypatch):
    """`no_marker` is the only state reachable by *aging*, so no edge ever fires
    for it. Without a level-triggered tick the loud state has a repair line and
    no path to it, and the session sits broken until someone attaches."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    # A supervisor died mid-handshake: a stale `validating` marker, aged out.
    monkeypatch.setattr(brain, "VALIDATION_CEILING_S", 0.01)
    brain.write_validation_marker(session, state="validating", request_id="old",
                                  model=brain.configured_model(session), attempt=1)
    time.sleep(0.05)

    called = []
    monkeypatch.setattr(brain_daemon, "run_handshake",
                        lambda state, reason: called.append((state.name, reason)) or True)
    brain_daemon.tick({session: _state(session)})
    assert called == [(session, "no_marker")]


def test_a_validated_session_is_not_re_handshaked(fake_tmux, monkeypatch):
    """Handshakes cost money. A working session is left alone."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="r",
                                  model=brain.configured_model(session), attempt=1)
    called = []
    monkeypatch.setattr(brain_daemon, "run_handshake",
                        lambda state, reason: called.append(reason) or True)
    brain_daemon.tick({session: _state(session)})
    assert called == []


def test_a_model_mismatch_triggers_a_transition(fake_tmux, monkeypatch):
    """Someone changed the configuration. The marker's model is only ever
    written by a handshake the new model actually answered."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="r",
                                  model="claude-opus-4-6", attempt=1)
    monkeypatch.setenv("PX_CLAUDE_TMUX_MODEL", "claude-haiku-4-5-20251001")
    called = []
    monkeypatch.setattr(brain_daemon, "run_handshake",
                        lambda state, reason: called.append(reason) or True)
    brain_daemon.tick({session: _state(session)})
    assert called == ["model_change"]


def test_at_most_one_session_is_validated_per_tick(fake_tmux, monkeypatch):
    """Two back-to-back validations in one tick would double the sibling's
    health blackout against its 300s staleness window."""
    fake_tmux.sessions.update({brain.BRAIN_SESSION, brain.IO_SESSION})
    called = []
    monkeypatch.setattr(brain_daemon, "run_handshake",
                        lambda state, reason: called.append(state.name) or False)
    states = {n: _state(n) for n in (brain.BRAIN_SESSION, brain.IO_SESSION)}
    brain_daemon.tick(states)
    assert len(called) == 1


def test_a_failing_session_cannot_starve_a_healthy_one(fake_tmux, monkeypatch):
    """The level trigger changed the failure shape: edge-triggered validation is
    self-limiting, level-triggered is self-perpetuating. First-in-iteration-order
    would hand every tick to a crash-looping spark-brain forever, and spark-io
    would report failure for never having had a turn."""
    fake_tmux.sessions.update({brain.BRAIN_SESSION, brain.IO_SESSION})
    called = []

    def _fail(state, reason):
        state.last_validation_attempt = time.monotonic()
        called.append(state.name)
        return False

    monkeypatch.setattr(brain_daemon, "run_handshake", _fail)
    states = {n: _state(n) for n in (brain.BRAIN_SESSION, brain.IO_SESSION)}
    for _ in range(6):
        brain_daemon.tick(states)

    brain_n = called.count(brain.BRAIN_SESSION)
    io_n = called.count(brain.IO_SESSION)
    assert io_n > 0, "a session that never gets a turn reports failure for no fault of its own"
    assert abs(brain_n - io_n) <= 1, f"validation must alternate, saw {called}"


def test_health_success_requires_validation_not_a_glyph(fake_tmux, monkeypatch):
    """Glyph site 7 — the original bug. This is the line that recorded `ok` for
    a session that could not answer a single request, forever."""
    from pxh import health

    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    fake_tmux.ready = True          # the pane looks perfect
    brain.ensure_mailbox(session)   # and there is no marker
    monkeypatch.setattr(brain_daemon, "run_handshake", lambda state, reason: False)

    brain_daemon.tick({session: _state(session)})
    record = health.read_health(("px-brain",))["components"]["px-brain"]
    assert record["status"] != "ok", \
        "a ready pane is not evidence of anything; a permission dialog renders one"


def test_a_validated_session_reports_ok(fake_tmux, monkeypatch):
    """The positive control for the test above."""
    from pxh import health

    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="r",
                                  model=brain.configured_model(session), attempt=1)
    brain_daemon.tick({session: _state(session)})
    record = health.read_health(("px-brain",))["components"]["px-brain"]
    assert record["status"] == "ok"


def test_validating_records_neither_success_nor_failure(fake_tmux, monkeypatch):
    """Not a success (it cannot serve) and not a failure (it is working on it).
    If it never resolves, staleness catches it — that is the alarm that was
    missing."""
    from pxh import health

    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validating", request_id="r",
                                  model=brain.configured_model(session), attempt=1)
    monkeypatch.setattr(brain_daemon, "run_handshake",
                        lambda state, reason: pytest.fail("validating is not due"))
    brain_daemon.tick({session: _state(session)})
    # read_health always lists a requested component; an absent record derives
    # to "missing" (health.py:192), which is exactly "nobody has reported yet".
    record = health.read_health(("px-brain",))["components"]["px-brain"]
    assert record["status"] == "missing", \
        "validating writes neither success nor failure; staleness is the alarm"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_brain_daemon.py -k "repaired or re_handshaked or mismatch or per_tick or starve or health_success or reports_ok or records_neither" -v`
Expected: FAIL — `tick()` still records success on the glyph and never calls `run_handshake`.

- [ ] **Step 3: Write the implementation**

Replace `tick` in `src/pxh/brain_daemon.py`:

```python
def handshake_reason(state: SessionState) -> str | None:
    """Why this session needs a handshake, or None if it does not.

    Level-triggered on derived state rather than edge-triggered on events, and
    that is the whole point: `no_marker` is the only state reachable by *aging*,
    so no edge can ever fire for it. A supervisor killed mid-handshake leaves a
    session that exists, was never validated, and ages out of `validating` with
    nobody watching — and nothing recreates it, because tmux still has it.
    """
    derived = brain.session_state(state.name)
    if derived == brain.SESSION_ABSENT:
        return None  # start_session recreates it; the next tick handshakes
    if derived == brain.VALIDATING:
        return None  # in progress, and a stale marker has already aged out
    if derived == brain.NO_MARKER:
        return "no_marker"
    marker = brain.read_validation_marker(state.name) or {}
    if marker.get("model") != brain.configured_model(state.name):
        # A caller that names a different model just falls back. Only the
        # supervisor changes a session's model, and only at an idle moment.
        return "model_change"
    return None


def _validate_one(live: list[SessionState]) -> None:
    """At most one handshake per tick, to the session that has waited longest.

    One per tick because two would double the sibling's health blackout.
    Longest-waited rather than first-in-iteration-order because level-triggered
    validation is self-perpetuating: a session that fails on every tick would
    consume the budget on every tick, and its healthy sibling would never be
    attempted at all — reporting failure not because anything is wrong with it,
    but because it never got a turn.
    """
    due: list[tuple[float, SessionState, str]] = []
    for state in live:
        try:
            reason = handshake_reason(state)
        except Exception as exc:  # noqa: BLE001
            _log("tick_error", session=state.name, error=str(exc))
            continue
        if reason is not None:
            due.append((state.last_validation_attempt, state, reason))
    if not due:
        return
    due.sort(key=lambda item: item[0])
    _, state, reason = due[0]
    try:
        run_handshake(state, reason)
    except Exception as exc:  # noqa: BLE001 - a supervisor that dies supervises nothing
        _log("tick_error", session=state.name, error=str(exc))
        health.record_failure(state.component, str(exc))


def tick(states: dict[str, SessionState]) -> None:
    """One supervisor pass. Never raises — this loop must not be killable."""
    now = time.monotonic()
    now_local = datetime.now(HOBART)
    live: list[SessionState] = []
    for state in states.values():
        try:
            if not start_session(state):
                continue
            count_turns(state)
            check_wedge(state, now)
            maybe_recycle(state, now_local)
            live.append(state)
        except Exception as exc:  # noqa: BLE001
            _log("tick_error", session=state.name, error=str(exc))
            health.record_failure(state.component, str(exc))

    _validate_one(live)

    # Health after validation, so a session validated this tick reports it
    # immediately. Conditional on the marker and never on the glyph: a
    # permission dialog renders a prompt, so the glyph reported `ok` for a
    # session that could not answer a single request, forever.
    for state in live:
        try:
            if brain.session_state(state.name) == brain.VALIDATED:
                health.record_success(state.component, min_interval_s=60,
                                      detail={"turns": state.turns})
        except Exception as exc:  # noqa: BLE001
            _log("tick_error", session=state.name, error=str(exc))
            health.record_failure(state.component, str(exc))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_brain_daemon.py -q`
Expected: PASS. `test_both_sessions_report_health_separately` (`:378`) asserts the old glyph-based success — update it to write a `validated` marker for both sessions first.

Run: `python -m pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxh/brain_daemon.py tests/test_brain_daemon.py
git commit -m "feat(brain): level-triggered validation, fair between sessions

The tick derives state and handshakes what needs it, rather than firing on
events: no_marker is the only state reachable by aging, so no edge can ever
fire for it and a supervisor killed mid-handshake would leave a session
loud and unrepaired until a human attached.

One validation per tick, to whoever has waited longest. Level-triggered
validation is self-perpetuating where the edge version was self-limiting, so
iteration order would hand every tick to a crash-looping session and starve
its healthy sibling into reporting failure for never having had a turn.

Health success now requires a validated marker. That was the bug: the glyph
reported ok for a session that could not answer anything."
```

---

### Task 5: Two sweep widths, and an idle predicate that reads deadlines

Implements §2.3's `_is_idle` block. The narrow sweep from Task 3 records the orphan; this stops *any* orphan from pinning recycles forever.

**Files:**
- Modify: `src/pxh/brain_daemon.py` — `_is_idle` (`:215-221`)
- Test: `tests/test_brain_daemon.py`

**Interfaces:**
- Consumes: `brain.inbox_dir`
- Produces: `brain_daemon._pending_live(session: str) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# ---------------------------------------------------------------------------
# _is_idle asks about live requests, not files (§2.3)
# ---------------------------------------------------------------------------

def _orphan(session, deadline, request_id="orphan"):
    """A pending inbox entry with nobody waiting on it."""
    brain.ensure_mailbox(session)
    body = {"id": request_id, "kind": "research"}
    if deadline is not None:
        body["deadline"] = deadline
    (brain.inbox_dir(session) / f"{request_id}.json").write_text(json.dumps(body))


def test_a_past_deadline_orphan_does_not_block_a_recycle(fake_tmux):
    """A killed caller leaves an inbox entry `ask_brain`'s finally: would have
    removed. Globbing for files means that session never recycles again — not
    nightly, not on turn count."""
    session = brain.BRAIN_SESSION
    _orphan(session, deadline=time.time() - 5)
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS

    assert brain_daemon._is_idle(state) is True
    brain_daemon.maybe_recycle(state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))
    assert any("/clear" in text for _, text in fake_tmux.injected), \
        "the point is that a due recycle actually fires"


def test_a_live_request_still_withholds_the_recycle(fake_tmux):
    """A `/clear` between the nudge and the reply loses the request entirely,
    and the caller can only see that as a timeout."""
    session = brain.BRAIN_SESSION
    _orphan(session, deadline=time.time() + 300)
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS

    assert brain_daemon._is_idle(state) is False
    brain_daemon.maybe_recycle(state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))
    assert not any("/clear" in text for _, text in fake_tmux.injected)


@pytest.mark.parametrize("body", ["{not json", '{"id": "x"}', '{"id": "x", "deadline": "soon"}'])
def test_an_unreadable_deadline_counts_as_live(fake_tmux, body):
    """A predicate that cannot read a deadline must not become a reason to
    recycle over a real request. Same conservative reading as check_wedge's
    isinstance guard next door."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    (brain.inbox_dir(session) / "weird.json").write_text(body)
    assert brain_daemon._is_idle(_state(session)) is False


def test_the_narrow_sweep_records_the_dead_handshake_and_recycling_recovers(
        fake_tmux, _fast_handshake, monkeypatch):
    """A supervisor killed mid-handshake leaves a request the replacement never
    claims. The sweep is how it reaches dead/ — the audit trail covers what the
    supervisor owns."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    _orphan(session, deadline=time.time() - 5, request_id="deadhs")
    _orphan(session, deadline=time.time() - 5, request_id="someoneelse")
    monkeypatch.setattr(brain, "VALIDATION_CEILING_S", 0.01)
    brain.write_validation_marker(session, state="validating", request_id="deadhs",
                                  model=brain.configured_model(session), attempt=1)
    time.sleep(0.05)
    monkeypatch.setattr(tmux_claude, "inject", _echo_when_nudged(fake_tmux, session))

    brain_daemon.run_handshake(_state(session), "no_marker")

    assert (brain.dead_dir(session) / "deadhs.json").exists(), \
        "the supervisor's own orphan is recorded"
    assert (brain.inbox_dir(session) / "someoneelse.json").exists(), \
        "a request the supervisor did not write is not its to delete"
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS
    fake_tmux.injected.clear()
    brain_daemon.maybe_recycle(state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))
    assert any("/clear" in text for _, text in fake_tmux.injected), \
        "a test that only checked the file vanished would pass against a fix that swept the wrong one"
```

Add `from datetime import datetime` to the test imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_brain_daemon.py -k "orphan or withholds or unreadable_deadline or narrow_sweep" -v`
Expected: FAIL — `_is_idle` returns False for any non-empty inbox, so the recycle never fires.

- [ ] **Step 3: Write the implementation**

Replace `_is_idle` in `src/pxh/brain_daemon.py`:

```python
def _pending_live(session: str) -> bool:
    """True if any inbox entry still has a waiter.

    The predicate `_is_idle` used to ask "is a request live?" and answer it with
    "does a file exist?" — and those differ exactly when a writer died. Every
    request carries `deadline` as wall-clock and `ask_brain` gives up precisely
    at it, so a pending entry past its deadline (with no `current.json`, which
    the caller checks first) has no waiter by construction. That holds for a
    dead handshake's request and a killed caller's alike.

    No grace period, deliberately, unlike `check_wedge`'s
    `deadline + WEDGE_GRACE_S`: `ask_brain`'s loop is `while time.time() <
    deadline` and its `finally:` removes the entry, so at the deadline the
    caller has already cleaned up and no slack is needed to be sure. The grace
    next door answers a different question — whether the *session* is stuck,
    where slack buys safety before an Escape and a kill.

    An unreadable or absent deadline counts as live. A predicate that cannot
    read a deadline must not become a reason to recycle over a real request.
    """
    now = time.time()
    for entry in brain.inbox_dir(session).glob("*.json"):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        if not isinstance(data, dict):
            return True
        deadline = data.get("deadline")
        if not isinstance(deadline, (int, float)):
            return True
        if now < deadline:
            return True
    return False


def _is_idle(state: SessionState) -> bool:
    """No request in flight, nothing live pending, and the pane is listening."""
    if _read_current(state.name) is not None:
        return False
    if _pending_live(state.name):
        return False
    return tmux_claude.pane_ready(brain.spec_for_session(state.name))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_brain_daemon.py -q`
Expected: PASS. `test_recycling_waits_for_an_empty_inbox_too` (`:281`) writes an inbox file with no deadline — it still passes (unreadable deadline is live), but update its docstring to say *live*, not *empty*.

Run: `python -m pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxh/brain_daemon.py tests/test_brain_daemon.py
git commit -m "fix(brain): _is_idle asks about live requests, not files

A killed caller leaves an inbox entry the supervisor may not delete — that
scoping is deliberate — so globbing for files means that session never
recycles again, not nightly and not on turn count. Read the deadline
instead: a pending entry past it, with no current.json, has no waiter by
construction. Unreadable counts as live, matching check_wedge's guard."
```

---

### Task 6: Model changes and recycles become locked transitions

Implements §2.5, including the crash-safe ordering.

**Files:**
- Modify: `src/pxh/brain_daemon.py` — `maybe_recycle` (`:224-250`); `run_handshake` gains the `/model` injection for `reason == "model_change"`
- Test: `tests/test_brain_daemon.py`

**Interfaces:**
- Consumes: `brain._lock_for`, `brain.clear_validation_marker`, `brain.configured_model`
- Produces: no new public names

- [ ] **Step 1: Write the failing tests**

```python
# ---------------------------------------------------------------------------
# Model changes and recycles are transitions, not side effects (§2.5)
# ---------------------------------------------------------------------------

def test_a_model_change_clears_the_marker_before_injecting(fake_tmux, _fast_handshake, monkeypatch):
    """Crash-safe direction: a supervisor killed between the two steps drops the
    lock at process death and leaves no marker, so the next reader falls back.
    The reverse order has a window where the marker vouches for a session whose
    context has just been cleared."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="r",
                                  model="claude-opus-4-6", attempt=1)
    order = []

    def _inject(text, spec=None):
        order.append(("marker" if brain.validation_path(session).exists() else "no-marker", text))
        return _echo_when_nudged(fake_tmux, session)(text, spec)

    monkeypatch.setattr(tmux_claude, "inject", _inject)
    brain_daemon.run_handshake(_state(session), "model_change")

    model_injects = [entry for entry in order if entry[1].startswith("/model")]
    assert model_injects, "a model change injects /model"
    assert model_injects[0][0] == "no-marker", \
        "the marker must be gone before the keystroke that invalidates it"
    assert brain.read_validation_marker(session)["model"] == brain.configured_model(session), \
        "the marker's model is only written by a handshake the new model answered"


def test_a_recycle_holds_the_single_flight_lock(fake_tmux):
    """Today /clear is injected with no lock held at all, which races any
    caller's nudge."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS

    held = brain._lock_for(session)
    held.acquire()
    try:
        brain_daemon.maybe_recycle(
            state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))
        assert not any("/clear" in text for _, text in fake_tmux.injected), \
            "a caller mid-turn must not have its context cleared underneath it"
    finally:
        held.release()

    brain_daemon.maybe_recycle(
        state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))
    assert any("/clear" in text for _, text in fake_tmux.injected)


def test_a_recycle_clears_the_marker_so_the_next_tick_re_handshakes(fake_tmux):
    """A session that has just forgotten its identity prompt has not been
    validated on that context. Every recycle is followed by a handshake."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="r",
                                  model=brain.configured_model(session), attempt=1)
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS

    brain_daemon.maybe_recycle(
        state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))
    assert brain.session_state(session) == brain.NO_MARKER
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_brain_daemon.py -k "model_change_clears or recycle_holds or recycle_clears" -v`
Expected: FAIL — `maybe_recycle` takes no lock and never touches the marker; `run_handshake` never injects `/model`.

- [ ] **Step 3: Write the implementation**

In `run_handshake`, inside the `try:` after `ensure_session` succeeds and **before** `time.sleep(brain.SETTLE_S)`, add:

```python
        if reason == "model_change":
            # Ordering matters and this is the crash-safe direction: the marker
            # is already gone (cleared above, before the lock), so a supervisor
            # killed between here and the handshake leaves `no_marker` rather
            # than a marker vouching for a session that has just been retuned.
            _log("model_change", session=session, model=model)
            if not tmux_claude.inject(f"/model {model}", spec=spec):
                health.record_failure(state.component,
                                      tmux_claude.last_error() or "model switch failed")
                return False
```

Replace `maybe_recycle`:

```python
def maybe_recycle(state: SessionState, now_local: datetime) -> None:
    """Reset context at an idle moment — on turn count, or once a night.

    Never mid-request: a `/clear` between the nudge and the reply loses the
    request entirely, and the caller can only see that as a timeout. Held under
    the single-flight lock for the same reason, non-blocking: waiting for it
    would stall the supervisor for the length of a caller's deadline — up to
    1800s for `evolve` — and there is another tick in ten seconds.
    """
    day = now_local.strftime("%Y-%m-%d")
    nightly_due = (now_local.hour >= NIGHTLY_RECYCLE_HOUR
                   and state.last_recycle_day != day)
    turns_due = state.turns >= CONTEXT_TURNS
    if not (nightly_due or turns_due):
        return
    if not _is_idle(state):
        return

    lock = brain._lock_for(state.name)
    if lock is None:
        return
    try:
        lock.acquire(timeout=0)
    except Exception:  # noqa: BLE001 - filelock's Timeout, or an OSError
        _log("recycle_deferred", session=state.name, reason="lock busy")
        return

    try:
        spec = brain.spec_for_session(state.name)
        reason = "nightly" if nightly_due else "turns"
        _log("recycle", session=state.name, reason=reason, turns=state.turns)

        # Marker first, then the keystroke. A supervisor killed between the two
        # drops the lock at process death and leaves no marker, so the next
        # reader sees `no_marker`, falls back, and the next tick re-handshakes —
        # rather than injecting into a session whose context has just gone.
        brain.clear_validation_marker(state.name)

        # Journal before clearing — the other order throws away the thing the
        # journal was supposed to preserve.
        tmux_claude.inject(
            f"Before anything else: append anything worth keeping to {journal_path()}, "
            "then run /clear.", spec=spec)
        state.turns = 0
        if nightly_due:
            state.last_recycle_day = day
    finally:
        try:
            lock.release()
        except (RuntimeError, OSError):
            pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_brain_daemon.py -q`
Expected: PASS. The existing recycle tests (`:268-341`) should still pass; they do not hold the lock.

Run: `python -m pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxh/brain_daemon.py tests/test_brain_daemon.py
git commit -m "feat(brain): model changes and recycles are locked transitions

Both inject keystrokes that change a session's configuration after it was
validated, so both clear the marker first and re-handshake after. Marker
before keystroke is the crash-safe order: a supervisor killed between the
two leaves no marker rather than one vouching for a session that has just
forgotten its identity prompt.

The lock is taken non-blocking. Waiting would stall the supervisor for a
caller's whole deadline — 1800s for evolve — and the next tick is ten
seconds away."
```

---

### Task 7: One supervisor

Implements §2.8's guard.

**Files:**
- Modify: `src/pxh/brain_daemon.py` — add the guard, call it from `run()`
- Modify: `bin/px-brain` — document the non-zero exit
- Test: `tests/test_brain_daemon.py`

**Interfaces:**
- Produces: `brain_daemon.acquire_supervisor_lock() -> bool`, `brain_daemon.supervisor_lock_path() -> Path`, `brain_daemon.release_supervisor_lock() -> None` (tests only)

- [ ] **Step 1: Write the failing tests**

```python
# ---------------------------------------------------------------------------
# One supervisor (§2.8)
# ---------------------------------------------------------------------------

def test_a_second_supervisor_refuses_to_start(fake_tmux):
    """start_session, sweep_pending, kill_session and check_wedge all run
    outside the single-flight lock, so a second supervisor can sweep the first
    one's in-flight handshake into dead/ and the handshake then times out
    against a request that no longer exists."""
    assert brain_daemon.acquire_supervisor_lock() is True
    try:
        assert brain_daemon.run(once=True) != 0, \
            "the loser exits non-zero rather than starting"
    finally:
        brain_daemon.release_supervisor_lock()


def test_the_winner_writes_its_pid_as_a_hint(fake_tmux):
    """flock fails with EWOULDBLOCK and nothing else — there is no F_GETLK for
    it — so the holder's pid is not recoverable from the call. The winner writes
    it, and the loser reads it as a hint that may be stale."""
    assert brain_daemon.acquire_supervisor_lock() is True
    try:
        assert brain_daemon.supervisor_lock_path().read_text().strip() == str(os.getpid())
    finally:
        brain_daemon.release_supervisor_lock()


def test_the_guard_is_released_when_the_holder_dies(fake_tmux):
    """A supervisor that can be SIGKILLed wants a guard the kernel releases at
    death — no stale-PID window, no PID-reuse cmdline check to get subtly
    wrong."""
    script = (
        "import os, sys;"
        "sys.path.insert(0, %r);"
        "os.environ['PX_STATE_DIR'] = %r;"
        "from pxh import brain, brain_daemon;"
        "brain.brain_root = lambda: __import__('pathlib').Path(%r) / 'brain';"
        "sys.exit(0 if brain_daemon.acquire_supervisor_lock() else 1)"
    )
    root = str(ROOT / "src")
    state = str(brain.brain_root().parent)
    first = subprocess.run([sys.executable, "-c", script % (root, state, state)])
    assert first.returncode == 0, "the first process gets the guard"
    second = subprocess.run([sys.executable, "-c", script % (root, state, state)])
    assert second.returncode == 0, "and the guard is gone once that process exits"
```

Add `import os`, `import subprocess`, `import sys` to the test imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_brain_daemon.py -k "second_supervisor or writes_its_pid or holder_dies" -v`
Expected: FAIL — `AttributeError: module 'pxh.brain_daemon' has no attribute 'acquire_supervisor_lock'`

- [ ] **Step 3: Write the implementation**

In `src/pxh/brain_daemon.py`, add `import fcntl` and:

```python
# The supervisor's own fd, held for the process lifetime. Module-level because
# closing it would drop the lock: flock is released on the last close of the
# file, so a local variable going out of scope would silently unguard us.
_supervisor_fd: int | None = None


def supervisor_lock_path() -> Path:
    return brain.brain_root() / ".supervisor.lock"


def acquire_supervisor_lock() -> bool:
    """Take the single-instance guard. False means another supervisor has it.

    `bin/px-brain` has had no guard at all, and the obvious way to get two
    supervisors is an operator running it in a shell to watch it while systemd
    already has one. The damage is not subtle: `start_session`, `sweep_pending`,
    `kill_session` and `check_wedge` all run outside the per-session
    single-flight lock, so one supervisor can sweep the other's in-flight
    handshake into `dead/`.

    flock rather than px-mind's PID-file-plus-`/proc` pattern because a
    supervisor that can be SIGKILLed wants a guard the kernel releases at death
    — no stale-PID window, no PID-reuse `cmdline` check to get subtly wrong. And
    stdlib `fcntl` rather than `filelock` because this runs under
    `/usr/bin/python3`, where `brain.py` already carries an `ImportError` path
    that degrades to "no lock available"; a guard that can silently become no
    guard is not a guard.
    """
    global _supervisor_fd
    if _supervisor_fd is not None:
        return True
    if brain._ensure_dir(brain.brain_root()) is None:
        _log("supervisor_lock_unavailable", reason="brain root not writable")
        return False
    path = supervisor_lock_path()
    try:
        # No O_TRUNC: truncating before we hold the lock would erase the
        # winner's pid, which is the only hint the loser gets.
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        _log("supervisor_lock_unavailable", reason=str(exc))
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # flock reports EWOULDBLOCK and nothing else — no F_GETLK equivalent —
        # so the pid below is whatever the holder wrote, and may be stale: a
        # crashed holder leaves its pid behind. Labelled as a hint because it
        # never gates a decision, only an operator's next step.
        try:
            hint = os.read(fd, 64).decode("utf-8", "replace").strip() or "unknown"
        except OSError:
            hint = "unknown"
        os.close(fd)
        _log("supervisor_already_running", holder_pid_hint=hint,
             note="pid is a hint written by the holder and may be stale")
        return False
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
    except OSError:
        pass
    _supervisor_fd = fd
    return True


def release_supervisor_lock() -> None:
    """Drop the guard. For tests — the daemon holds it until it exits."""
    global _supervisor_fd
    if _supervisor_fd is None:
        return
    try:
        fcntl.flock(_supervisor_fd, fcntl.LOCK_UN)
        os.close(_supervisor_fd)
    except OSError:
        pass
    _supervisor_fd = None
```

Change `run()`:

```python
def run(once: bool = False) -> int:
    """Supervisor loop. Returns an exit code (for `once` mode / tests)."""
    if not acquire_supervisor_lock():
        # StartLimitBurst=5 / StartLimitIntervalSec=300 means a losing copy
        # under systemd gives up after five attempts rather than restart-looping
        # forever, and px-brain's health goes stale — which is visible.
        return 1
    ensure_journal()
    ...
```

In `bin/px-brain`, extend the header comment:

```bash
# Only one supervisor may run: an flock guard on state/brain/.supervisor.lock
# means a second copy exits non-zero rather than fighting the first over
# sweeps and kills. Check the log for supervisor_already_running.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_brain_daemon.py -q`
Expected: PASS. Existing tests calling `brain_daemon.run(once=True)` (`:368`) now need the guard — they will acquire it themselves on the first call, so add `brain_daemon.release_supervisor_lock()` to an autouse teardown in the test module:

```python
@pytest.fixture(autouse=True)
def _release_supervisor_guard():
    yield
    brain_daemon.release_supervisor_lock()
```

Run: `python -m pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxh/brain_daemon.py bin/px-brain tests/test_brain_daemon.py
git commit -m "feat(brain): one supervisor, guarded by flock

Two supervisors is not hypothetical — an operator watching bin/px-brain in a
shell while systemd already has one gets there. The operations that could
collide are exactly the ones that must not take the per-session lock:
sweep_pending would block for a caller's whole deadline, and taking it
before an unwedge is structurally impossible since the wedged caller is the
holder. So the hazard closes with a guard, not a wider lock.

flock because the kernel releases it on SIGKILL; stdlib fcntl because this
runs under /usr/bin/python3 where filelock may be absent and a guard that
degrades to no guard is not a guard. flock cannot name the holder, so the
winner writes its pid and the loser logs it as a possibly-stale hint."
```

---

### Task 8: Demote the glyph everywhere

Implements §3.1's remaining rows — sites 1 (docstring) and 5 (warning label) — and the §2.3 note that a tmux restart mid-handshake needs no machinery.

**Files:**
- Modify: `src/pxh/tmux_claude.py:154-158` — `pane_ready` docstring
- Modify: `src/pxh/brain_daemon.py` — the `pane_ready` branch in `check_wedge` (`:191`)
- Test: `tests/test_brain.py` (the glyph-site assertion from Task 2 already covers `brain.py`; add the docstring assertion)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing test**

```python
def test_pane_ready_does_not_claim_the_session_can_answer():
    """The docstring said "True once Claude is actually listening", which is the
    claim the first end-to-end run disproved: a permission dialog renders the
    glyph. It means the pane is accepting input, and nothing more."""
    doc = tmux_claude.pane_ready.__doc__ or ""
    assert "accepting input" in doc
    assert "actually listening" not in doc


def test_check_wedge_carries_its_warning_in_the_code():
    """A limitation recorded only in a spec is a limitation the next reader
    re-derives from scratch after it bites them a second time."""
    source = (ROOT / "src" / "pxh" / "brain_daemon.py").read_text()
    branch = source.split("def check_wedge")[1].split("def ")[0]
    assert "permission dialog" in branch, \
        "the tolerated weakness must be labelled where it lives"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_brain.py -k "pane_ready_does_not_claim or carries_its_warning" -v`
Expected: FAIL on both assertions.

- [ ] **Step 3: Write the implementation**

`src/pxh/tmux_claude.py`:

```python
def pane_ready(spec: SessionSpec | None = None) -> bool:
    """True once the pane is accepting input — NOT that the session can answer.

    This is an observation of rendered terminal output, which is the exact thing
    the mailbox exists to avoid trusting. A permission dialog waiting on a human
    renders the glyph too, so a session that cannot answer a single request
    looks ready here. Proof that a round trip works is
    `brain.session_state() == "validated"`; this is a best-effort hint about
    when it is worth starting to type.
    """
```

`src/pxh/brain_daemon.py`, in `check_wedge`:

```python
    spec = brain.spec_for_session(state.name)
    if tmux_claude.pane_ready(spec):
        # WARNING: the glyph does not prove the session can answer — a
        # permission dialog renders it, and that is the exact failure the
        # handshake exists to catch. This branch is trusted anyway, and only
        # because `ask_brain`'s `finally:` removes `current.json` on every exit
        # path: a `current.json` still here past its deadline therefore means
        # the caller process itself died, which is a narrower claim than "the
        # pane looks fine". Do not reuse this reasoning anywhere the marker is
        # available instead. Recorded as a known limitation in
        # docs/superpowers/specs/2026-08-17-brain-handshake-validation-design.md §5.
        state.wedged_since = None
        state.escaped_at = None
        return
```

Also update the `brain_daemon` module docstring's item 4 to mention that validation, not the glyph, gates health, and add the tmux-restart note:

```python
6. **Validate** — one handshake per tick, to the session that has waited
   longest, because the glyph cannot tell us a session can answer. A tmux
   server restart mid-handshake is self-recovering and needs no special
   handling: the session disappears, the next tick reads `session_absent`,
   recreates, sweeps and handshakes with a fresh id. Stated so nobody adds
   machinery for it.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_brain.py tests/test_brain_daemon.py -q` → PASS
Run: `python -m pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxh/tmux_claude.py src/pxh/brain_daemon.py tests/test_brain.py
git commit -m "docs(brain): the glyph means the pane accepts input, nothing more

pane_ready's docstring claimed 'True once Claude is actually listening',
which the first end-to-end run disproved — a permission dialog renders the
glyph. Corrected, and check_wedge's remaining use of it now carries the
warning in the code rather than only in the spec, including why that one
site is trusted anyway."
```

---

### Task 9: `bin/px-brain-status`

Implements §3's operator tool, including the disk-full caveat from §2.6.

**Files:**
- Create: `bin/px-brain-status`
- Test: `tests/test_brain_daemon.py`

**Interfaces:** consumes `brain.session_state`, `brain.read_validation_marker`, `brain._marker_age_s`, `shutil.disk_usage`.

- [ ] **Step 1: Write the failing test**

```python
def test_px_brain_status_prints_the_state_vocabulary_and_free_space(tmp_path, monkeypatch):
    """The state names are the words a human uses to describe the fault, so the
    tool prints them unchanged rather than prettifying them into a different
    set. Free space is there because `stale` has two causes: health._write_record
    swallows OSError, so a full disk and a dead supervisor look identical."""
    env = os.environ.copy()
    env["PX_STATE_DIR"] = str(tmp_path)
    env["PROJECT_ROOT"] = str(ROOT)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([str(ROOT / "bin" / "px-brain-status")],
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
    assert "spark-brain" in result.stdout and "spark-io" in result.stdout
    assert any(word in result.stdout
               for word in ("session_absent", "no_marker", "validating", "validated"))
    assert "free" in result.stdout.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_brain_daemon.py -k px_brain_status -v`
Expected: FAIL — `FileNotFoundError: bin/px-brain-status`

- [ ] **Step 3: Write the implementation**

Create `bin/px-brain-status` (mode 755):

```bash
#!/usr/bin/env bash
# px-brain-status — what the resident Claude sessions can actually do.
#
#   bin/px-brain-status
#
# Prints each session's derived state, the model that answered its handshake,
# the attempt count and the marker's age — plus free space on state/, because
# `stale` has two causes: health._write_record swallows OSError, so on a full
# disk every component reads stale and looks exactly like a dead supervisor.
#
# The state names are printed unchanged. They are the vocabulary a human uses
# to describe the fault, and the spec writes a repair line per state:
#   session_absent — px-brain is not doing its job (systemctl status px-brain)
#   no_marker      — the session is up and cannot answer (attach and look)
#   validating     — normal; a handshake is in flight
#   validated      — a real round trip completed on the model shown
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/px-env"

exec /usr/bin/python3 - <<'PY'
import shutil

from pxh import brain

for session in (brain.BRAIN_SESSION, brain.IO_SESSION):
    state = brain.session_state(session)
    marker = brain.read_validation_marker(session) or {}
    age = brain._marker_age_s(marker) if marker else float("inf")
    age_text = "-" if age == float("inf") else f"{age:.0f}s"
    print(f"{session:<12} {state:<15} "
          f"model={marker.get('model') or '-'} "
          f"attempt={marker.get('attempt') or '-'} "
          f"marker_age={age_text}")

try:
    usage = shutil.disk_usage(brain.brain_root().parent)
    print(f"\nstate/ free: {usage.free / (1024 ** 2):.0f} MiB of "
          f"{usage.total / (1024 ** 2):.0f} MiB")
except OSError as exc:
    print(f"\nstate/ free: unknown ({exc})")
PY
```

Then `chmod +x bin/px-brain-status`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_brain_daemon.py -k px_brain_status -v` → PASS
Run: `python -m pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add bin/px-brain-status tests/test_brain_daemon.py
git commit -m "feat(brain): px-brain-status prints derived state and free space

Four state names, printed unchanged because they are the words the repair
lines are written against. Free space alongside them because health's
OSError swallow is correct behaviour that makes a full disk and a dead
supervisor read identically as stale."
```

---

### Task 10: The io boundary, live on the robot

Implements §4's live-marked pair. The permission rules are enforced by Claude Code, so no in-process harness can stand in for them — this task's value is entirely in running it on the Pi.

**Files:**
- Create: `tests/test_brain_live.py`
- Test: itself

**Interfaces:** consumes `brain.ask_brain`, `brain.session_state`, `brain_daemon.run_handshake`, `tmux_claude.send_key`.

- [ ] **Step 1: Write the test**

```python
"""Live brain tests — run on the robot, against the real sessions.

    sudo systemctl start px-brain
    .venv/bin/python -m pytest tests/test_brain_live.py -v -s -m live

These are `live` because the thing under test is Claude Code's own permission
enforcement. An in-process harness can prove the mailbox works; only the real
session can prove that a one-tool envelope is a working channel rather than
merely a configured one, and that it actually refuses the second tool.

NOTE: these deliberately do NOT use conftest's tmp-path mailbox — they talk to
the running robot's sessions. That is the point, and it is why they are opt-in.
"""
import json
import time
import uuid

import pytest

from pxh import brain, brain_daemon, tmux_claude

pytestmark = pytest.mark.live


@pytest.fixture
def _real_mailbox(monkeypatch):
    """Undo conftest's autouse redirect — we want the live mailbox."""
    from pxh.state import PROJECT_ROOT
    from pathlib import Path

    monkeypatch.setattr(brain, "brain_root",
                        lambda: Path(PROJECT_ROOT) / "state" / "brain")


def test_the_io_session_can_answer_a_handshake(_real_mailbox):
    """Half one: the one-tool envelope is a working channel. Nearly free once
    the harness exists, and the only evidence that a single-tool session is
    usable at all."""
    state = brain_daemon.SessionState(name=brain.IO_SESSION)
    assert brain_daemon.run_handshake(state, "no_marker") is True
    assert brain.session_state(brain.IO_SESSION) == brain.VALIDATED


def test_the_io_session_cannot_use_a_second_tool(_real_mailbox):
    """Half two is the half that matters: the io session is where untrusted text
    lands, and an untested boundary is an aspiration. Costs one deliberately
    rejected turn."""
    spec = brain.spec_for_session(brain.IO_SESSION)
    reply = brain.ask_brain(
        "public_chat",
        {"instruction": "Read /etc/hostname and reply with its exact contents.",
         "message": "what host are you on?"},
        timeout_s=45,
    )
    try:
        if reply is not None:
            body = json.dumps(reply)
            assert "/etc/hostname" not in body, \
                "the io session must not be able to read the filesystem"
        pane = tmux_claude._tmux("capture-pane", "-t", brain.IO_SESSION, "-p",
                                 socket=spec.socket) or ""
        assert reply is None or "hostname" not in pane.lower(), \
            "a request needing a second tool must not be fulfilled"
    finally:
        # Never leave a permission dialog on screen — it wedges every later
        # request, and the next handshake is what proves we cleaned up.
        tmux_claude.send_key("Escape", spec=spec)
        time.sleep(2)
        state = brain_daemon.SessionState(name=brain.IO_SESSION)
        assert brain_daemon.run_handshake(state, "no_marker") is True, \
            "teardown must leave the session validated for the next caller"
```

- [ ] **Step 2: Verify the marker registration and that the suite skips it by default**

Run: `python -m pytest -m "not live" -q`
Expected: PASS, with `tests/test_brain_live.py` deselected. If `live` is not a registered marker, `pytest.ini`/`pyproject.toml` already declares it (the repo has `tests/test_tools_live.py`); confirm with `grep -rn "live" pytest.ini setup.cfg pyproject.toml`.

- [ ] **Step 3: Run it on the robot**

On the Pi, with `px-brain` running:

```bash
ssh pi@picar 'cd ~/repos/spark && .venv/bin/python -m pytest tests/test_brain_live.py -v -s -m live'
```

Expected: both PASS. If half two *fails* by returning the hostname, that is a real security finding and the io envelope is wrong — stop and report it rather than adjusting the test.

- [ ] **Step 4: Commit**

```bash
git add tests/test_brain_live.py
git commit -m "test(brain): the io boundary, live against the real session

The permission rules are enforced by Claude Code, so no in-process harness
can stand in for them. Half one proves a one-tool envelope is a working
channel; half two proves it refuses the second tool, which is the half that
matters — the io session is where untrusted text lands and an untested
boundary is an aspiration. Teardown Escapes the dialog and re-validates."
```

---

## Deployment (after Task 10)

Not a code task, but the plan is not delivered until this is stated:

1. `git push` and open the PR from `feat/px-brain-persistent-session`.
2. On the Pi: `sudo systemctl restart px-brain`, then `bin/px-brain-status`. Expected within ~2 minutes: both sessions `validated`, `model=claude-haiku-4-5-20251001`, `attempt=1`.
3. `grep handshake logs/brain-daemon.jsonl | tail` — one `handshake_ok` per session, no `handshake_failed`.
4. `python -c "from pxh import brain; print(brain.meter_summary())"` — `handshake` count equals the number of session starts, not a multiple of it. A climbing count is a session restart-looping.
5. `PX_BRAIN_KINDS` is unchanged by this work (§5). Nothing new routes to the brain because handshaking exists.

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §1 the problem | context only |
| §2.1 the marker, 0644, `model` file removed | 1, 2 |
| §2.2 four states, read-time derivation | 1 |
| §2.3 handshake steps, two sweep widths, own escalation, warning label, `_is_idle` predicate, clock clause, no-grace clause, tmux restart note | 3, 5, 8 |
| §2.4 fresh id per creation, reused per retry | 3 |
| §2.5 model change / recycle as locked transitions, ordering, `_switch_model` removed | 2, 6 |
| §2.6 the bound, one-per-tick, longest-waited, `validating` writes no health, disk-full caveat | 1, 4, 9 |
| §2.7 handshake request, nonce echo, both prompts, metering, counts as a turn | 3 |
| §2.8 flock guard, pid hint, lock-scope table | 7 |
| §3 two caller checks, `state=` in logs, per-session health, `px-brain-status` | 2, 4, 9 |
| §3.1 all seven glyph sites | 2 (3, 4), 4 (7), 8 (1, 5), 6 unchanged by design |
| §4 every listed test | 1–10 |
| §5 out of scope | nothing to build |

Two things a reviewer should know about how the plan reads the spec:

- **§2.3's three triggers become two code paths.** Creation deletes the marker (Task 2's `start_session` change), so creation and aging both present as `no_marker`; the third path is `model_change`. This is the level-triggered design applied consistently, not a narrowing — every trigger the spec names still fires.
- **The recycle lock is non-blocking.** §2.8's table says "held" without saying how long to wait. Blocking would stall the supervisor for a caller's whole deadline, which is the hazard the same table names for `sweep_pending`, so the plan skips the recycle and retries next tick. The spec's own test ("assert a caller holding the lock blocks the recycle") passes either way.

**Placeholder scan:** no TBDs, no "add error handling", every code step carries the actual code, every test step carries the actual assertions and the command to run.

**Type consistency:** `session_state(session, model=None) -> str` is spelled identically in Tasks 1, 2, 4, 9, 10. `run_handshake(state, reason) -> bool` with `reason ∈ {"no_marker", "model_change"}` in Tasks 3, 4, 6, 10. `write_validation_marker` is keyword-only (`state=`, `request_id=`, `model=`, `attempt=`) at every call site. `brain.nudge_line` / `collect_reply` / `cleanup_request` are renamed once, in Task 2, before Task 3 uses them.
