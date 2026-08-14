"""Ownership contract for processes that temporarily exclude px-alive."""

import json
import time

from pxh.gpio_lease import GpioLeaseGuard, GpioLeaseStore


def test_second_owner_cannot_replace_a_live_lease(tmp_path):
    store = GpioLeaseStore(tmp_path, pid_alive=lambda pid: pid in {1060, 2222})
    first = store.acquire("voice", pid=1060, now=100.0, ttl_s=30.0)

    assert first is not None
    assert store.acquire("wander", pid=2222, now=101.0, ttl_s=30.0) is None
    assert store.current(now=101.0)["owner_pid"] == 1060


def test_only_matching_token_can_refresh_or_release(tmp_path):
    store = GpioLeaseStore(tmp_path, pid_alive=lambda _pid: True)
    lease = store.acquire("voice", pid=1060, now=100.0, ttl_s=30.0)

    assert lease is not None
    assert store.refresh("wrong-token", now=110.0, ttl_s=30.0) is False
    assert store.release("wrong-token") is False
    assert store.current(now=110.0)["lease_id"] == lease.lease_id

    assert store.refresh(lease.lease_id, now=110.0, ttl_s=30.0) is True
    assert store.release(lease.lease_id) is True
    assert store.current(now=111.0) is None


def test_expired_or_dead_owner_is_rejected_deterministically(tmp_path):
    alive = {1060}
    store = GpioLeaseStore(tmp_path, pid_alive=lambda pid: pid in alive)
    lease = store.acquire("voice", pid=1060, now=100.0, ttl_s=20.0)
    assert lease is not None

    assert store.current(now=121.0) is None

    replacement = store.acquire("wander", pid=2222, now=121.0, ttl_s=20.0)
    assert replacement is not None
    alive.clear()
    assert store.current(now=122.0) is None


def test_exploration_state_is_not_the_gpio_lease(tmp_path):
    store = GpioLeaseStore(tmp_path, pid_alive=lambda _pid: True)
    lease = store.acquire("voice", pid=1060, now=100.0, ttl_s=30.0)

    assert lease is not None
    assert (tmp_path / "gpio_lease.json").exists()
    assert not (tmp_path / "exploring.json").exists()
    payload = json.loads((tmp_path / "gpio_lease.json").read_text())
    assert payload["owner_kind"] == "voice"
    assert payload["owner_pid"] == 1060


def test_guard_refreshes_and_releases_only_its_own_lease(tmp_path):
    store = GpioLeaseStore(tmp_path, pid_alive=lambda _pid: True)
    guard = GpioLeaseGuard(store, "voice", ttl_s=0.08, refresh_s=0.02, pid=1060)

    assert guard.acquire() is True
    lease_id = store.current()["lease_id"]
    time.sleep(0.12)
    assert store.current() is not None

    # A different generation cannot clear the live guard.
    assert store.release("wrong-token") is False
    assert store.current()["lease_id"] == lease_id
    guard.release()
    assert store.current() is None
