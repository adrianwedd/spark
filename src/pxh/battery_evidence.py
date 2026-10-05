"""Best-effort raw ADC evidence, never an input to battery decisions.

One poller owns this store. All filesystem work (including startup/rotation)
is confined to StallProofLineWriter's daemon thread. No flush on the poll path.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from .logging import StallProofLineWriter

MAX_BYTES = 256 * 1024  # each of current and .1; at most 512 KiB total
MAX_LINE_BYTES = 512
QUEUE_MAX = 64


class BatteryEvidence(StallProofLineWriter):
    """Bounded JSONL history; drops/errors appear on the next successful line.

    A stalled writer loses oldest queued samples, not polling time. Counters
    are process-local; a power loss can lose queued/unflushed data and a partial
    final line. No fsync: diagnostic retention is not a durability guarantee.
    """

    def __init__(self, path: Path, *, clock=time.time):
        self._clock = clock
        self.storage_errors = 0
        super().__init__(path, queue_max=QUEUE_MAX)

    def record(self, raw_volts: float, volts: float, classification: str,
               charging: bool) -> None:
        """Enqueue only fixed diagnostic fields; never propagate failure."""
        try:
            # Strict JSON cannot represent non-finite ADC values. Preserve
            # their identity as strings, without changing the poller's policy.
            raw = raw_volts if math.isfinite(raw_volts) else str(raw_volts)
            rounded = volts if math.isfinite(volts) else str(volts)
            line = json.dumps({
                "sample_ts": self._clock(), "raw_volts": raw,
                "volts": rounded, "classification": classification,
                "charging": charging,
            }, separators=(",", ":"), allow_nan=False)
            if len(line.encode("utf-8")) <= MAX_LINE_BYTES:
                self.write(line)
        except Exception:
            pass

    def _write_line(self, line: str) -> None:
        # Override the generic writer's plain-text gap marker: every line here
        # is JSON, and the error count is visible after storage recovers.
        try:
            record = json.loads(line)
            record["queue_dropped"] = self.dropped
            record["storage_errors"] = self.storage_errors
            data = (json.dumps(record, separators=(",", ":"),
                               allow_nan=False) + "\n").encode("utf-8")
            if len(data) > MAX_LINE_BYTES:
                self.storage_errors += 1
                return
            self._store(data)
        except Exception:
            self.storage_errors += 1

    def _store(self, data: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size + len(data) > MAX_BYTES:
            # A previous run may have left an oversized file. Do not retain it.
            if size > MAX_BYTES:
                self.path.unlink()
            else:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        with self.path.open("ab") as handle:
            handle.write(data)
