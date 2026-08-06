"""Charge-state detection from a noisy battery voltage series.

The robot HAT's ADC jitters by roughly +/-0.05V between polls, while charging
lifts a 2S pack only ~0.025V per 30s poll. Comparing consecutive readings
therefore measures noise, not charge — SPARK reported ``charging: false``
through an entire afternoon on the charger because of exactly that.

The fix is to stop looking at adjacent samples. Averaging each half of a
6-sample window drops the noise by ~sqrt(3) while the charge signal grows
linearly with the window, which separates the two cleanly.
"""
from __future__ import annotations

from collections import deque

# Samples per window. At the 30s poll interval this is 3 minutes: long enough
# for the trend to clear the noise, short enough to notice a plug/unplug.
WINDOW = 6

# Minimum difference between the two half-window means to call a trend.
# Charging separates the halves by ~0.075V; pure jitter by under 0.04V.
TREND_V = 0.04

# Consecutive windows that must agree before the reported state flips. Stops a
# single noisy window from announcing a plug-in that did not happen.
CONFIRM = 2


class ChargeDetector:
    """Tracks battery voltage and reports whether the pack is charging.

    ``update(volts)`` returns the current charging state. The state only
    changes after CONFIRM consecutive windows agree, and holds its previous
    value in between, so a flat pack never flaps.
    """

    def __init__(self) -> None:
        self._window: deque[float] = deque(maxlen=WINDOW)
        self._streak = 0
        self.charging = False

    def _trend(self) -> float:
        """Later-half mean minus earlier-half mean; positive means rising."""
        half = WINDOW // 2
        samples = list(self._window)
        earlier = sum(samples[:half]) / half
        later = sum(samples[-half:]) / half
        return later - earlier

    def update(self, volts: float) -> bool:
        self._window.append(volts)
        if len(self._window) < WINDOW:
            return self.charging  # not enough history to judge

        delta = self._trend()
        if delta >= TREND_V:
            self._streak = max(self._streak, 0) + 1
        elif delta <= -TREND_V:
            self._streak = min(self._streak, 0) - 1
        else:
            self._streak = 0  # inside the noise band — no evidence either way

        if self._streak >= CONFIRM:
            self.charging = True
        elif self._streak <= -CONFIRM:
            self.charging = False
        return self.charging
