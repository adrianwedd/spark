"""Tests for the arecord-backed microphone capture path.

No `arecord` binary required: ArecordStream accepts a `command` override, so a
short python subprocess stands in as a deterministic PCM source.
"""

import struct
import sys
import time

import pytest

from pxh.mic_stream import (
    DEFAULT_BUFFER_S,
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


class _LockProbingStream(ArecordStream):
    """Records whether the window max is written while the drain lock is held.

    Structural, not a race: a test that tried to *trigger* the interleaving
    would be a flaky test of the scheduler. This asks the question directly —
    `_cond._is_owned()` is true only when the writing thread itself holds the
    lock `take_gap_window_ms()` drains under.
    """

    def __init__(self, *args, **kwargs):
        self.lock_held_on_write: list[bool] = []
        super().__init__(*args, **kwargs)

    @property
    def reader_gap_window_max_ms(self) -> float:
        return self._gap_window_max

    @reader_gap_window_max_ms.setter
    def reader_gap_window_max_ms(self, value: float) -> None:
        cond = getattr(self, "_cond", None)
        self.lock_held_on_write.append(bool(cond is not None and cond._is_owned()))
        self._gap_window_max = value


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


# --- #283: ALSA slack, and evidence about where the stall was --------------
#
# The app-side ring buffer is 10 s, but ALSA's own buffer is what overruns when
# the reader thread stalls, and it was `chunk_frames * 8` — 0.37 s at 44.1 kHz
# with a 2048-frame period. Production overruns are measured in *seconds*,
# which only makes sense if the reader was stalled far longer than the buffer
# could cover.


def test_alsa_buffer_holds_seconds_and_whole_periods():
    chunk_frames = 2048
    stream = ArecordStream(rate=44100, chunk_frames=chunk_frames)
    cmd = stream.build_command()
    buffer_frames = int(cmd[cmd.index("--buffer-size") + 1])
    assert buffer_frames % chunk_frames == 0, "arecord wants whole periods"
    # Sized against the *app* ring, not against a round number: if ALSA holds
    # less than the app ring, a long reader stall overruns the kernel ring
    # first, and that loss is silent — nothing counts it (#283). The app ring
    # must be the first thing to give, because it counts and logs its drops.
    assert buffer_frames / 44100 > DEFAULT_BUFFER_S, (
        "ALSA ring must exceed the app-side ring so the counted ring overflows first"
    )
    assert cmd[cmd.index("--period-size") + 1] == str(chunk_frames)


def test_reader_gap_is_tracked_per_capture():
    """The overrun line now says whether the *reader* was stalled."""
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    stream = ArecordStream(chunk_frames=chunk_frames,
                           command=_pcm_command(6, chunk_bytes, delay_s=0.05))
    try:
        with stream.capturing():
            for _ in range(6):
                stream.read(chunk_frames, exception_on_overflow=False)
        assert stream.reader_gap_max_ms > 20.0, "a 50 ms gap must be visible"
        assert stream.reader_gap_last_ms > 0.0
    finally:
        stream.close()


def test_a_new_capture_resets_the_gap_window():
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    stream = ArecordStream(chunk_frames=chunk_frames,
                           command=_pcm_command(4, chunk_bytes, delay_s=0.05))
    try:
        with stream.capturing():
            stream.read(chunk_frames, exception_on_overflow=False)
            time.sleep(0.05)
            stream.read(chunk_frames, exception_on_overflow=False)
        assert stream.reader_gap_max_ms > 20.0
        with stream.capturing():
            pass
        assert stream.reader_gap_max_ms == 0.0, "extrema are windowed, never lifetime"
    finally:
        stream.close()


def test_drop_logging_does_not_hold_the_stream_lock():
    """A blocking log write under the lock would stall every consumer."""
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    observations: list[tuple[str, bool]] = []
    stream = ArecordStream(chunk_frames=chunk_frames, buffer_s=0.01,
                           command=_pcm_command(200, chunk_bytes))

    def probe(msg: str) -> None:
        free = stream._cond.acquire(blocking=False)
        observations.append((msg, free))
        if free:
            stream._cond.release()

    stream._log = probe
    try:
        stream.start_stream()
        deadline = time.monotonic() + 5
        while stream.dropped_chunks == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert observations, "the drop path should have logged"
        assert all(free for _msg, free in observations), (
            "logging happened while the stream lock was held"
        )
    finally:
        stream.close()

def test_the_gap_window_is_written_under_the_lock_it_is_drained_with():
    """A read-then-zero drain plus a lock-free update loses the worst gap.

    `take_gap_window_ms()` reads the max and zeroes it while the reader thread
    raises it. Made mutual, a gap belongs to exactly one window. Made racy, the
    compare-and-set can be clobbered by the reset: the window that held the
    worst gap reports 0 and the *next* window reports it — a misattribution in
    the metric #283's acceptance rests on, and one that would make an overrun
    look like it happened in a quiet window (#283).
    """
    chunk_frames = 64
    chunk_bytes = chunk_frames * 2
    stream = _LockProbingStream(
        chunk_frames=chunk_frames,
        command=_pcm_command(6, chunk_bytes, delay_s=0.05),
    )
    stream.lock_held_on_write.clear()  # drop the assignments from __init__
    try:
        with stream.capturing():
            for _ in range(6):
                stream.read(chunk_frames, exception_on_overflow=False)
    finally:
        stream.close()
    assert stream.lock_held_on_write, "the reader should have recorded a gap"
    assert all(stream.lock_held_on_write), (
        "the window max was written without holding _cond, so a concurrent "
        "take_gap_window_ms() reset can drop that gap into the next window"
    )


def test_take_gap_window_reports_the_worst_gap_and_resets():
    """The overrun report only exists when something overruns; this is the same
    measurement as evidence in normal operation (#283)."""
    stream = ArecordStream(chunk_frames=8, buffer_s=0.01, alsa_buffer_s=0.5,
                           device="x", log=lambda *_: None)
    stream.reader_gap_window_max_ms = 12.5
    assert stream.take_gap_window_ms() == 12.5
    assert stream.take_gap_window_ms() == 0.0, "the window resets on read"

    # A larger gap raises the window; a smaller one does not lower it.
    stream.reader_gap_window_max_ms = 0.0
    stream.reader_gap_window_max_ms = max(stream.reader_gap_window_max_ms, 3.0)
    stream.reader_gap_window_max_ms = max(stream.reader_gap_window_max_ms, 1.0)
    assert stream.take_gap_window_ms() == 3.0
