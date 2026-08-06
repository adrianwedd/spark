"""Tests for charge-state detection from a noisy voltage series.

The robot's ADC jitters about +/-0.05V between polls while charging only lifts
the pack ~0.025V per 30s poll. Any detector comparing adjacent readings is
therefore trying to read a signal below its own noise floor — which is why
SPARK sat at charging=false for a whole afternoon on the charger. These tests
use voltage traces measured on the robot on 2026-08-06.
"""
from __future__ import annotations

import pytest

from pxh.battery_trend import ChargeDetector


# Measured on the Pi at 30s intervals while plugged in: a slow rise with
# jitter larger than the per-poll gain.
CHARGING_TRACE = [6.53, 6.58, 6.55, 6.62, 6.60, 6.68, 6.65, 6.72,
                  6.70, 6.78, 6.75, 6.84, 6.81, 6.75, 6.79, 6.85]

# The afternoon's discharge into the brownout, same jitter, opposite slope.
DISCHARGING_TRACE = [7.30, 7.25, 7.28, 7.20, 7.22, 7.14, 7.16, 7.08,
                     7.10, 7.02, 7.04, 6.96, 6.98, 6.90, 6.86, 6.80]

# Plugged in but essentially holding — neither charging nor draining.
FLAT_TRACE = [7.00, 7.04, 6.98, 7.02, 6.97, 7.03, 6.99, 7.01,
              7.00, 6.96, 7.02, 6.98, 7.03, 6.99, 7.01, 6.97]


def _run(trace: list[float]) -> list[bool]:
    det = ChargeDetector()
    return [det.update(v) for v in trace]


def test_detects_charging_despite_noise_larger_than_per_poll_gain():
    """The real failure: a genuine charge must be seen even under jitter."""
    states = _run(CHARGING_TRACE)
    assert states[-1] is True, "never detected charging on a real charging trace"


def test_never_reports_charging_while_discharging():
    states = _run(DISCHARGING_TRACE)
    assert not any(states), "reported charging while the pack was draining"


def test_flat_pack_does_not_flap_into_charging():
    """Pure noise around a constant voltage must not toggle the state."""
    states = _run(FLAT_TRACE)
    assert not any(states), "noise alone flipped the detector to charging"


def test_single_up_blip_does_not_flip_state():
    """One noisy jump is not a plug-in event."""
    det = ChargeDetector()
    for v in [7.00, 6.98, 7.01, 6.99, 7.00, 6.98]:
        det.update(v)
    assert det.update(7.09) is False


def test_unplug_is_detected_after_charging():
    det = ChargeDetector()
    for v in CHARGING_TRACE:
        det.update(v)
    assert det.charging is True
    # Charger pulled: pack starts draining from where it got to.
    states = [det.update(v) for v in
              [6.83, 6.78, 6.80, 6.72, 6.74, 6.66, 6.68, 6.60, 6.62, 6.54]]
    assert states[-1] is False, "never noticed the charger was pulled"


def test_reports_nothing_until_window_is_full():
    """A cold start must not guess a state from one or two readings."""
    det = ChargeDetector()
    assert det.update(6.53) is False
    assert det.charging is False
