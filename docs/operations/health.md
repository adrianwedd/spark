# Daemon Health

**Owns:** `src/pxh/health.py`, `state/health/`, `bin/px-health-report`.

---

## Invariant

### Health answers a question `systemctl status` cannot

`systemctl` knows whether a process is running. Health knows whether it is
**doing its job**. Every daemon calls `record_success()` / `record_failure()`;
`read_health()` aggregates.

### Status is derived at read time, never stored

A dead daemon cannot leave a lying `ok` behind. The ladder:

`ok` → `degraded` (1–2 failures) → `stale` (silent past its per-component
window) → `failing` (≥3 consecutive) / `missing` (no file at all).

Absent files report `missing` rather than being silently omitted — a daemon
that never started is exactly what you want to see. `KNOWN_COMPONENTS` is
derived from `STALE_AFTER_S`, so a component with a window is a component
that gets reported.

### Staleness windows are per-component, because cadences differ by orders of magnitude

`px-mind` ticks every 60s; `px-blog` runs daily. One shared window would either
call the blog broken or never notice the mind had stopped.

| Component | Window | Why |
|---|---|---|
| `px-mind` | 300s | awareness ticks every 60s |
| `px-mind-reflection` | 3600s | backs off to 8× its 300s base when nobody is around |
| `px-alive` | 300s | idle actions are sporadic; heartbeat is periodic |
| `px-wake-listen` | 900s | reports on wake events plus a periodic heartbeat |
| `px-post` | 3600s | only runs when a postable thought appears |
| `px-blog` | 86400s | daily at its most frequent |
| `px-brain`, `px-brain-io` | 300s | ticks every 10s, throttles writes to once a minute |
| default | 900s | `DEFAULT_STALE_AFTER_S` |

### Successes throttle. Failures never do.

`record_success(..., min_interval_s=N)` exists because `px-alive` ticks twice a
second and an fsync per tick would wear the SD card.

**A failure clears the throttle**, so the recovery is written immediately.
Without that, a flapping component accumulates failures while its successes are
dropped, and reads as `failing` while working.

### Reporting never raises

Health must not be able to kill the daemon it reports on. Every reporting path
swallows its own errors.

### Read `read_health()` directly when px-mind might be down

`px-mind` publishes the aggregate to `state/health.json` and into
`awareness["health"]`, and `summarize()` feeds reflection context. That
snapshot is convenient, not authoritative. **Any reader that must be correct
when px-mind is down calls `read_health()` itself.**

### Storage is one file per component, `1777`

See [operations/state-and-runtime](state-and-runtime.md) for why. Do not
consolidate into one file and do not tighten the mode.

---

## Known limitation

**Health is blind to chronic partial failure.** Status keys off *consecutive*
failures, so a component failing a steady 18% of the time reads as `ok`
indefinitely — every failure is cleared by the next success.

When diagnosing, check success/failure **ratios**, not `overall`.

---

## Why it looks like this

*History, not rule.*

The store was a single shared JSON file first. It needed a lock; the lock was
created by whichever daemon got there first; `px-alive` runs as root and
created it 0644; every `pi` daemon then failed with `EACCES` and reported
nothing at all — a health system whose failure mode was total silence.

Splitting to one file per component removed the lock, the read-modify-write
race, and the ownership hazard in a single change, which is why it is preferred
over "fix the lock permissions".
