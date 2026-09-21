"""Arrival detection must not manufacture an arrival from a change of evidence.

Measured on the robot 2026-09-21, on the revision that shipped the stale-fix
gate (#305's first half): one tracker legitimately publishes **two
representations** — a coordinate fix at 0.01 km, and `{"semantic": true,
"address": "Office Mini"}` — and the stale-fix gate correctly discarded its
23.95 h old coordinate fix, 4.01 km away, as evidence. The arrival detector
compared the two representations as if they were two locations, read the
*alternation* as movement, and fired:

    17   findmyhub: adrian at_home False→True
     0   findmyhub: adrian at_home True→False
     7   expressing: action=greet_arrival

for a tracker that never left home. Every one of those arrivals rested on a fix
the gate had just refused to treat as evidence about now.

The fix is a named state (`HOME` / `AWAY` / `UNKNOWN`) carrying the *kind* of
evidence that produced it, and one edge rule that refuses an edge across
incomparable evidence. These tests pin the behaviour, not the mechanism.
"""
from __future__ import annotations

import itertools

import pxh.mind as mind
from pxh import presence
from pxh.mind import _findmyhub_transitions

_TS = itertools.count(1)


def setup_function(_fn):
    mind._last_known_findmyhub = {}
    mind._latch_suppressed = {}
    mind._latch_far_streak = {}
    mind._latch_stale = {}


def _coord(distance_km, *, ts=None, age_s: float = 60, accuracy_m=100):
    return {
        "distance_km": distance_km, "accuracy_m": accuracy_m, "lat": -43.13567,
        "lon": 147.11840, "age_s": age_s, "ts": ts if ts is not None else next(_TS),
    }


def _semantic(address, *, ts=None, age_s: float = 60):
    return {"semantic": True, "address": address, "ts": ts if ts is not None else next(_TS),
            "age_s": age_s}


def test_the_reproduction_no_longer_produces_an_arrival():
    """coordinate → semantic → coordinate, no physical movement: no arrival.

    The exact three-sample sequence that fired `person_arrived_home:adrian` on the
    robot: a home fix, the same tracker published as a semantic address, then a
    home fix again.
    """
    assert _findmyhub_transitions({"adrian": _coord(0.01)}) == []
    assert _findmyhub_transitions({"adrian": _semantic("Office Mini")}) == []
    assert _findmyhub_transitions({"adrian": _coord(0.01)}) == []


def test_repeated_representation_alternation_never_greets():
    """Twenty alternations and no movement: still zero arrivals."""
    transitions: list[str] = []
    for _ in range(10):
        transitions += _findmyhub_transitions({"adrian": _coord(0.01)})
        transitions += _findmyhub_transitions({"adrian": _semantic("Office Mini")})
    assert transitions == []


def test_unknown_never_becomes_away_and_so_never_becomes_an_arrival():
    """`UNKNOWN → HOME` is a first sighting, not an arrival.

    A tracker whose first usable fix arrives *after* a stale or dropped one has no
    decided state; promoting that absence to AWAY is what turns one fix into a
    greeting.
    """
    # A stale far fix cannot latch away — there is nothing to compare it to yet.
    assert _findmyhub_transitions({"adrian": _coord(9.0, age_s=13.8 * 3600)}) == []
    assert mind._last_known_findmyhub["adrian"]["at_home"] == presence.UNKNOWN
    assert "at_home_kind" not in mind._last_known_findmyhub["adrian"]

    # ...so the next home fix is not an arrival either.
    assert _findmyhub_transitions({"adrian": _coord(0.01)}) == []
    assert mind._last_known_findmyhub["adrian"]["at_home"] == presence.AT_HOME


def test_a_represented_alternation_does_not_leave_away_behind_it():
    """A *real* away, then a representation change, then a real return.

    The representation change is not evidence either way: it neither greets nor
    destroys the departure the coordinates established. The genuine coordinate
    return then greets exactly once.
    """
    _findmyhub_transitions({"adrian": _coord(0.01)})
    _findmyhub_transitions({"adrian": _coord(9.0)})
    away = {"adrian": _coord(9.0)}
    _findmyhub_transitions(away)
    assert away["adrian"]["at_home"] == presence.AWAY

    # An unplaceable address: no claim, so the decided state is held (name and
    # kind together) rather than being re-published as a boolean.
    switched = {"adrian": _semantic("Office Mini")}
    assert _findmyhub_transitions(switched) == []
    assert switched["adrian"]["at_home"] == presence.AWAY
    assert switched["adrian"]["at_home_kind"] == presence.COORDINATE

    # The real return is a coordinate away→home and greets once.
    assert _findmyhub_transitions({"adrian": _coord(0.01)}) == ["person_arrived_home:adrian"]


