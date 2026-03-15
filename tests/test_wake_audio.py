"""Tests for pure-Python audio helpers in px-wake-listen (replacing deprecated audioop)."""

import math
import struct


# ── Inline copies of the functions from bin/px-wake-listen ──────────────
# These are copied here so we can test without importing the full script
# (which requires pyaudio, vosk, etc.).

def rms(data: bytes) -> float:
    """Compute RMS amplitude of 16-bit PCM chunk."""
    count = len(data) // 2
    if count == 0:
        return 0.0
    shorts = struct.unpack(f"{count}h", data)
    return (sum(s * s for s in shorts) / count) ** 0.5


def _ratecv(data, width, nchannels, inrate, outrate, state):
    """Pure-Python replacement for audioop.ratecv."""
    if width != 2:
        raise ValueError(f"only 16-bit (width=2) supported, got {width}")

    from math import gcd
    d = gcd(inrate, outrate)
    inrate //= d
    outrate //= d

    n_samples = len(data) // 2
    samples = struct.unpack(f"<{n_samples}h", data)

    if state is None:
        d_offset = -outrate
        prev_sample = 0
    else:
        _, d_offset, prev_sample = state

    out = []
    idx = 0
    cur_sample = prev_sample

    while True:
        while d_offset < 0:
            if idx >= n_samples:
                new_state = (0, d_offset, cur_sample)
                return struct.pack(f"<{len(out)}h", *out), new_state
            prev_sample = cur_sample
            cur_sample = samples[idx]
            idx += 1
            d_offset += outrate

        if outrate == 0:
            val = cur_sample
        else:
            val = (prev_sample * d_offset + cur_sample * (outrate - d_offset)) // outrate
        if val > 32767:
            val = 32767
        elif val < -32768:
            val = -32768
        out.append(val)
        d_offset -= inrate

    new_state = (0, d_offset, cur_sample)
    return struct.pack(f"<{len(out)}h", *out), new_state


# ── Tests ───────────────────────────────────────────────────────────────

class TestRms:
    """Test pure-Python RMS calculation."""

    def test_silence(self):
        """RMS of silence (all zeros) is 0."""
        data = struct.pack("<4h", 0, 0, 0, 0)
        assert rms(data) == 0.0

    def test_empty(self):
        """RMS of empty buffer is 0."""
        assert rms(b"") == 0.0

    def test_known_value(self):
        """RMS of constant amplitude samples."""
        # All samples = 1000 → RMS = 1000.0
        samples = [1000] * 100
        data = struct.pack(f"<{len(samples)}h", *samples)
        assert abs(rms(data) - 1000.0) < 0.01

    def test_sine_wave(self):
        """RMS of a sine wave ≈ amplitude / sqrt(2)."""
        amplitude = 10000
        n = 1000
        samples = [int(amplitude * math.sin(2 * math.pi * i / n)) for i in range(n)]
        data = struct.pack(f"<{n}h", *samples)
        expected = amplitude / math.sqrt(2)
        assert abs(rms(data) - expected) < 50  # within 0.5% tolerance

    def test_max_amplitude(self):
        """RMS handles full-scale 16-bit samples."""
        data = struct.pack("<2h", 32767, -32768)
        result = rms(data)
        assert result > 32760

    def test_matches_manual_calculation(self):
        """RMS matches hand-computed value."""
        samples = [3, 4]  # sqrt((9 + 16) / 2) = sqrt(12.5) ≈ 3.5355
        data = struct.pack(f"<{len(samples)}h", *samples)
        expected = math.sqrt(12.5)
        assert abs(rms(data) - expected) < 0.001


class TestRatecv:
    """Test pure-Python sample-rate conversion."""

    def test_basic_downsample(self):
        """Downsampling produces fewer output samples."""
        n_in = 44100  # 1 second at 44100 Hz
        samples = [int(1000 * math.sin(2 * math.pi * 440 * i / 44100)) for i in range(n_in)]
        data = struct.pack(f"<{n_in}h", *samples)
        out, _ = _ratecv(data, 2, 1, 44100, 16000, None)
        n_out = len(out) // 2
        # Should produce ~16000 samples (± a few for rounding)
        assert abs(n_out - 16000) < 10

    def test_stateful_streaming(self):
        """Stateful resampling across chunks produces correct total output."""
        # Generate 2 seconds of audio at 44100 Hz in 4 chunks
        total_samples = 44100 * 2
        chunk_size = total_samples // 4
        samples = [int(500 * math.sin(2 * math.pi * 440 * i / 44100)) for i in range(total_samples)]

        state = None
        total_out = b""
        for c in range(4):
            start = c * chunk_size
            end = start + chunk_size
            chunk_data = struct.pack(f"<{chunk_size}h", *samples[start:end])
            out, state = _ratecv(chunk_data, 2, 1, 44100, 16000, state)
            total_out += out

        n_out = len(total_out) // 2
        # Should produce ~32000 samples for 2 seconds at 16000 Hz
        assert abs(n_out - 32000) < 20

    def test_identity_rate(self):
        """Same input and output rate produces same number of samples."""
        samples = list(range(100))
        data = struct.pack(f"<{len(samples)}h", *samples)
        out, _ = _ratecv(data, 2, 1, 16000, 16000, None)
        assert len(out) == len(data)

    def test_unsupported_width(self):
        """Width != 2 raises ValueError."""
        import pytest
        with pytest.raises(ValueError, match="only 16-bit"):
            _ratecv(b"\x00\x00\x00\x00", 4, 1, 44100, 16000, None)
