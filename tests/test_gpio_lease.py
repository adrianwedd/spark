"""Ownership contract for processes that temporarily exclude px-alive."""

import json
import time

import pytest

from pxh.gpio_lease import GpioLeaseGuard, GpioLeaseMonitor, GpioLeaseStore


def test_false_owner_cannot_replace_a_live_lease(tmp_path):
    """A second process cannot overwrite GPIO authority held by a live owner."""
    store = GpioLeaseStore(tmp_path, pid_alive=lambda pid: pid in {1060, 2222})
    first = store.acquire("voice", pid=1060, now=100.0, ttl_s=30.0)

    assert first is not None
    assert store.acquire("wander", pid=2222, now=101.0, ttl_s=30.0) is None
    assert store.current(now=101.0)["owner_pid"] == 1060


def test_wrong_token_cannot_refresh_or_release(tmp_path):
    """Only the generation that acquired a lease may extend or clear it."""
    store = GpioLeaseStore(tmp_path, pid_alive=lambda _pid: True)
    lease = store.acquire("voice", pid=1060, now=100.0, ttl_s=30.0)

    assert lease is not None
    assert store.refresh("wrong-token", now=110.0, ttl_s=30.0) is False
    assert store.release("wrong-token") is False
    assert store.current(now=110.0)["lease_id"] == lease.lease_id


def test_matching_token_refreshes_and_releases(tmp_path):
    """A legitimate owner can extend and then release its own authority."""
    store = GpioLeaseStore(tmp_path, pid_alive=lambda _pid: True)
    lease = store.acquire("wander", pid=1060, now=100.0, ttl_s=20.0)

    assert lease is not None
    assert store.refresh(lease.lease_id, now=110.0, ttl_s=30.0) is True
    assert store.current(now=121.0) is not None
    assert store.release(lease.lease_id) is True
    assert store.current(now=121.0) is None


def test_expired_lease_is_rejected_deterministically(tmp_path):
    """Expiry removes authority even while its former owner PID is alive."""
    store = GpioLeaseStore(tmp_path, pid_alive=lambda _pid: True)
    assert store.acquire("voice", pid=1060, now=100.0, ttl_s=20.0) is not None

    assert store.current(now=120.0) is None
    assert store.acquire("wander", pid=2222, now=120.0, ttl_s=20.0) is not None


def test_dead_owner_is_rejected_deterministically(tmp_path):
    """A dead PID cannot retain authority until its wall-clock expiry."""
    alive = {1060}
    store = GpioLeaseStore(tmp_path, pid_alive=lambda pid: pid in alive)
    assert store.acquire("voice", pid=1060, now=100.0, ttl_s=20.0) is not None

    alive.clear()
    assert store.current(now=101.0) is None
    assert store.acquire("wander", pid=2222, now=101.0, ttl_s=20.0) is not None


def test_exploration_state_is_not_gpio_authority(tmp_path):
    """A non-exploration owner never asserts exploring.json as a side effect."""
    store = GpioLeaseStore(tmp_path, pid_alive=lambda _pid: True)
    lease = store.acquire("voice", pid=1060, now=100.0, ttl_s=30.0)

    assert lease is not None
    assert (tmp_path / "gpio_lease.json").exists()
    assert not (tmp_path / "exploring.json").exists()
    payload = json.loads((tmp_path / "gpio_lease.json").read_text())
    assert payload["owner_kind"] == "voice"
    assert payload["owner_pid"] == 1060


