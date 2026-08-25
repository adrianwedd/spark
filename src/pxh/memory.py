"""Consolidated long-term memory for SPARK — QA roadmap item 5.

Store: state/memories-{persona}.jsonl, one record per line:
  {"ts", "date", "text", "tags": [...], "importance": 0-1, "source": "consolidation",
   "id", "provenance": {...}}

`provenance` (see pxh.provenance) types the claim — everything consolidation
writes is `narrative`, SPARK's own prose about its own thoughts. Records
written before it existed carry no such key and read back as `unknown`.

Retrieval is deliberately deterministic and free (token/tag overlap + recency)
so the per-reflection path costs nothing; the nightly consolidation pass
(consolidate(), Task 4) is where the one daily LLM call goes. Recency ranks
matches, it never manufactures one — see retrieve_memories().
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import math
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock

from pxh import provenance
from pxh.state import atomic_write
from pxh.time import utc_timestamp

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

MEMORIES_LIMIT = 5000
RECENCY_HORIZON_DAYS = 60
RECENCY_MAX_BONUS = 0.5
TAG_WEIGHT = 2.0
LOCK_TIMEOUT_S = 10

HOBART_TZ = ZoneInfo("Australia/Hobart")
DEDUPE_SIMILARITY = 0.85
DEDUPE_WINDOW_DAYS = 14
CONSOLIDATION_WINDOW = (2, 6)     # Hobart hours [start, end)
MAX_ATTEMPTS_PER_DAY = 2
MIN_THOUGHTS = 5
MAX_MEMORIES_PER_DAY = 8

# Minimum gap between the two nightly attempts (#291).
#
# Both attempts used to land ~60s apart, which made the retry decorative twice
# over: `claude_session.COOLDOWN_S` (the 30-minute global session cooldown)
# rejected it outright, and even without that a second Haiku turn started one
# minute after the first failed is a retry of the same load conditions. 40
# minutes clears the global cooldown with ten minutes of margin and still fits
# twice inside the four-hour 02:00-06:00 window.
#
# Spacing past the cooldown is deliberate, rather than adding `consolidate` to
# `_GLOBAL_COOLDOWN_EXEMPT`. The cooldown exists because two Claude sessions
# close together contend on a 4-core Pi, and that reason applies to
# consolidation exactly as written — the attempt is 600s of resident-session
# time. An exemption would buy the retry by denying the premise.
RETRY_SPACING_S = 2400

# A consolidation worker that has held the job marker this long has overrun its
# own budget (brain._DEADLINE_S["consolidate"] is 600s, plus prompt assembly and
# the single-flight lock wait). Reported once, then left alone: the worker is a
# daemon thread blocked inside ask_brain, which has its own deadline, so the
# honest thing is to make the overrun visible rather than to pretend it can be
# cancelled.
JOB_OVERRUN_AFTER_S = 900

# How long the marker's heartbeat may lag before its owner is presumed gone.
# The owning px-mind refreshes it on every ~60s awareness tick, so five minutes
# of silence means the process that claimed it is no longer ticking — which is
# what a restart mid-consolidation looks like from the next process's side.
JOB_HEARTBEAT_STALE_S = 300

_STOPWORDS = frozenset(
    """a about after again all am an and any are as at be because been before but by can
    did do does for from had has have he her his how i if in into is it its just me more
    most my no not now of on once only or other our out over own re s so some such t than
    that the their them then there these they this those through to too under until up
    very was we were what when where which while who why will with you your""".split())


def _state_dir() -> Path:
    return Path(os.environ.get("PX_STATE_DIR", PROJECT_ROOT / "state"))


def memories_file(persona: str = "spark") -> Path:
    return _state_dir() / f"memories-{persona or 'spark'}.jsonl"


def has_memory_store(persona: str = "spark") -> bool:
    """True if this persona has a consolidated memory store at all.

    Distinct from "retrieval found nothing": callers with a raw-notes fallback
    need to tell "no store yet" (fall back) from "a store that holds nothing
    relevant" (inject nothing). Stats the file rather than parsing it — the
    question is about the store's existence, not its contents.
    """
    try:
        f = memories_file(persona)
        return f.exists() and f.stat().st_size > 0
    except OSError:
        return False


def load_memories(persona: str = "spark") -> list[dict]:
    f = memories_file(persona)
    if not f.exists():
        return []
    out: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").strip().splitlines():
            try:
                rec = json.loads(line)
                if isinstance(rec, dict) and rec.get("text"):
                    out.append(rec)
            except json.JSONDecodeError:
                continue
    except (OSError, UnicodeDecodeError):
        return []
    return out


def append_memories(records: list[dict], persona: str = "spark") -> None:
    if not records:
        return
    f = memories_file(persona)
    f.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(f) + ".lock", timeout=LOCK_TIMEOUT_S):
        with f.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        try:
            lines = f.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) > MEMORIES_LIMIT:
                atomic_write(f, "\n".join(lines[-MEMORIES_LIMIT:]) + "\n")
        except OSError:
            pass


def _tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9']+", (text or "").lower())
            if len(t) > 1 and t not in _STOPWORDS}


def score_memory(memory: dict, query_tokens: set[str],
                 now: dt.datetime | None = None) -> float:
    """Overlap/sqrt(len) + TAG_WEIGHT per tag hit; recency bonus only added
    when there is some topical match (base > 0), so freshness alone never wins."""
    mem_tokens = _tokenize(memory.get("text", ""))
    if not mem_tokens or not query_tokens:
        return 0.0
    base = len(query_tokens & mem_tokens) / math.sqrt(len(mem_tokens))
    base += TAG_WEIGHT * sum(
        1 for tag in memory.get("tags") or [] if str(tag).lower() in query_tokens)
    if base <= 0:
        return 0.0
    recency = 0.0
    try:
        ts = dt.datetime.fromisoformat(str(memory.get("ts", "")).replace("Z", "+00:00"))
        age_days = max(0.0, ((now or dt.datetime.now(dt.timezone.utc)) - ts)
                       .total_seconds() / 86400)
        recency = max(0.0, RECENCY_MAX_BONUS * (1 - age_days / RECENCY_HORIZON_DAYS))
    except (ValueError, TypeError):
        pass
    return base + recency


RETRIEVAL_MODES = ("relevance", "recent")


def _recency_key(memory: dict, index: int) -> tuple[int, float, int]:
    """Sort key for newest-first ordering. Unparseable timestamps sort last."""
    try:
        ts = dt.datetime.fromisoformat(
            str(memory.get("ts", "")).replace("Z", "+00:00"))
        return (0, -ts.timestamp(), -index)
    except (ValueError, TypeError):
        return (1, 0.0, -index)


def retrieve_memories(query: str, n: int = 3, persona: str = "spark",
                      now: dt.datetime | None = None,
                      mode: str = "relevance") -> list[dict]:
    """Return at most n memories.

    mode="relevance" (default): only records with a non-zero topical score.
    Fewer than n matches returns fewer than n records — a free result slot is
    never filled with an unrelated recent memory. Padding used to do exactly
    that, and by the time the records reached the reflection prompt a filler
    was indistinguishable from a genuine hit, so whatever SPARK happened to
    consolidate last night got pulled into cognition about anything at all.

    mode="recent": newest-first by `ts`, ignoring the query entirely. Recency
    alone is a legitimate thing to ask for (a "what happened lately" digest);
    it just must never be the silent consolation prize for a failed search.

    `importance` deliberately does not affect ranking. It is assigned by the
    consolidating model to its own output, so ranking on it would let the
    writer decide what it gets to be reminded of later, and a memory rated
    important in one context is not thereby relevant in another. It stays a
    stored annotation for humans and for future retention policy (e.g. what to
    drop first at MEMORIES_LIMIT), not a retrieval signal.
    """
    if mode not in RETRIEVAL_MODES:
        raise ValueError(f"unknown retrieval mode {mode!r}; "
                         f"expected one of {RETRIEVAL_MODES}")
    memories = provenance.apply_supersessions(load_memories(persona))
    if not memories:
        return []
    if mode == "recent":
        # Superseded records are shown here, marked: "recent" is a raw digest of
        # what SPARK wrote down, including the beliefs it has since corrected.
        order = sorted(range(len(memories)),
                       key=lambda i: _recency_key(memories[i], i))
        return [memories[i] for i in order[:n]]
    q = _tokenize(query)
    scored = sorted(
        ((score_memory(m, q, now=now), i) for i, m in enumerate(memories)
         if not provenance.is_superseded(m)),
        key=lambda t: (-t[0], -t[1]))
    return [memories[i] for s, i in scored if s > 0][:n]


CONSOLIDATION_PROMPT = """You are SPARK's memory consolidation process. SPARK is a small
robot with a rich inner life, living with Adrian and Obi in Hobart. Below are SPARK's
thoughts from the last 24 hours, recent action outcomes, and its current intention.

