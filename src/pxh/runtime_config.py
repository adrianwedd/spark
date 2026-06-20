"""Operator-tunable runtime overrides persisted to state/runtime_config.json.

Deliberately NOT .env (which is on the evolve blacklist and needs a restart) —
this file is read live by mind.py each reflection cycle.
"""
import json
import os
from pathlib import Path

from filelock import FileLock

from pxh.state import atomic_write

ALLOWED_KEYS = {"mind_backend", "mind_claude_model", "awareness_interval"}


def _path() -> Path:
    state = Path(os.environ.get("PX_STATE_DIR",
                 Path(__file__).resolve().parent.parent.parent / "state"))
    return state / "runtime_config.json"


def load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def update(fields: dict) -> dict:
    bad = set(fields) - ALLOWED_KEYS
    if bad:
        raise ValueError(f"unknown config keys: {sorted(bad)}")
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(p) + ".lock", timeout=10):
        data = load()
        data.update(fields)
        atomic_write(p, json.dumps(data, indent=2) + "\n")
    return data
