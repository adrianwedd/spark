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


from pxh.wake_audio import _ratecv  # canonical copy; bin/px-wake-listen keeps a fallback clone


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


# ── Anti-aliased streaming resampler (pxh.wake_audio) ───────────────────

import numpy as np
import pytest

from pxh.wake_audio import StreamingResampler


def _tone(freq_hz: float, duration_s: float = 1.0, rate: int = 44100,
          amplitude: int = 12000) -> bytes:
    n = int(rate * duration_s)
    t = np.arange(n) / rate
    samples = (amplitude * np.sin(2 * np.pi * freq_hz * t)).astype(np.int16)
    return samples.tobytes()


def _band_power_db(pcm: bytes, rate: int, freq_hz: float, rel_to: float) -> float:
    """Power at freq_hz relative to rel_to (dB), via FFT peak in a ±100 Hz band."""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    # Skip filter warm-up
    x = x[rate // 10:]
    win = np.hanning(len(x))
    spec = np.abs(np.fft.rfft(x * win))
    freqs = np.fft.rfftfreq(len(x), 1 / rate)

    def peak(f):
        band = (freqs > f - 100) & (freqs < f + 100)
        return spec[band].max()

    return 20 * np.log10(peak(freq_hz) / max(rel_to, 1e-9))


class TestStreamingResampler:
    """44100→16000 resampling must not alias ultrasonic/HF content into the speech band."""

    def test_rejects_alias_of_12khz_tone(self):
        """A 12 kHz tone must NOT reappear at its 4.1 kHz alias in 16 kHz output."""
        pcm = _tone(12000)
        rs = StreamingResampler(44100, 16000)
        out = rs.process(pcm)
        x = np.frombuffer(out, dtype=np.int16).astype(np.float64)
        ref = 12000 * len(x) / 4  # rough full-scale-tone FFT peak reference
        # Alias of 12 kHz when sampled at 16 kHz: 16000 - 12000 = 4000 Hz
        alias_db = _band_power_db(out, 16000, 4000.0, ref)
        assert alias_db < -40, f"12 kHz tone aliased to 4 kHz at {alias_db:.1f} dB"

    def test_preserves_speech_band_tone(self):
        """A 1 kHz tone passes through at roughly unity gain."""
        pcm = _tone(1000)
        rs = StreamingResampler(44100, 16000)
        out = rs.process(pcm)
        x = np.frombuffer(out, dtype=np.int16).astype(np.float64)
        # RMS of a 12000-amplitude sine ≈ 8485; allow ±1.5 dB
        rms_out = np.sqrt(np.mean(x[1600:] ** 2))
        assert 7000 < rms_out < 10300, f"1 kHz tone RMS {rms_out:.0f}, expected ~8485"

    def test_streaming_matches_batch(self):
        """Chunked processing equals one-shot processing (streaming state is exact)."""
        rng = np.random.default_rng(42)
        pcm = (rng.integers(-8000, 8000, 44100)).astype(np.int16).tobytes()
        batch = StreamingResampler(44100, 16000).process(pcm)

        rs = StreamingResampler(44100, 16000)
        chunks = b""
        for i in range(0, len(pcm), 2048):
            chunks += rs.process(pcm[i:i + 2048])

        assert chunks == batch

    def test_output_length(self):
        """1 s at 44100 → ~16000 samples out."""
        pcm = _tone(440, duration_s=1.0)
        out = StreamingResampler(44100, 16000).process(pcm)
        assert abs(len(out) // 2 - 16000) < 20

    def test_old_ratecv_aliases_without_filter(self):
        """Regression guard: the plain linear-interp path DOES alias (documents why
        the FIR pre-filter exists). If this starts passing, the resamplers may
        have been swapped and the guard test above is meaningless."""
        pcm = _tone(12000)
        out, _ = _ratecv(pcm, 2, 1, 44100, 16000, None)
        x = np.frombuffer(out, dtype=np.int16).astype(np.float64)
        ref = 12000 * len(x) / 4
        alias_db = _band_power_db(out, 16000, 4000.0, ref)
        assert alias_db > -20, "linear interp unexpectedly rejects aliases now"
