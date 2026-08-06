"""Charge-state detection from a noisy battery voltage series.

On this robot the charge signal is smaller than the noise around it. Measured
on 2026-08-06: the pack gains ~0.004V per 30s poll on the charger, while
consecutive readings swing by up to 0.17V. Comparing adjacent polls therefore
measures noise, which is why SPARK reported ``charging: false`` through an
entire afternoon plugged in.

Two things make the signal recoverable:

* Most of the swing is *load*, not ADC error — px-alive's servo sweeps drag
  the rail down. Load only ever pulls voltage down, never up, so a rolling
  maximum over SMOOTH_WINDOW recovers the resting voltage. Measured effect:
  residual noise 0.042V -> 0.026V, and the recovered slope rises with it.
* A least-squares slope over WINDOW smoothed samples uses every point, rather
  than differencing two of them.

Thresholds are set from a bootstrap over the measured residuals (2000 trials
per condition): this configuration reports a false charge on a steady pack
0.6% of the time per 30-minute window, and detects a real charge 85% of the
time. That asymmetry is deliberate. A false ``charging`` suppresses the
low-battery emergency shutdown, which is how a pack gets to brown the Pi out;
a missed one costs a needless shutdown or a late chime. When in doubt, say
not charging.
"""
from __future__ import annotations

from collections import deque

# Rolling maximum over this many polls, to reject load dips.
SMOOTH_WINDOW = 3

# Samples in the trend window. At the 30s poll interval this is 8 minutes.
WINDOW = 16

# Minimum |slope| in volts per poll to count as a trend. Noise alone reaches
# ~0.0043 V/poll at 3 sigma on the smoothed series; a real charge runs ~0.008.
TREND_V_PER_POLL = 0.006

# Consecutive windows that must agree before the reported state flips.
CONFIRM = 3


def _slope(samples: list[float]) -> float:
    """Least-squares slope in units per sample. Positive means rising."""
    n = len(samples)
    if n < 2:
        return 0.0
    mid = (n - 1) / 2
    mean = sum(samples) / n
    num = sum((i - mid) * (v - mean) for i, v in enumerate(samples))
    den = sum((i - mid) ** 2 for i in range(n))
    return num / den


class ChargeDetector:
    """Tracks battery voltage and reports whether the pack is charging.

    ``update(volts)`` returns the current charging state. The state only flips
    after CONFIRM consecutive windows agree and holds its previous value in
    between, so a steady pack never flaps.
    """

    def __init__(self) -> None:
        self._recent: deque[float] = deque(maxlen=SMOOTH_WINDOW)
        self._smoothed: deque[float] = deque(maxlen=WINDOW)
        self._streak = 0
        self.charging = False

    def update(self, volts: float) -> bool:
        self._recent.append(volts)
        self._smoothed.append(max(self._recent))

        if len(self._smoothed) < WINDOW:
            return self.charging  # not enough history to judge

        slope = _slope(list(self._smoothed))
        if slope >= TREND_V_PER_POLL:
            self._streak = max(self._streak, 0) + 1
        elif slope <= -TREND_V_PER_POLL:
            self._streak = min(self._streak, 0) - 1
        else:
            self._streak = 0  # inside the noise band — no evidence either way

        if self._streak >= CONFIRM:
            self.charging = True
        elif self._streak <= -CONFIRM:
            self.charging = False
        return self.charging
