"""Shared speaker-amp (MAX98357A, GPIO 20) enable helper.

GPIO 20 is a bare sysfs output pin toggled high by robot_hat.enable_speaker();
nothing anywhere in this codebase ever lowers it again (verified 2026-08-23 —
no disable_speaker call, no other GPIO20 write). It is unrelated to the PCA9685
servo bus px-alive holds or the camera Frigate holds, so enabling it never
contends with either for hardware ownership.

Because the pin is a one-way latch, a `pinctrl get 20` query (~4ms, no sudo)
tells every caller in every process whether it's already enabled before paying
for the real enable path, which is 2-3 nested subprocess spawns (robot_hat's
own pinctrl + sox calls, plus this module's python3 fallback) and has been
observed to take 3-17s under load. Before this module, `bin/px-wake-listen`
called the expensive path on every chime (2-4+ times per conversation turn)
and `bin/tool-voice` called it a further time per spoken line, unconditionally
and — in tool-voice's case — with no timeout and no logging at all, making it
an invisible unbounded stall candidate.
"""
from __future__ import annotations

import subprocess
import time
from typing import Callable, Optional

LogFn = Callable[[str], None]


def is_speaker_enabled() -> Optional[bool]:
    """Query the live GPIO20 state via pinctrl. None if pinctrl is unavailable."""
    try:
        proc = subprocess.run(
            ["pinctrl", "get", "20"], capture_output=True, text=True, timeout=1.0, check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.lower()
    return "op" in out and "hi" in out


def ensure_speaker_enabled(timeout_s: float = 3.0, log: Optional[LogFn] = None,
                            sudo: bool = False) -> bool:
    """Make sure the speaker amp is enabled, skipping the expensive path if it
    already is. Returns True once enabled (or already-enabled), False on
    failure/timeout of the real enable path — callers should proceed with
    playback regardless (aplay/espeak exiting 0 with no audible sound is the
    existing, already-documented failure mode; this only makes the cause
    diagnosable and avoids paying for it needlessly).
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    if is_speaker_enabled():
        _log("enable_speaker: already enabled (pinctrl), skipping")
        return True

    t0 = time.monotonic()
    try:
        from robot_hat import enable_speaker
        enable_speaker()
        _log(f"enable_speaker: direct import ok ({time.monotonic() - t0:.2f}s)")
        return True
    except ModuleNotFoundError:
        pass
    except Exception as exc:
        _log(f"enable_speaker (direct) failed after {time.monotonic() - t0:.2f}s: {exc}")

    cmd = ["/usr/bin/python3", "-c", "from robot_hat import enable_speaker; enable_speaker()"]
    if sudo:
        cmd = ["sudo", "-n"] + cmd
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip().splitlines()
            _log(f"enable_speaker (subprocess) failed after {elapsed:.2f}s: "
                 f"{err[-1] if err else 'unknown'}")
            return False
        _log(f"enable_speaker: subprocess ok ({elapsed:.2f}s)")
        return True
    except subprocess.TimeoutExpired:
        _log(f"enable_speaker (subprocess) timed out after {time.monotonic() - t0:.2f}s "
             f"(budget {timeout_s:.1f}s)")
        return False
    except Exception as exc:
        _log(f"enable_speaker (subprocess) failed after {time.monotonic() - t0:.2f}s: {exc}")
        return False
