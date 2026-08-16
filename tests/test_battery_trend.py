"""Tests for charge-state detection from a noisy battery voltage series.

Detecting a charge on this robot is harder than it looks. The pack gains only
~0.004V per 30s poll on the charger, while the reported voltage swings by up
to 0.17V between consecutive polls — mostly px-alive's servo sweeps loading
the rail, not ADC error. The signal sits well under the noise, which is why
SPARK reported ``charging: false`` through an entire afternoon plugged in.

Load can only ever pull the rail *down*, so a rolling maximum recovers the
resting voltage and removes most of the swing; a least-squares slope over the
smoothed series then uses every sample rather than differencing two of them.

REAL_CHARGING_TRACE below is genuinely measured: 30 consecutive polls read
from state/battery.json on 2026-08-06 while the robot sat on the charger. The
flat and discharging series are *derived* from it (detrended and reversed) so
they carry the same real noise, and are labelled as such rather than passed
off as separate measurements.
"""
from __future__ import annotations

import pytest

from pxh.battery_trend import ChargeDetector, _slope

# Measured: 30 polls at 30s intervals, on charger, 2026-08-06 16:00-16:15.
REAL_CHARGING_TRACE = [
    7.06, 7.10, 7.06, 7.10, 7.13, 7.08, 7.11, 7.14, 7.13, 7.16,
    7.19, 7.17, 7.19, 7.14, 7.16, 7.19, 7.22, 7.23, 7.17, 7.20,
    7.20, 7.18, 7.19, 7.12, 7.19, 7.06, 7.13, 7.20, 7.20, 7.23,
]


def _detrended(trace: list[float]) -> list[float]:
    """Same noise, no trend — what a pack holding steady on the bench looks like."""
    s = _slope(trace)
    mid = (len(trace) - 1) / 2
    return [round(v - s * (i - mid), 3) for i, v in enumerate(trace)]


REAL_FLAT_TRACE = _detrended(REAL_CHARGING_TRACE)
REAL_DISCHARGING_TRACE = list(reversed(REAL_CHARGING_TRACE))


def _run(trace: list[float]) -> list[bool]:
    det = ChargeDetector()
    return [det.update(v) for v in trace]


def test_detects_real_charge_despite_noise_exceeding_the_signal():
    """The afternoon's actual failure: a genuine charge must be seen."""
    assert _run(REAL_CHARGING_TRACE)[-1] is True, (
        "never detected charging on a measured charging trace"
    )


def test_never_reports_charging_while_draining():
    assert not any(_run(REAL_DISCHARGING_TRACE)), (
        "reported charging while the pack was draining"
    )


def test_steady_pack_never_reports_charging():
    """A false 'charging' suppresses the emergency shutdown — the costly error."""
    assert not any(_run(REAL_FLAT_TRACE)), "noise alone flipped the detector to charging"


def test_holds_previous_state_until_window_fills():
    """A cold start must not guess from a handful of readings."""
    det = ChargeDetector()
    for v in REAL_CHARGING_TRACE[:6]:
        assert det.update(v) is False
    assert det.charging is False


def test_unplug_is_noticed_after_a_detected_charge():
    det = ChargeDetector()
    for v in REAL_CHARGING_TRACE:
        det.update(v)
    assert det.charging is True, "precondition: should be charging by end of trace"
    # Charger pulled — the pack drains back down through the same noise.
    states = [det.update(v) for v in REAL_DISCHARGING_TRACE]
    assert states[-1] is False, "never noticed the charger was pulled"


def test_single_spike_does_not_flip_state():
    """One outlier poll is not a plug-in event."""
    det = ChargeDetector()
    for v in REAL_FLAT_TRACE:
        det.update(v)
    assert det.update(7.40) is False


def test_slope_matches_least_squares_by_hand():
    assert _slope([1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)
    assert _slope([4.0, 3.0, 2.0, 1.0]) == pytest.approx(-1.0)
    assert _slope([2.0, 2.0, 2.0, 2.0]) == pytest.approx(0.0)