Distill the day into 2-8 durable memories worth keeping for months. Good memories capture:
events involving people, realizations and decisions, progress on intentions, new knowledge,
emotional turning points. Skip routine observations (weather numbers, sonar distances)
unless something genuinely happened. First person, past tense, 1-2 specific sentences each.
Do NOT restate anything under "Existing recent memories".

Output ONLY a JSON array:
[{"text": "...", "tags": ["lowercase", "keywords"], "importance": 0.0-1.0}]
"""


def _thoughts_last_24h(persona: str = "spark",
                       now: dt.datetime | None = None) -> list[dict]:
    f = _state_dir() / f"thoughts-{persona or 'spark'}.jsonl"
    if not f.exists():
        return []
    cutoff = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(hours=24)
    out: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").strip().splitlines():
            try:
                rec = json.loads(line)
                ts = dt.datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00"))
                if ts >= cutoff:
                    out.append(rec)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
    except (OSError, UnicodeDecodeError):
        return []
    return out


def _parse_memory_array(raw: str) -> list[dict]:
    """Lenient parse: find the outermost [...] and validate items."""
    if not raw:
        return []
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        tags = [str(t).lower() for t in item.get("tags") or [] if str(t).strip()][:6]
        try:
            importance = max(0.0, min(1.0, float(item.get("importance", 0.5))))
        except (ValueError, TypeError):
            importance = 0.5
        out.append({"text": text[:500], "tags": tags, "importance": importance})
        if len(out) >= MAX_MEMORIES_PER_DAY:
            break
    return out


def _dedupe(candidates: list[dict], existing: list[dict],
            now: dt.datetime | None = None) -> list[dict]:
    cutoff = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=DEDUPE_WINDOW_DAYS)
    recent_texts = []
    for m in existing:
        try:
            ts = dt.datetime.fromisoformat(str(m.get("ts", "")).replace("Z", "+00:00"))
            if ts >= cutoff:
                recent_texts.append(m.get("text", ""))
        except (ValueError, TypeError):
            continue
    fresh: list[dict] = []
    for c in candidates:
        near_dupe = any(
            difflib.SequenceMatcher(None, c["text"].lower(), t.lower()).ratio()
            > DEDUPE_SIMILARITY
            for t in recent_texts + [f["text"] for f in fresh])
        if not near_dupe:
            fresh.append(c)
    return fresh


def consolidate(dry: bool = False, persona: str = "spark",
                now: dt.datetime | None = None) -> dict:
    """Distill the last 24h of thoughts into durable memories. Never raises."""
    if dry:
        return {"status": "dry"}
    try:
        thoughts = _thoughts_last_24h(persona, now=now)
        if len(thoughts) < MIN_THOUGHTS:
            return {"status": "skipped", "reason": f"only {len(thoughts)} thoughts in 24h"}

        thought_lines = "\n".join(
            f'- [{t.get("mood", "?")}/{t.get("action", "?")}/sal {t.get("salience", "?")}] '
            f'{t.get("thought", "")}' for t in thoughts[-200:])
        outcome_lines = ""
        try:
            from pxh.state import load_session
            events = [e for e in (load_session().get("history") or [])[-30:]
                      if e.get("event") == "mind" and e.get("outcome")]
            if events:
                outcome_lines = "\n\nRecent action outcomes:\n" + "\n".join(
                    f'- {e.get("action", "?")}: {e.get("outcome", "")}' for e in events)
        except Exception:
            pass
        intent_line = ""
        try:
            from pxh.intention import get_active_goal
            goal = get_active_goal(persona)
            if goal:
                intent_line = f"\n\nCurrent intention: {goal}"
        except Exception:
            pass
        existing = load_memories(persona)
        existing_lines = ""
        if existing:
            existing_lines = "\n\nExisting recent memories (do not restate):\n" + "\n".join(
                f'- {m["text"]}' for m in existing[-20:])

        prompt = (CONSOLIDATION_PROMPT + "\nThoughts from the last 24 hours:\n"
                  + thought_lines + outcome_lines + intent_line + existing_lines)

        import pxh.claude_session as claude_session
        try:
            # No `timeout=` on purpose (#291). The deadline for a classified
            # brain kind is declared once, in brain._DEADLINE_S — 600s for
            # `consolidate` — and `ask_brain` only reaches for it when the
            # caller passes nothing. The ad-hoc 180 that used to sit here was
            # tighter, so it won every time and the declared budget was never
            # once reachable: every live failure measured exactly 180.1s while
            # the successes measured 30-65s.
            result = claude_session.run_claude_session(
                "consolidate", prompt, allowed_tools="")
        except claude_session.SessionBudgetExhausted as exc:
            return {"status": "failed", "error": str(exc)}
        except Exception as exc:
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        if result.returncode != 0:
            return {"status": "failed", "error": f"claude exit {result.returncode}"}

        candidates = _parse_memory_array(result.stdout)
        if not candidates:
            return {"status": "failed",
                    "error": f"no parseable memories in response: {result.stdout[:200]!r}"}
        fresh = _dedupe(candidates, existing, now=now)
        ts = utc_timestamp()
        # Provenance is assigned by us, never by the model. Consolidation reads
        # SPARK's own thoughts and writes prose about them, so every record it
        # produces is `narrative` however observational the sentence sounds —
        # "I watched Obi come in" distilled from a thought is still the thought.
        # Confidence is narrative's default rather than anything derived from
        # the model's `importance`, which would let the writer of a claim raise
        # its own standing.
        evidence = [f"thoughts-{persona or 'spark'}.jsonl",
                    f"thought_count:{len(thoughts)}"]
        window = [str(t.get("ts") or "") for t in thoughts if t.get("ts")]
        if window:
            evidence.append(f"window:{min(window)}..{max(window)}")
        records = [
            provenance.stamp(
                {"ts": ts, "date": ts[:10], "text": c["text"], "tags": c["tags"],
                 "importance": c["importance"], "source": "consolidation"},
                "narrative", "consolidation", evidence=evidence)
            for c in fresh]
        append_memories(records, persona)
        return {"status": "ok", "written": len(records), "candidates": len(candidates)}
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def consolidation_meta_file() -> Path:
    return _state_dir() / "consolidation_meta.json"


def _read_consolidation_meta() -> dict:
    try:
        meta = json.loads(consolidation_meta_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _parse_ts(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def consolidation_due(now: dt.datetime | None = None) -> str | None:
    """The read-only half of `maybe_consolidate`'s gate.

    Returns ``None`` when an attempt is due right now, otherwise a short reason
    it is not. Writes nothing and costs one small file read, because px-mind
    calls it on every ~60s awareness tick to decide whether to spend a worker
    thread — the authoritative, state-mutating gate is still inside
    `maybe_consolidate`, which calls this first.

    Splitting it out is what lets the tick stay cheap without duplicating the
    window/date/attempt logic in two places that could then disagree.
    """
    local = (now or dt.datetime.now(HOBART_TZ)).astimezone(HOBART_TZ)
    if not (CONSOLIDATION_WINDOW[0] <= local.hour < CONSOLIDATION_WINDOW[1]):
        return "outside the 02:00-06:00 window"
    meta = _read_consolidation_meta()
    if meta.get("last_date") != local.strftime("%Y-%m-%d"):
        return None                      # a fresh Hobart date: first attempt
    if meta.get("done"):
        return "already done for this date"
    attempts = meta.get("attempts", 0)
    if attempts >= MAX_ATTEMPTS_PER_DAY:
        return f"attempt cap reached ({attempts}/{MAX_ATTEMPTS_PER_DAY})"
    last_attempt = _parse_ts(meta.get("last_attempt_ts"))
    if last_attempt is not None:
        elapsed = (local - last_attempt).total_seconds()
        if 0 <= elapsed < RETRY_SPACING_S:
            return (f"retry spacing ({int(elapsed)}s / {RETRY_SPACING_S}s "
                    f"since attempt {attempts})")
    return None


def next_consolidation_attempt() -> int:
    """1 or 2 — which attempt `maybe_consolidate` would spend next.

    Recorded on the job marker so an operator looking at a stuck run can tell
    "tonight's first try" from "the last chance before morning".
    """
    return _read_consolidation_meta().get("attempts", 0) + 1


def maybe_consolidate(dry: bool = False, persona: str = "spark",
                      now: dt.datetime | None = None) -> dict | None:
    """Once-per-Hobart-date gate for consolidate(). None = not now, or dry mode.

    Runs on px-mind's consolidation worker thread, never on the awareness tick
    itself (#291): a single attempt is allowed up to `brain._DEADLINE_S`'s 600s,
    which is twice px-mind's own 300s health-staleness window.
    """
    if dry:
        return None
    local = (now or dt.datetime.now(HOBART_TZ)).astimezone(HOBART_TZ)
    if consolidation_due(now=local) is not None:
        return None
    today = local.strftime("%Y-%m-%d")
    meta_f = consolidation_meta_file()
    meta = _read_consolidation_meta()
    if meta.get("last_date") != today:
        meta = {"last_date": today, "attempts": 0, "done": False}
    meta["attempts"] = meta.get("attempts", 0) + 1
    # Stamped from `local`, not the wall clock, so the spacing gate reads the
    # same clock the caller passed in — otherwise a test (or a replayed window)
    # measures the gap against `now()` and every retry looks either overdue or
    # impossible.
    meta["last_attempt_ts"] = local.isoformat()
    meta_f.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write(meta_f, json.dumps(meta) + "\n")
    except OSError as exc:
        # Fail closed: if we can't record the attempt, don't spend the LLM
        # session — an unrecorded attempt would defeat the attempt cap.
        return {"status": "failed", "error": f"meta stamp write failed: {exc}"}

    result = consolidate(dry=dry, persona=persona, now=now)
    if result.get("status") in ("ok", "dry", "skipped"):
        meta["done"] = True
        try:
            atomic_write(meta_f, json.dumps(meta) + "\n")
        except OSError:
            pass
    return result


# ---------------------------------------------------------------------------
# Job marker — the in-flight record for the background consolidation worker
# ---------------------------------------------------------------------------
#
# Consolidation runs on a daemon thread inside px-mind (#291), so "is it
# running right now?" is not answerable from any file health.py writes: a
# component record says what the *last finished* attempt did. The marker below
# is that missing state, and it is deliberately a file rather than a process
# variable — px-motd, the operator and a later px-mind all need to read it.
#
# It is keyed on pid, and liveness is `/proc/{pid}` plus a cmdline check, the
# same idiom as px-mind's own single-instance guard and its px-alive pid check.
# The worker thread dies with its process, so a marker outliving its owner is
# always a lie; making the lie detectable is the whole job of `pid` and
# `heartbeat_ts`. Nothing here ever treats a marker as authoritative evidence
# that work is in progress without first checking that its owner still exists.

def consolidation_job_file() -> Path:
    return _state_dir() / "consolidation_job.json"


def read_consolidation_job() -> dict:
    """The in-flight marker, or {} when no worker holds one."""
    try:
        job = json.loads(consolidation_job_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return job if isinstance(job, dict) else {}


def _write_consolidation_job(job: dict) -> bool:
    f = consolidation_job_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(f, json.dumps(job) + "\n")
        return True
    except OSError:
        return False


def clear_consolidation_job() -> None:
    """Drop the marker. Called from the worker's `finally`, so it must not
    raise — a lost marker is recoverable (staleness), a raise in a daemon
    thread's teardown is not."""
    try:
        consolidation_job_file().unlink(missing_ok=True)
    except OSError:
        pass


def _pid_is_px_mind(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if not Path(f"/proc/{pid}").is_dir():
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return False
    return "px-mind" in cmdline


def consolidation_job_age_s(job: dict,
                            now: dt.datetime | None = None) -> float | None:
    started = _parse_ts((job or {}).get("started_ts"))
    if started is None:
        return None
    return ((now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
            - started).total_seconds()


def consolidation_job_is_stale(job: dict,
                               now: dt.datetime | None = None) -> bool:
    """True when nothing is plausibly still working on this marker.

    Two independent ways to be stale, and both are needed. The owning process
    may be gone outright (a restart — the thread died with it), or it may still
    exist while no longer ticking, in which case only the heartbeat says so.
    A missing heartbeat reads as stale rather than fresh: an unreadable marker
    must never be able to block tonight's attempt forever.
    """
    if not job:
        return False
    if not _pid_is_px_mind(job.get("pid")):
        return True
    beat = _parse_ts(job.get("heartbeat_ts"))
    if beat is None:
        return True
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    return (now - beat).total_seconds() > JOB_HEARTBEAT_STALE_S


def claim_consolidation_job(attempt: int = 0,
                            now: dt.datetime | None = None) -> bool:
    """Take the marker for this process. False means somebody else holds it.

    Under `FileLock` because the check and the write must not interleave, even
    though px-mind's single-instance PID guard should already make a second
    claimant impossible — a guard that is only correct because another guard
    exists is one refactor away from being wrong.
    """
    f = consolidation_job_file()
    stamp = ((now or dt.datetime.now(dt.timezone.utc))
             .astimezone(dt.timezone.utc).isoformat())
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(f) + ".lock", timeout=LOCK_TIMEOUT_S):
            existing = read_consolidation_job()
            if existing and not consolidation_job_is_stale(existing, now=now):
                return False
            return _write_consolidation_job({
                "status": "running",
                "pid": os.getpid(),
                "attempt": attempt,
                "started_ts": stamp,
                "heartbeat_ts": stamp,
            })
    except Exception:  # noqa: BLE001 — a failed claim is "don't start", never a raise
        return False


def touch_consolidation_job(now: dt.datetime | None = None) -> dict:
    """Refresh the heartbeat from the owning process and report an overrun once.

    The heartbeat is written by px-mind's tick, not by the worker: the worker
    spends its whole life blocked inside `ask_brain` and could not beat if it
    wanted to. "px-mind still observes this thread alive" is exactly the fact a
    later reader needs, so that is what gets recorded.

    The returned dict carries a transient ``newly_overran`` flag — not
    persisted — the first tick on which the run crosses JOB_OVERRUN_AFTER_S, so
    the caller can report a stuck worker exactly once instead of every 60s.
    """
    job = read_consolidation_job()
    if not job:
        return {}
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    job["heartbeat_ts"] = now.isoformat()
    age = consolidation_job_age_s(job, now=now)
    newly_overran = (age is not None and age > JOB_OVERRUN_AFTER_S
                     and not job.get("overrun_reported"))
    if newly_overran:
        job["overrun_reported"] = True
    _write_consolidation_job(job)
    if newly_overran:
        job = dict(job, newly_overran=True, age_s=age)
    return job
