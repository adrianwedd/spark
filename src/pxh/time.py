from __future__ import annotations

import datetime as _dt


def utc_timestamp() -> str:
    """Return an ISO 8601 timestamp (UTC) with second precision."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_time_line(tz_name: str = "Australia/Hobart",
                    now: _dt.datetime | None = None) -> str:
    """Human-readable current local time, for prompt injection (#301).

    The voice prompt is full of UTC 'Z' timestamps (session history,
    conversation buffer, thoughts) and carried no local-time anchor at all,
    so the model read 02:08Z as 2 AM wall-clock and told Obi to go to bed
    at midday. This line is the anchor.
    """
    from zoneinfo import ZoneInfo

    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    local = now.astimezone(ZoneInfo(tz_name))
    offset = local.strftime("%z")  # e.g. +1000
    stamp = local.strftime("%A %d %B %Y, %I:%M %p").replace(" 0", " ").lstrip("0")
    return f"{stamp} ({tz_name}, UTC{offset[:3]}:{offset[3:]})"
