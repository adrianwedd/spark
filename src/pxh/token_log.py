"""Token usage accounting for SPARK LLM calls.

Writes cumulative totals to state/token_usage.json (FileLock protected).
Estimate: len(text.encode('utf-8')) // 4 tokens — consistent approximation
for Claude (English text averages ~3.5–4 bytes per token).

Usage:
    from pxh.token_log import log_usage
    log_usage(prompt, response_text)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from filelock import FileLock

from .time import utc_timestamp

_log = logging.getLogger("pxh.token_log")


def _est(text: str) -> int:
    """Estimate token count from UTF-8 byte length."""
    return max(1, len(text.encode("utf-8")) // 4)


def _state_dir() -> Path:
    root = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parent.parent.parent))
    return Path(os.environ.get("PX_STATE_DIR", root / "state"))


def log_usage(input_text: str, output_text: str) -> None:
    """Accumulate estimated token counts into state/token_usage.json."""
    state_dir = _state_dir()
    usage_file = state_dir / "token_usage.json"
    lock = FileLock(str(usage_file) + ".lock", timeout=3)
    try:
        with lock:
            try:
                existing = json.loads(usage_file.read_text())
            except Exception:
                existing = {"input_tokens": 0, "output_tokens": 0, "call_count": 0}
            existing["input_tokens"] = existing.get("input_tokens", 0) + _est(input_text)
            existing["output_tokens"] = existing.get("output_tokens", 0) + _est(output_text)
            existing["call_count"] = existing.get("call_count", 0) + 1
            existing["ts"] = utc_timestamp()
            usage_file.write_text(json.dumps(existing))
    except Exception:
        _log.warning("token accounting failed", exc_info=True)
