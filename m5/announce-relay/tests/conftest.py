import sys
from pathlib import Path

# Make the announce_relay package importable when running pytest from this dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from announce_relay import config


@pytest.fixture(autouse=True)
def tmp_dirs(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    priv = tmp_path / "priv"
    cache.mkdir()
    priv.mkdir()
    monkeypatch.setattr(config, "CACHE_DIR", cache)
    monkeypatch.setattr(config, "PRIV_DIR", priv)
    monkeypatch.setattr(config, "RELAY_TOKEN", "test-token")
    return {"cache": cache, "priv": priv}
