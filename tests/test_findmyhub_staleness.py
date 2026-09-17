"""A fix older than the limit cannot change a tracker's latched state (#305).

The flap this closes: `state/findmyhub.json` is rewritten every ~5 min, so the
*file*-level staleness gate always passes, while the *fix* inside it was measured
13.8 hours old (4.3 days for another tracker) and 4.43 km away. A stale far fix
therefore latched `away`, and the next fresh home fix flipped it back — an arrival
manufactured out of nothing but the file being current.
"""
from __future__ import annotations

import pxh.mind as mind
from pxh.mind import _findmyhub_transitions, _latch_findmyhub_states


def _fix(*, distance_km, age_s, ts):
    return {
        "distance_km": distance_km, "accuracy_m": 100, "lat": -43.16, "lon": 147.08,
        "age_s": age_s, "ts": ts,
    }


def setup_function(_fn):
    mind._last_known_findmyhub = {}
    mind._latch_suppressed = {}
    mind._latch_far_streak = {}
    mind._latch_stale = {}


def test_a_stale_far_fix_cannot_latch_away_and_manufacture_an_arrival():
    """Fresh home, then a *stale* far fix, then a fresh home fix: no arrival edge.

    This is the observed production sequence (18:06 home, 18:35 stale far from a
    13.8 h old fix, 18:51 home) which produced two greetings for one evening.
    """
    assert _findmyhub_transitions({"adrian": _fix(distance_km=0.01, age_s=60, ts=1)}) == []

    stale_far = {"adrian": _fix(distance_km=4.43, age_s=13.8 * 3600, ts=2)}
    assert _findmyhub_transitions(stale_far) == []
    assert stale_far["adrian"]["at_home"] is True, "the state must be held, not flipped"
    assert mind._latch_stale["adrian"] == 1

    fresh_home = {"adrian": _fix(distance_km=0.01, age_s=30, ts=3)}
    assert _findmyhub_transitions(fresh_home) == [], "no departure, so no arrival"


def test_a_fresh_far_fix_still_confirms_a_departure_and_then_a_real_arrival():
    """The gate must not break the real thing: fresh fixes decide."""
    _findmyhub_transitions({"adrian": _fix(distance_km=0.01, age_s=60, ts=1)})
    _findmyhub_transitions({"adrian": _fix(distance_km=9.0, age_s=120, ts=2)})
    away = {"adrian": _fix(distance_km=9.0, age_s=180, ts=3)}
    _findmyhub_transitions(away)
    assert away["adrian"]["at_home"] is False

    home = {"adrian": _fix(distance_km=0.01, age_s=45, ts=4)}
    assert _findmyhub_transitions(home) == ["person_arrived_home:adrian"]


def test_a_stale_fix_never_invents_a_home_state_either():
    """Symmetric: a stale *near* fix cannot claim someone is home."""
    stale_home = {"adrian": _fix(distance_km=0.01, age_s=5 * 3600, ts=1)}
    assert _findmyhub_transitions(stale_home) == []
    assert "at_home" not in stale_home
