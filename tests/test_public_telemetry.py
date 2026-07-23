import datetime as dt

from pxh.public_telemetry import (
    ACTIVITY_DELAY_S,
    delayed_activity,
    project_history,
    public_weather_summary,
)


def _ts(epoch: float) -> str:
    return dt.datetime.fromtimestamp(
        epoch, tz=dt.timezone.utc
    ).isoformat().replace("+00:00", "Z")


def test_delayed_activity_ignores_live_presence():
    now = 2_000_000_000.0
    samples = [
        {"ts": _ts(now - ACTIVITY_DELAY_S - 1), "person_present": False},
        {"ts": _ts(now - 1), "person_present": True},
    ]
    assert delayed_activity(samples, now) == ("quiet", ACTIVITY_DELAY_S + 1)


def test_project_history_strips_private_inputs():
    now = 2_000_000_000.0
    result = project_history(
        [{
            "ts": _ts(now - ACTIVITY_DELAY_S),
            "cpu_pct": 12,
            "ambient_rms": 900,
            "person_present": True,
        }],
        limit=10,
        now=now,
    )
    assert result == [{"ts": _ts(now - ACTIVITY_DELAY_S), "cpu_pct": 12}]


def test_public_weather_summary_keeps_weather_but_not_station():
    assert public_weather_summary(
        "At Grove, it's 12 degrees Celsius with a light breeze."
    ) == "At the local weather station, it's 12 degrees Celsius with a light breeze."
    assert public_weather_summary(
        "Weather at Grove."
    ) == "Local weather report."
