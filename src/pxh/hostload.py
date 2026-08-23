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

A 2026-08-23 read-only investigation of a sustained memory-pressure episode
(held up #285's live smoke test) found CPU PSI stayed at 0 the entire time
while memory/IO PSI reached 46%/65% full avg10 — this module originally only
sampled CPU PSI, so none of #270/#283's own logged events could be
correlated against the actual bottleneck. Memory/IO PSI, SwapFree and
per-unit cgroup containment pressure were added for that reason.
"""

from __future__ import annotations

from pathlib import Path

# The two services the 2026-08-23 episode found chronically pressed against
# their own cgroup ceiling. Deliberately a small fixed set, not every unit
# under system.slice — an unknown name is a caller bug (see
# cgroup_pressure_fields), not a runtime condition to shrug off.
_MONITORED_UNITS = {
    "px-wake-listen": Path("/sys/fs/cgroup/system.slice/px-wake-listen.service"),
    "px-brain": Path("/sys/fs/cgroup/system.slice/px-brain.service"),
}

# In-process only. A rate needs a prior sample; each daemon importing this
# module is a single long-lived process, so module-level state is safe and
# does not need to survive a restart — the first call in a process's
# lifetime simply reports rate=0, having no prior sample to diff against.
_last_events_high: dict[str, int] = {}


def _psi_avg10(path: Path) -> dict[str, float]:
    """`{"some": ..., "full": ...}` avg10 parsed from a /proc/pressure/* file."""
    out: dict[str, float] = {}
    try:
        for line in path.read_text().splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2 or parts[0] not in ("some", "full"):
                continue
            for token in parts[1].split():
                if token.startswith("avg10="):
                    out[parts[0]] = float(token.split("=", 1)[1])
                    break
    except (OSError, ValueError, IndexError):
        pass
    return out


def host_load_fields(prefix: str) -> dict[str, float]:
    """`{"load1_<prefix>": ..., "psi_cpu_avg10_<prefix>": ..., ...}`, never raising.

    `load1` (1-minute loadavg) lags — it's a 60s rolling window, so a brief
    spike is smoothed away by the time anything reads it. CPU-PSI `avg10` is
    the more responsive signal and is what should actually be compared
    against a specific timeout's duration. Both are kept: `load1` is the
    number anyone already knows how to read.

    `psi_cpu_avg10_<prefix>` keeps its original name and meaning (cpu's
    `some` line) for backward compatibility with existing log analysis over
    #270/#283 events. Memory and IO each report both `some` and `full`,
    since the two diverge in ways that matter — `full` means every non-idle
    task was stalled, not just one — and cpu is left as `some`-only rather
    than widened to match, since cpu `full` was 0.00 throughout the episode
    that motivated this and a second always-zero field is not worth a
    breaking rename of the existing key.
    """
    fields: dict[str, float] = {}
    try:
        fields[f"load1_{prefix}"] = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass

    cpu = _psi_avg10(Path("/proc/pressure/cpu"))
    if "some" in cpu:
        fields[f"psi_cpu_avg10_{prefix}"] = cpu["some"]

    mem = _psi_avg10(Path("/proc/pressure/memory"))
    if "some" in mem:
        fields[f"psi_mem_some_avg10_{prefix}"] = mem["some"]
    if "full" in mem:
        fields[f"psi_mem_full_avg10_{prefix}"] = mem["full"]

    io = _psi_avg10(Path("/proc/pressure/io"))
    if "some" in io:
        fields[f"psi_io_some_avg10_{prefix}"] = io["some"]
    if "full" in io:
        fields[f"psi_io_full_avg10_{prefix}"] = io["full"]

    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("SwapFree:"):
                fields[f"swap_free_kb_{prefix}"] = float(line.split()[1])
                break
    except (OSError, ValueError, IndexError):
        pass

    return fields


def cgroup_pressure_fields(unit: str, prefix: str) -> dict[str, float]:
    """Per-unit containment-pressure signal for a name in `_MONITORED_UNITS`.

    `mem_ratio_<unit>_<prefix>`: memory.current / memory.high. Above 1.0
    means the unit is being throttled by its own ceiling right now — this is
    what would have flagged px-wake-listen's 795k+ `events:high` count
    immediately instead of needing a manual read-only investigation to find
    it. Omitted if `memory.high` is `max` (no ceiling configured) or either
    file is unreadable — never a fabricated ratio.

    `events_high_<unit>_<prefix>`: the raw cumulative `memory.events` `high`
    counter. `events_high_rate_<unit>_<prefix>`: delta since this process's
    last call for this unit (0 on the first call, since there is no prior
    sample yet).

    An unknown `unit` raises `KeyError` — the monitored set is small, fixed,
    and only ever called with the two literal names below, so a bad key here
    is a caller bug worth failing loudly on rather than the silent-empty-dict
    contract `host_load_fields` uses for a genuinely absent /proc file.
    """
    cgroup_dir = _MONITORED_UNITS[unit]
    fields: dict[str, float] = {}

    try:
        current = float((cgroup_dir / "memory.current").read_text().strip())
        high_raw = (cgroup_dir / "memory.high").read_text().strip()
        if high_raw != "max":
            high = float(high_raw)
            if high > 0:
                fields[f"mem_ratio_{unit}_{prefix}"] = current / high
    except (OSError, ValueError):
        pass

    try:
        for line in (cgroup_dir / "memory.events").read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "high":
                high_count = int(value)
                fields[f"events_high_{unit}_{prefix}"] = float(high_count)
                last = _last_events_high.get(unit)
                fields[f"events_high_rate_{unit}_{prefix}"] = float(
                    high_count - last if last is not None else 0
                )
                _last_events_high[unit] = high_count
                break
    except (OSError, ValueError):
        pass

    return fields
