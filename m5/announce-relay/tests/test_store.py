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
