"""Env-driven settings for the announce relay. Loaded once at import."""
import os
from pathlib import Path


def _csv(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


RELAY_TOKEN        = os.environ.get("ANNOUNCE_RELAY_TOKEN", "")
AFTERWORDS_URL     = os.environ.get("AFTERWORDS_URL", "http://127.0.0.1:7860")
PUBLIC_BASE_URL    = os.environ.get("RELAY_PUBLIC_BASE_URL", "http://192.168.1.171:7862")

DATA_DIR           = Path(os.environ.get("RELAY_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
CACHE_DIR          = DATA_DIR / "cache"
PRIV_DIR           = DATA_DIR / "priv"

ALLOWED_VOICES     = _csv("RELAY_ALLOWED_VOICES", "data")
MAX_TEXT_BYTES     = int(os.environ.get("RELAY_MAX_TEXT_BYTES", "600"))
RATE_LIMIT_PER_MIN = int(os.environ.get("RELAY_RATE_LIMIT_PER_MIN", "30"))
CACHE_TTL_DAYS     = float(os.environ.get("RELAY_CACHE_TTL_DAYS", "7"))
PRIVATE_TTL_MIN    = float(os.environ.get("RELAY_PRIVATE_TTL_MIN", "3"))
JANITOR_INTERVAL_S = int(os.environ.get("RELAY_JANITOR_INTERVAL_S", "300"))
SYNTH_TIMEOUT      = float(os.environ.get("RELAY_SYNTH_TIMEOUT", "60"))
