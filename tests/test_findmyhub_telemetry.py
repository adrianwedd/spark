"""Evidence for a tracker that flaps between two places (#305, reopened).

The flapping tracker reports 0.01 km and 4.44 km alternately all evening, and
the two plausible mechanisms -- a *stale* far fix treated as current, and a fetch
mislabeled onto the wrong tracker -- are indistinguishable in a distance and
distinguishable in a coordinate. This test pins the telemetry that tells them
apart.
"""
from __future__ import annotations

from pxh.mind import _latch_detail


def test_latch_detail_carries_coordinates_and_age():
    curr = {"distance_km": 4.44, "accuracy_m": 99, "lat": -43.16194,
            "lon": 147.08465, "age_s": 370193}
    detail = _latch_detail(curr, "pending-far")
    assert "-43.16194" in detail and "147.08465" in detail
    assert "age=370193s" in detail
    assert "4.44km" in detail and "pending-far" in detail


def test_latch_detail_keeps_the_radii_and_reason():
    """The existing meanings stay: which reason, against which radii."""
    curr = {"distance_km": 0.01, "accuracy_m": 100, "lat": 1.0, "lon": 2.0, "age_s": 300}
    detail = _latch_detail(curr, "enter")
    assert detail.startswith("enter:")
    assert "0.15km enter / 0.30km exit" in detail
