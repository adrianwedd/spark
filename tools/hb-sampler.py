#!/usr/bin/env python3
"""Passive sampler for px-alive heartbeat gaps. Touches nothing it observes.

Polls the world-readable heartbeat at 200ms and records every inter-beat gap
over a threshold, tagged with the phase label that was live when the loop went
quiet. Read-only: no imports from pxh, no writes outside its own log.

The PID is sampled alongside, because a gap has two very different causes that
look identical in the heartbeat alone:

  * pid unchanged  -> the daemon was alive and the loop stalled in-process.
    This is the signal we want; the surviving hypothesis is a wedged I2C write
    in ease(), where the beat (px-alive:288) precedes set_cam_pan_angle (:296).
  * pid changed    -> the daemon died and systemd restarted it. Watchdog kills
    and SIGUSR1 kills both land here and are NOT loop stalls.

Without that split the SIGUSR1-kill noise floor contaminates the measurement,
which is the reason to sample the PID rather than clean the noise floor first.

Usage:  ./hb-sampler.py [--threshold 5.0] [--out PATH] [--duration-h 12]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HEARTBEAT = Path(os.environ.get("PX_ALIVE_HEARTBEAT", "/run/spark/alive_heartbeat.json"))
PID_FILE = Path(os.environ.get("PX_ALIVE_PID", "/home/pi/picar-x-hacking/logs/px-alive.pid"))
POLL_S = 0.2


def read_beat() -> tuple[float, str] | None:
    """(ts, mode) from the heartbeat, or None if unreadable mid-replace."""
    try:
        rec = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        return float(rec["ts"]), str(rec.get("mode", "?"))
    except (OSError, ValueError, KeyError, TypeError):
        # os.replace is atomic, so this is a missing file or a truncated read
        # during startup -- never a partial record. Skip the sample.
        return None


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=5.0,
                    help="record gaps at or above this many seconds")
    ap.add_argument("--out", default="/home/pi/picar-x-hacking/logs/hb-gaps.jsonl")
    ap.add_argument("--duration-h", type=float, default=12.0)
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.duration_h * 3600

    prev_ts: float | None = None
    prev_mode = "?"
    prev_pid = read_pid()
    max_gap = 0.0
    max_gap_mode = "?"
    n_beats = 0
    n_events = 0
    started = time.time()

    print(f"sampling {HEARTBEAT} every {POLL_S}s, "
          f"logging gaps >= {args.threshold}s to {out}", flush=True)

    while time.time() < deadline:
        beat = read_beat()
        pid = read_pid()
        if beat is not None:
            ts, mode = beat
            if prev_ts is not None and ts > prev_ts:
                gap = ts - prev_ts
                n_beats += 1
                restarted = pid is not None and prev_pid is not None and pid != prev_pid
                if not restarted and gap > max_gap:
                    max_gap, max_gap_mode = gap, prev_mode
                if gap >= args.threshold:
                    n_events += 1
                    rec = {
                        "wall": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "gap_s": round(gap, 3),
                        # the phase live when the loop went quiet is the mode of
                        # the *earlier* beat: the stall follows it
                        "mode_before": prev_mode,
                        "mode_after": mode,
                        "pid_before": prev_pid,
                        "pid_after": pid,
                        "restarted": restarted,
                    }
                    with out.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec) + "\n")
                    print(json.dumps(rec), flush=True)
            if prev_ts is None or ts > prev_ts:
                prev_ts, prev_mode = ts, mode
        if pid is not None:
            prev_pid = pid
        time.sleep(POLL_S)

    print(json.dumps({
        "summary": True,
        "ran_h": round((time.time() - started) / 3600, 2),
        "beats_seen": n_beats,
        "events": n_events,
        "max_in_process_gap_s": round(max_gap, 3),
        "max_in_process_gap_mode": max_gap_mode,
    }), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
