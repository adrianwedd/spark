"""Synthetic/offline evidence tests. Never launch the poller or import GPIO.

These series are invented, not measured discharge/charging evidence.
"""
from __future__ import annotations

import ast
import errno
import json
import threading
from pathlib import Path

import pytest

from pxh.battery_evidence import BatteryEvidence, MAX_BYTES, MAX_LINE_BYTES, QUEUE_MAX
from pxh.battery_trend import ChargeDetector


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_raw_voltage_clock_and_all_classifications(tmp_path):
    path = tmp_path / "history.jsonl"
    stamps = iter([1000, 1030, 1060])
    writer = BatteryEvidence(path, clock=lambda: next(stamps))
    try:
        writer.record(7.123456, 7.12, "warmup", False)
        writer.record(0.001234, 0.0, "rejected_adc", False)
        writer.record(7.234567, 7.23, "accepted", True)
        writer.flush()
        rows = _records(path)
        assert [r["sample_ts"] for r in rows] == [1000, 1030, 1060]
        assert [r["raw_volts"] for r in rows] == [7.123456, 0.001234, 7.234567]
        assert [r["volts"] for r in rows] == [7.12, 0.0, 7.23]
        assert [r["classification"] for r in rows] == ["warmup", "rejected_adc", "accepted"]
        assert [r["charging"] for r in rows] == [False, False, True]
        assert all(r["queue_dropped"] == r["storage_errors"] == 0 for r in rows)
    finally:
        writer.close()


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES, errno.EROFS])
def test_failed_storage_does_not_affect_detector_and_recovers(tmp_path, code):
    attempted = threading.Event()
    recover = threading.Event()

    class FailingStorage(BatteryEvidence):
        def _store(self, data):
            attempted.set()
            if not recover.is_set():
                raise OSError(code, "synthetic storage failure")
            super()._store(data)

    writer = FailingStorage(tmp_path / "history.jsonl", clock=lambda: 1000)
    baseline, observed = ChargeDetector(), ChargeDetector()
    # Rising then falling synthetic samples; writer results never enter update.
    series = [7 + i * 0.01 for i in range(40)] + [7.4 - i * 0.01 for i in range(40)]
    try:
        writer.record(0.0, 0.0, "rejected_adc", False)
        assert attempted.wait(1)
        writer.flush()
        assert writer.storage_errors == 1
        expected, actual = [], []
        for volts in series:
            expected.append(baseline.update(volts))
            charging = observed.update(volts)
            writer.record(volts, volts, "accepted", charging)
            actual.append(charging)
        writer.flush()
        assert actual == expected
        assert True in actual and actual[-1] is False
        recover.set()
        writer.record(7.0, 7.0, "accepted", False)
        writer.flush()
        assert _records(writer.path)[-1]["storage_errors"] >= 1
    finally:
        writer.close()


def test_stalled_storage_bounds_queue_without_blocking_producer(tmp_path):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class SlowStorage(BatteryEvidence):
        def _store(self, data):
            entered.set()
            assert release.wait(5), "test did not release synthetic storage"
            super()._store(data)

    writer = SlowStorage(tmp_path / "history.jsonl", clock=lambda: 1000)
    try:
        writer.record(7.0, 7.0, "warmup", False)
        assert entered.wait(1)

        def produce():
            for i in range(1000):
                writer.record(float(i), float(i), "accepted", False)
            finished.set()

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()
        # Completion while the storage event remains blocked is the invariant,
        # not a fragile assertion about filesystem speed or wall-clock latency.
        assert finished.wait(1)
        assert not release.is_set()
        assert writer._queue.qsize() == QUEUE_MAX
        assert writer.dropped == 1000 - QUEUE_MAX
        release.set()
        producer.join(1)
        writer.flush()
        rows = _records(writer.path)
        assert rows[-1]["raw_volts"] == 999
        assert rows[-1]["queue_dropped"] == 1000 - QUEUE_MAX
    finally:
        release.set()
        writer.close()


