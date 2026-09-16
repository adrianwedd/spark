"""Session dispatcher — provider routing, rate limiting, execution, logging.

Central entry point for every SPARK-initiated model call that is not ordinary
reflection. The name is historical: all of it now runs on the cognition tier —
one direct Ollama Cloud API call (`pxh.m5`) — and none of it reaches a CLI.
The `claude_*` names are kept for provenance in this change and are the next
thing to go; see the retirement note in `docs/` and #317 Phase 3.
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
    # 40 min (#291). It was 20 hours, which meant the *first* attempt of a
    # night consumed the only slot the second one could ever have used:
    # `memory.MAX_ATTEMPTS_PER_DAY` promised two tries between 03:00 and 06:00
    # and this made the second structurally unreachable. 40 min also clears the
    # 30-min global cooldown, so attempt 2 is spaced past it rather than
    # exempted from it.
    #
    # Deliberately *shorter* than `memory.RETRY_SPACING_S` (55 min), which is
    # no longer "matching" it (#310). These measure different intervals: this
    # one starts when the previous attempt finished, that one when it started.
    # Setting them equal is the defect that cost nine nights — an attempt that
    # spent its whole 600s deadline arrived at the retry with 1800s elapsed
    # here and 2400s elapsed there. The retry is spaced wide enough to cover
    # both; this stays at the contention bound it was chosen for.
    "consolidate": 2400,
}

_TYPE_QUOTAS: dict[str, int] = {
    "evolve": 1,
    "self_debug": 2,
    "research": 3,
    "compose": 2,
    "conversation": 4,
    "blog": 5,
    # Two, to match memory.MAX_ATTEMPTS_PER_DAY (#291). A quota of 1 made the
    # retry that module offers impossible to spend.
    "consolidate": 2,
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


def _answered(entry: dict) -> bool:
    """True if this entry records a call the model actually answered.

    `_log_session` writes two shapes: rc=0/`success` once the tier answered,
    and rc=1 with a failure outcome (`cognition_timeout`, `cognition_offline`,
    `cognition_bad_response`, `cognition_busy`) when it never did. Only the
    first is spend.

    Reading the second as if it were spend is #310's third defect. On
    2026-09-15 02:32:10 `research` logged rc=1 while the model was unreachable
    for every caller, and that one entry refused `consolidate` its own 02:41
    retry with "global cooldown (554s / 1800s)" — a component that could not
    have contended for the Pi locking out every other component for half an
    hour.
    It compounds, too: on an outage night every failing component writes one of
    these, and each re-arms the cooldown for the next, so the components
    serialise each other through a gate none of them can satisfy.

    A missing `returncode` counts as answered. Entries predate the field only
    in theory, and the safe direction on an unknown shape is to keep the
    cooldown the old code applied rather than to widen a gate on a parse
    failure.
    """
    return entry.get("returncode", 0) == 0


def _latest_ts(entries: list[dict]) -> dt.datetime | None:
    """The newest parsable `ts` in `entries` (oldest-first), or None."""
    for entry in reversed(entries):
        ts_str = entry.get("ts", "")
        try:
            return dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
    return None


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

    # Global cooldown (except self_debug). Only an *answered* session arms it;
    # see `_answered` for why an rc=1 entry must not (#310's third defect).
    if session_type not in _GLOBAL_COOLDOWN_EXEMPT:
        latest_ts = _latest_ts([e for e in entries if _answered(e)])
        if latest_ts:
            elapsed = (dt.datetime.now(dt.timezone.utc) - latest_ts).total_seconds()
            if elapsed < COOLDOWN_S:
                return f"global cooldown ({int(elapsed)}s / {COOLDOWN_S}s)"

    # Per-type cooldown. Deliberately *not* filtered by `_answered`: this one
    # bounds how often a kind may reach for the model at all, and a kind that
    # fails on its own schedule should not get to retry sooner because the
    # failure spent nothing. `memory.RETRY_SPACING_S` is raised to clear it
    # rather than this being weakened to accommodate the retry (#310).
    type_cooldown = _TYPE_COOLDOWNS.get(session_type, COOLDOWN_S)
    type_entries = [e for e in entries if e.get("type") == session_type]
    latest_ts = _latest_ts(type_entries)
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



class CognitionTierToolsForbidden(ValueError):
    """A cognition-tier kind was asked for tools it cannot have (#317).

    The tier is one direct API call: there is no tool envelope to widen and no
    session to widen it on. The tempting failure mode is to *ignore* the
    request and answer without the tools — a silently degraded answer that
    looks like a working one. Refusing is the honest outcome, and it is
    cheap to fix: collect what the model needs in Python and pass it in the
    prompt, which is what `self_debug` does.
    """


class ColdStartForbidden(RuntimeError):
    """Raised when a session type has no backend at all.

    This is what is left of the old "fail loudly rather than quietly spawn a
    fresh Claude" rule, and it is now the only rule: there is no second
    provider to fall back to, so an unclassified kind has nothing to fall back
    *from*. Every cold start used to cost more than the session it bypassed
    and billed metered API usage rather than Max; the tier is a single call
    with a declared budget, so the honest failure is a refusal, not a retry
    against something else.
    """


@dataclass
class RunResult:
    stdout: str
    stderr: str
    returncode: int
    duration_s: float
    model_used: str
    # What actually served the call (#317). Callers that report "model_used"
    # to an operator should be able to say which provider it came from —
    # before this, every caller here was implicitly Claude.
    provider: str = ""


def _log_session(
    session_type: str,
    model: str,
    duration_s: float,
    returncode: int,
    outcome: str,
    *,
    provider: str = "",
    tokens: dict | None = None,
) -> str:
    """Log a session to the session log.  Returns session_id.

    `provider` is recorded rather than inferred (#317): the entry names the
    tier that actually served the call, so a reader never has to know which
    code path ran to interpret `model`. `tokens` carries Ollama's counters
    when the provider supplied them; the resident path has none to give.
    """
    now = dt.datetime.now(dt.timezone.utc)
    session_id = f"sess-{now.strftime('%Y%m%d-%H%M%S')}-{int(now.microsecond / 1000):03d}"
    entry = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": session_type,
        "model": model,
        "provider": provider,
        "duration_s": round(duration_s, 1),
        "returncode": returncode,
        "outcome": outcome,
        "session_id": session_id,
    }
    if tokens:
        entry["tokens"] = tokens

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
# Provider routing (#317)
# ---------------------------------------------------------------------------
# Every classified kind is served by the cognition tier: one direct HTTP call.
# None of them needs tools (`allowed_tools=""`), and every caller's prompt is
# self-contained — each opens with "You are SPARK ..." and states its own
# output format — so nothing depended on a resident session's system prompt or
# on a tool envelope that only a CLI could provide.
#
# The resident Claude session is gone (#317 Phase 3), and this comment is
# deliberately longer than the code it explains, because "we deleted the
# mailbox" is a change a future reader will otherwise try to undo. What it
# cost, all of it observed on this robot: answers lost when the model copied
# yesterday's request id; a turn worked and answered in 47s and then reported
# as a ten-minute timeout because nothing carried the answer; a whole night's
# consolidation behind an unanswered "Contains subshell / Do you want to
# proceed?" dialog (#314); nine consecutive nights with no long-term memory
# (#310); and a human-only `/login` as the terminal recovery action (#311).
#
# None of it is reachable from here any more, and the reason is structural
# rather than a promise: there is no session to be logged out of, no inbox
# file addressed by a request id, and no reply command to shell-quote. The
# answer *is* the HTTP response body.
COGNITION_PROVIDER = "ollama-cloud"

_COGNITION_KINDS = frozenset({"consolidate", "research", "compose", "blog",
                               "self_debug"})

# Provider-neutral per-kind model override. The Claude-era names
# (`PX_CLAUDE_MODEL_RESEARCH` and friends) deliberately do not apply here: the
# value they hold is a Claude model id, and handing that to Ollama would be a
# different mistake than ignoring it. Set these only to override the tier's
# configured model (`PX_M5_SPARK_MODEL`) for one kind.
_COGNITION_MODEL_ENV = {
    "consolidate": "PX_MODEL_CONSOLIDATE",
    "research": "PX_MODEL_RESEARCH",
    "compose": "PX_MODEL_COMPOSE",
    "blog": "PX_MODEL_BLOG",
    "self_debug": "PX_MODEL_SELF_DEBUG",
}

# `evolve` is deliberately absent from `_COGNITION_KINDS` and therefore has no
# backend at all: it needs to work inside a git worktree, and the tier is one
# tool-free API call with no filesystem to widen. Disabled is the honest state
# for it — the alternative was a "legacy cold Claude" bucket, which is what
# this whole change exists to abolish. px-evolve raises until the tier can
# hold a worktree; that is a known, deliberate outage, not a regression.


def cognition_model(session_type: str, override: str | None = None) -> str | None:
    """The model the cognition tier should ask for, or None to use the tier's own.

    Precedence: an explicit caller override, then the provider-neutral
    per-kind name (`PX_MODEL_RESEARCH`), then `PX_M5_SPARK_MODEL` — resolved
    inside `m5`, so the tier keeps one configured default.
    """
    if override:
        return override
    var = _COGNITION_MODEL_ENV.get(session_type)
    if var:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return None


def _run_via_cognition(session_type: str, prompt: str, timeout: int | None,
                       model_override: str | None = None) -> RunResult:
    """Serve a tool-free session from the cognition tier: one direct API call.

    The answer *is* the HTTP response body, which is the whole point of the
    migration (#317, #314). There is no session to be logged out of, no inbox
    file to be addressed by request id, no reply command to be shell-quoted,
    and no permission dialog to sit in front of it — a language-model answer
    cannot be produced and then lost in transit here, because nothing carries
    it but the socket.

    Failure is reported, never escalated to another provider: `status` is
    mapped to a distinct outcome (`cognition_timeout`, `cognition_offline`,
    `cognition_bad_response`, `cognition_busy`) so an operator can tell a slow
    tier from a missing credential from a concurrent call, and the caller's
    existing `returncode != 0` path does the rest.
    """
    from . import m5  # local import keeps the HTTP client off the import path

    start = time.monotonic()
    result = m5.ask_m5(session_type, prompt, "",
                       timeout_s=timeout,
                       model=cognition_model(session_type, model_override))
    duration = time.monotonic() - start
    model = result.model or m5.configured_model() or ""
    tokens = {"prompt": result.prompt_eval_count, "eval": result.eval_count}

    if result.status == "available":
        _log_session(session_type, model, duration, 0, "success",
                     provider=COGNITION_PROVIDER, tokens=tokens)
        return RunResult(stdout=result.response, stderr="", returncode=0,
                         duration_s=duration, model_used=model,
                         provider=COGNITION_PROVIDER)

    _log_session(session_type, model, duration, 1, f"cognition_{result.status}",
                 provider=COGNITION_PROVIDER, tokens=tokens)
    return RunResult(
        stdout="",
        stderr=f"cognition tier {result.status}: {result.error}",
        returncode=1,
        duration_s=duration,
        model_used=model,
        provider=COGNITION_PROVIDER,
    )


def run_claude_session(
    session_type: str,
    prompt: str,
    timeout: int | None = None,
    allowed_tools: str = "",
    skip_permissions: bool = False,
    cwd: str | Path | None = None,
    model_override: str | None = None,
    skip_budget_check: bool = False,
) -> RunResult:
    """Run a session with budget checking, provider routing, and logging.

    timeout: seconds, or None (the default) to use the deadline the kind
    declares. Prefer None — the declared per-kind deadline is the one source
    of truth, and an ad-hoc override that is tighter silently replaces it (see
    #291).
    model_override: use this model instead of the session-type default.
    skip_budget_check: skip rate-limit check (use for sub-phases of an already-checked session).
    Raises SessionBudgetExhausted if rate-limited (unless skip_budget_check=True).

    `cwd`, `allowed_tools` and `skip_permissions` are still accepted because
    callers still pass them, and they are all meaningless on a tool-free API
    call. `allowed_tools` is *refused* rather than ignored — see below.
    """
    if not skip_budget_check:
        reason = check_budget(session_type)
        if reason:
            raise SessionBudgetExhausted(reason)

    if session_type in _COGNITION_KINDS:
        if allowed_tools or skip_permissions:
            # Not ignored, and not silently answered without them: a caller
            # that asked for tools and got a tool-less answer would have no
            # way to tell that from a working one.
            raise CognitionTierToolsForbidden(
                f"{session_type!r} runs on the cognition tier, which has no "
                f"tools (asked for {allowed_tools!r}). Collect what it needs "
                f"in Python and pass it in the prompt instead."
            )
        return _run_via_cognition(session_type, prompt, timeout, model_override)

    # Fail closed, and now there is nothing to fail *open* to. The old default
    # pointed the other way — an unrecognised kind fell through to `claude -p`
    # — which made "I forgot to classify this" and "I decided this may spawn a
    # fresh Claude" the same act. `evolve` is the one kind that lands here, on
    # purpose; see the note above `_COGNITION_KINDS`.
    raise ColdStartForbidden(
        f"session_type {session_type!r} has no backend. The kinds this "
        f"dispatcher serves are {sorted(_COGNITION_KINDS)}; a new kind needs a "
        f"provider, not a cold-started CLI."
    )


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
    # quiet_mode.py derives the enabled/expiry decision policy.py's Rule 1
    # reads as a plain bool (#209); state.py holds the one write path
    # (set_quiet_mode/clear_quiet_mode) and the read-time derivation. Neither
    # matches WHITELIST_PATTERNS today, but named here on the same footing as
    # the policy/wake_grant pair — a future broader pattern must not silently
    # hand self-evolution a way to make quiet mode fail open.
    "src/pxh/quiet_mode.py",
    "src/pxh/state.py",
    # Resident-only Claude. The scanner and the suite that calls it are
    # protected together and for the same reason the policy pair is: an
    # evolution PR that may edit either one can satisfy the invariant by
    # weakening the thing that checks it. The rule this pins — that the
    # resident sessions are the only Claude substrate — is constitutional,
    # not a preference to be re-argued by a future PR.
    "tools/check_resident_claude.py",
    "tests/test_resident_only_invariant.py",
    # Delegated-agent authority boundary (#281). Same reasoning as the
    # resident-only pair above: an evolution PR that can edit the checker
    # can satisfy it by weakening what it checks. The restricted agent
    # definition itself is also named here — .claude/agents/ doesn't match
    # any WHITELIST_PATTERNS today, but a future broader pattern must not
    # silently hand self-evolution a way to widen spark-investigator's
    # tools list back toward Bash/Write/Edit/Agent.
    "tools/check_investigator_agent.py",
    "tests/test_agent_authority_invariant.py",
    ".claude/agents/spark-investigator.md",
    # Person memory. The firewall keeping what a child said in private out of
    # reflection — and therefore out of public thoughts, the blog and Bluesky —
    # is that `mind.py` never opens `people-*.jsonl`. `mind.py` is a whitelisted
    # evolution target, so the module that decides whether that bridge exists
    # must not be one SPARK can propose editing, and neither must the test that
    # checks it. Same reasoning as the policy and resident-only pairs above.
    "src/pxh/people.py",
    "tests/test_people_invariants.py",
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