def test_guard_keeps_legitimate_owner_live_then_releases(tmp_path):
    """The guard refresh thread keeps its lease live until explicit cleanup.

    Instead of sleeping a fixed wall-clock duration and hoping the refresh
    thread ran in time (which flakes under load — #239), we poll for the
    observable effect: ``expires_at`` advancing beyond the initial TTL,
    proving at least one refresh succeeded.  A generous timeout bounds the
    wait so a broken refresh thread still fails the test deterministically.
    """
    store = GpioLeaseStore(tmp_path, pid_alive=lambda _pid: True)
    guard = GpioLeaseGuard(store, "voice", ttl_s=0.08, refresh_s=0.02, pid=1060)

    assert guard.acquire() is True
    lease_id = store.current()["lease_id"]
    initial_expires = store.current()["expires_at"]

    # Wait for the refresh thread to extend expires_at — deterministic
    # synchronization instead of a fixed sleep that races the scheduler.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        current = store.current()
        if current is not None and current["expires_at"] > initial_expires:
            break
        time.sleep(0.005)
    else:
        pytest.fail("refresh thread did not extend expires_at within 5s")

    assert store.current() is not None
    assert store.release("wrong-token") is False
    assert store.current()["lease_id"] == lease_id
    assert guard.release() is True
    assert store.current() is None


def test_unreadable_lease_state_fails_closed(tmp_path, monkeypatch):
    """I/O failure must be surfaced so a consumer refuses to claim GPIO."""
    store = GpioLeaseStore(tmp_path, pid_alive=lambda _pid: True)
    store.path.write_text("{}")
    real_read_text = type(store.path).read_text

    def unreadable(path, *args, **kwargs):
        if path == store.path:
            raise PermissionError("denied")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(store.path), "read_text", unreadable)

    with pytest.raises(PermissionError, match="denied"):
        store.current()


@pytest.mark.parametrize("payload", [
    "not json",
    "[]",
    '{"active": true}',
    '{"active": true, "lease_id": "", "owner_kind": "voice", '
    '"owner_pid": 1, "expires_at": 2}',
    '{"active": true, "lease_id": "x", "owner_kind": "voice", '
    '"owner_pid": "bad", "expires_at": 2}',
    '{"active": true, "lease_id": "x", "owner_kind": "voice", '
    '"owner_pid": 1, "expires_at": "bad"}',
    '{"active": true, "lease_id": "x", "owner_kind": "voice", '
    '"owner_pid": "1", "expires_at": 2}',
    '{"active": true, "lease_id": "x", "owner_kind": "voice", '
    '"owner_pid": true, "expires_at": 2}',
    '{"active": true, "lease_id": "x", "owner_kind": "voice", '
    '"owner_pid": 1.5, "expires_at": 2}',
    '{"active": true, "lease_id": "x", "owner_kind": "voice", '
    '"owner_pid": 1, "expires_at": "nan"}',
])
def test_corrupt_lease_state_fails_closed(tmp_path, payload):
    """Malformed authority cannot be treated as an empty claim."""
    store = GpioLeaseStore(tmp_path, pid_alive=lambda _pid: True)
    store.path.write_text(payload)

    with pytest.raises(ValueError, match="invalid GPIO lease"):
        store.current()
    with pytest.raises(ValueError, match="invalid GPIO lease"):
        store.acquire("wander", pid=2222)


def test_guard_notifies_owner_when_refresh_is_lost(tmp_path):
    """A failed renewal becomes observable immediately to the hardware owner."""
    notified = []
    store = GpioLeaseStore(tmp_path, pid_alive=lambda _pid: True)
    guard = GpioLeaseGuard(
        store,
        "wander",
        ttl_s=0.08,
        refresh_s=0.02,
        pid=1060,
        on_lost=lambda: notified.append("lost"),
    )
    assert guard.acquire() is True
    store.refresh = lambda *_args, **_kwargs: False

    assert guard.lost.wait(0.2) is True
    assert notified == ["lost"]
    assert guard.owns_gpio is False


def test_borrower_notifies_when_parent_lease_disappears(tmp_path):
    store = GpioLeaseStore(tmp_path, pid_alive=lambda _pid: True)
    lease = store.acquire("voice", pid=1060, ttl_s=60)
    notified = []
    monitor = GpioLeaseMonitor(
        store, lease.lease_id, check_s=0.01, on_lost=lambda: notified.append("lost")
    )
    monitor.start()

    assert store.release(lease.lease_id) is True
    assert monitor.lost.wait(0.2) is True
    assert notified == ["lost"]
