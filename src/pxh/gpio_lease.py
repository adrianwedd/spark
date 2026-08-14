"""Tokenized, expiring ownership for processes that exclude ``px-alive``.

``exploring.json`` describes exploration.  This module owns the separate
hardware-coordination contract so voice, announcement, race, and wander jobs
do not impersonate exploration merely because they temporarily need GPIO.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from pxh.state import atomic_write


@dataclass(frozen=True)
class GpioLease:
    lease_id: str
    owner_kind: str
    owner_pid: int


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


class GpioLeaseStore:
    """Atomic single-owner lease backed by ``state/gpio_lease.json``."""

    def __init__(self, state_dir: Path, *, pid_alive: Callable[[int], bool] = _pid_alive):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "gpio_lease.json"
        self.lock_path = self.state_dir / "gpio_lease.lock"
        self._pid_alive = pid_alive

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            try:
                os.chmod(self.lock_path, 0o666)
            except OSError:
                pass
            with os.fdopen(fd, "r+") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                yield
        finally:
            # fdopen owns fd on the normal path; only close if entering it failed.
            try:
                os.close(fd)
            except OSError:
                pass

    def _read(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _valid(self, data: dict | None, now: float) -> bool:
        if not data or not data.get("active"):
            return False
        try:
            pid = int(data["owner_pid"])
            expires_at = float(data["expires_at"])
            lease_id = data["lease_id"]
        except (KeyError, TypeError, ValueError):
            return False
        return bool(lease_id) and expires_at > now and self._pid_alive(pid)

    def current(self, *, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self._locked():
            data = self._read()
            return data if self._valid(data, now) else None

    def acquire(self, owner_kind: str, *, pid: int | None = None,
                now: float | None = None, ttl_s: float = 60.0) -> GpioLease | None:
        now = time.time() if now is None else now
        pid = os.getpid() if pid is None else pid
        with self._locked():
            current = self._read()
            if self._valid(current, now):
                return None
            lease = GpioLease(uuid.uuid4().hex, str(owner_kind), int(pid))
            payload = {
                "active": True,
                "lease_id": lease.lease_id,
                "owner_kind": lease.owner_kind,
                "owner_pid": lease.owner_pid,
                "acquired_at": now,
                "expires_at": now + ttl_s,
            }
            atomic_write(self.path, json.dumps(payload))
            return lease

    def refresh(self, lease_id: str, *, now: float | None = None,
                ttl_s: float = 60.0) -> bool:
        now = time.time() if now is None else now
        with self._locked():
            current = self._read()
            if not self._valid(current, now) or current.get("lease_id") != lease_id:
                return False
            current["expires_at"] = now + ttl_s
            atomic_write(self.path, json.dumps(current))
            return True

    def release(self, lease_id: str) -> bool:
        with self._locked():
            current = self._read()
            if not current or current.get("lease_id") != lease_id:
                return False
            try:
                self.path.unlink()
            except FileNotFoundError:
                return False
            return True


class GpioLeaseGuard:
    """Own and periodically renew one GPIO lease until explicit release."""

    def __init__(self, store: GpioLeaseStore, owner_kind: str, *,
                 ttl_s: float = 60.0, refresh_s: float = 20.0,
                 pid: int | None = None):
        self.store = store
        self.owner_kind = owner_kind
        self.ttl_s = ttl_s
        self.refresh_s = refresh_s
        self.pid = os.getpid() if pid is None else pid
        self.lease: GpioLease | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> bool:
        if self.lease is not None:
            return True
        lease = self.store.acquire(
            self.owner_kind, pid=self.pid, ttl_s=self.ttl_s)
        if lease is None:
            return False
        self.lease = lease
        self._stop.clear()

        def _refresh() -> None:
            while not self._stop.wait(self.refresh_s):
                if self.lease is None or not self.store.refresh(
                        self.lease.lease_id, ttl_s=self.ttl_s):
                    return

        self._thread = threading.Thread(
            target=_refresh, daemon=True, name="gpio-lease-refresh")
        self._thread.start()
        return True

    def release(self) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.refresh_s * 2))
        lease, self.lease = self.lease, None
        self._thread = None
        return lease is not None and self.store.release(lease.lease_id)
