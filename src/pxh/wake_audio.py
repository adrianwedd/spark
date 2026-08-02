"""Anti-aliased streaming resampler for wake-word audio capture.

The USB mic records at 44100 Hz; every STT backend (SenseVoice, whisper,
Zipformer, Vosk) consumes 16000 Hz. Plain linear-interpolation resampling
folds everything above 8 kHz back into the speech band, so the signal must
be low-pass filtered *before* the rate conversion.

Numpy-only on purpose: the Pi's system python (which bin scripts run under)
has numpy but scipy availability is not guaranteed everywhere tests run.
"""

from __future__ import annotations

import struct

import numpy as np


def design_lowpass(num_taps: int, cutoff_hz: float, rate: int) -> np.ndarray:
    """Windowed-sinc (Hamming) FIR low-pass, unity DC gain."""
    if num_taps % 2 == 0:
        raise ValueError("num_taps must be odd for a symmetric linear-phase filter")
    m = np.arange(num_taps) - (num_taps - 1) / 2
    fc = cutoff_hz / rate  # normalised (cycles/sample)
    h = 2 * fc * np.sinc(2 * fc * m)
    h *= np.hamming(num_taps)
    return h / h.sum()


class StreamingResampler:
    """Stateful 16-bit mono PCM resampler: FIR low-pass, then linear interpolation.

    Exact-streaming: feeding audio chunk-by-chunk yields byte-identical output
    to one-shot processing. Group delay is (num_taps-1)/2 input samples (~1 ms
    at 97 taps / 44100 Hz) — irrelevant for STT.
    """

    def __init__(self, inrate: int, outrate: int,
                 num_taps: int = 97, cutoff_hz: float | None = None):
        if outrate > inrate:
            raise ValueError("only downsampling is supported")
        self.inrate = inrate
        self.outrate = outrate
        # Default cutoff: 90% of the output Nyquist, leaving transition-band
        # headroom before aliasing starts (7200 Hz for 16 kHz output).
        if cutoff_hz is None:
            cutoff_hz = 0.45 * outrate
        self._taps = design_lowpass(num_taps, cutoff_hz, inrate)
        self._tail = np.zeros(num_taps - 1, dtype=np.float64)  # input history
        self._interp_state = None

    def process(self, data: bytes) -> bytes:
        """Filter and resample one chunk; carries state across calls."""
        if not data:
            return b""
        x = np.frombuffer(data, dtype="<i2").astype(np.float64)
        buf = np.concatenate([self._tail, x])
        y = np.convolve(buf, self._taps, mode="valid")  # len == len(x)
        self._tail = buf[-(len(self._taps) - 1):]
        filtered = np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()
        out, self._interp_state = _ratecv(
            filtered, 2, 1, self.inrate, self.outrate, self._interp_state)
        return out


def _ratecv(data: bytes, width: int, nchannels: int, inrate: int, outrate: int, state):
    """Pure-Python replacement for audioop.ratecv (removed in Python 3.13).

    Linear-interpolation rate conversion of 16-bit PCM. NOT anti-aliased on
    its own — use StreamingResampler, which band-limits the input first.
    Accepts and returns a state tuple for streaming, matching the
    audioop.ratecv(data, width, nchannels, inrate, outrate, state) signature.
    """
    if width != 2:
        raise ValueError(f"only 16-bit (width=2) supported, got {width}")

    from math import gcd
    d = gcd(inrate, outrate)
    inrate //= d
    outrate //= d

    n_samples = len(data) // 2
    samples = struct.unpack(f"<{n_samples}h", data)

    # State: (prev_i, d_offset, prev_sample) — d_offset tracks the fractional
    # position within the linear interpolation between input samples.
    if state is None:
        prev_i = 0
        d_offset = -outrate  # trigger reading the first sample immediately
        prev_sample = 0
    else:
        prev_i, d_offset, prev_sample = state

    out = []
    idx = 0
    cur_sample = prev_sample

    while True:
        while d_offset < 0:
            if idx >= n_samples:
                new_state = (prev_i, d_offset, cur_sample)
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
