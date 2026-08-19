"""Claude session manager — model routing, rate limiting, execution, logging.

Central dispatcher for all SPARK-initiated Claude Code interactions.
Used by px-evolve, tool-research, tool-compose, and mind.py self-debug.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from filelock import FileLock
except ImportError:
    FileLock = None


HOBART_TZ = ZoneInfo("Australia/Hobart")
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STATE_DIR = Path(os.environ.get("PX_STATE_DIR", PROJECT_ROOT / "state"))
SESSION_LOG = STATE_DIR / "claude_sessions.jsonl"
SESSION_LOCK = str(SESSION_LOG) + ".lock"
LOCK_TIMEOUT_S = 10

# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------

_DEFAULT_MODELS: dict[str, str] = {
    "evolve": "claude-opus-4-6",
    "self_debug": "claude-sonnet-4-6",
    "research": "claude-haiku-4-5-20251001",
    "compose": "claude-haiku-4-5-20251001",
    "conversation": "claude-sonnet-4-6",
    "blog": "claude-haiku-4-5-20251001",
    "consolidate": "claude-haiku-4-5-20251001",
}

_ENV_OVERRIDES: dict[str, str] = {
    "evolve": "PX_CLAUDE_MODEL_EVOLVE",
    "self_debug": "PX_CLAUDE_MODEL_DEBUG",
    "research": "PX_CLAUDE_MODEL_RESEARCH",
    "compose": "PX_CLAUDE_MODEL_COMPOSE",
    "conversation": "PX_CLAUDE_MODEL_CONVERSATION",
    "blog": "PX_CLAUDE_MODEL_BLOG",
    "consolidate": "PX_CLAUDE_MODEL_CONSOLIDATE",
}


def _model_for_type(session_type: str) -> str:
    """Return the Claude model ID for a given session type."""
    if session_type not in _DEFAULT_MODELS:
        raise ValueError(f"Unknown session type: {session_type!r}")
    env_var = _ENV_OVERRIDES[session_type]
    return os.environ.get(env_var, _DEFAULT_MODELS[session_type])


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

DAILY_CAP = int(os.environ.get("PX_CLAUDE_DAILY_CAP", "8"))
COOLDOWN_S = int(os.environ.get("PX_CLAUDE_COOLDOWN_S", "1800"))  # 30 min
BUDGET_DISABLED = os.environ.get("PX_CLAUDE_BUDGET_DISABLED", "0") != "0"

_TYPE_COOLDOWNS: dict[str, int] = {
    "evolve": 86400,       # 24 hours
    "self_debug": 21600,   # 6 hours
    "research": 7200,      # 2 hours
    "compose": 14400,      # 4 hours
    "conversation": 900,   # 15 min
    "blog": 1800,          # 30 min
    "consolidate": 72000,  # 20 hours
}

_TYPE_QUOTAS: dict[str, int] = {
    "evolve": 1,
    "self_debug": 2,
    "research": 3,
    "compose": 2,
    "conversation": 4,
    "blog": 5,
    "consolidate": 1,
}

# Higher number = higher priority.  Used for budget-tight gating.
_PRIORITY: dict[str, int] = {
    "self_debug": 5,
    "evolve": 4,
    "conversation": 3,
    "research": 2,
    "compose": 1,
    "blog": 2,
    "consolidate": 2,
}

_GLOBAL_COOLDOWN_EXEMPT = {"self_debug", "blog"}


def _load_session_log() -> list[dict]:
    """Read session log, skipping malformed lines."""
    if not SESSION_LOG.exists():
        return []
    entries = []
    for line in SESSION_LOG.read_text(encoding="utf-8").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _today_entries(entries: list[dict]) -> list[dict]:
    """Filter entries to those from today (Hobart timezone)."""
    now_hobart = dt.datetime.now(HOBART_TZ)
    today_start = now_hobart.replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    for e in entries:
        ts_str = e.get("ts", "")
        try:
            ts = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.astimezone(HOBART_TZ) >= today_start:
                result.append(e)
        except (ValueError, TypeError):
            continue
    return result


def check_budget(session_type: str) -> str | None:
    """Check if a session is allowed.  Returns None if OK, reason string if blocked."""
    if BUDGET_DISABLED:
        return None

    if session_type not in _DEFAULT_MODELS:
        return f"unknown session type: {session_type}"

    entries = _load_session_log()
    today = _today_entries(entries)

    # Daily cap
    if len(today) >= DAILY_CAP:
        return f"daily cap reached ({len(today)}/{DAILY_CAP})"

    # Priority gating: low-priority blocked when <=2 sessions remain
    remaining = DAILY_CAP - len(today)
    if remaining <= 2:
        priority = _PRIORITY.get(session_type, 0)
        # Only allow priority >= 4 (self_debug, evolve) when budget is tight
        if priority < 4:
            return f"budget tight ({remaining} remaining), {session_type} priority too low"

    # Per-type daily quota
    type_today = [e for e in today if e.get("type") == session_type]
    quota = _TYPE_QUOTAS.get(session_type, 1)
    if len(type_today) >= quota:
        return f"{session_type} quota reached ({len(type_today)}/{quota})"

    # Global cooldown (except self_debug)
    if session_type not in _GLOBAL_COOLDOWN_EXEMPT and entries:
        latest_ts = None
        for e in reversed(entries):
            ts_str = e.get("ts", "")
            try:
                latest_ts = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                break
            except (ValueError, TypeError):
                continue
        if latest_ts:
            elapsed = (dt.datetime.now(dt.timezone.utc) - latest_ts).total_seconds()
            if elapsed < COOLDOWN_S:
                return f"global cooldown ({int(elapsed)}s / {COOLDOWN_S}s)"

    # Per-type cooldown
    type_cooldown = _TYPE_COOLDOWNS.get(session_type, COOLDOWN_S)
    type_entries = [e for e in entries if e.get("type") == session_type]
    if type_entries:
        latest_ts = None
        for e in reversed(type_entries):
            ts_str = e.get("ts", "")
            try:
                latest_ts = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                break
            except (ValueError, TypeError):
                continue
        if latest_ts:
            elapsed = (dt.datetime.now(dt.timezone.utc) - latest_ts).total_seconds()
            if elapsed < type_cooldown:
                return f"{session_type} cooldown ({int(elapsed)}s / {type_cooldown}s)"

    return None


# ---------------------------------------------------------------------------
# Session execution
# ---------------------------------------------------------------------------

class SessionBudgetExhausted(Exception):
    """Raised when a session is blocked by rate limiting."""
    pass


@dataclass
class RunResult:
    stdout: str
    stderr: str
    returncode: int
    duration_s: float
    model_used: str


def _log_session(
    session_type: str,
    model: str,
    duration_s: float,
    returncode: int,
    outcome: str,
) -> str:
    """Log a session to the session log.  Returns session_id."""
    now = dt.datetime.now(dt.timezone.utc)
    session_id = f"sess-{now.strftime('%Y%m%d-%H%M%S')}-{int(now.microsecond / 1000):03d}"
    entry = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": session_type,
        "model": model,
        "duration_s": round(duration_s, 1),
        "returncode": returncode,
        "outcome": outcome,
        "session_id": session_id,
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _append_session_entry(entry)

    return session_id


def _append_session_entry(entry: dict) -> None:
    """Append a single entry to the session log under lock."""
    SESSION_LOG.parent.mkdir(parents=True, exist_ok=True)
    lock_path = str(SESSION_LOG) + ".lock"
    if FileLock is not None:
        with FileLock(lock_path, timeout=LOCK_TIMEOUT_S):
            with SESSION_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
    else:
        with SESSION_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def budget_summary() -> str:
    """One-line summary of today's session usage, for injection into the
    px-mind reflection context so SPARK can reason about its own budget."""
    if BUDGET_DISABLED:
        return ""
    today = _today_entries(_load_session_log())
    by_type: dict[str, int] = {}
    for e in today:
        t = e.get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1
    per_type = ", ".join(
        f"{t} {by_type.get(t, 0)}/{q}" for t, q in _TYPE_QUOTAS.items())
    summary = f"{len(today)}/{DAILY_CAP} sessions used ({per_type})."
    blocked = []
    for t in _TYPE_QUOTAS:
        reason = check_budget(t)
        if reason:
            blocked.append(f"{t} ({reason})")
    if blocked:
        summary += " Currently blocked: " + "; ".join(blocked) + "."
    return summary


# ---------------------------------------------------------------------------
# Routing: persistent session vs. one-shot subprocess
# ---------------------------------------------------------------------------

# Which session types are served by the resident Claude session in tmux
# (src/pxh/brain.py) rather than by a `claude -p` subprocess. This is a rollout
# dial, not a preference: `claude -p` is on its way out of the codebase
# entirely, and each type moves across once its behaviour through the brain has
# been watched in production. Phase 1 is research and compose — low stakes,
# easy to verify, and neither needs tools beyond the brain's own envelope.
#
# Types NOT listed here still take the old path. `evolve` in particular cannot
# move until the brain can work inside a git worktree, since a resident
# session's tool envelope is fixed at launch and cannot be widened per call.
#
# `reflection` is here despite not routing through run_claude_session at all —
# its call site is mind.call_llm's tier 2, which shells out directly. It reads
# this same dial (via brain_kinds()) so there is one switch for the rollout
# rather than two that can disagree.
_DEFAULT_BRAIN_KINDS = "research,compose,post_qa,reflection"


def brain_kinds() -> frozenset[str]:
    """Read at call time so the rollout can be widened or rolled back live."""
    raw = os.environ.get("PX_BRAIN_KINDS", _DEFAULT_BRAIN_KINDS)
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


# Kept as the old private spelling for in-tree callers and tests.
_brain_kinds = brain_kinds


def _run_via_brain(
    session_type: str,
    prompt: str,
    timeout: int,
    model: str,
) -> RunResult:
    """Serve a session from the resident Claude session instead of a subprocess.

    The RunResult contract is unchanged, so callers keep their existing
    error handling: a brain that is down, wedged or busy comes back as a
    non-zero returncode, which is exactly what they already do on a failed
    `claude -p`.

    `allowed_tools`, `skip_permissions` and `cwd` have no meaning here — a
    resident session's envelope is fixed when it launches and cannot be
    widened for one request. That is a security property, not a limitation to
    work around.
    """
    from . import brain  # local import keeps the tmux dependency off the hot path

    start = time.monotonic()
    reply = brain.ask_brain(session_type, {"prompt": prompt},
                            timeout_s=timeout, model=model)
    duration = time.monotonic() - start

    if reply is None:
        _log_session(session_type, model, duration, 1, "brain_unavailable")
        return RunResult(
            stdout="",
            stderr="brain unavailable — the resident Claude session did not answer",
            returncode=1,
            duration_s=duration,
            model_used=model,
        )

    answer = reply.get("reply")
    if not isinstance(answer, str):
        # The session replied with structured JSON. Callers of this function
        # all read stdout as text, so hand it back the way a subprocess would.
        answer = json.dumps(answer)

    _log_session(session_type, model, duration, 0, "success")
    return RunResult(
        stdout=answer,
        stderr="",
        returncode=0,
        duration_s=duration,
        model_used=model,
    )


def run_claude_session(
    session_type: str,
    prompt: str,
    timeout: int = 300,
    allowed_tools: str = "",
    skip_permissions: bool = False,
    cwd: str | Path | None = None,
    model_override: str | None = None,
    skip_budget_check: bool = False,
) -> RunResult:
    """Run a Claude session with budget checking, model routing, and logging.

    model_override: use this model instead of the session-type default.
    skip_budget_check: skip rate-limit check (use for sub-phases of an already-checked session).
    Raises SessionBudgetExhausted if rate-limited (unless skip_budget_check=True).
    """
    if not skip_budget_check:
        reason = check_budget(session_type)
        if reason:
            raise SessionBudgetExhausted(reason)

    model = model_override or _model_for_type(session_type)
    work_dir = str(cwd) if cwd else str(PROJECT_ROOT)

    if session_type in _brain_kinds():
        return _run_via_brain(session_type, prompt, timeout, model)

    # Build command
    cmd = [
        os.environ.get("PX_CLAUDE_BIN", "claude"), "-p", prompt,
        "--model", model,
        "--no-session-persistence",
        "--output-format", "text",
    ]
    if allowed_tools is not None:
        cmd.extend(["--allowedTools", allowed_tools])
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")

    # Strip Claude Code env vars for clean nested invocation
    run_env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("CLAUDE_CODE")
        and k not in ("CLAUDECODE", "DISABLE_CLAUDE_CODE_PROTECTIONS")
    }

    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd, cwd=work_dir, capture_output=True, text=True,
            timeout=timeout, env=run_env,
        )
        duration = time.monotonic() - start
        outcome = "success" if result.returncode == 0 else f"exit:{result.returncode}"
        _log_session(session_type, model, duration, result.returncode, outcome)
        return RunResult(
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            duration_s=duration,
            model_used=model,
        )
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        _log_session(session_type, model, duration, -1, "timeout")
        raise


# ---------------------------------------------------------------------------
# File whitelist enforcement (used by px-evolve)
# ---------------------------------------------------------------------------

WHITELIST_PATTERNS = [
    "src/pxh/spark_config.py",
    "src/pxh/mind.py",
    "src/pxh/voice_loop.py",
    "bin/tool-",
    "tests/",
    "docs/prompts/",
]

BLACKLIST_FILES = {
    "src/pxh/api.py",
    "bin/tool-chat",
    "bin/tool-chat-vixen",
    "bin/px-evolve",
    ".env",
    # Constitutional layer (#174). policy.py holds the behavioural invariants
    # SPARK must obey regardless of prompt or persona; the invariants file
    # pins both those rules AND that each dispatcher actually calls them.
    # Both are blacklisted explicitly rather than relying on policy.py simply
    # not matching a whitelist pattern today — a future broader pattern (e.g.
    # "src/pxh/") must not silently unprotect them.
    "src/pxh/policy.py",
    "tests/test_policy_invariants.py",
    # The wake grant is the one input that can *unblock* audio, so the module
    # that mints and validates it, and the module both chokepoints load it
    # through, are protected on the same footing as the rules themselves.
    # Neither is in WHITELIST_PATTERNS today; they are named here anyway for
    # the reason given above — a future broader pattern must not silently
    # hand SPARK the ability to write himself a permanent 3am grant.
    "src/pxh/wake_grant.py",
    "src/pxh/policy_context.py",
}

BLACKLIST_PATTERNS = [
    "docs/prompts/persona-",
    "systemd/",
]


def file_in_whitelist(path: str) -> bool:
    """Check if a file path is in the evolution whitelist."""
    if path in BLACKLIST_FILES:
        return False
    if any(path.startswith(p) for p in BLACKLIST_PATTERNS):
        return False
    return any(path.startswith(p) or path == p for p in WHITELIST_PATTERNS)