def test_a_coordinate_claim_does_not_re_decide_a_semantic_state():
    """A representation change can neither greet nor deadlock the latch.

    A tracker decided AWAY from an address, then read as a coordinate fix at home:
    the state is updated but no arrival fires, because the two sides are different
    kinds of claim about the same name. A later *comparable* pair (coordinate away
    → coordinate home) still greets, so the refusal does not strand the tracker.
    """
    _findmyhub_transitions({"adrian": _semantic("12 Thorp Street")})
    assert mind._last_known_findmyhub["adrian"]["at_home"] == presence.AWAY

    switched = {"adrian": _coord(0.01)}
    assert _findmyhub_transitions(switched) == []  # not an arrival
    assert switched["adrian"]["at_home"] == presence.AT_HOME
    assert switched["adrian"]["at_home_kind"] == presence.COORDINATE

    # Comparability restored: a genuine coordinate departure and return greets.
    _findmyhub_transitions({"adrian": _coord(9.0)})
    _findmyhub_transitions({"adrian": _coord(9.0)})
    assert mind._last_known_findmyhub["adrian"]["at_home"] == presence.AWAY
    assert _findmyhub_transitions({"adrian": _coord(0.01)}) == ["person_arrived_home:adrian"]


def test_stale_evidence_does_not_move_the_remembered_state():
    """A stale fix must not mutate the state a later arrival is measured against."""
    _findmyhub_transitions({"adrian": _coord(0.01)})
    for _ in range(3):
        assert _findmyhub_transitions({"adrian": _coord(9.0, age_s=5 * 3600)}) == []
    assert mind._last_known_findmyhub["adrian"]["at_home"] == presence.AT_HOME
    assert mind._latch_stale["adrian"] == 3


def test_a_real_coordinate_departure_and_return_still_greets_once():
    """The behaviour that must survive: a trustworthy AWAY → HOME arrival.

    Two agreeing far fixes confirm the departure (presence.EXIT_CONFIRMATIONS),
    then the first home fix is believed immediately — no debounce on the side that
    greets.
    """
    _findmyhub_transitions({"adrian": _coord(0.01)})
    _findmyhub_transitions({"adrian": _coord(9.0)})
    _findmyhub_transitions({"adrian": _coord(9.0)})
    assert mind._last_known_findmyhub["adrian"]["at_home"] == presence.AWAY
    assert _findmyhub_transitions({"adrian": _coord(0.01)}) == ["person_arrived_home:adrian"]


def test_a_semantic_address_that_is_home_still_greets_from_a_known_away():
    """Semantic evidence is still usable — within its own kind.

    A known address is unambiguous, so it is single-sample evidence in both
    directions (unlike a coordinate, which needs EXIT_CONFIRMATIONS to leave home).
    """
    # First sighting: a known away address, then home — a real arrival.
    _findmyhub_transitions({"adrian": _semantic("12 Thorp Street")})
    assert mind._last_known_findmyhub["adrian"]["at_home"] == presence.AWAY
    assert _findmyhub_transitions({"adrian": _semantic("12 The Shack")}) == [
        "person_arrived_home:adrian"
    ]
    assert mind._last_known_findmyhub["adrian"]["at_home"] == presence.AT_HOME

    # Home → away is not an arrival, and away → home is one again.
    assert _findmyhub_transitions({"adrian": _semantic("12 Thorp Street")}) == []
    assert mind._last_known_findmyhub["adrian"]["at_home"] == presence.AWAY
    assert _findmyhub_transitions({"adrian": _semantic("12 The Shack")}) == [
        "person_arrived_home:adrian"
    ]


def test_edge_rule_refuses_incomparable_evidence_directly():
    """The rule itself, at the boundary, with no dict shapes in the way."""
    HOME, AWAY, UNKNOWN = presence.AT_HOME, presence.AWAY, presence.UNKNOWN
    coord, sem = presence.COORDINATE, presence.SEMANTIC

    assert presence.arrival_edge(AWAY, coord, HOME, coord) is True
    assert presence.arrival_edge(AWAY, sem, HOME, sem) is True
    # A representation change is not movement, in either direction.
    assert presence.arrival_edge(AWAY, coord, HOME, sem) is False
    assert presence.arrival_edge(AWAY, sem, HOME, coord) is False
    # UNKNOWN is not AWAY on the far side, and is not a claim on the near side.
    assert presence.arrival_edge(UNKNOWN, None, HOME, coord) is False
    assert presence.arrival_edge(AWAY, coord, UNKNOWN, None) is False
    assert presence.arrival_edge(None, None, HOME, coord) is False
    # The pre-#305 boolean dialect still reads correctly (#156's contract).
    assert presence.arrival_edge(False, coord, True, coord) is True  # type: ignore[arg-type]
    assert presence.arrival_edge(True, coord, True, coord) is False  # type: ignore[arg-type]


def test_only_a_named_state_is_published_to_consumers():
    """Every published state is one of the three names — never a bare boolean."""
    for fix in (_coord(0.01), _coord(9.0), _semantic("Office Mini"),
                _coord(9.0, age_s=13.8 * 3600)):
        batch = {"adrian": fix}
        _findmyhub_transitions(batch)
        assert batch["adrian"]["at_home"] in presence.AT_HOME_STATES
