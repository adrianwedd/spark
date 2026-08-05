"""Tests for the arecord-backed microphone capture path.

No `arecord` binary required: ArecordStream accepts a `command` override, so a
short python subprocess stands in as a deterministic PCM source.
"""

import struct
import sys
import time

import pytest

from pxh.mic_stream import (
    ArecordStream,
    parse_arecord_devices,
    resolve_arecord_device,
)

ARECORD_L = """**** List of CAPTURE Hardware Devices ****
card 3: vc4hdmi [vc4-hdmi], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 4: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""


def _pcm_command(num_chunks: int, chunk_bytes: int, delay_s: float = 0.0):
    """A subprocess emitting `num_chunks` chunks of a known ramp, then EOF."""
    src = (
        "import sys,time\n"
        f"for i in range({num_chunks}):\n"
        f"    sys.stdout.buffer.write(bytes([i % 256]) * {chunk_bytes})\n"
        "    sys.stdout.buffer.flush()\n"
        f"    time.sleep({delay_s})\n"
    )
    return [sys.executable, "-c", src]


# --- device resolution -------------------------------------------------------


def test_parse_arecord_devices_extracts_card_and_device():
    devices = parse_arecord_devices(ARECORD_L)
    assert devices == [
        (3, 0, "vc4hdmi [vc4-hdmi] MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]"),
        (4, 0, "Device [USB PnP Sound Device] USB Audio [USB Audio]"),
    ]


def test_resolve_prefers_matching_card_not_the_first_one():
    # The HDMI card is listed first; 'USB' must still win, or capture opens the
    # wrong device entirely.
    assert resolve_arecord_device("USB", listing=ARECORD_L) == "plughw:4,0"


def test_resolve_match_is_case_insensitive():
    assert resolve_arecord_device("usb pnp", listing=ARECORD_L) == "plughw:4,0"


def test_resolve_falls_back_to_first_card_when_no_match():
    assert resolve_arecord_device("nonexistent", listing=ARECORD_L) == "plughw:3,0"


def test_resolve_falls_back_to_default_when_no_cards():
    assert resolve_arecord_device("USB", listing="no soundcards found...") == "default"


def test_uses_plughw_and_s16le_at_the_requested_rate():
    stream = ArecordStream(device="plughw:4,0", rate=44100, channels=1,
                           chunk_frames=2048)
    cmd = stream.build_command()
    assert cmd[0] == "arecord"
    assert "-D" in cmd and cmd[cmd.index("-D") + 1] == "plughw:4,0"
    assert cmd[cmd.index("-f") + 1] == "S16_LE"
    assert cmd[cmd.index("-r") + 1] == "44100"
    assert cmd[cmd.index("-c") + 1] == "1"
    assert cmd[cmd.index("-t") + 1] == "raw"


# --- reading -----------------------------------------------------------------


def test_read_returns_exactly_the_requested_frames():
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    stream = ArecordStream(chunk_frames=chunk_frames,
                           command=_pcm_command(20, chunk_bytes))
    try:
        data = stream.read(chunk_frames, exception_on_overflow=False)
        assert len(data) == chunk_bytes
    finally:
        stream.close()


def test_no_samples_are_lost_across_reads():
    """The whole point of the change: every byte arecord produced comes out.

    PyAudio dropped ~32% here and reported nothing.
    """
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    n = 20
    stream = ArecordStream(chunk_frames=chunk_frames,
                           command=_pcm_command(n, chunk_bytes))
    try:
        got = b"".join(
            stream.read(chunk_frames, exception_on_overflow=False) for _ in range(n)
        )
        expected = b"".join(bytes([i % 256]) * chunk_bytes for i in range(n))
        assert got == expected
        assert stream.dropped_chunks == 0
    finally:
        stream.close()


def test_reads_smaller_than_a_chunk_keep_the_remainder():
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    stream = ArecordStream(chunk_frames=chunk_frames,
                           command=_pcm_command(4, chunk_bytes))
    try:
        halves = [stream.read(chunk_frames // 2, exception_on_overflow=False)
                  for _ in range(4)]
        assert b"".join(halves) == (b"\x00" * chunk_bytes) + (b"\x01" * chunk_bytes)
    finally:
        stream.close()


def test_reads_larger_than_a_chunk_span_chunks():
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    stream = ArecordStream(chunk_frames=chunk_frames,
                           command=_pcm_command(6, chunk_bytes))
    try:
        data = stream.read(chunk_frames * 3, exception_on_overflow=False)
        assert data == (b"\x00" * chunk_bytes + b"\x01" * chunk_bytes
                        + b"\x02" * chunk_bytes)
    finally:
        stream.close()


def test_read_accepts_16bit_pcm_roundtrip():
    """Sanity-check the byte plumbing against real S16_LE samples."""
    chunk_frames = 8
    samples = [0, 1000, -1000, 32767, -32768, 5, -5, 12345]
    payload = struct.pack(f"<{len(samples)}h", *samples)
    src = (
        "import sys\n"
        f"sys.stdout.buffer.write({payload!r})\n"
        "sys.stdout.buffer.flush()\n"
    )
    stream = ArecordStream(chunk_frames=chunk_frames,
                           command=[sys.executable, "-c", src])
    try:
        data = stream.read(chunk_frames, exception_on_overflow=False)
        assert list(struct.unpack(f"<{len(samples)}h", data)) == samples
    finally:
        stream.close()


# --- ring buffer -------------------------------------------------------------


def test_slow_consumer_drops_oldest_and_counts_it():
    """A stalled consumer (STT, then an LLM call) must not block the producer.

    If arecord blocked on a full pipe it would overrun its own ALSA buffer —
    exactly the failure being fixed. Drops are bounded, counted, and logged.
    """
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    logs = []
    stream = ArecordStream(chunk_frames=chunk_frames, buffer_s=0.01,
                           log=logs.append,
                           command=_pcm_command(200, chunk_bytes))
    try:
        stream.start_stream()
        deadline = time.monotonic() + 5
        while stream.dropped_chunks == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stream.dropped_chunks > 0, "producer should have outrun the buffer"
        assert any("dropped" in m for m in logs), "drops must be logged, not silent"
        # Still usable afterwards.
        assert len(stream.read(chunk_frames, exception_on_overflow=False)) == chunk_bytes
    finally:
        stream.close()


def test_idle_drops_are_classified_benign_and_logged_quietly():
    """Backlog discarded outside a capture is expected, not audio loss.

    Every voice turn stops reading for seconds (STT, LLM, SPARK speaking) and
    overflows the ring. Counting that as loss is what saturated the counter.
    """
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    logs = []
    stream = ArecordStream(chunk_frames=chunk_frames, buffer_s=0.01,
                           log=logs.append,
                           command=_pcm_command(200, chunk_bytes))
    try:
        stream.start_stream()
        deadline = time.monotonic() + 5
        while stream.dropped_chunks == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stream.dropped_idle > 0
        assert stream.dropped_active == 0, "no capture was declared"
        assert any("while idle" in m for m in logs)
        assert not any("DURING CAPTURE" in m for m in logs)
    finally:
        stream.close()


def test_drops_inside_a_declared_capture_are_reported_as_lost_audio():
    """Inside capturing(), an overflow means speech was clipped — log loudly."""
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    logs = []
    stream = ArecordStream(chunk_frames=chunk_frames, buffer_s=0.01,
                           log=logs.append,
                           command=_pcm_command(200, chunk_bytes))
    try:
        stream.start_stream()
        with stream.capturing():
            deadline = time.monotonic() + 5
            while stream.dropped_active == 0 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert stream.dropped_active > 0
        assert any("DURING CAPTURE" in m for m in logs)
    finally:
        stream.close()


def test_capturing_is_reentrant_and_resets_after_the_block():
    stream = ArecordStream(chunk_frames=64, command=[sys.executable, "-c", "pass"])
    try:
        assert stream._capturing == 0
        with stream.capturing():
            with stream.capturing():
                assert stream._capturing == 2
            assert stream._capturing == 1
        assert stream._capturing == 0
    finally:
        stream.close()


def test_total_drop_count_still_sums_both_classes():
    """dropped_chunks stays the back-compat total px-mic-check reports on."""
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    stream = ArecordStream(chunk_frames=chunk_frames, buffer_s=0.01,
                           command=_pcm_command(200, chunk_bytes))
    try:
        stream.start_stream()
        deadline = time.monotonic() + 5
        while stream.dropped_chunks == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stream.dropped_chunks == stream.dropped_active + stream.dropped_idle
    finally:
        stream.close()


def test_flush_discards_backlog_and_returns_count():
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    stream = ArecordStream(chunk_frames=chunk_frames,
                           command=_pcm_command(10, chunk_bytes))
    try:
        stream.start_stream()
        deadline = time.monotonic() + 5
        while len(stream._buf) < 5 and time.monotonic() < deadline:
            time.sleep(0.02)
        dropped = stream.flush()
        assert dropped >= 5
        assert len(stream._buf) == 0
    finally:
        stream.close()


# --- failure handling --------------------------------------------------------


def test_read_times_out_rather_than_hanging_forever():
    """A wedged mic must surface as OSError so systemd can restart the daemon."""
    src = "import time\ntime.sleep(30)\n"
    stream = ArecordStream(chunk_frames=64, read_timeout_s=0.3,
                           command=[sys.executable, "-c", src])
    try:
        with pytest.raises(OSError, match="timed out"):
            stream.read(64, exception_on_overflow=False)
    finally:
        stream.close()


def test_dead_producer_raises_after_restart_limit():
    logs = []
    stream = ArecordStream(chunk_frames=64, max_restarts=1, read_timeout_s=5,
                           log=logs.append,
                           command=[sys.executable, "-c", "pass"])
    try:
        with pytest.raises(OSError):
            stream.read(64, exception_on_overflow=False)
        assert stream.restarts == 1, "should have attempted its one restart"
    finally:
        stream.close()


def test_producer_restart_recovers_the_stream():
    """A transient exit should not end a conversation."""
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    logs = []
    stream = ArecordStream(chunk_frames=chunk_frames, max_restarts=3,
                           log=logs.append,
                           command=_pcm_command(2, chunk_bytes))
    try:
        # 2 chunks per process life; reading 4 forces at least one respawn.
        for _ in range(4):
            assert len(stream.read(chunk_frames, exception_on_overflow=False)) == chunk_bytes
        assert stream.restarts >= 1
        assert any("restart" in m for m in logs)
    finally:
        stream.close()


def test_close_terminates_the_subprocess():
    src = "import time\ntime.sleep(30)\n"
    stream = ArecordStream(chunk_frames=64, command=[sys.executable, "-c", src])
    stream.start_stream()
    proc = stream._proc
    assert proc is not None and proc.poll() is None
    stream.close()
    assert proc.poll() is not None


def test_start_stream_is_idempotent():
    src = "import time\ntime.sleep(30)\n"
    stream = ArecordStream(chunk_frames=64, command=[sys.executable, "-c", src])
    try:
        stream.start_stream()
        first = stream._proc
        stream.start_stream()
        assert stream._proc is first
    finally:
        stream.close()


def test_stderr_lines_are_logged():
    """arecord's own 'overrun!!!' warnings must reach the log."""
    src = "import sys\nsys.stderr.write('overrun!!! (at least 12.345 ms long)\\n')\n"
    logs = []
    stream = ArecordStream(chunk_frames=64, max_restarts=0, log=logs.append,
                           command=[sys.executable, "-c", src])
    try:
        stream.start_stream()
        deadline = time.monotonic() + 5
        while not any("overrun" in m for m in logs) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert any("overrun" in m for m in logs)
    finally:
        stream.close()


def test_context_manager_closes():
    src = "import time\ntime.sleep(30)\n"
    with ArecordStream(chunk_frames=64, command=[sys.executable, "-c", src]) as s:
        proc = s._proc
    assert proc is not None and proc.poll() is not None
