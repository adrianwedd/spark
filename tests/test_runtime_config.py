"""Tests for pxh.runtime_config."""
from __future__ import annotations
import importlib
import pytest


def test_runtime_config_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    import pxh.runtime_config as rc
    importlib.reload(rc)
    assert rc.load() == {}
    rc.update({"mind_backend": "ollama"})
    assert rc.load()["mind_backend"] == "ollama"


def test_runtime_config_rejects_unknown_key(monkeypatch, tmp_path):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    import pxh.runtime_config as rc
    importlib.reload(rc)
    with pytest.raises(ValueError):
        rc.update({"evil": "x"})
