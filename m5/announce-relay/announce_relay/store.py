"""Text sanitization, content-addressed caching, atomic writes, and TTL janitor."""
import contextlib
import hashlib
import os
import tempfile
import threading
import time
import unicodedata
import uuid
import wave
from pathlib import Path

from . import config

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


_MARKDOWN_DROP = "<>*_`#~[]"


def sanitize_text(text: str) -> str:
    """Strip control chars/symbols/emoji + markdown punctuation; collapse whitespace.

    Control chars and emoji are replaced with a SPACE (not dropped) so that a
    newline/tab between two words cannot merge them ("a\\nb" -> "a b", never "ab").
    Markdown scaffolding chars are dropped (they sit at word boundaries).
    """
    text = unicodedata.normalize("NFKC", text or "")
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] == "C" or cat in ("So", "Sk"):   # control/format/surrogate or symbol/emoji
            out.append(" ")
            continue
        if ch in _MARKDOWN_DROP:
            continue
        out.append(ch)
    return " ".join("".join(out).split()).strip()


def announce_key(voice: str, text: str) -> str:
    raw = f"{voice}|en|{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def public_path(key: str) -> Path:
    return config.CACHE_DIR / f"{key}.wav"


def private_path() -> Path:
    return config.PRIV_DIR / f"{uuid.uuid4().hex}.wav"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def key_lock(key: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _locks[key] = lk
        return lk


def wav_duration_s(path: Path) -> float | None:
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as w:
            return round(w.getnframes() / float(w.getframerate()), 2)
    except Exception:
        return None


def _prune_locks() -> None:
    """Drop per-key locks that no one currently holds — bounds unbounded growth."""
    with _locks_guard:
        for key in [k for k, lk in _locks.items() if lk.acquire(blocking=False)]:
            _locks[key].release()
            del _locks[key]


def run_janitor(now: float | None = None) -> int:
    """Delete cache/*.wav older than TTL and priv/*.wav older than the private TTL."""
    now = now if now is not None else time.time()
    removed = 0
    pub_cut = now - config.CACHE_TTL_DAYS * 86400
    priv_cut = now - config.PRIVATE_TTL_MIN * 60
    for d, cut in ((config.CACHE_DIR, pub_cut), (config.PRIV_DIR, priv_cut)):
        if not d.exists():
            continue
        for f in d.glob("*.wav"):
            try:
                if f.stat().st_mtime < cut:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    _prune_locks()
    return removed
