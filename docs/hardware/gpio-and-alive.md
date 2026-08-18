# GPIO Exclusivity and px-alive

**Owns:** which process may touch the hardware, and how it hands over.
`bin/px-alive`, `bin/px-env`'s `yield_alive`, `src/pxh/gpio_lease.py`.

Decision authority — whether an action is *allowed* — is
[architecture/policy-and-authority](../architecture/policy-and-authority.md).
This page is about physical exclusivity.

---

## Invariant

### Exactly one process holds the Picarx handle

`Picarx()` claims GPIO5 via `reset_mcu()`, and **`close()` does not release
it** — only full process exit does, and `lgpiod` needs time to reclaim the pin
after the client fd closes.

`px-alive` is the default holder. It keeps a **persistent** handle; do not
refactor it to create and destroy one per action, because each `reset_mcu()`
leaks GPIO5.

### Handover is by signal, then by lease

Two mechanisms, for two different durations:

**Short tool runs — `yield_alive`** (defined in `bin/px-env`). Sends `SIGUSR1`
to the pid in `$LOG_DIR/px-alive.pid`, polls `/proc/<pid>` until it disappears,
then waits a further 2.0s for `lgpiod` to reclaim the pin. systemd restarts
px-alive 10s later.

**Long-running owners — the lease.** `src/pxh/gpio_lease.py` holds a tokenized
`state/gpio_lease.json` and refreshes it while hardware is in use.
`GpioLeaseGuard` acquires it; `wander.py` exports `PX_GPIO_LEASE_ID`, which is
how `bin/tool-describe-scene` and `bin/tool-announce` **borrow** the lease
instead of aborting when they find one held.

`state/exploring.json` is **not** a lease. It describes wander intent and state
only. Do not use it to arbitrate hardware.

> **Known defect — [#205](https://github.com/adrianwedd/spark/issues/205).**
> `yield_alive`'s poll loop is `for i in $(seq 25)` at 0.2s — a 5s budget with
> **no failure branch**. A slow px-alive shutdown (measured 8.75s, camera
> teardown) falls through to the `sleep 2.0` and hands the caller a bogus
> success, which then dies with `'GPIO busy'`. It reads as flaky hardware.
> Not fixed here.

### Readiness is not liveness

`px-alive.service` is `Type=notify`. `WatchdogSec=15` **only arms after
`READY=1`**, which the daemon sends from `notify_ready()` at the first state
where it is actually working — holding the Picarx handle, or deliberately not
holding it (on charger, in I2C backoff).

Hardware acquisition therefore runs under `TimeoutStartSec=60`, because
`Picarx.__init__` can block past 15s contending for I2C with a tool that just
took GPIO. Normal acquisition is ~6s. Pre-`READY` heartbeats also send
`EXTEND_TIMEOUT_USEC`, covering an unbounded park behind a foreign lease.

**Do not add heartbeats inside initialisation instead.** That would keep the
watchdog fed while wedged, blinding it to the exact thing it exists to catch.

### `os.getlogin()` must stay patched

`picarx.py:48` calls `os.getlogin()` in `Picarx.__init__()`. Under systemd
there is no `/dev/tty`, so it raises `OSError: [Errno 6]`.

`~/.local/lib/python3.11/site-packages/usercustomize.py` wraps `os.getlogin()`
with a fallback to `LOGNAME`/`USER`. **Do not remove it** — it affects all 14+
GPIO scripts, and root needs its own copy after a re-image.

### The grayscale ADC lies for the first ~0.75s

Reads taken immediately after `Picarx()` return a fabricated
`[2571, 3085, 3599]`. Use `wander.wait_for_grayscale()`. A *partially* latched
read counts as latched. `race.py` has not been verified against this.

---

## Why it looks like this

*History, not rule.*

The 2.0s tail on `yield_alive` is empirical, not theoretical: `px.close()`
returning is not the same event as the kernel releasing the pin, and 1s was not
enough.

The readiness/liveness split came out of a restart storm — 86 watchdog kills in
a measured 6 hours. Three causes, and the first was a pre-`READY` `WATCHDOG=1`
that armed the watchdog before the daemon was doing anything watchable. The
other two were SD-card fsyncs and are covered in
[operations/state-and-runtime](../operations/state-and-runtime.md).

What finally cracked it was a `/proc` D-state sampler, after three wrong
theories. Re-run that sampler after *each* fix rather than after all of them —
the storm had more than one cause and fixing one left it looking unchanged.
