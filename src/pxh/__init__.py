"""Shared utilities for PiCar-X hacking helpers.

Imports are lazy to avoid pulling in filelock (pxh.state) when only lightweight
modules like pxh.race, pxh.utils, or pxh.time are needed — bin scripts run under
/usr/bin/python3 which may not have filelock installed.
"""


def __getattr__(name: str):
    """Lazy re-exports — only imported on first access."""
    _state_names = {"load_session", "save_session", "update_session", "ensure_session"}
    if name in _state_names:
        from .state import load_session, save_session, update_session, ensure_session  # noqa: F401
        return locals()[name]
    if name == "log_event":
        from .logging import log_event
        return log_event
    if name == "utc_timestamp":
        from .time import utc_timestamp
        return utc_timestamp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "load_session",
    "save_session",
    "update_session",
    "ensure_session",
    "log_event",
    "utc_timestamp",
]
