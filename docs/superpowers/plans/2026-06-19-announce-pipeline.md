# Announce Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let SPARK speak deliberate announcements through the household Google Nest devices in the cloned `data` voice, by pre-synthesizing audio on M5 and casting it via Home Assistant.

**Architecture:** A thin FastAPI "announce relay" on M5 (port 7862, LAN-bound) fronts the localhost-only Afterwords TTS (port 7860): it synthesizes text to a complete WAV, validates + caches it, and serves it unauthenticated at an **IP-based** URL that a Chromecast can fetch. A new Pi tool `bin/tool-announce` POSTs text to the relay, then calls HA `media_player.play_media` to cast the resulting URL to a single Nest entity. Triggers (voice loop, px-mind, message_obi) dispatch into the tool; the robot's local espeak path stays fully independent of any relay/HA failure.

**Tech Stack:** Python 3.11 stdlib (`urllib`, `wave`, `hashlib`, `threading`), FastAPI + uvicorn (relay only), Home Assistant REST API, launchd (M5), systemd (Pi). Pi-side tool and triggers use **stdlib only** (system Python lacks `requests`).

## Global Constraints

Copy these verbatim into every task's mental model — they are project-wide invariants from the spec:

- **Relay port `7862`** (not 7861 — Pi's `px-tts-glados` owns 7861). Afterwords stays on **`127.0.0.1:7860`**, never rebound to `0.0.0.0`.
- **All cast/audio URLs use M5's IP `192.168.1.171`, never `M5.local`** — Nest devices can't resolve mDNS (constraint #6).
- **`/audio/<id>.wav` is unauthenticated** — Chromecast can't send auth headers (constraint #5). Validate `<id>` against `^[a-f0-9-]{16,36}\.wav$` and resolve strictly within the audio dirs (no path traversal).
- **Pre-synthesize to a complete file, then cast** — synth is ~2–8 s warm, ~33 s cold; never stream live (constraint #2).
- **v1 casts to a SINGLE entity** — no HA speaker group exists; multiple distinct targets echo (constraint #7).
- **Voice is hardcoded `data`** for v1; `lang` fixed to `en` (param reserved, not exposed). No voice-allowlist machinery on the Pi side (YAGNI); the relay independently enforces `RELAY_ALLOWED_VOICES` (defense in depth).
- **`ANNOUNCE_MAX_CHARS = 200`**; **connect timeout 5 s / read timeout 70 s** on the Pi→relay call.
- **Public cache TTL 7 days; private (`priv/`) TTL 3 minutes**, both swept by a background janitor (startup + interval, not only on request).
- **Night silence 19:00–07:00 Hobart** applies to **all** announce paths (incl. user-initiated). Sourced from `spark_config` (`NIGHT_SILENCE_START_H`/`END_H`), applied via `ZoneInfo("Australia/Hobart")` — never hardcoded offsets.
- **`PX_DRY=1` gates all network egress.** **`ANNOUNCE_ENABLED = False`** ships off; flipped True only once the relay is live on M5.
- **Pi tool MUST `yield_alive` + write `state/exploring.json`** while it waits (synth can take tens of seconds) so `px-alive` doesn't kill/restart it mid-call. Never raises — failure returns `{"status":"error"}`.

---

## Phase 0 — Live validation gates (MUST pass before any code)

These are throwaway manual tests run against the real Nest + HA. **Do not start Task 1 until both pass**, because their outcome pins config values and may add a transcode requirement.

- [ ] **G1 — WAV-on-Cast.** Serve a static 24 kHz mono WAV from M5 by IP (e.g. `python3 -m http.server 7862` in a dir holding `test.wav`, reachable at `http://192.168.1.171:7862/test.wav`). From HA Developer Tools → Actions, call `media_player.play_media` with that URL to a Nest and confirm it **audibly plays**.
  - **If it plays:** record `media_content_type` that worked. Proceed.
  - **If it does NOT play:** the relay must transcode WAV→MP3 (`Content-Type: audio/mpeg`, `.mp3` extension). Note this; fold the transcode into Task 4's `synthesize()` (add an ffmpeg/`lameenc` step) and change the served extension/regex accordingly before proceeding.
- [ ] **G2 — Correct entity + media_content_type.** In HA Developer Tools → States, find which `media_player.*` entity for the Nest Hub Max actually exposes `group_members` / casts (`nest_hub_max` vs `nest_hub_max_2`). Cast the G1 test URL to candidate entities; identify the one that works and the `media_content_type` value that works (`audio/wav` MIME vs HA's `"music"`).
  - **Record both.** These pin `ANNOUNCE_DEFAULT_TARGETS`, `ANNOUNCE_ALLOWED_TARGETS`, and `ANNOUNCE_MEDIA_CONTENT_TYPE` in Task 1.

**Gate outcome to capture (fill before Task 1):**

```
G1 result:           PASS (WAV plays) | FAIL (transcode to MP3 required)
G2 castable entity:  media_player.________________
G2 media_content_type: ________________   (e.g. "music" or "audio/wav")
```

---

## File Structure

**M5 relay** — `m5/announce-relay/` (new package, deployed to M5):

| File | Responsibility |
|---|---|
| `announce_relay/__init__.py` | Package marker |
| `announce_relay/config.py` | Env-driven settings (ports, dirs, TTLs, token, allowlist) |
| `announce_relay/store.py` | Sanitize, hash key, cache/private paths, atomic write, per-key locks, WAV duration, janitor |
| `announce_relay/synth.py` | Afterwords client + WAV validation; `SynthError` |
| `announce_relay/app.py` | FastAPI app: `/announce`, `/audio/<id>.wav`, `/health`, auth, rate limit, janitor loop |
| `tests/conftest.py`, `tests/test_store.py`, `tests/test_synth.py`, `tests/test_app.py` | Unit + TestClient tests (mock afterwords) |
| `requirements.txt`, `.env.example`, `com.spark.announce-relay.plist`, `install.sh`, `README.md` | Deployment |

**Pi side** — existing repo:

| File | Responsibility |
|---|---|
| `src/pxh/spark_config.py` | New `ANNOUNCE_*` + `NIGHT_SILENCE_*` constants |
| `bin/tool-announce` | New tool: POST relay → cast via HA; stdlib only |
| `src/pxh/voice_loop.py` | `tool_announce` in `ALLOWED_TOOLS`/`TOOL_COMMANDS` + `validate_action` branch |
| `src/pxh/mind.py` | Night-silence via config; `announce` action; `message_obi` announce fire |
| `docs/prompts/claude-voice-system.md`, `codex-voice-system.md` | Document the new tool |
| `tests/test_tools.py`, `tests/test_voice_loop.py`, `tests/test_mind*.py` | Pi-side tests |

---

## Task 1: Config constants (`spark_config.py`)

**Files:**
- Modify: `src/pxh/spark_config.py` (append a new block after the existing `OBI_CHAT_*` constants near line 21)
- Test: `tests/test_spark_config.py` (create if absent, else add a test fn)

**Interfaces:**
- Produces: `ANNOUNCE_ENABLED: bool`, `ANNOUNCE_RELAY_URL: str`, `ANNOUNCE_VOICE: str`, `ANNOUNCE_DEFAULT_TARGETS: list[str]`, `ANNOUNCE_ALLOWED_TARGETS: list[str]`, `ANNOUNCE_MAX_CHARS: int`, `ANNOUNCE_CONNECT_TIMEOUT: int`, `ANNOUNCE_READ_TIMEOUT: int`, `ANNOUNCE_MEDIA_CONTENT_TYPE: str`, `HA_BASE_URL: str`, `NIGHT_SILENCE_START_H: int`, `NIGHT_SILENCE_END_H: int`. Consumed by `voice_loop.py`, `mind.py`, `bin/tool-announce`.

- [ ] **Step 1: Write the failing test**

In `tests/test_spark_config.py`:

```python
from pxh import spark_config as cfg


def test_announce_constants_present_and_safe():
    # Ships OFF until the relay is live on M5
    assert cfg.ANNOUNCE_ENABLED is False
    # IP-based, never M5.local (Nest can't resolve mDNS)
    assert "192.168.1.171" in cfg.ANNOUNCE_RELAY_URL
    assert "M5.local" not in cfg.ANNOUNCE_RELAY_URL
    assert cfg.ANNOUNCE_VOICE == "data"
    # v1 casts to exactly one default entity (no speaker group -> echo)
    assert len(cfg.ANNOUNCE_DEFAULT_TARGETS) == 1
    assert set(cfg.ANNOUNCE_DEFAULT_TARGETS).issubset(set(cfg.ANNOUNCE_ALLOWED_TARGETS))
    assert cfg.ANNOUNCE_MAX_CHARS == 200
    assert cfg.ANNOUNCE_CONNECT_TIMEOUT == 5
    assert cfg.ANNOUNCE_READ_TIMEOUT == 70
    assert cfg.NIGHT_SILENCE_START_H == 19
    assert cfg.NIGHT_SILENCE_END_H == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_spark_config.py::test_announce_constants_present_and_safe -v`
Expected: FAIL with `AttributeError: module 'pxh.spark_config' has no attribute 'ANNOUNCE_ENABLED'`

- [ ] **Step 3: Add the constants**

Append to `src/pxh/spark_config.py` after the `OBI_CHAT_*` block (~line 21).

> **STOP — substitute the Phase-0 G2 values before committing.** The three lines below are *placeholders*: `ANNOUNCE_DEFAULT_TARGETS` (the entity that actually casts — may be `…_2`), `ANNOUNCE_ALLOWED_TARGETS` (must include that entity), and `ANNOUNCE_MEDIA_CONTENT_TYPE` (`"music"` vs `"audio/wav"`). The test below deliberately asserts only *shape* (single default, default ⊆ allowed) so it stays valid for whatever G2 pinned — but the values must be the real ones from your gate run.

```python
# --- Announce pipeline (data-voice over Google Nest) ----------------------
ANNOUNCE_ENABLED         = False  # ships off; flip True once relay is live on M5
ANNOUNCE_RELAY_URL       = "http://192.168.1.171:7862"   # IP, not M5.local (Nest mDNS)
ANNOUNCE_VOICE           = "data"
# v1: single entity to avoid multi-target echo; IDs pinned by gate G2.
ANNOUNCE_DEFAULT_TARGETS = ["media_player.nest_hub_max"]
ANNOUNCE_ALLOWED_TARGETS = ["media_player.nest_hub_max", "media_player.nest_mini",
                            "media_player.googlehome1094"]
ANNOUNCE_MEDIA_CONTENT_TYPE = "music"   # pinned by gate G2 ("music" vs "audio/wav")
ANNOUNCE_MAX_CHARS       = 200    # ~15-20s audio; bounds synth time + URL/log size
ANNOUNCE_CONNECT_TIMEOUT = 5      # fast-fail if relay/M5 down
ANNOUNCE_READ_TIMEOUT    = 70     # survives a cold ~33s synth + overhead
HA_BASE_URL              = "http://homeassistant.local:8123"

# Night silence bounds (Hobart time), applied via ZoneInfo("Australia/Hobart").
# Sourced here so mind.py stops hardcoding 19/7.
NIGHT_SILENCE_START_H    = 19
NIGHT_SILENCE_END_H      = 7
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_spark_config.py::test_announce_constants_present_and_safe -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxh/spark_config.py tests/test_spark_config.py
git commit -m "feat(announce): add ANNOUNCE_* and NIGHT_SILENCE_* config constants"
```

---

## Task 2: Relay store — sanitize, hashing, paths, atomic write, locks, duration

**Files:**
- Create: `m5/announce-relay/announce_relay/__init__.py` (empty)
- Create: `m5/announce-relay/announce_relay/config.py`
- Create: `m5/announce-relay/announce_relay/store.py`
- Create: `m5/announce-relay/tests/conftest.py`
- Test: `m5/announce-relay/tests/test_store.py`

**Interfaces:**
- Produces (`store`): `sanitize_text(text:str)->str`, `announce_key(voice:str,text:str)->str` (16-hex), `public_path(key:str)->Path`, `private_path()->Path`, `atomic_write(path:Path,data:bytes)->None`, `key_lock(key:str)->threading.Lock`, `wav_duration_s(path:Path)->float|None`, `run_janitor(now:float|None=None)->int`.
- Produces (`config`): module attrs `RELAY_TOKEN`, `AFTERWORDS_URL`, `PUBLIC_BASE_URL`, `CACHE_DIR`, `PRIV_DIR`, `ALLOWED_VOICES`, `MAX_TEXT_BYTES`, `RATE_LIMIT_PER_MIN`, `CACHE_TTL_DAYS`, `PRIVATE_TTL_MIN`, `JANITOR_INTERVAL_S`, `SYNTH_TIMEOUT`.

- [ ] **Step 1: Create the config module**

`m5/announce-relay/announce_relay/config.py`:

```python
"""Env-driven settings for the announce relay. Loaded once at import."""
import os
from pathlib import Path


def _csv(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


RELAY_TOKEN        = os.environ.get("ANNOUNCE_RELAY_TOKEN", "")
AFTERWORDS_URL     = os.environ.get("AFTERWORDS_URL", "http://127.0.0.1:7860")
PUBLIC_BASE_URL    = os.environ.get("RELAY_PUBLIC_BASE_URL", "http://192.168.1.171:7862")

DATA_DIR           = Path(os.environ.get("RELAY_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
CACHE_DIR          = DATA_DIR / "cache"
PRIV_DIR           = DATA_DIR / "priv"

ALLOWED_VOICES     = _csv("RELAY_ALLOWED_VOICES", "data")
MAX_TEXT_BYTES     = int(os.environ.get("RELAY_MAX_TEXT_BYTES", "600"))
RATE_LIMIT_PER_MIN = int(os.environ.get("RELAY_RATE_LIMIT_PER_MIN", "30"))
CACHE_TTL_DAYS     = float(os.environ.get("RELAY_CACHE_TTL_DAYS", "7"))
PRIVATE_TTL_MIN    = float(os.environ.get("RELAY_PRIVATE_TTL_MIN", "3"))
JANITOR_INTERVAL_S = int(os.environ.get("RELAY_JANITOR_INTERVAL_S", "300"))
SYNTH_TIMEOUT      = float(os.environ.get("RELAY_SYNTH_TIMEOUT", "60"))
```

- [ ] **Step 2: Create the test conftest (redirects dirs to tmp)**

`m5/announce-relay/tests/conftest.py`:

```python
import sys
from pathlib import Path

# Make the announce_relay package importable when running pytest from this dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from announce_relay import config


@pytest.fixture(autouse=True)
def tmp_dirs(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    priv = tmp_path / "priv"
    cache.mkdir()
    priv.mkdir()
    monkeypatch.setattr(config, "CACHE_DIR", cache)
    monkeypatch.setattr(config, "PRIV_DIR", priv)
    monkeypatch.setattr(config, "RELAY_TOKEN", "test-token")
    return {"cache": cache, "priv": priv}
```

- [ ] **Step 3: Write the failing tests**

`m5/announce-relay/tests/test_store.py`:

```python
import time
from announce_relay import store, config


def test_sanitize_strips_markdown_emoji_control():
    # Property-based, not exact-equality (exact form is brittle across unicode tweaks).
    out = store.sanitize_text("Hello **world** \U0001F600 \x07 done")
    for bad in ("*", "<", ">", "\x07", "\U0001F600"):
        assert bad not in out
    assert "Hello" in out and "world" in out and "done" in out


def test_sanitize_collapses_whitespace_without_merging_words():
    # Newlines/tabs become spaces, not nothing — words must not merge.
    assert store.sanitize_text("a   b\n\tc") == "a b c"
    assert store.sanitize_text("foo\nbar") == "foo bar"


def test_announce_key_deterministic_and_voice_sensitive():
    k1 = store.announce_key("data", "hello")
    k2 = store.announce_key("data", "hello")
    k3 = store.announce_key("other", "hello")
    assert k1 == k2 and len(k1) == 16
    assert k1 != k3


def test_public_vs_private_paths_differ():
    pub = store.public_path(store.announce_key("data", "hi"))
    priv = store.private_path()
    assert pub.parent == config.CACHE_DIR
    assert priv.parent == config.PRIV_DIR
    # private names are random 32-hex uuids; public is the 16-hex content hash
    assert len(priv.stem) == 32 and len(pub.stem) == 16


def test_atomic_write_and_wav_duration():
    # Minimal 1-second 24kHz mono 16-bit WAV
    import wave
    p = config.CACHE_DIR / "x.wav"
    frames = b"\x00\x00" * 24000
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(frames)
    store.atomic_write(p, buf.getvalue())
    assert p.exists()
    assert abs(store.wav_duration_s(p) - 1.0) < 0.05


def test_janitor_deletes_expired_public_and_private(tmp_dirs):
    old_pub = config.CACHE_DIR / "old.wav"
    old_priv = config.PRIV_DIR / "old.wav"
    fresh_priv = config.PRIV_DIR / "fresh.wav"
    for p in (old_pub, old_priv, fresh_priv):
        p.write_bytes(b"RIFFxxxxWAVE")
    now = time.time()
    import os
    os.utime(old_pub, (now - 8 * 86400, now - 8 * 86400))   # 8 days > 7d TTL
    os.utime(old_priv, (now - 600, now - 600))              # 10 min > 3 min TTL
    os.utime(fresh_priv, (now - 30, now - 30))              # 30s < 3 min TTL
    removed = store.run_janitor(now=now)
    assert removed == 2
    assert not old_pub.exists() and not old_priv.exists()
    assert fresh_priv.exists()
```

Run: `cd m5/announce-relay && python -m pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'announce_relay.store'`

- [ ] **Step 4: Create the package marker and store module**

`m5/announce-relay/announce_relay/__init__.py`: empty file.

`m5/announce-relay/announce_relay/store.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd m5/announce-relay && python -m pytest tests/test_store.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add m5/announce-relay/announce_relay/__init__.py m5/announce-relay/announce_relay/config.py m5/announce-relay/announce_relay/store.py m5/announce-relay/tests/conftest.py m5/announce-relay/tests/test_store.py
git commit -m "feat(announce-relay): store layer — sanitize, hashing, atomic write, janitor"
```

---

## Task 3: Relay synth client + WAV validation

**Files:**
- Create: `m5/announce-relay/announce_relay/synth.py`
- Test: `m5/announce-relay/tests/test_synth.py`

**Interfaces:**
- Consumes: `config.AFTERWORDS_URL`, `config.SYNTH_TIMEOUT`.
- Produces: `synthesize(text:str, voice:str)->bytes` (validated WAV bytes), `ping()->bool`, `class SynthError(Exception)` with `.status:int` and `.detail:str`. Raises `SynthError(502, ...)` on non-WAV/unreachable, `SynthError(504, ...)` on timeout.

- [ ] **Step 1: Write the failing tests**

`m5/announce-relay/tests/test_synth.py`:

```python
import io
import wave
import socket
import urllib.error
import pytest
from announce_relay import synth


def _wav_bytes(seconds=0.1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\x00\x00" * int(24000 * seconds))
    return buf.getvalue()


class _Resp:
    def __init__(self, data, ctype):
        self._data = data
        self.headers = {"Content-Type": ctype}
    def read(self):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_synthesize_returns_validated_wav(monkeypatch):
    payload = _wav_bytes()
    monkeypatch.setattr(synth.urllib.request, "urlopen",
                        lambda url, timeout=None: _Resp(payload, "audio/wav"))
    out = synth.synthesize("hello", "data")
    assert out == payload


def test_synthesize_rejects_non_wav_content_type(monkeypatch):
    monkeypatch.setattr(synth.urllib.request, "urlopen",
                        lambda url, timeout=None: _Resp(b"<html>error</html>", "text/html"))
    with pytest.raises(synth.SynthError) as ei:
        synth.synthesize("hello", "data")
    assert ei.value.status == 502


def test_synthesize_rejects_truncated_wav(monkeypatch):
    monkeypatch.setattr(synth.urllib.request, "urlopen",
                        lambda url, timeout=None: _Resp(b"RIFF", "audio/wav"))
    with pytest.raises(synth.SynthError) as ei:
        synth.synthesize("hello", "data")
    assert ei.value.status == 502


def test_synthesize_maps_timeout_to_504(monkeypatch):
    def _boom(url, timeout=None):
        raise socket.timeout("timed out")
    monkeypatch.setattr(synth.urllib.request, "urlopen", _boom)
    with pytest.raises(synth.SynthError) as ei:
        synth.synthesize("hello", "data")
    assert ei.value.status == 504


def test_synthesize_maps_unreachable_to_502(monkeypatch):
    def _boom(url, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(synth.urllib.request, "urlopen", _boom)
    with pytest.raises(synth.SynthError) as ei:
        synth.synthesize("hello", "data")
    assert ei.value.status == 502
```

Run: `cd m5/announce-relay && python -m pytest tests/test_synth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'announce_relay.synth'`

- [ ] **Step 2: Create the synth module**

`m5/announce-relay/announce_relay/synth.py`:

```python
"""Afterwords TTS client with strict WAV validation."""
import contextlib
import io
import socket
import urllib.error
import urllib.parse
import urllib.request
import wave

from . import config


class SynthError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def _is_wav(data: bytes) -> bool:
    """Header check PLUS a real wave.open parse (rejects truncated/zero-frame WAVs)."""
    if len(data) <= 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return False
    try:
        with contextlib.closing(wave.open(io.BytesIO(data), "rb")) as w:
            return w.getnframes() > 0
    except (wave.Error, EOFError):
        return False


def synthesize(text: str, voice: str) -> bytes:
    """GET afterwords /synthesize and return validated WAV bytes, else raise SynthError."""
    q = urllib.parse.urlencode({"text": text, "voice": voice, "lang": "en"})
    url = f"{config.AFTERWORDS_URL}/synthesize?{q}"
    try:
        with urllib.request.urlopen(url, timeout=config.SYNTH_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read()
    except (socket.timeout, TimeoutError) as e:
        raise SynthError(504, f"afterwords synth timeout: {e}") from e
    except urllib.error.URLError as e:
        raise SynthError(502, f"afterwords unreachable: {e}") from e
    if "audio/wav" not in ctype:
        raise SynthError(502, f"unexpected content-type: {ctype!r}")
    if not _is_wav(data):
        raise SynthError(502, "response is not a valid WAV")
    return data


def ping() -> bool:
    """Best-effort liveness probe for /health."""
    try:
        with urllib.request.urlopen(config.AFTERWORDS_URL, timeout=2) as resp:
            return resp.status < 500
    except Exception:
        return False
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `cd m5/announce-relay && python -m pytest tests/test_synth.py -v`
Expected: PASS (5 tests)

- [ ] **Step 4: Commit**

```bash
git add m5/announce-relay/announce_relay/synth.py m5/announce-relay/tests/test_synth.py
git commit -m "feat(announce-relay): afterwords synth client with WAV validation"
```

> **G1 note:** if Phase 0 G1 failed, add a WAV→MP3 transcode step at the end of `synthesize()` here (return MP3 bytes), change `_is_wav`/served extension accordingly, and update the `/audio` regex + `Content-Type` in Task 4/5. Pin that decision before continuing.

---

## Task 4: Relay app — `/announce`, auth, rate limit, voice allowlist

**Files:**
- Create: `m5/announce-relay/announce_relay/app.py`
- Test: `m5/announce-relay/tests/test_app.py`

**Interfaces:**
- Consumes: `store.*`, `synth.synthesize`, `config.*`.
- Produces: FastAPI `app`. `POST /announce` body `{text:str, voice:str="data", cache:bool=true}` → `{audio_url, voice, cached, duration_s}`. Auth `Authorization: Bearer <RELAY_TOKEN>`. Errors: 400 (empty/oversized/bad-voice), 401 (bad token), 429 (rate), 502/504 (synth).

- [ ] **Step 1: Write the failing tests**

`m5/announce-relay/tests/test_app.py`:

```python
import io
import wave
import pytest
from fastapi.testclient import TestClient


def _wav_bytes():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 2400)
    return buf.getvalue()


@pytest.fixture()
def client(monkeypatch):
    from announce_relay import app as appmod, synth
    monkeypatch.setattr(synth, "synthesize", lambda text, voice: _wav_bytes())
    appmod._rate.clear()   # _rate is a module global — reset so tests don't bleed quota
    return TestClient(appmod.app)


AUTH = {"Authorization": "Bearer test-token"}


def test_announce_requires_auth(client):
    r = client.post("/announce", json={"text": "hi"})
    assert r.status_code == 401


def test_announce_public_returns_ip_url_and_caches(client):
    r = client.post("/announce", json={"text": "hello there", "cache": True}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["audio_url"].startswith("http://192.168.1.171:7862/audio/")
    assert body["cached"] is False  # first call synthesizes
    assert body["duration_s"] > 0
    # second identical call hits cache
    r2 = client.post("/announce", json={"text": "hello there", "cache": True}, headers=AUTH)
    assert r2.json()["cached"] is True
    assert r2.json()["audio_url"] == body["audio_url"]


def test_announce_private_uses_priv_namespace(client):
    r = client.post("/announce", json={"text": "secret", "cache": False}, headers=AUTH)
    assert r.status_code == 200
    # private filenames are random hex (32), not the 16-hex public hash
    name = r.json()["audio_url"].rsplit("/", 1)[-1]
    assert len(name) == len("0" * 32) + len(".wav")


def test_announce_rejects_unknown_voice(client):
    r = client.post("/announce", json={"text": "hi", "voice": "evil"}, headers=AUTH)
    assert r.status_code == 400


def test_announce_rejects_empty_after_sanitize(client):
    r = client.post("/announce", json={"text": "   **  ** "}, headers=AUTH)
    assert r.status_code == 400


def test_announce_rejects_oversized(client, monkeypatch):
    from announce_relay import config
    monkeypatch.setattr(config, "MAX_TEXT_BYTES", 10)
    r = client.post("/announce", json={"text": "x" * 50}, headers=AUTH)
    assert r.status_code == 400


def test_announce_rate_limited(client, monkeypatch):
    from announce_relay import config
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 2)
    for _ in range(2):
        assert client.post("/announce", json={"text": "hello"}, headers=AUTH).status_code == 200
    assert client.post("/announce", json={"text": "again now"}, headers=AUTH).status_code == 429
```

Run: `cd m5/announce-relay && python -m pytest tests/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'announce_relay.app'`

- [ ] **Step 2: Create the app module (`/announce` + `/health` only for now)**

`m5/announce-relay/announce_relay/app.py`:

```python
"""Announce relay: synth text on M5, cache, serve unauthenticated for Chromecast."""
import asyncio
import re
import threading
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import config, store, synth

app = FastAPI(title="announce-relay")

_AUDIO_RE = re.compile(r"^[a-f0-9-]{16,36}\.wav$")
_rate: dict[str, deque] = defaultdict(deque)

# Afterwords is a single GPU model — serialize ALL synth jobs process-wide
# (the per-key lock only dedups identical text; different texts must still queue).
_synth_gate = threading.Lock()


def _synth_serialized(text: str, voice: str) -> bytes:
    with _synth_gate:
        return synth.synthesize(text, voice)


class AnnounceBody(BaseModel):
    text: str
    voice: str = "data"
    cache: bool = True


@app.exception_handler(synth.SynthError)
async def _synth_err(request, exc: synth.SynthError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


def _check_auth(authorization: str | None) -> str:
    expected = f"Bearer {config.RELAY_TOKEN}"
    if not config.RELAY_TOKEN or authorization != expected:
        raise HTTPException(401, "bad token")
    return authorization


def _check_rate(token: str) -> None:
    now = time.time()
    dq = _rate[token]
    while dq and dq[0] < now - 60:
        dq.popleft()
    if len(dq) >= config.RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "rate limited")
    dq.append(now)


@app.post("/announce")
def announce(body: AnnounceBody, authorization: str | None = Header(default=None)):
    token = _check_auth(authorization)
    _check_rate(token)

    voice = body.voice
    if voice not in config.ALLOWED_VOICES:
        raise HTTPException(400, f"voice not allowed: {voice}")

    text = store.sanitize_text(body.text or "")
    if not text:
        raise HTTPException(400, "empty text after sanitization")
    if len(text.encode("utf-8")) > config.MAX_TEXT_BYTES:
        raise HTTPException(400, "text too large")

    if body.cache:
        key = store.announce_key(voice, text)
        path = store.public_path(key)
        # cached=True unless THIS request is the one that synthesizes (cache miss).
        cached = True
        if not path.exists():
            with store.key_lock(key):          # dedup identical concurrent misses
                if not path.exists():
                    data = _synth_serialized(text, voice)   # process-wide synth queue
                    store.atomic_write(path, data)
                    cached = False
        name = path.name
    else:
        path = store.private_path()
        data = _synth_serialized(text, voice)
        store.atomic_write(path, data)
        name = path.name
        cached = False

    return {
        "audio_url": f"{config.PUBLIC_BASE_URL}/audio/{name}",
        "voice": voice,
        "cached": cached,
        "duration_s": store.wav_duration_s(path),
    }


@app.get("/health")
def health():
    n = len(list(config.CACHE_DIR.glob("*.wav"))) if config.CACHE_DIR.exists() else 0
    return {"status": "ok", "afterwords": synth.ping(), "cache_files": n}
```

Note: the `tmp_dirs` autouse fixture from `conftest.py` already sets `config.RELAY_TOKEN = "test-token"` and points `CACHE_DIR`/`PRIV_DIR` at tmp.

- [ ] **Step 3: Run tests to verify they pass**

Run: `cd m5/announce-relay && python -m pytest tests/test_app.py -v`
Expected: PASS (7 tests). (`/audio` + janitor loop tests come in Task 5.)

- [ ] **Step 4: Commit**

```bash
git add m5/announce-relay/announce_relay/app.py m5/announce-relay/tests/test_app.py
git commit -m "feat(announce-relay): POST /announce with auth, rate limit, voice allowlist, caching"
```

---

## Task 5: Relay `/audio` serving (path-traversal guard) + janitor loop

**Files:**
- Modify: `m5/announce-relay/announce_relay/app.py` (add `/audio/{name}` route + startup janitor)
- Test: `m5/announce-relay/tests/test_app.py` (append)

**Interfaces:**
- Consumes: `config.CACHE_DIR`, `config.PRIV_DIR`, `store.run_janitor`, `config.JANITOR_INTERVAL_S`.
- Produces: `GET /audio/{name}` → `FileResponse` (`audio/wav`, no auth), 404 on bad/missing id. Background janitor on startup + every `JANITOR_INTERVAL_S`.

- [ ] **Step 1: Write the failing tests (append to `test_app.py`)**

```python
def test_audio_serves_cached_file(client):
    body = client.post("/announce", json={"text": "play me"}, headers=AUTH).json()
    name = body["audio_url"].rsplit("/", 1)[-1]
    r = client.get(f"/audio/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"


def test_audio_rejects_path_traversal(client):
    for bad in ["../config.py", "..%2f..%2fetc%2fpasswd", "foo/../bar.wav", "evil.txt"]:
        r = client.get(f"/audio/{bad}")
        assert r.status_code == 404


def test_audio_404_for_unknown_id(client):
    r = client.get("/audio/" + "a" * 16 + ".wav")
    assert r.status_code == 404


def test_startup_runs_janitor(monkeypatch):
    from announce_relay import app as appmod
    calls = []
    monkeypatch.setattr(appmod.store, "run_janitor", lambda now=None: calls.append(1) or 0)
    with TestClient(appmod.app):  # triggers startup event
        pass
    assert calls  # janitor ran at least once on startup
```

Run: `cd m5/announce-relay && python -m pytest tests/test_app.py -k "audio or janitor" -v`
Expected: FAIL — `/audio` returns 404 for the valid file (route missing) / startup janitor not wired.

- [ ] **Step 2: Add the `/audio` route and janitor startup to `app.py`**

Add after the `/health` route in `m5/announce-relay/announce_relay/app.py`:

```python
@app.get("/audio/{name}")
def audio(name: str):
    if not _AUDIO_RE.match(name):
        raise HTTPException(404, "not found")
    for d in (config.CACHE_DIR, config.PRIV_DIR):
        base = d.resolve()
        candidate = (d / name).resolve()
        if candidate.parent == base and candidate.is_file():
            return FileResponse(str(candidate), media_type="audio/wav")
    raise HTTPException(404, "not found")


async def _janitor_loop():
    while True:
        await asyncio.sleep(config.JANITOR_INTERVAL_S)
        store.run_janitor()


@contextlib.asynccontextmanager
async def _lifespan(_app):
    store.run_janitor()                              # sweep once on startup
    task = asyncio.create_task(_janitor_loop())
    _app.state.janitor_task = task
    try:
        yield
    finally:                                         # cancel cleanly — no leaked task
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# Attach the lifespan to the already-constructed app (supported Starlette hook).
app.router.lifespan_context = _lifespan
```

Add `import contextlib` to the imports at the top of `app.py` (alongside `import asyncio`).

**Why lifespan, not `@app.on_event("startup")`:** `on_event` is deprecated in current FastAPI, and a bare `asyncio.create_task` is never cancelled — under `TestClient` (which DOES run lifespan) repeated suites leak janitor loops and can hang. The lifespan context cancels the task on shutdown.

- [ ] **Step 3: Run tests to verify they pass**

Run: `cd m5/announce-relay && python -m pytest tests/test_app.py -v`
Expected: PASS (all 11 tests)

- [ ] **Step 4: Commit**

```bash
git add m5/announce-relay/announce_relay/app.py m5/announce-relay/tests/test_app.py
git commit -m "feat(announce-relay): GET /audio with path-traversal guard + startup janitor"
```

---

## Task 6: Relay deployment artifacts (launchd, install, requirements, README)

**Files:**
- Create: `m5/announce-relay/requirements.txt`
- Create: `m5/announce-relay/.env.example`
- Create: `m5/announce-relay/com.spark.announce-relay.plist`
- Create: `m5/announce-relay/install.sh`
- Create: `m5/announce-relay/README.md`

**Interfaces:** Produces a launchd-managed service binding `0.0.0.0:7862`. No automated test — verified by the deployment smoke check at the end.

- [ ] **Step 1: Create `requirements.txt`**

`m5/announce-relay/requirements.txt`:

```
fastapi>=0.110
uvicorn>=0.29
httpx>=0.27   # required by fastapi.testclient (tests only)
pytest>=8.0   # tests only
```

- [ ] **Step 2: Create `.env.example`**

`m5/announce-relay/.env.example`:

```
# Copy to .env on M5 and fill in. Loaded by install.sh into the launchd plist env.
ANNOUNCE_RELAY_TOKEN=change-me-long-random
AFTERWORDS_URL=http://127.0.0.1:7860
RELAY_PUBLIC_BASE_URL=http://192.168.1.171:7862
RELAY_DATA_DIR=/Users/<m5user>/announce-relay-data
RELAY_ALLOWED_VOICES=data
RELAY_MAX_TEXT_BYTES=600
RELAY_RATE_LIMIT_PER_MIN=30
RELAY_CACHE_TTL_DAYS=7
RELAY_PRIVATE_TTL_MIN=3
RELAY_JANITOR_INTERVAL_S=300
RELAY_SYNTH_TIMEOUT=60
```

- [ ] **Step 3: Create the run wrapper (sources the FULL `.env`)**

The relay reads **all** its config from `os.environ` (`config.py`). launchd's `EnvironmentVariables` dict is awkward to keep in sync with `.env`, so the plist execs a wrapper that sources `.env` and then launches uvicorn — this guarantees every `.env` value (AFTERWORDS_URL, RELAY_PUBLIC_BASE_URL, TTLs, rate limit, allowlist, …) actually reaches the process, not just the token.

`m5/announce-relay/run.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
set -a; source ./.env; set +a            # export EVERY var from .env
exec .venv/bin/uvicorn announce_relay.app:app --host 0.0.0.0 --port 7862
```

- [ ] **Step 4: Create the launchd plist**

`m5/announce-relay/com.spark.announce-relay.plist` (the `<m5user>` placeholder is substituted during install). It execs the wrapper — no per-var `EnvironmentVariables` block, so `.env` is the single source of truth:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.spark.announce-relay</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/<m5user>/announce-relay/run.sh</string>
    </array>
    <key>WorkingDirectory</key><string>/Users/<m5user>/announce-relay</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/Users/<m5user>/announce-relay/relay.log</string>
    <key>StandardErrorPath</key><string>/Users/<m5user>/announce-relay/relay.err</string>
</dict>
</plist>
```

- [ ] **Step 5: Create `install.sh`**

`m5/announce-relay/install.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
# Run on M5 from a checkout of m5/announce-relay/. Requires .env to exist.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

[[ -f .env ]] || { echo "create .env first (cp .env.example .env)"; exit 1; }
chmod +x run.sh

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Validate config + create data dirs (run.sh sources .env at runtime; the token
# and all other vars live ONLY in .env, never baked into the plist).
set -a; source .env; set +a
mkdir -p "${RELAY_DATA_DIR:?set RELAY_DATA_DIR in .env}/cache" "${RELAY_DATA_DIR}/priv"

PLIST="$HOME/Library/LaunchAgents/com.spark.announce-relay.plist"
sed -e "s#<m5user>#$USER#g" com.spark.announce-relay.plist > "$PLIST"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Loaded com.spark.announce-relay. Health:"
sleep 2
curl -fsS "http://127.0.0.1:7862/health" && echo
```

- [ ] **Step 6: Create `README.md`**

`m5/announce-relay/README.md`: document purpose (LAN front door to localhost afterwords), the two endpoints, the `data`-voice contract, the IP-not-mDNS rule, how to run tests (`python -m pytest`), and how to deploy (`cp .env.example .env`, edit, `./install.sh`). One paragraph each — keep it operational.

- [ ] **Step 7: Smoke-verify locally (no commit gate, just confidence)**

Run from `m5/announce-relay/`:

```bash
RELAY_DATA_DIR=/tmp/ar ANNOUNCE_RELAY_TOKEN=dev .venv/bin/uvicorn announce_relay.app:app --port 7862 &
curl -fsS http://127.0.0.1:7862/health   # expect {"status":"ok",...}
kill %1
```

Expected: `/health` returns JSON with `status: ok`.

- [ ] **Step 8: Commit**

```bash
git add m5/announce-relay/requirements.txt m5/announce-relay/.env.example m5/announce-relay/com.spark.announce-relay.plist m5/announce-relay/run.sh m5/announce-relay/install.sh m5/announce-relay/README.md
git commit -m "feat(announce-relay): launchd deployment (run.sh sources .env), install, docs"
```

---

## Task 7: Pi tool `bin/tool-announce`

**Files:**
- Create: `bin/tool-announce`
- Test: `tests/test_tools.py` (append dry-run + mocked-network tests)

**Interfaces:**
- Consumes (env): `PX_ANNOUNCE_TEXT` (required), `PX_ANNOUNCE_TARGETS` (optional CSV), `PX_ANNOUNCE_PRIVATE` (`1`→relay `cache:false`), `PX_DRY`, secrets `ANNOUNCE_RELAY_TOKEN`/`PX_HA_TOKEN`. Reads `pxh.spark_config` for relay URL, targets, timeouts, media type, HA base.
- Produces: single JSON line `{"status":"ok|dry|error","audio_url"?,"targets"?,"voice"?,"cached"?,"duration_s"?,"error"?}`. Casts to a single Nest via HA `media_player.play_media`. Never raises.

- [ ] **Step 1: Write the failing dry-run test (append to `tests/test_tools.py`)**

```python
def test_tool_announce_dry_run(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_ANNOUNCE_TEXT"] = "Dinner is ready"
    stdout = run_tool(["bin/tool-announce"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "dry"
    assert payload["voice"] == "data"
    assert payload["targets"]  # default target resolved
```

Run: `python -m pytest tests/test_tools.py::test_tool_announce_dry_run -v`
Expected: FAIL — `bin/tool-announce` does not exist (subprocess `check=True` raises / FileNotFound).

- [ ] **Step 2: Create `bin/tool-announce`**

`bin/tool-announce`:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/px-env"

# Slow synth: release px-alive so it doesn't kill us mid-call (skip in dry).
if [[ "${PX_DRY:-0}" == "0" ]]; then
    yield_alive
fi

python - "$@" <<'PY'
"""Tool wrapper: synth text on the M5 relay, then cast to a single Nest via HA."""
from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from pxh.state import update_session
from pxh import spark_config as cfg

PROJECT_ROOT = Path(os.environ["PROJECT_ROOT"])
STATE_DIR    = Path(os.environ.get("PX_STATE_DIR", str(PROJECT_ROOT / "state")))
HOBART_TZ    = ZoneInfo("Australia/Hobart")


def _set_exploring(active: bool) -> None:
    """Write exploring.json so px-alive yields/doesn't restart us mid-call."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "exploring.json"
    data = {"active": active}
    if active:
        data["pid"] = os.getpid()
        data["started"] = dt.datetime.now(dt.timezone.utc).isoformat()
    fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _emit(payload: dict) -> int:
    print(json.dumps(payload))
    return 0


def _is_night_silence() -> bool:
    """True during the night-silence window — applies to ALL announce paths."""
    hour = dt.datetime.now(HOBART_TZ).hour
    return hour >= cfg.NIGHT_SILENCE_START_H or hour < cfg.NIGHT_SILENCE_END_H


def _resolve_targets() -> list[str]:
    """Resolve to a SINGLE allowed target (v1 is single-target — multiple echo)."""
    raw = os.environ.get("PX_ANNOUNCE_TARGETS", "").strip()
    targets = [t.strip() for t in raw.split(",") if t.strip()] if raw else list(cfg.ANNOUNCE_DEFAULT_TARGETS)
    allowed = [t for t in targets if t in cfg.ANNOUNCE_ALLOWED_TARGETS]
    return allowed[:1]   # at most one


class _RelayHTTPError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"relay {status}: {detail}")


def _post_json(url: str, body: dict, headers: dict, connect_to: int, read_to: int) -> dict:
    """True split timeout: connect within connect_to, then read within read_to.

    urllib's single `timeout=` can't separate connect from read, so we drive
    http.client directly: open the connection with the short connect budget,
    then widen the socket timeout to the long read budget before reading the
    response (a cold synth can take ~33s).
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(host, port, timeout=connect_to)
    try:
        conn.connect()                       # bounded by connect_to (fast-fail if down)
        conn.sock.settimeout(read_to)        # widen for the slow synth read
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                     headers={"Content-Type": "application/json", **headers})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        if resp.status >= 400:
            raise _RelayHTTPError(resp.status, raw[:200])
        return json.loads(raw)               # may raise ValueError on a non-JSON body
    finally:
        conn.close()


def _ha_state(entity_id: str, ha_base: str, ha_token: str) -> str | None:
    url = f"{ha_base}/api/states/{entity_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ha_token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8")).get("state")
    except Exception:
        return None


def _ha_cast(entity_id: str, audio_url: str, ha_base: str, ha_token: str) -> bool:
    url = f"{ha_base}/api/services/media_player/play_media"
    body = {
        "entity_id": entity_id,
        "media_content_id": audio_url,
        "media_content_type": cfg.ANNOUNCE_MEDIA_CONTENT_TYPE,
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return 200 <= resp.status < 300


def main() -> int:
    text = (os.environ.get("PX_ANNOUNCE_TEXT") or "").strip()
    dry = os.environ.get("PX_DRY", "0") != "0"
    private = os.environ.get("PX_ANNOUNCE_PRIVATE", "0") != "0"
    targets = _resolve_targets()

    if not text:
        return _emit({"status": "error", "error": "PX_ANNOUNCE_TEXT is required"})
    if not targets:
        return _emit({"status": "error", "error": "no allowed targets resolved"})

    if dry:
        return _emit({"status": "dry", "voice": cfg.ANNOUNCE_VOICE,
                      "targets": targets, "text": text[:cfg.ANNOUNCE_MAX_CHARS]})

    # Night silence applies to EVERY announce path (incl. user-initiated voice loop).
    # This is the single chokepoint all triggers pass through, so the gate lives here.
    if _is_night_silence():
        return _emit({"status": "suppressed", "reason": "night_silence",
                      "voice": cfg.ANNOUNCE_VOICE, "targets": targets})

    relay_token = os.environ.get("ANNOUNCE_RELAY_TOKEN", "")
    ha_token = os.environ.get("PX_HA_TOKEN", "")

    _set_exploring(True)
    try:
        # 1) synth on the relay
        try:
            result = _post_json(
                f"{cfg.ANNOUNCE_RELAY_URL}/announce",
                {"text": text[:cfg.ANNOUNCE_MAX_CHARS], "voice": cfg.ANNOUNCE_VOICE,
                 "cache": not private},
                {"Authorization": f"Bearer {relay_token}"},
                cfg.ANNOUNCE_CONNECT_TIMEOUT, cfg.ANNOUNCE_READ_TIMEOUT,
            )
        except _RelayHTTPError as e:
            return _emit({"status": "error", "error": f"relay {e.status}: {e.detail}"})
        except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError) as e:
            # OSError covers connect timeout/refused; ValueError covers a non-JSON body.
            return _emit({"status": "error", "error": f"relay unreachable: {e}"})

        audio_url = result.get("audio_url")
        if not audio_url:
            return _emit({"status": "error", "error": f"relay returned no audio_url: {result}"})

        # 2) cast to each resolved target (single in v1); skip unavailable
        cast_ok = []
        for entity in targets:
            state = _ha_state(entity, cfg.HA_BASE_URL, ha_token)
            if state == "unavailable":
                continue  # skip + log via payload below
            if state == "playing":
                pass  # destructive to current playback (documented v1 behavior)
            try:
                if _ha_cast(entity, audio_url, cfg.HA_BASE_URL, ha_token):
                    cast_ok.append(entity)
            except Exception:
                pass  # best-effort per target

        payload = {
            "status": "ok" if cast_ok else "error",
            "audio_url": audio_url,
            "targets": cast_ok,
            "voice": result.get("voice", cfg.ANNOUNCE_VOICE),
            "cached": result.get("cached"),
            "duration_s": result.get("duration_s"),
        }
        if not cast_ok:
            payload["error"] = "no target accepted the cast"
        try:
            update_session(
                fields={"last_action": "tool_announce"},
                history_entry={"event": "announce", "targets": cast_ok, "text": text[:80]},
            )
        except Exception:
            pass   # session bookkeeping must never break the announce contract
        return _emit(payload)
    finally:
        _set_exploring(False)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as e:   # never raises — always emit a JSON error line
        print(json.dumps({"status": "error", "error": f"tool-announce crashed: {e}"}))
        raise SystemExit(0)
PY
```

- [ ] **Step 3: Make executable and run the dry-run test**

```bash
chmod +x bin/tool-announce
python -m pytest tests/test_tools.py::test_tool_announce_dry_run -v
```

Expected: PASS

- [ ] **Step 4: Write mocked live-path tests (append to `tests/test_tools.py`)**

These mock the relay + HA by pointing the tool at a local `http.server` stub via env. Simplest reliable approach: a fixture that starts a `threading`-backed `http.server` capturing requests.

```python
import http.server
import json as _json
import threading


class _StubHandler(http.server.BaseHTTPRequestHandler):
    captured = []  # class-level capture: list of (method, path, body)

    def log_message(self, *a):  # silence
        pass

    def _send(self, code, obj):
        body = _json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # HA state lookups: /api/states/<entity>
        _StubHandler.captured.append(("GET", self.path, None))
        self._send(200, {"state": "idle"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = _json.loads(self.rfile.read(length) or b"{}")
        _StubHandler.captured.append(("POST", self.path, body))
        if self.path.endswith("/announce"):
            self._send(200, {"audio_url": "http://192.168.1.171:7862/audio/abc123.wav",
                             "voice": "data", "cached": False, "duration_s": 1.2})
        else:  # HA play_media
            self._send(200, [{"entity_id": "media_player.nest_hub_max", "state": "playing"}])


def _start_stub():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_tool_announce_live_path_posts_relay_and_ha(isolated_project, monkeypatch):
    _StubHandler.captured = []
    srv = _start_stub()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "0"
    env["PX_ANNOUNCE_TEXT"] = "Dinner is ready"
    env["PX_BYPASS_SUDO"] = "1"
    # Point both relay and HA at the stub via spark_config override env (see Step 5 note).
    env["PX_ANNOUNCE_RELAY_URL"] = base
    env["PX_HA_BASE_URL"] = base
    env["ANNOUNCE_RELAY_TOKEN"] = "t"
    env["PX_HA_TOKEN"] = "t"
    env["PX_NIGHT_SILENCE_START_H"] = "99"   # force "never night" — deterministic
    env["PX_NIGHT_SILENCE_END_H"] = "0"
    try:
        stdout = run_tool(["bin/tool-announce"], env)
    finally:
        srv.shutdown()
    payload = parse_json(stdout)
    assert payload["status"] == "ok"
    assert payload["audio_url"].endswith("/audio/abc123.wav")
    assert payload["targets"] == ["media_player.nest_hub_max"]
    paths = [p for (_, p, _) in _StubHandler.captured]
    assert any(p.endswith("/announce") for p in paths)
    assert any("/api/services/media_player/play_media" in p for p in paths)


def test_tool_announce_suppressed_during_night_silence(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "0"
    env["PX_ANNOUNCE_TEXT"] = "Should not play"
    env["PX_NIGHT_SILENCE_START_H"] = "0"    # force "always night"
    env["PX_NIGHT_SILENCE_END_H"] = "24"
    # No relay/HA stub: if the gate is broken it'll error trying to reach the relay,
    # which is itself a failure — a working gate returns before any network egress.
    stdout = run_tool(["bin/tool-announce"], env)
    payload = parse_json(stdout)
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "night_silence"


def test_tool_announce_resolves_single_target_from_multiple(isolated_project):
    # Even if multiple allowed targets are requested, v1 casts to exactly one (echo).
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "1"
    env["PX_ANNOUNCE_TEXT"] = "hi"
    env["PX_ANNOUNCE_TARGETS"] = "media_player.nest_hub_max,media_player.nest_mini"
    payload = parse_json(run_tool(["bin/tool-announce"], env))
    assert payload["status"] == "dry"
    assert len(payload["targets"]) == 1
```

- [ ] **Step 5: Make relay/HA URLs env-overridable for the live test**

The live test needs to redirect `cfg.ANNOUNCE_RELAY_URL` and `cfg.HA_BASE_URL`. Add env overrides at the top of `bin/tool-announce`'s Python block (right after the `cfg` import) so tests can point at the stub without editing config:

```python
# Test/ops override hooks (production uses spark_config defaults). The tool runs
# as a fresh subprocess, so config can't be monkeypatched in-process — these env
# overrides are the test seam. Night-hour overrides make the clock-dependent gate
# deterministic in tests (e.g. START=99,END=0 -> never night; START=0,END=24 -> always).
if os.environ.get("PX_ANNOUNCE_RELAY_URL"):
    cfg.ANNOUNCE_RELAY_URL = os.environ["PX_ANNOUNCE_RELAY_URL"]
if os.environ.get("PX_HA_BASE_URL"):
    cfg.HA_BASE_URL = os.environ["PX_HA_BASE_URL"]
if os.environ.get("PX_NIGHT_SILENCE_START_H"):
    cfg.NIGHT_SILENCE_START_H = int(os.environ["PX_NIGHT_SILENCE_START_H"])
if os.environ.get("PX_NIGHT_SILENCE_END_H"):
    cfg.NIGHT_SILENCE_END_H = int(os.environ["PX_NIGHT_SILENCE_END_H"])
```

- [ ] **Step 6: Run all tool-announce tests**

Run: `python -m pytest tests/test_tools.py -k announce -v`
Expected: PASS (dry-run + live-path)

- [ ] **Step 7: Commit**

```bash
git add bin/tool-announce tests/test_tools.py
git commit -m "feat(announce): bin/tool-announce — relay synth + HA cast, yield_alive, dry-run"
```

---

## Task 8: Voice-loop wiring (`tool_announce`) + prompt/persona docs

**Files:**
- Modify: `src/pxh/voice_loop.py` (`ALLOWED_TOOLS` ~line 24-68, `TOOL_COMMANDS` ~line 70-114, `validate_action` ~line 595)
- Modify: `docs/prompts/claude-voice-system.md`, `docs/prompts/codex-voice-system.md`
- Test: `tests/test_voice_loop.py` (append)

**Interfaces:**
- Consumes: `spark_config.ANNOUNCE_MAX_CHARS`, `ANNOUNCE_ALLOWED_TARGETS`.
- Produces: `tool_announce` recognized by the loop; `validate_action({"tool":"tool_announce","params":{...}})` → `("tool_announce", {"PX_ANNOUNCE_TEXT":..., "PX_ANNOUNCE_TARGETS"?:...})`. Clamps text to `ANNOUNCE_MAX_CHARS`; rejects targets ∉ allowlist.

- [ ] **Step 1: Write the failing tests (append to `tests/test_voice_loop.py`)**

```python
from pxh.voice_loop import validate_action, ALLOWED_TOOLS, TOOL_COMMANDS, VoiceLoopError
import pytest


def test_tool_announce_registered():
    assert "tool_announce" in ALLOWED_TOOLS
    assert "tool_announce" in TOOL_COMMANDS


def test_validate_announce_clamps_text():
    tool, env = validate_action({"tool": "tool_announce", "params": {"text": "x" * 500}})
    assert tool == "tool_announce"
    assert len(env["PX_ANNOUNCE_TEXT"]) == 200  # ANNOUNCE_MAX_CHARS


def test_validate_announce_requires_text():
    with pytest.raises(VoiceLoopError):
        validate_action({"tool": "tool_announce", "params": {"text": "   "}})


def test_validate_announce_rejects_any_disallowed_target():
    # Mixed good+bad must RAISE, not silently drop the bad one.
    with pytest.raises(VoiceLoopError):
        validate_action({"tool": "tool_announce", "params": {
            "text": "hi", "targets": ["media_player.nest_hub_max", "media_player.evil"]}})


def test_validate_announce_rejects_all_bad_targets():
    with pytest.raises(VoiceLoopError):
        validate_action({"tool": "tool_announce", "params": {
            "text": "hi", "targets": ["media_player.evil"]}})


def test_validate_announce_single_target_from_allowed_list():
    # Multiple ALLOWED targets -> v1 takes exactly one (single-target).
    _, env = validate_action({"tool": "tool_announce", "params": {
        "text": "hi", "targets": ["media_player.nest_hub_max", "media_player.nest_mini"]}})
    assert env["PX_ANNOUNCE_TARGETS"] == "media_player.nest_hub_max"
```

Run: `python -m pytest tests/test_voice_loop.py -k announce -v`
Expected: FAIL — `tool_announce` not in `ALLOWED_TOOLS`.

- [ ] **Step 2: Register the tool**

In `src/pxh/voice_loop.py`, add `"tool_announce",` to the `ALLOWED_TOOLS` set (after `"tool_story",` in the cognitive-tools block, line ~67):

```python
    "tool_story",
    "tool_announce",
}
```

And add the matching entry to `TOOL_COMMANDS`. **Verified form:** neighbouring entries are bare `Path` values `BIN_DIR / "tool-x"` (NOT lists, NOT strings) — `BIN_DIR` is defined at the top of `voice_loop.py`. Add after the `"tool_story"` entry:

```python
    "tool_story":            BIN_DIR / "tool-story",
    "tool_announce":         BIN_DIR / "tool-announce",
}
```

- [ ] **Step 3: Add the `validate_action` branch**

Add an import near the top of `voice_loop.py` (with other `spark_config` references, or add one):

```python
from pxh.spark_config import ANNOUNCE_ALLOWED_TARGETS, ANNOUNCE_MAX_CHARS
```

Add a branch in `validate_action` (alongside the `tool_voice` branch, ~line 601):

```python
elif tool == "tool_announce":
    text = params.get("text")
    if not isinstance(text, str) or not text.strip():
        raise VoiceLoopError("tool_announce requires a non-empty text parameter")
    sanitized["PX_ANNOUNCE_TEXT"] = text.strip()[:ANNOUNCE_MAX_CHARS]
    raw_targets = params.get("targets") or []
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    # Reject ANY target outside the allowlist — the LLM must not drive arbitrary
    # HA entities (spec safety). Silent filtering would let a bad target slip past.
    bad = [t for t in raw_targets if t not in ANNOUNCE_ALLOWED_TARGETS]
    if bad:
        raise VoiceLoopError(f"tool_announce: disallowed target(s): {bad}")
    if raw_targets:
        # v1 is single-target (multiple distinct casts echo) — take the first.
        sanitized["PX_ANNOUNCE_TARGETS"] = raw_targets[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_voice_loop.py -k announce -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Document the tool in the prompt files**

In `docs/prompts/claude-voice-system.md` and `docs/prompts/codex-voice-system.md`, add `tool_announce` to the tool list with a one-line description and a JSON example, matching the existing entries' format:

```
- tool_announce — Speak an announcement aloud through the household Google Nest
  speakers in your `data` voice. Use ONLY when explicitly asked to announce /
  broadcast something to the room. Params: {"text": "...", "targets"?: [...]}.
  Example: {"tool": "tool_announce", "params": {"text": "Dinner is ready"}}
```

(Persona docs `persona-gremlin.md`/`persona-vixen.md` are intentionally **not** updated — announce is a SPARK capability in the `data` voice, not a GREMLIN/VIXEN jailbreak tool.)

- [ ] **Step 6: Run the full voice-loop + tools suite**

Run: `python -m pytest tests/test_voice_loop.py tests/test_tools.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/pxh/voice_loop.py docs/prompts/claude-voice-system.md docs/prompts/codex-voice-system.md tests/test_voice_loop.py
git commit -m "feat(announce): wire tool_announce into voice loop + validate_action + prompts"
```

---

## Task 9: Replace hardcoded night silence in `mind.py` with config bounds

**Files:**
- Modify: `src/pxh/mind.py` (night-silence block ~line 2871-2876; day/night split ~line 3334-3338)
- Test: `tests/test_mind.py` (append, or appropriate existing mind test file)

**Interfaces:**
- Consumes: `spark_config.NIGHT_SILENCE_START_H`, `NIGHT_SILENCE_END_H`.
- Produces: `_is_night_silence(hour:int)->bool` helper used by the expression suppressor; behavior identical to the old `hour >= 19 or hour < 7` but sourced from config.

- [ ] **Step 1: Write the failing test (append to `tests/test_mind.py`)**

```python
from pxh import mind


def test_is_night_silence_uses_config_bounds():
    assert mind._is_night_silence(19) is True
    assert mind._is_night_silence(23) is True
    assert mind._is_night_silence(6) is True
    assert mind._is_night_silence(7) is False
    assert mind._is_night_silence(12) is False
    assert mind._is_night_silence(18) is False
```

Run: `python -m pytest tests/test_mind.py::test_is_night_silence_uses_config_bounds -v`
Expected: FAIL — `mind._is_night_silence` undefined.

- [ ] **Step 2: Add the helper and import**

In `src/pxh/mind.py`, add the config import near the other `spark_config` imports:

```python
from pxh.spark_config import NIGHT_SILENCE_START_H, NIGHT_SILENCE_END_H
```

Add the helper near the `HOBART_TZ`/`OBI_DAY_*` constants (~line 154):

```python
def _is_night_silence(hour: int) -> bool:
    """True during the unconditional night-silence window (Hobart hour-of-day)."""
    return hour >= NIGHT_SILENCE_START_H or hour < NIGHT_SILENCE_END_H
```

- [ ] **Step 3: Replace the hardcoded suppressor (line ~2871-2876)**

Change:

```python
_night_hour = dt.datetime.now(HOBART_TZ).hour
if _night_hour >= 19 or _night_hour < 7:
    if action not in ("wait", "remember"):
        log(f"expression: suppressed {action} — night silence (19:00–07:00)")
        return
```

to:

```python
_night_hour = dt.datetime.now(HOBART_TZ).hour
if _is_night_silence(_night_hour):
    if action not in ("wait", "remember"):
        log(f"expression: suppressed {action} — night silence "
            f"({NIGHT_SILENCE_START_H:02d}:00–{NIGHT_SILENCE_END_H:02d}:00)")
        return
```

Also update the day/night phrase split (line ~3334-3338) for DRY:

```python
if isinstance(phrases, dict):
    hour = dt.datetime.now(HOBART_TZ).hour
    slot = "night" if _is_night_silence(hour) else "day"
    phrases = phrases.get(slot, phrases.get("day", list(phrases.values())[0]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mind.py::test_is_night_silence_uses_config_bounds -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxh/mind.py tests/test_mind.py
git commit -m "refactor(mind): source night-silence bounds from spark_config"
```

---

## Task 10: `announce` cognitive action in `mind.py` (non-blocking dispatch)

**Files:**
- Modify: `src/pxh/mind.py` (`VALID_ACTIONS` ~line 416; expression action handler; add `_dispatch_announce`)
- Test: `tests/test_mind.py` (append)

**Interfaces:**
- Consumes: `spark_config.ANNOUNCE_ENABLED`, `bin/tool-announce`.
- Produces: `_dispatch_announce(text:str, private:bool=False)->None` (fire-and-forget `subprocess.Popen`, gated by `ANNOUNCE_ENABLED`); `"announce"` added to `VALID_ACTIONS`; expression handler branch for `action == "announce"`. Inherits all existing suppressors (night silence at Task 9 returns before the handler) and the expression cooldown.

- [ ] **Step 1: Write the failing tests (append to `tests/test_mind.py`)**

```python
import subprocess
from pxh import mind


def test_announce_in_valid_actions():
    assert "announce" in mind.VALID_ACTIONS


def test_dispatch_announce_disabled_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(mind.spark_config, "ANNOUNCE_ENABLED", False)
    monkeypatch.setattr(mind.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    mind._dispatch_announce("hello")
    assert calls == []


def test_dispatch_announce_enabled_fires_popen_nonblocking(monkeypatch):
    calls = []

    class _FakePopen:
        def __init__(self, *a, **k):
            calls.append((a, k))

    monkeypatch.setattr(mind.spark_config, "ANNOUNCE_ENABLED", True)
    monkeypatch.setattr(mind.subprocess, "Popen", _FakePopen)
    mind._dispatch_announce("hello", private=True)
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["env"]["PX_ANNOUNCE_TEXT"] == "hello"
    assert kwargs["env"]["PX_ANNOUNCE_PRIVATE"] == "1"
```

Run: `python -m pytest tests/test_mind.py -k announce -v`
Expected: FAIL — `announce` not in `VALID_ACTIONS` / `_dispatch_announce` undefined.

- [ ] **Step 2: Add `announce` to `VALID_ACTIONS`**

In `src/pxh/mind.py`, line ~416-422:

```python
VALID_ACTIONS = {"wait", "greet", "greet_arrival", "comment", "remember", "look_at",
                 "weather_comment", "scan", "explore",
                 "play_sound", "photograph", "emote", "look_around",
                 "time_check", "calendar_check", "morning_fact",
                 "introspect", "evolve",
                 "research", "compose", "self_debug", "blog_essay",
                 "message_obi", "announce"}
```

- [ ] **Step 3: Add the module import and `_dispatch_announce` helper**

`mind.py` currently imports config **by name** (`from pxh.spark_config import (...)`), so `mind.spark_config` does not exist — the Task-10 test monkeypatches `mind.spark_config`, and `_dispatch_announce` reads `spark_config.ANNOUNCE_ENABLED` (a mutable flag), so add the **module** import alongside the existing one:

```python
from pxh import spark_config            # module handle (flag read at call time, monkeypatchable)
```

`mind.py` already defines `BIN_DIR = PROJECT_ROOT / "bin"` (line 49) and imports `subprocess` — reuse both. Add near the other expression helpers:

```python
def _dispatch_announce(text: str, private: bool = False) -> None:
    """Fire bin/tool-announce off the critical path (non-blocking). No-op if disabled."""
    if not spark_config.ANNOUNCE_ENABLED:
        return
    if not text or not text.strip():
        return
    env = os.environ.copy()
    env["PX_ANNOUNCE_TEXT"] = text.strip()[:spark_config.ANNOUNCE_MAX_CHARS]
    if private:
        env["PX_ANNOUNCE_PRIVATE"] = "1"
    try:
        subprocess.Popen([str(BIN_DIR / "tool-announce")], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("expression: announce dispatched (non-blocking)")
    except Exception as e:
        log(f"expression: announce dispatch failed: {e}")
```

- [ ] **Step 4: Add the `announce` action branch in the expression handler**

In the expression action dispatch (the `if action == ... elif ...` chain, near the `message_obi` branch ~line 3270), add:

```python
elif action == "announce":
    if not text:
        log("expression: announce has no text — skipping")
    else:
        _dispatch_announce(text)
```

(This sits after the night-silence suppressor from Task 9, so `announce` is automatically suppressed 19:00–07:00 and by any earlier quiet/school/bedtime returns, and is rate-limited by the existing expression cooldown.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_mind.py -k announce -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/pxh/mind.py tests/test_mind.py
git commit -m "feat(announce): add announce cognitive action (non-blocking dispatch)"
```

---

## Task 11: `message_obi` fires a private announce

**Files:**
- Modify: `src/pxh/mind.py` (`message_obi` handler ~line 3270-3300)
- Test: `tests/test_mind.py` (append)

**Interfaces:**
- Consumes: `_dispatch_announce` (Task 10).
- Produces: `_emit_message_obi(text:str)->None` — the existing message_obi backoff+write logic extracted from `expression()` into a directly-testable callable, with the new `_dispatch_announce(text, private=True)` fire added after the chat entry is written. `expression()`'s `message_obi` branch becomes a one-line call.

**Why extract:** the message_obi logic currently lives inline in `expression()`'s `if/elif` chain, behind upstream gates (night silence, obi_mode). Driving `expression()` in a unit test means simulating every gate — brittle. Lifting the block into `_emit_message_obi(text)` lets the test call it directly. This is the refactor both codex and hermes recommended.

- [ ] **Step 1: Write the failing test (append to `tests/test_mind.py`)**

```python
def test_emit_message_obi_fires_private_announce(monkeypatch):
    fired = []
    monkeypatch.setattr(mind, "_dispatch_announce",
                        lambda text, private=False: fired.append((text, private)))
    # Stub the obi-chat IO so the helper reaches the "write entry" path (not suppressed).
    monkeypatch.setattr(mind, "_read_obi_chat_timestamps", lambda: (0.0, 0.0))
    monkeypatch.setattr(mind, "_read_obi_chat_meta", lambda: {})
    monkeypatch.setattr(mind, "_append_obi_chat", lambda entry: None)
    monkeypatch.setattr(mind, "_write_obi_chat_meta", lambda meta: None)

    mind._emit_message_obi("Obi, are you there?")
    assert fired == [("Obi, are you there?", True)]


def test_emit_message_obi_suppressed_no_announce(monkeypatch):
    fired = []
    monkeypatch.setattr(mind, "_dispatch_announce",
                        lambda text, private=False: fired.append((text, private)))
    # last_spark_ts > last_obi_ts and recent -> awaiting reply within backoff -> suppressed.
    import time as _t
    now = _t.time()
    monkeypatch.setattr(mind, "_read_obi_chat_timestamps", lambda: (now, 0.0))
    monkeypatch.setattr(mind, "_read_obi_chat_meta", lambda: {"backoff_s": 9999})
    monkeypatch.setattr(mind, "_append_obi_chat", lambda entry: None)
    monkeypatch.setattr(mind, "_write_obi_chat_meta", lambda meta: None)

    mind._emit_message_obi("still waiting")
    assert fired == []   # no announce when the nudge is backoff-suppressed
```

Run: `python -m pytest tests/test_mind.py -k emit_message_obi -v`
Expected: FAIL — `mind._emit_message_obi` undefined.

- [ ] **Step 2: Extract the helper and add the announce fire**

In `src/pxh/mind.py`, **move** the existing `message_obi` body (the block currently at ~lines 3271-3300) into a new module-level helper, parameterized by `text`, and add the announce line after `_append_obi_chat(entry)`. The logic is unchanged except the new `_dispatch_announce` call; suppression `return`s become early returns from the helper:

```python
def _emit_message_obi(text: str) -> None:
    """Write a message_obi nudge (with exponential backoff) and announce it to Obi."""
    if not text:
        log("expression: message_obi has no text — skipping")
        return
    now_ts = time.time()
    last_spark_ts, last_obi_ts = _read_obi_chat_timestamps()
    meta = _read_obi_chat_meta()
    backoff_s = float(meta.get("backoff_s", OBI_CHAT_BASE_BACKOFF_S))
    if last_obi_ts > last_spark_ts > 0:                 # Obi replied -> reset backoff
        backoff_s = OBI_CHAT_BASE_BACKOFF_S
    awaiting = last_spark_ts > last_obi_ts and last_spark_ts > 0
    if awaiting:
        elapsed = now_ts - last_spark_ts
        if elapsed < backoff_s:
            log(f"expression: suppressed message_obi — awaiting reply "
                f"(backoff {backoff_s:.0f}s, elapsed {elapsed:.0f}s)")
            return                                       # suppressed -> NO announce
        backoff_s = min(backoff_s * 2, OBI_CHAT_MAX_BACKOFF_S)
        log(f"expression: message_obi nudge — backoff doubled to {backoff_s:.0f}s")
    else:
        backoff_s = OBI_CHAT_BASE_BACKOFF_S
    msg_id = format(int(now_ts * 1000) % 0xFFFFFFFF, "08x")
    entry = {"id": msg_id, "ts": utc_timestamp(), "role": "spark", "text": text[:500]}
    _append_obi_chat(entry)
    _write_obi_chat_meta({"backoff_s": backoff_s, "last_spark_ts": now_ts})
    _dispatch_announce(text, private=True)               # Obi HEARS the DM in the data voice
    log(f"expression: message_obi written (id={msg_id})")
```

Then replace the inline branch in `expression()` (lines ~3270-3300) with:

```python
        elif action == "message_obi":
            _emit_message_obi(text)
```

> Lift the real block from the file — the code above mirrors it, but if the file differs, the file is authoritative; only the added `_dispatch_announce(...)` line and the `def`/`return` wrapping are new.

- [ ] **Step 3: Run tests to verify they pass**

Run: `python -m pytest tests/test_mind.py -k emit_message_obi -v`
Expected: PASS (2 tests)

- [ ] **Step 4: Run the full mind suite**

Run: `python -m pytest tests/test_mind.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxh/mind.py tests/test_mind.py
git commit -m "feat(announce): message_obi fires a private announce so Obi hears it"
```

---

## Task 12: Full-suite gate + rollout notes

**Files:**
- Modify: `CLAUDE.md` (add an "Announce Pipeline" subsection under Architecture)
- No code changes; this task verifies the whole pipeline and documents rollout.

- [ ] **Step 1: Run the entire Pi test suite**

Run: `python -m pytest -m "not live" -q`
Expected: PASS (existing 716 + new announce tests). Investigate any failure before proceeding.

- [ ] **Step 2: Run the relay suite**

Run: `cd m5/announce-relay && python -m pytest -q`
Expected: PASS (store + synth + app)

- [ ] **Step 3: Document the pipeline in `CLAUDE.md`**

Add a concise "### Announce Pipeline (tool-announce + M5 relay)" subsection under Architecture summarizing: M5 relay on 7862 fronting localhost afterwords; IP-not-mDNS rule; `data` voice; single-target v1; night silence via `spark_config`; `ANNOUNCE_ENABLED` flag; private `priv/` namespace + 3 min TTL for `message_obi` audio. Match the density of neighbouring subsections.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document announce pipeline in CLAUDE.md"
```

- [ ] **Step 5: Rollout (manual, gated — do NOT flip until verified live)**

In order:
1. Deploy relay to M5: `scp -r m5/announce-relay <m5>:~/ && ssh <m5> 'cd announce-relay && cp .env.example .env'`, edit `.env` (set `ANNOUNCE_RELAY_TOKEN`, `RELAY_DATA_DIR`), then `./install.sh`. Confirm `curl http://192.168.1.171:7862/health` from the Pi returns `status: ok` (proves LAN reachability by IP).
2. End-to-end manual smoke from the Pi with `ANNOUNCE_ENABLED` still `False` but tool runnable directly:
   ```bash
   ANNOUNCE_RELAY_TOKEN=<token> PX_HA_TOKEN=<token> \
     PX_ANNOUNCE_TEXT="Announce pipeline online" PX_DRY=0 bin/tool-announce
   ```
   Confirm the Nest audibly plays the `data` voice. If silent, revisit G1 (transcode) / G2 (entity + media type).
3. Pull Spark code to the Pi (`git pull`). Restart px-mind (`sudo systemctl restart px-mind`) for the new `announce` action. The voice-loop tool needs no restart (fresh subprocess per wake event).
4. Flip `ANNOUNCE_ENABLED = True` in `src/pxh/spark_config.py`, commit, pull to Pi, restart px-mind.

```bash
# After step 4:
git add src/pxh/spark_config.py
git commit -m "chore(announce): enable announce pipeline (relay live on M5)"
```

---

## Self-Review

**Spec coverage check** (each spec section → task):

- Goal / `data` voice over Nest → Tasks 2-7 (relay + tool). ✓
- Constraint #1 (afterwords localhost; relay is LAN front door) → relay binds 7862, afterwords stays 127.0.0.1:7860 (Task 6 plist / Task 2 config). ✓
- Constraint #2 (pre-synthesize, no live stream) → relay synth-to-file (Task 4). ✓
- Constraint #3 (no server cache / non-deterministic) → content-addressed cache at relay (Task 2/4). ✓
- Constraint #4 (`data` GET /synthesize?...&lang=en → audio/wav) → `synth.synthesize` (Task 3). ✓
- Constraint #5 (`/audio` unauth) → Task 5. ✓
- Constraint #6 (IP not mDNS) → `PUBLIC_BASE_URL` + config IP + Global Constraints (Tasks 1, 2). ✓
- Constraint #7 (single target / echo) → `ANNOUNCE_DEFAULT_TARGETS` len 1, validate_action filters (Tasks 1, 8). ✓
- Constraint #8 (`_2` ambiguity) → Phase 0 G2 pins entity. ✓
- Gates G1/G2 → Phase 0. ✓
- Relay `/announce` (auth, voice allowlist, size, rate, public/private, per-key lock, validate-before-cache, atomic write) → Tasks 2-4. ✓
- Relay `/audio` (regex, no traversal) → Task 5. ✓
- Relay `/health` → Task 4. ✓
- Janitor (public 7d / private 3min, startup+interval) → Tasks 2, 5. ✓
- Keep-warm default OFF → not implemented (YAGNI for v1; `RELAY_KEEP_WARM` deliberately omitted — add later). Noted as out-of-scope-for-now. ✓
- `bin/tool-announce` (stdlib, yield_alive + exploring.json, split connect/read timeout, target resolution, unavailable-skip, destructive-on-playing, update_session, never raises, PX_DRY) → Task 7. ✓
- Config block → Task 1. ✓
- Triggers: voice loop (Task 8), px-mind announce (Task 10), message_obi private announce (Task 11). ✓
- Night silence via config across all paths → Task 9 + inherited by Tasks 10/11. ✓
- Testing matrix → covered per task. ✓
- Deployment/rollout → Tasks 6, 12. ✓

**Gaps deliberately deferred (per spec "Out of scope"):** listen-via-Nest, `message_adrian`, GLaDOS-voice-on-Nest, multi-room sync / speaker group, save/restore of prior playback, keep-warm. None block v1.

**Type consistency:** `_dispatch_announce(text, private=False)` used identically in Tasks 10 & 11. `announce_key`→`public_path` 16-hex consistent (Task 2 tests + app). `ANNOUNCE_MEDIA_CONTENT_TYPE` defined Task 1, consumed Task 7. `PX_ANNOUNCE_TEXT`/`PX_ANNOUNCE_TARGETS`/`PX_ANNOUNCE_PRIVATE` env names consistent across tool (7), voice_loop (8), mind (10/11).

---

## QA revisions applied (hermes / codex / agy)

Multi-model QA on the first draft. Mapping reviewer → fix:

**Blockers (all fixed):**
- **Janitor leak / deprecated `on_event`** (codex MAJOR, hermes BLOCKER, agy BLOCKER — unanimous) → Task 5 rewritten to a `lifespan` context manager with a tracked, cancelled task.
- **Split connect/read timeout was fake** — `socket.create_connection` preflight then `urlopen(timeout=70)` still allows a 70 s connect hang (codex BLOCKER, hermes BLOCKER, agy MINOR) → Task 7 `_post_json` rewritten on `http.client`: connect under 5 s, then widen socket to 70 s for the read. Also removes the preflight-`.close()` that caused `ConnectionReset` noise in the stub test (agy).
- **Night silence only gated mind.expression(), not the voice-loop path** (codex BLOCKER) → Task 7 enforces night silence **inside `bin/tool-announce`** (the single chokepoint every trigger passes through), with a deterministic env-override test seam.
- **launchd plist ignored most of `.env`** (codex BLOCKER) → Task 6 adds `run.sh` that sources the full `.env`; plist execs it; `.env` is the single source of truth.

**Majors (fixed):**
- **Synth not serialized across distinct keys** (codex) → Task 4 global `_synth_gate` around every synth (afterwords is single-model).
- **`sanitize_text` merged words across newlines; tests invalid** (agy, hermes) → Task 2 maps control/emoji → space (not drop); sanitize tests made property-based.
- **`_rate` global bled across tests** (codex) → Task 4 `client` fixture clears it.
- **Multi-target allowed despite single-target v1** (codex) → Task 7 `_resolve_targets` + Task 8 `validate_action` clamp to one.
- **`validate_action` silently filtered disallowed targets** (codex) → Task 8 now **raises** on any disallowed target.
- **Task 11 test called nonexistent `_handle_expression_action`** (codex, hermes, agy) → Task 11 extracts `_emit_message_obi(text)` and tests that.
- **`mind.spark_config` not importable for monkeypatch** (codex) → Task 10 adds `from pxh import spark_config`.
- **`tool-announce` not truly "never raises"** (codex, agy) → Task 7 broad guard around `main()`, `ValueError`/`HTTPError`/4xx-body handling, `update_session` wrapped.
- **`_dispatch_announce` rebuilt `PROJECT_ROOT/bin`; `TOOL_COMMANDS` value was a list** (hermes, agy) → use existing `BIN_DIR`; `TOOL_COMMANDS` value is a bare `Path` (verified against source).
- **G2 placeholders could ship unsubstituted** (codex) → Task 1 STOP callout; test asserts shape only.

**Minors (fixed):** weak `_is_wav` → `wave.open` parse (hermes, Task 3); unbounded `_locks` → `_prune_locks` in janitor (agy MAJOR / codex MINOR, Task 2); brittle `private_path` inequality test → format assertion (hermes, Task 2).

**Reviewed, intentionally not changed:** hermes flagged the public-cache `cached` flag as inverted — re-read confirms it was correct (cache hit → `True`, only the synthesizing request sets `False`); Task 4 code restructured for clarity to defuse the ambiguity. Synchronous FastAPI route under threadpool (hermes MAJOR) is acceptable — the global synth gate bounds real concurrency to 1; home-LAN load won't exhaust the 40-thread pool. `ping()` probing the afterwords root (codex MINOR) is fine — any `<500` means reachable, which is the intent. `python -` vs `/usr/bin/python3` in the heredoc (codex MINOR) matches the working `tool-describe-scene` sibling (venv python is correct for a stdlib+pxh tool with no GPIO deps).
