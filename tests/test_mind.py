"""Tests for night-silence helper and announce action in pxh.mind."""
import subprocess
from pxh import mind


def test_is_night_silence_uses_config_bounds():
    assert mind._is_night_silence(19) is True
    assert mind._is_night_silence(23) is True
    assert mind._is_night_silence(6) is True
    assert mind._is_night_silence(7) is False
    assert mind._is_night_silence(12) is False
    assert mind._is_night_silence(18) is False


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
