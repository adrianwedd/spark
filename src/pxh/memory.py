"""Consolidated long-term memory for SPARK — QA roadmap item 5.

Store: state/memories-{persona}.jsonl, one record per line:
  {"ts", "date", "text", "tags": [...], "importance": 0-1, "source": "consolidation"}

Retrieval is deliberately deterministic and free (token/tag overlap + recency)
so the per-reflection path costs nothing; the nightly consolidation pass
(consolidate(), Task 4) is where the one daily LLM call goes.
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
    except OSError:
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


def retrieve_memories(query: str, n: int = 3, persona: str = "spark",
                      now: dt.datetime | None = None) -> list[dict]:
    memories = load_memories(persona)
    if not memories:
        return []
    q = _tokenize(query)
    scored = sorted(
        ((score_memory(m, q, now=now), i) for i, m in enumerate(memories)),
        key=lambda t: (-t[0], -t[1]))
    chosen = [i for s, i in scored if s > 0][:n]
    if len(chosen) < n:  # pad with the most-recent memories (by parsed ts) not already chosen
        chosen_set = set(chosen)

        def _pad_key(i: int) -> tuple[int, float, int]:
            try:
                ts = dt.datetime.fromisoformat(
                    str(memories[i].get("ts", "")).replace("Z", "+00:00"))
                return (0, -ts.timestamp(), -i)
            except (ValueError, TypeError):
                return (1, 0.0, -i)

        remaining = sorted(
            (i for i in range(len(memories)) if i not in chosen_set), key=_pad_key)
        chosen.extend(remaining[:n - len(chosen)])
    return [memories[i] for i in chosen[:n]]


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
    except OSError:
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
        result = claude_session.run_claude_session(
            "consolidate", prompt, timeout=180, allowed_tools="")
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
    records = [{"ts": ts, "date": ts[:10], "text": c["text"], "tags": c["tags"],
                "importance": c["importance"], "source": "consolidation"} for c in fresh]
    append_memories(records, persona)
    return {"status": "ok", "written": len(records), "candidates": len(candidates)}


def consolidation_meta_file() -> Path:
    return _state_dir() / "consolidation_meta.json"


def maybe_consolidate(dry: bool = False, persona: str = "spark",
                      now: dt.datetime | None = None) -> dict | None:
    """Once-per-Hobart-date gate for consolidate(). None = not now."""
    local = (now or dt.datetime.now(HOBART_TZ)).astimezone(HOBART_TZ)
    if not (CONSOLIDATION_WINDOW[0] <= local.hour < CONSOLIDATION_WINDOW[1]):
        return None
    today = local.strftime("%Y-%m-%d")
    meta_f = consolidation_meta_file()
    meta = {}
    try:
        meta = json.loads(meta_f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    if meta.get("last_date") != today:
        meta = {"last_date": today, "attempts": 0, "done": False}
    if meta.get("done") or meta.get("attempts", 0) >= MAX_ATTEMPTS_PER_DAY:
        return None
    meta["attempts"] = meta.get("attempts", 0) + 1
    meta_f.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(meta_f, json.dumps(meta) + "\n")

    result = consolidate(dry=dry, persona=persona, now=now)
    if result.get("status") in ("ok", "dry", "skipped"):
        meta["done"] = True
        atomic_write(meta_f, json.dumps(meta) + "\n")
    return result
