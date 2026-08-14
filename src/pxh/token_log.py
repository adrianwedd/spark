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
import tempfile
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


def log_usage(input_text: str, output_text: str, backend: str = "unknown") -> None:
    """Accumulate estimated token counts into state/token_usage.json.

    `backend` splits the totals per tier under ``by_backend``. The top-level
    totals mix free (Ollama) and paid (Claude) calls, so they cannot answer
    "what am I spending" — only the per-backend breakdown can. Pass the tier
    that actually served, not the one that was configured.
    """
    state_dir = _state_dir()
    usage_file = state_dir / "token_usage.json"
    lock = FileLock(str(usage_file) + ".lock", timeout=3)
    try:
        with lock:
            try:
                existing = json.loads(usage_file.read_text())
            except Exception:
                existing = {"input_tokens": 0, "output_tokens": 0, "call_count": 0}
            in_tok, out_tok = _est(input_text), _est(output_text)
            existing["input_tokens"] = existing.get("input_tokens", 0) + in_tok
            existing["output_tokens"] = existing.get("output_tokens", 0) + out_tok
            existing["call_count"] = existing.get("call_count", 0) + 1

            by_backend = existing.get("by_backend")
            if not isinstance(by_backend, dict):
                by_backend = {}
            slot = by_backend.get(backend)
            if not isinstance(slot, dict):
                slot = {"input_tokens": 0, "output_tokens": 0, "call_count": 0}
            slot["input_tokens"] = slot.get("input_tokens", 0) + in_tok
            slot["output_tokens"] = slot.get("output_tokens", 0) + out_tok
            slot["call_count"] = slot.get("call_count", 0) + 1
            slot["last_ts"] = utc_timestamp()
            by_backend[backend] = slot
            existing["by_backend"] = by_backend

            existing["ts"] = utc_timestamp()
            fd, tmp = tempfile.mkstemp(dir=str(usage_file.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(existing, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, str(usage_file))
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception:
        _log.warning("token accounting failed", exc_info=True)
