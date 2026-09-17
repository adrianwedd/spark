"""#305's hysteresis, tested as a state machine rather than as a threshold.

The acceptance criteria are about a *sequence*: a stationary tracker sitting on
the radius boundary must produce at most one arrival, and a genuine away→home
fix must still be believed immediately. A bare-radius test cannot express either
of those, which is why these tests always feed a series.
"""

from __future__ import annotations

import pxh.presence as presence

ACC = 34.0  # the live Chipolo accuracy that motivated #305


def test_first_fix_inside_the_enter_radius_latches_home():
    assert presence.latch_at_home(0.12, ACC, None) == (True, "enter")


def test_first_fix_outside_the_enter_radius_latches_away():
    assert presence.latch_at_home(1.4, ACC, None) == (False, "away")


def test_a_fix_in_the_band_holds_a_home_latch():
    """The band is the whole fix: 0.21 km is past the enter radius but is not
    evidence of leaving, so the state must not move."""
    state, reason = presence.latch_at_home(0.21, ACC, True)
    assert state is None  # None means "no new information", not "away"
    assert reason == "hold-band"


def test_a_fix_in_the_band_does_not_promote_an_away_tracker():
    state, reason = presence.latch_at_home(0.21, ACC, False)
    assert state is None
    assert reason == "away"


def test_exit_needs_the_wider_radius_and_then_fires_once():
    assert presence.latch_at_home(0.22, ACC, True) == (None, "hold-band")
    assert presence.latch_at_home(0.35, ACC, True) == (False, "exit")
    # ...and the same tracker re-entering is an arrival edge again.
    assert presence.latch_at_home(0.12, ACC, False) == (True, "enter")


def test_staying_home_is_not_a_hold():
    """'stay' is a decision that confirms the state; 'hold-band' is a refusal.
    Collapsing them would make the evidence log unreadable."""
    assert presence.latch_at_home(0.10, ACC, True) == (True, "stay")


def test_accuracy_gate_blocks_a_coarse_arrival():
    # 400 m of stated accuracy cannot distinguish "home" from 400 m away — and
    # it is a *suppressed arrival*, not an absence of information about a
    # tracker we have never seen.
    assert presence.latch_at_home(0.10, 400.0, False) == (None, "hold-accuracy")
    assert presence.latch_at_home(0.10, 400.0, True) == (None, "hold-accuracy")
    # No previous state at all: nothing to hold.
    assert presence.latch_at_home(0.10, 400.0, None) == (None, "unknown")


def test_accuracy_gate_is_judged_against_the_radius_in_play():
    """The gate is per-comparison, not a single global accuracy limit: a 200 m
    fix is too coarse to prove *arrival* (enter radius 150 m) but is good enough
    to prove *departure* (exit radius 300 m)."""
    assert presence.latch_at_home(0.10, 200.0, False) == (None, "hold-accuracy")
    assert presence.latch_at_home(0.40, 200.0, True) == (False, "exit")


def test_missing_accuracy_is_treated_as_perfect_not_as_evidence_to_flip():
    """A tracker that omits accuracy keeps working; the latch still applies."""
    assert presence.latch_at_home(0.12, None, False) == (True, "enter")
    assert presence.latch_at_home(0.21, None, True) == (None, "hold-band")


def test_hold_reasons_are_declared_as_the_evidence_worthy_ones():
    """mind logs suppressed flips by tag, so the tags are part of the contract."""
    assert set(presence.SUPPRESSED_FLIP_REASONS) <= set(presence.HOLD_REASONS)
    assert "unknown" not in presence.SUPPRESSED_FLIP_REASONS
    assert set(presence.DECISION_REASONS).isdisjoint(presence.HOLD_REASONS)


def test_describe_location_uses_the_same_bands():
    assert presence.describe_location(0.10) == "at home"
    assert presence.describe_location(0.21) == "near home"
    assert presence.describe_location(1.4) == "1.4km from home"


def test_boundary_jitter_series_moves_the_state_once():
    """The filed incident, as a series: 34 m-accurate fixes around 150 m."""
    series = [0.12, 0.16, 0.13, 0.18, 0.14, 0.21, 0.11, 0.19, 0.15, 0.17]
    state: bool | None = None
    changes = 0
    for distance in series:
        new_state, _reason = presence.latch_at_home(distance, ACC, state)
        if new_state is not None and new_state != state:
            changes += 1
            state = new_state
    assert changes == 1
    assert state is True
