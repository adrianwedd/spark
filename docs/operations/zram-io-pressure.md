# zram swap: breaking the SD-card IO-pressure → audio-loss loop (#283, #247)

Status: **INSTALLED and live 2026-08-26** — zram0 active at priority 100
(1024 MB, zstd), `/var/swap` retained at −2 as fallback. Installed via
`sudo bash systemd/zram/install-zram.sh` after caching a sudo credential
(`sudo -v` in a real TTY; this session's `!` shell has no PTY for a password
prompt, and #281 removed the blanket NOPASSWD). No reboot was required.
Observation period (steps 5-6) is now running against natural load.

## Post-install verification (2026-08-26T19:35 AEST)

zram0 live at prio 100 above /var/swap (−2); comp_algorithm `zstd`; swap total
1.5 GiB. All services active (px-brain `validated`, px-wake-listen, px-mind,
px-api-server, px-alive, px-frigate-stream); `/api/v1/health` = ok; cgroup
MemoryCurrent and `/proc/pressure` both still read. No swap storm — zram used
0 B immediately post-activation (kernel migrates only on new pressure; the
existing 98 MB in /var/swap stays until touched). vmstat si/so fell to 0 and
IO PSI some avg10 dropped from ~85% (mid-install, apt draining) toward ~41%
within a minute. The installer's own priority check originally fired one step
early — before the generator materialized the unit on daemon-reload — and was
fixed to reload-then-start-then-wait; a re-run or reboot now verifies cleanly.

## The loop being broken

Memory pressure → kernel swaps to `/var/swap` on mmcblk0 → SD write latency
stalls all IO → `arecord` overruns → truncated audio → tiny/hallucinated
transcripts → bad wake behaviour. #219 bounded the memory side; this change
removes the SD card from the swap path.

## Baseline (2026-08-26, before install)

| Metric | Value |
|---|---|
| RAM / swap | 3.7 GiB / 512 MiB file `/var/swap` (dphys-swapfile, prio −2), ~98 MiB used |
| zram | module present (6.12.96+rpt-rpi-v8), not loaded; no generator installed; `zstd.ko`/`lz4.ko` present |
| vm.swappiness / vm.page-cluster | 60 / 3 (defaults) |
| IO PSI (moment of capture) | some avg60 = 44.5%, full avg60 = 35.7% |
| Memory PSI | ~0 at capture (bursty; up to ~11% at overruns) |
| mmcblk0 write latency | 10 s diskstats delta: 61 write ops, 10 824 ms of write queue time — ≈177 ms/op, write side saturated |
| Overruns (lifetime instrumented) | 630 in `logs/px-wake-listen.log`; the analyzed 110-overrun set: median psi_io_some_avg10 = 91.6%, 94% above 50% |
| Overrun rate 2026-08-26 | 35 by 15:33 local, ongoing (e.g. 15:32:40 at psi_io_some = 99.98) |
| swap_free_kb at overrun (n=110) | median 10 212 KB free of 524 284 — **swap effectively exhausted at overrun time**, min 0 |
| px-wake-listen | memory.current 657 MiB, MemoryHigh 896 MiB, MemoryMax 1280 MiB |
| px-brain | memory.current 612 MiB, MemoryHigh 960 MiB, MemoryMax 1536 MiB |
| IOAccounting / IOWeight | `no` / unset on every px-* service |
| Frigate pipeline disk IO | ffmpeg `write_bytes: 0` (pure restream) — **no evidence camera pipeline competes for disk writes** |

## The change

`systemd-zram-generator` (bookworm 1.1.2) with
[`systemd/zram/zram-generator.conf`](../../systemd/zram/zram-generator.conf):
zram0, 1024 MB uncompressed, **zstd**, priority **100**. `/var/swap` stays
enabled at priority −2 as overflow — kernel uses zram first, SD swap only if
zram fills. Sysctl pair in
[`systemd/zram/99-spark-zram.conf`](../../systemd/zram/99-spark-zram.conf):
`vm.page-cluster=0`, `vm.swappiness=100` (rationale in the files).

Rollback: exact block at the bottom of
[`systemd/zram/install-zram.sh`](../../systemd/zram/install-zram.sh) — remove
two config files, swapoff zram0, restore sysctls. `/var/swap` is never
modified, so rollback restores the exact prior state.

## IOWeight decision

**Not applied.** The evidence gate in the goal was "does the camera/Frigate
pipeline materially compete during audio overruns" — it does not: the ffmpeg
restream writes nothing to disk (44 MiB read since start, 0 written), and
IOAccounting is off everywhere so there is no per-service IO signal to act on.
The disk writers are swap and state/log fsyncs. Revisit only if post-zram data
still shows IO PSI clustering at overruns.

## Post-install verification checklist

1. `swapon --show` → zram0 prio 100 above /var/swap prio −2.
2. `bin/px-brain-status`, `systemctl status px-wake-listen px-mind px-api-server`,
   one `bin/px-mic-check` when the mic is free.
3. `cat /sys/block/zram0/mm_stat` (compression ratio sanity),
   `/proc/pressure/io` trend.
4. Over the following days (natural load, no manufactured storm): overrun
   count/rate in `logs/px-wake-listen.log`, IO PSI distribution at overruns,
   `si`/`so` in vmstat, tiny-transcript rate, brain timeout rate.

## #270 note

The #270 separator is px-brain `mem_ratio` near MemoryHigh, not IO PSI. zram
changes the *cost* of reclaim, not the cgroup accounting — do not claim #270
fixed without post-change evidence; use the cleaner regime to re-test the
correlation.
