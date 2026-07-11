"""Consolidated long-term memory for SPARK — QA roadmap item 5.

Store: state/memories-{persona}.jsonl, one record per line:
  {"ts", "date", "text", "tags": [...], "importance": 0-1, "source": "consolidation"}

Retrieval is deliberately deterministic and free (token/tag overlap + recency)
so the per-reflection path costs nothing; the nightly consolidation pass
(consolidate(), Task 4) is where the one daily LLM call goes.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from pathlib import Path

from filelock import FileLock

from pxh.state import atomic_write
from pxh.time import utc_timestamp

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

MEMORIES_LIMIT = 5000
RECENCY_HORIZON_DAYS = 60
RECENCY_MAX_BONUS = 0.5
TAG_WEIGHT = 2.0
LOCK_TIMEOUT_S = 10

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
    if len(chosen) < n:  # pad with the most recent memories not already chosen
        for i in range(len(memories) - 1, -1, -1):
            if i not in chosen:
                chosen.append(i)
            if len(chosen) >= n:
                break
    return [memories[i] for i in chosen[:n]]