def test_rotation_and_restart_have_fixed_byte_bound(tmp_path):
    directory = tmp_path / "evidence"
    path = directory / "history.jsonl"
    writer = BatteryEvidence(path, clock=lambda: 1000)
    try:
        # Synchronous writer-side calls prevent queue loss obscuring rotation.
        for i in range(5000):
            writer._write_line(json.dumps({"sample_ts": i, "raw_volts": 7.123,
                                          "classification": "accepted"}))
        backup = path.with_name(path.name + ".1")
        assert backup.exists()
        assert path.stat().st_size <= MAX_BYTES
        assert backup.stat().st_size <= MAX_BYTES
        assert set(directory.iterdir()) == {path, backup}
        assert _records(path)[-1]["sample_ts"] == 4999
        assert all(len(line) + 1 <= MAX_LINE_BYTES for line in path.read_bytes().splitlines())
    finally:
        writer.close()
    restarted = BatteryEvidence(path, clock=lambda: 6000)
    try:
        restarted.record(7.0, 7.0, "warmup", False)
        restarted.flush()
        assert _records(path)[-1]["sample_ts"] == 6000
        assert sum(p.stat().st_size for p in directory.iterdir()) <= 2 * MAX_BYTES
    finally:
        restarted.close()


def test_nonfinite_raw_values_remain_strict_json(tmp_path):
    writer = BatteryEvidence(tmp_path / "history.jsonl", clock=lambda: 1000)
    try:
        for value in [float("nan"), float("inf"), float("-inf")]:
            writer.record(value, value, "accepted", False)
        writer.flush()
        assert [r["raw_volts"] for r in _records(writer.path)] == ["nan", "inf", "-inf"]
    finally:
        writer.close()


@pytest.mark.parametrize("value,error", [(float("nan"), ValueError),
                                         (float("inf"), OverflowError),
                                         (7.2, None)])
def test_poller_conversion_boundary_retains_fault_before_original_error(tmp_path, value, error):
    """Execute only the actual pure converter/diagnostic branch, never GPIO."""
    source = (Path(__file__).parents[1] / "bin/px-battery-poll").read_text()
    tree = ast.parse(source.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0])
    converter = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name == "volts_to_pct")
    loop = next(n for n in tree.body if isinstance(n, ast.While))
    conversion = next(n for n in loop.body[0].body if isinstance(n, ast.Try)
                      and "pct = volts_to_pct(volts)" in ast.unparse(n))
    writer = BatteryEvidence(tmp_path / "history.jsonl", clock=lambda: 1000)
    scope = {"raw_volts": value, "volts": round(value, 2),
             "was_charging": False, "evidence": writer}
    code = compile(ast.Module(body=[converter, conversion], type_ignores=[]),
                   "offline-poller-conversion", "exec")
    try:
        if error:
            with pytest.raises(error):
                exec(code, scope)
            writer.flush()
            row = _records(writer.path)[0]
            assert row["classification"] == "conversion_error"
            assert row["raw_volts"] == str(value)
            assert row["charging"] is False
        else:
            exec(code, scope)
            assert scope["pct"] == 50
            writer.flush()
            assert not writer.path.exists(), "ordinary sample is recorded by its later branch"
    finally:
        writer.close()


def test_poller_integration_without_executing_poller():
    source = (Path(__file__).parents[1] / "bin/px-battery-poll").read_text()
    tree = ast.parse(source.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0])
    loop = next(node for node in tree.body if isinstance(node, ast.While))
    assert isinstance(loop.body[0], ast.Try)
    statements = loop.body[0].body
    rejected = next(n for n in statements if isinstance(n, ast.If)
                    and ast.unparse(n.test) == "volts < VOLTAGE_FLOOR")
    warmup = next(n for n in statements if isinstance(n, ast.If)
                  and ast.unparse(n.test) == "warmup_remaining > 0")
    assert ast.unparse(rejected.body[0]) == "evidence.record(raw_volts, volts, 'rejected_adc', was_charging)"
    assert ast.unparse(warmup.body[0]) == "evidence.record(raw_volts, volts, 'warmup', False)"
    assert any(isinstance(n, ast.Continue) for n in rejected.body)
    assert any(isinstance(n, ast.Continue) for n in warmup.body)
    update_index = next(i for i, n in enumerate(statements)
                        if ast.unparse(n) == "charging = detector.update(volts)")
    assert ast.unparse(statements[update_index + 1]) == "evidence.record(raw_volts, volts, 'accepted', charging)"
    assert "evidence.flush" not in source
    assert "raw_volts = get_battery_voltage()" in source
    assert "volts = round(raw_volts, 2)" in source
