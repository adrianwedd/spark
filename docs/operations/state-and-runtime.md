# State and Runtime

**Owns:** where data lives and how it is written.
`src/pxh/state.py`, `src/pxh/runtime_paths.py`, and the `state/` directory.

---

## Invariant

### Runtime state is not source code

`state/` holds the robot's living state. Almost none of it is tracked: only
`state/session.template.json` and `state/spark-reflect/CLAUDE.md` are in git.

**Never commit a runtime state file.** A tracked `session.json`,
`awareness.json`, or `thoughts-*.jsonl` would make every deploy a state
rollback and would put a child's session data into a public repository. The
`.gitignore` rules are a safety mechanism, not tidiness.

`site/data/feed.json` and `site/data/blog.json` are the deliberate exception —
they are the public site's **offline fallback**, so they are tracked on purpose.

**First use:** `cp state/session.template.json state/session.json`

### Three storage classes, and putting a file in the wrong one has bitten us

| Class | Location | For |
|---|---|---|
| Durable | `state/` on the SD card | survives reboot and matters afterwards |
| Runtime | `/run/spark` (tmpfs) | rewritten every loop, meaningless after a power cut |
| Logs | `$LOG_DIR` | append-only, rotated |

`src/pxh/runtime_paths.py` owns the runtime class. `RUNTIME_DIR_ENV` is
`PX_ALIVE_HEARTBEAT_DIR` — a historical name that governs the whole runtime
directory, not just the heartbeat. Writer (`bin/px-alive`, root) and readers
(`api`, `health`, `mind`, `mcp_server`, all as `pi`) must agree on the
location, so the env var and default live in one module rather than in each of
them.

**A file rewritten every loop belongs on tmpfs.** Measured on the live Pi: a
169-byte fsync+replace into `state/` has a p50 of 12 ms but a tail reaching
**21.5 s** under load. The identical write to tmpfs measures 0.63 ms with no
tail.

### `atomic_write()` is the only way to write durable state

mkstemp + fsync + `os.replace`. The fsync is for SD-card durability; the
replace is what makes a reader either see the old file or the new one, never a
half-written one.

`mkstemp` needs **directory** write permission, which is why several state
directories are `1777` — see below.

### Session access is lock-protected, and `FileLock` is not reentrant

`state.py` guards `state/session.json` with a `FileLock` at
`LOCK_TIMEOUT_S = 10` — fail fast rather than hang forever.

**`update_session()` calls `ensure_session()` *before* acquiring the lock**,
precisely because `FileLock` is not reentrant. Moving that call inside the lock
deadlocks.

Readers that must not block have `load_session_readonly()`.

### Directories shared between root and pi are `1777`

`state/health/` and `state/brain/` are created sticky and world-writable, like
`/tmp`. `_ensure_health_dir()` re-chmods on every write.

This is load-bearing. SPARK's daemons do not all run as the same user —
`px-alive` and `px-battery-poll` run as root while everything else runs as
`pi` — and a root-created `0755` directory locks every `pi` writer out of
`atomic_write`'s `mkstemp`. Whichever user wins the creation race, both can
write.

**Do not "tighten" these to 0755.**

### One file per component, not one shared file

`state/health/<component>.json`. A shared file would need a `FileLock`, and a
root-created lock at 0644 locks out every `pi` daemon with `EACCES`.
Per-component files remove the lock, the read-modify-write race, and the
ownership hazard together. See [operations/health](health.md).

---

## Why it looks like this

*History, not rule.*

The tmpfs split was forced by a watchdog restart storm on `px-alive`. A `/proc`
sampler caught the daemon in uninterruptible sleep on `fsync` of
`state/tmp<rand>.tmp` in 27 of 58 samples, 24 of them parked in
`jbd2_log_wait_commit`, with 23 consecutive samples on a single temp file. That
accounted for 66 of 86 watchdog kills in a measured 6-hour window.

`WatchdogSec=15` sits *under* the 21.5 s write tail, so systemd was SIGABRT-ing
a perfectly healthy daemon — and because the process was blocked in
uninterruptible I/O, it took a SIGKILL to actually die.

The heartbeat moved to tmpfs first and the storm continued, because the live
sonar write had been left behind and was enough to sustain it on its own. That
is why `runtime_paths.py` is generic in the filename rather than one pair of
helpers per file: **anything that lands in this class later gets the same
treatment automatically.**
