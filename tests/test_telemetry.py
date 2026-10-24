"""Unavailable hardware must not become current readings in any consumer."""
import datetime as dt
import json

import pytest

from pxh.telemetry import read_battery, read_sonar

NOW = 1792799000


def battery(**updates):
    data = {"ts": dt.datetime.fromtimestamp(NOW, dt.timezone.utc).isoformat(),
            "pct": 72, "volts": 8.1, "charging": False}
    return data | updates


@pytest.mark.parametrize("updates", [
    {"ts": "2026-10-03T03:54:29Z"}, {"ts": "bad"}, {"ts": None},
    {"ts": dt.datetime.fromtimestamp(NOW + 60, dt.timezone.utc).isoformat()},
    {"pct": None}, {"pct": True}, {"pct": 101}, {"pct": -1}, {"pct": float('nan')},
    {"volts": 0}, {"volts": 11.19}, {"volts": float('inf')}, {"charging": "false"},
])
def test_invalid_battery_is_unavailable(tmp_path, updates):
    path = tmp_path / 'battery.json'
    path.write_text(json.dumps(battery(**updates)))
    assert read_battery(path, now=NOW) is None


def test_valid_battery_expires(tmp_path):
    path = tmp_path / 'battery.json'
    path.write_text(json.dumps(battery()))
    assert read_battery(path, now=NOW + 90)['pct'] == 72
    assert read_battery(path, now=NOW + 91) is None


@pytest.mark.parametrize('data', [None, [], {}, {'pct': 100}])
def test_malformed_battery_is_unavailable(tmp_path, data):
    path = tmp_path / 'battery.json'
    path.write_text(json.dumps(data))
    assert read_battery(path, now=NOW) is None


@pytest.mark.parametrize('distance', [None, 0, -1, True, '12', float('nan'), float('inf')])
def test_invalid_sonar_is_unavailable(tmp_path, distance):
    path = tmp_path / 'sonar.json'
    path.write_text(json.dumps({'ts': NOW, 'distance_cm': distance}))
    assert read_sonar(path, now=NOW) is None


def test_sonar_expiry_and_future_timestamp(tmp_path):
    path = tmp_path / 'sonar.json'
    path.write_text(json.dumps({'ts': NOW, 'distance_cm': 42}))
    assert read_sonar(path, now=NOW + 60)['distance_cm'] == 42
    assert read_sonar(path, now=NOW + 61) is None
    assert read_sonar(path, now=NOW - 1) is None


def test_missing_readings(tmp_path):
    assert read_battery(tmp_path / 'missing') is None
    assert read_sonar(tmp_path / 'missing') is None
