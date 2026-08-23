"""Tests for pxh.speaker_amp — the shared GPIO20 enable-speaker helper.

Covers the two failure modes this module exists to fix: px-wake-listen paying
the full enable_speaker subprocess cost on every chime even though GPIO20 is
already latched high, and tool-voice's prior unbounded (no-timeout) call.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from pxh import speaker_amp


def _pinctrl_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["pinctrl", "get", "20"], returncode=returncode, stdout=stdout, stderr="")


def test_is_speaker_enabled_true_when_pinctrl_reports_output_high():
    with patch("subprocess.run", return_value=_pinctrl_result("20: op -- pd | hi // GPIO20 = output")):
        assert speaker_amp.is_speaker_enabled() is True


def test_is_speaker_enabled_false_when_pinctrl_reports_low():
    with patch("subprocess.run", return_value=_pinctrl_result("20: op -- pd | lo // GPIO20 = output")):
        assert speaker_amp.is_speaker_enabled() is False


def test_is_speaker_enabled_none_when_pinctrl_missing():
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        assert speaker_amp.is_speaker_enabled() is None


def test_ensure_speaker_enabled_skips_expensive_path_when_already_high():
    logs = []
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _pinctrl_result("20: op -- pd | hi // GPIO20 = output")

    with patch("subprocess.run", side_effect=fake_run):
        result = speaker_amp.ensure_speaker_enabled(log=logs.append)

    assert result is True
    assert len(calls) == 1  # only the pinctrl query — no enable subprocess spawned
    assert any("already enabled" in m for m in logs)


def test_ensure_speaker_enabled_times_out_bounded_when_pin_is_low():
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["pinctrl", "get"]:
            return _pinctrl_result("20: op -- pd | lo // GPIO20 = output")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    logs = []
    with patch("subprocess.run", side_effect=fake_run):
        with patch("pxh.speaker_amp.time.monotonic", side_effect=[0.0, 0.0, 3.0]):
            result = speaker_amp.ensure_speaker_enabled(timeout_s=3.0, log=logs.append)

    assert result is False
    assert any("timed out" in m for m in logs)


def test_ensure_speaker_enabled_prefixes_sudo_when_requested():
    seen_cmds = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(cmd)
        if cmd[:2] == ["pinctrl", "get"]:
            return _pinctrl_result("20: op -- pd | lo // GPIO20 = output")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", side_effect=fake_run):
        result = speaker_amp.ensure_speaker_enabled(sudo=True, log=lambda _m: None)

    assert result is True
    enable_cmd = seen_cmds[-1]
    assert enable_cmd[:2] == ["sudo", "-n"]
