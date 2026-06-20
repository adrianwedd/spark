"""Tests for wake_utils: effective_onset_rms."""


def test_effective_onset_rms_whisper_when_asleep(monkeypatch):
    from pxh.wake_utils import effective_onset_rms
    monkeypatch.delenv("PX_SPEECH_ONSET_RMS", raising=False)
    monkeypatch.delenv("PX_WHISPER_ONSET_RMS", raising=False)
    assert effective_onset_rms(False) == 800
    assert effective_onset_rms(True) == 450


def test_effective_onset_rms_env_override(monkeypatch):
    from pxh.wake_utils import effective_onset_rms
    monkeypatch.setenv("PX_WHISPER_ONSET_RMS", "300")
    assert effective_onset_rms(True) == 300
