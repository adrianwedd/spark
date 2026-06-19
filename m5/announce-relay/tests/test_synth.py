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
