# Power and Battery

**Owns:** charge detection and the emergency shutdown.
`src/pxh/battery_trend.py`, `bin/px-battery-poll`.

---

## Invariant

### Charging cannot be detected from adjacent polls

On this pack the charge signal is **smaller than the noise around it**.
Measured 2026-08-06: ~0.004 V gained per 30s poll on the charger, against
consecutive readings swinging by up to **0.17 V**.

Differencing two polls therefore measures noise. That bug reported
`charging: false` through an entire afternoon plugged in.

### Recovery is a rolling max, then a least-squares slope

Two facts make the signal recoverable:

- **Most of the swing is load, not ADC error** — px-alive's servo sweeps drag
  the rail down. Load only ever pulls voltage *down*, never up, so a rolling
  maximum over `SMOOTH_WINDOW` recovers resting voltage. Measured: residual
  noise 0.042 V → 0.026 V.
- **A least-squares slope over `WINDOW` smoothed samples uses every point**,
  rather than differencing two of them.

| Constant | Value | Meaning |
|---|---|---|
| `SMOOTH_WINDOW` | 3 | rolling max, rejects load dips |
| `WINDOW` | 16 | trend window — 8 minutes at a 30s poll |
| `TREND_V_PER_POLL` | 0.006 | noise reaches ~0.0043 at 3σ; a real charge ~0.008 |
| `CONFIRM` | 3 | consecutive agreeing windows before the state flips |

### The thresholds are deliberately skewed, and the asymmetry is the safety property

Bootstrapped over measured residuals (2000 trials per condition): **0.6% false
charge per 30-minute window, 85% detection of a real charge.**

A false `charging` **suppresses the low-battery emergency shutdown** — which is
how a pack gets to brown the Pi out. A missed one costs a needless shutdown or
a late chime. **When in doubt, say not charging.**

Detection costs ~10 minutes, so the plug-in chime lags. That is the price of
the window, not a bug.

**Re-tune only against a fresh measured trace. Never against intuition.**

### Emergency shutdown at ≤10%

Speaks a warning, then `sudo shutdown -h now`. The alarm beeps
(`mind._play_alarm_beeps()`) are an inventoried **ungated** audio producer —
see [architecture/policy-and-authority](../architecture/policy-and-authority.md).

---

## Why it looks like this

*History, not rule.*

The rolling max is not a smoothing filter chosen for elegance. It exploits a
physical asymmetry specific to this robot: electrical load can only reduce
terminal voltage. On a system where noise were symmetric, a rolling max would
bias the estimate upward and be the wrong tool.

The skew toward under-reporting charge exists because the two error directions
have wildly different costs, and the expensive one is silent — a suppressed
shutdown does not announce itself, it just ends with a corrupted SD card.
