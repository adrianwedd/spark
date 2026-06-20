"""Announce relay: synth text on M5, cache, serve unauthenticated for Chromecast."""
import asyncio
import contextlib
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


@app.get("/audio/{name}")
def audio(name: str):
    if not _AUDIO_RE.match(name):
        raise HTTPException(404, "not found")
    for d in (config.CACHE_DIR, config.PRIV_DIR):
        base = d.resolve()
        candidate = (d / name).resolve()
        if candidate.parent == base and candidate.is_file():
            # Enforce the private TTL at serve time too — the janitor only runs
            # every JANITOR_INTERVAL_S, so an expired DM clip would otherwise stay
            # fetchable (unauthenticated) for minutes past its stated TTL.
            if d == config.PRIV_DIR:
                age = time.time() - candidate.stat().st_mtime
                if age >= config.PRIVATE_TTL_MIN * 60:
                    with contextlib.suppress(OSError):
                        candidate.unlink()
                    raise HTTPException(404, "not found")
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
