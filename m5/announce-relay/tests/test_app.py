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
