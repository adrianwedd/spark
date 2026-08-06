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


def test_audio_expired_private_file_not_served(client, tmp_dirs, monkeypatch):
    # A private DM audio past its TTL must not be fetchable even if the janitor
    # (which only runs every JANITOR_INTERVAL_S) hasn't swept it yet.
    import os
    from announce_relay import config
    monkeypatch.setattr(config, "PRIVATE_TTL_MIN", 3)
    body = client.post("/announce", json={"text": "the secret", "cache": False}, headers=AUTH).json()
    name = body["audio_url"].rsplit("/", 1)[-1]
    path = tmp_dirs["priv"] / name
    assert path.is_file()
    old = path.stat().st_mtime - (config.PRIVATE_TTL_MIN * 60 + 1)
    os.utime(path, (old, old))
    r = client.get(f"/audio/{name}")
    assert r.status_code == 404


def test_audio_fresh_private_file_served(client):
    body = client.post("/announce", json={"text": "still fresh", "cache": False}, headers=AUTH).json()
    name = body["audio_url"].rsplit("/", 1)[-1]
    r = client.get(f"/audio/{name}")
    assert r.status_code == 200
    assert r.content[:4] == b"RIFF"


def test_audio_answers_head(client):
    # Chromecast preflights with HEAD; 405 makes the Nest load the cast and
    # then never start playing.
    body = client.post("/announce", json={"text": "head check"}, headers=AUTH).json()
    name = body["audio_url"].rsplit("/", 1)[-1]
    r = client.head(f"/audio/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content == b""


def test_startup_runs_janitor(monkeypatch):
    from announce_relay import app as appmod
    calls = []
    monkeypatch.setattr(appmod.store, "run_janitor", lambda now=None: calls.append(1) or 0)
    with TestClient(appmod.app):  # triggers startup event
        pass
    assert calls  # janitor ran at least once on startup


from announce_relay import config

CARD = {"headline": "8pm dose", "body": "Time for your meds.", "variant": "meds"}


@pytest.fixture()
def card_client(client, monkeypatch, tmp_dirs):
    """Endpoint-contract client with the render/mux toolchain stubbed out.

    These tests assert the HTTP contract, not that Pillow and ffmpeg work —
    that is tests/test_card.py and tests/test_video.py. Stubbing keeps them
    fast and keeps a host without ffmpeg from failing contract tests.
    """
    from announce_relay import app as appmod

    def fake_render(headline, body, variant, when=None):
        p = tmp_dirs["priv"] / "stub.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        return p

    def fake_mux(png, wav, tail_s=1.5):
        p = tmp_dirs["priv"] / f"{'a' * 32}.mp4"
        p.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return p

    monkeypatch.setattr(appmod.card, "render_card", fake_render)
    monkeypatch.setattr(appmod.video, "mux", fake_mux)
    return client


def test_card_requires_auth(client):
    r = client.post("/card", json=CARD)
    assert r.status_code == 401


def test_card_returns_video_url_and_kind(card_client):
    r = card_client.post("/card", json=CARD, headers=AUTH)
    assert r.status_code == 200
    b = r.json()
    assert b["kind"] == "video"
    assert "/video/" in b["url"]
    # Derive from config — RELAY_PUBLIC_BASE_URL differs between dev and the box.
    assert b["url"].startswith(config.PUBLIC_BASE_URL)
    assert b["duration_s"] > 0
    assert b["variant"] == "meds"


def test_card_duration_includes_the_cast_tail(card_client):
    from announce_relay import video
    r = card_client.post("/card", json=CARD, headers=AUTH)
    b = r.json()
    assert b["kind"] == "video"
    # 0.1s of synthesized speech in the stub + the tail.
    assert b["duration_s"] >= video.TAIL_S


def test_card_rejects_disallowed_voice(client):
    r = client.post("/card", json={**CARD, "voice": "nope"}, headers=AUTH)
    assert r.status_code == 400


def test_card_rejects_empty_after_sanitization(client):
    r = client.post("/card", json={"headline": "***", "body": "***"}, headers=AUTH)
    assert r.status_code == 400


def test_card_falls_back_to_audio_when_render_fails(client, monkeypatch):
    from announce_relay import app as appmod, card

    def boom(*a, **kw):
        raise card.CardError("no font")
    monkeypatch.setattr(appmod.card, "render_card", boom)

    r = client.post("/card", json=CARD, headers=AUTH)
    assert r.status_code == 200          # a dose reminder must not be lost
    b = r.json()
    assert b["kind"] == "audio"
    assert "/audio/" in b["url"]
    assert b["duration_s"] > 0


def test_card_falls_back_to_audio_when_mux_fails(client, monkeypatch):
    from announce_relay import app as appmod, video

    def boom(*a, **kw):
        raise video.MuxError("no ffmpeg")
    monkeypatch.setattr(appmod.video, "mux", boom)

    r = client.post("/card", json=CARD, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["kind"] == "audio"


def test_card_falls_back_on_unexpected_exception(client, monkeypatch):
    # Pillow raises a wide, version-dependent set; none of it may 500.
    from announce_relay import app as appmod

    def boom(*a, **kw):
        raise RuntimeError("something exotic")
    monkeypatch.setattr(appmod.card, "render_card", boom)

    r = client.post("/card", json=CARD, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["kind"] == "audio"


def test_video_route_answers_head(card_client):
    # Chromecast preflights with HEAD; a 405 makes the cast load and never start.
    name = card_client.post("/card", json=CARD, headers=AUTH).json()["url"].rsplit("/", 1)[-1]
    assert card_client.head(f"/video/{name}").status_code == 200
    r = card_client.get(f"/video/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"


def test_video_route_rejects_traversal(card_client):
    assert card_client.get("/video/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert card_client.get("/video/nope.mp4").status_code == 404


def test_health_reports_allowed_voices(client):
    # Lets a restart be verified without reading .env (which holds the token).
    b = client.get("/health").json()
    assert isinstance(b["voices"], list)
    assert set(b["voices"]) == set(config.ALLOWED_VOICES)
