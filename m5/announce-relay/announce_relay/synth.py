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
