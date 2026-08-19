"""The suite must never read or mutate the live robot's session.

This is a harness invariant, not a feature test. Three autouse fixtures in
conftest.py already redirect health writes, the brain mailbox and the alive
heartbeat away from live state; the session was the fourth instance of the
same hazard and the only one left unfixed (#210).

The concrete symptom was six `test_mind_utils` expression tests going red on
the robot and nowhere else: `mind.expression()` is a #174 enforcement point,
the live session carries `spark_quiet_mode: true` (#209, an unattributable
latch we deliberately do not clear), and `policy.evaluate()` blocked every
dispatch before the test's mock could fire. Those failures were carried for
months as a known-red baseline, which is precisely how a suite stops carrying
information.

Note what these tests deliberately do NOT do: prove isolation by writing to
the session and checking the live file is unchanged. Under a regression that
probe would itself mutate the running robot. Asserting on the resolved *path*
proves the same property without ever touching live state.
"""

from pathlib import Path

import pytest

from pxh import state

# Derived from the module's own location rather than from PX_STATE_DIR, so the
# check still means "the real robot's session" even under a test that repoints
# the state dir.
LIVE_SESSION = (
    Path(state.__file__).resolve().parents[2] / "state" / "session.json"
)


def test_an_ordinary_test_never_resolves_to_the_live_session():
    """The session an ordinary test sees must not be the robot's own."""
    assert state.session_path().resolve() != LIVE_SESSION


def test_an_ordinary_test_sees_a_session_it_can_safely_write():
    """Isolation must survive a write, not just a read.

    `update_session()` mutates whatever `session_path()` resolves to. If that
    is the live file, a test run silently rewrites the running robot's state.
    """
    resolved = state.session_path().resolve()
    assert LIVE_SESSION not in resolved.parents
    assert resolved != LIVE_SESSION


def test_an_ordinary_test_sees_quiet_mode_off_whatever_the_robot_is_doing():
    """The default session is deterministic, not a sample of the robot.

    This is the assertion that was actually failing in test_mind_utils: on the
    robot the live session answers True here, and every policy-gated dispatch
    downstream is blocked.
    """
    assert state.load_session().get("spark_quiet_mode") is False


def test_a_test_that_sets_its_own_session_path_still_wins(tmp_path, monkeypatch):
    """The autouse fixture must not clobber an explicit, test-owned path.

    22 sites across 9 test files set PX_SESSION_PATH for themselves, three
    different ways. A fixture that ran after them — or that pinned the value —
    would break every one. Guard, not a bug reproduction: this passes before
    the fixture exists too, and its job is to fail if someone later makes the
    fixture session-scoped or moves it after test setup.
    """
    mine = tmp_path / "mine" / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(mine))
    assert state.session_path() == mine


@pytest.mark.live
def test_hardware_tests_keep_the_real_session():
    """The escape hatch: `live` tests run against the robot's own session.

    Hardware tests exist to exercise the real machine; isolating their session
    would make them assert against a fiction. Deselected by `-m 'not live'`,
    so this does not run in an ordinary suite.
    """
    assert state.session_path().resolve() == LIVE_SESSION
