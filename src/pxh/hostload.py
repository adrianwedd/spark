"""Best-effort host load snapshot, shared by anything diagnosing a deadline
blown on this Pi rather than by its own caller.

#270 (resident `voice_turn` sometimes burning its full 45s budget) and #283
(arecord ALSA-level overruns during live capture, up to 56.8s) both showed
the same shape: the code doing the timing out has no bug ask_brain's own
lock_wait_s/validating_wait_s couldn't already rule out, but the timeouts
cluster in windows of heavy concurrent host activity. This lives in its own
module, not duplicated per caller, because two independent subsystems now
need the same signal to check the same hypothesis against real production
events rather than a synthetic-load assay.
"""

from __future__ import annotations

from pathlib import Path


def host_load_fields(prefix: str) -> dict[str, float]:
    """`{"load1_<prefix>": ..., "psi_cpu_avg10_<prefix>": ...}`, never raising.

    `load1` (1-minute loadavg) lags — it's a 60s rolling window, so a brief
    spike is smoothed away by the time anything reads it. CPU-PSI `avg10` is
    the more responsive signal and is what should actually be compared
    against a specific timeout's duration. Both are kept: `load1` is the
    number anyone already knows how to read.
    """
    fields: dict[str, float] = {}
    try:
        fields[f"load1_{prefix}"] = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        line = Path("/proc/pressure/cpu").read_text().splitlines()[0]
        for token in line.split():
            if token.startswith("avg10="):
                fields[f"psi_cpu_avg10_{prefix}"] = float(token.split("=", 1)[1])
                break
    except (OSError, ValueError, IndexError):
        pass
    return fields
