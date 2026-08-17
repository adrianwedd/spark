# Brain handshake validation — proving a session can answer before trusting it

Date: 2026-08-17
Status: design agreed in review; spec written for review
Extends: `docs/superpowers/specs/2026-08-01-px-brain-design.md`

## 1. The problem: `❯` is not a readiness signal

The px-brain design treats `pane_ready()` — the prompt glyph `❯` appearing in
`capture-pane` — as the signal that a session is listening. The first end-to-end
run against a real Claude TUI (2026-08-17) showed that it is not. Three findings,
in the order they bit:

1. **A permission dialog still shows the glyph.** When a nudge names a command
   the launcher's `--allowedTools` does not admit, Claude Code raises a dialog
   and waits for a human. The pane looks ready. `ask_brain` injects, the session
   never replies, the caller times out and falls back.
2. **The wedge is unrecoverable while reporting `ok`.** `check_wedge()`
   (`brain_daemon.py:191`) reads `pane_ready()` and concludes "prompt is back —
   the session finished or gave up", clears its escalation state, and returns.
   The tick then records `record_success()` because the pane is ready. So a
   session that cannot answer a single request reports healthy, forever.
3. **The transport is fine.** tmux injection, the mailbox, `atomic_write`, and
   `tool-brain-reply`'s validation all work. What is missing is any evidence
   that a *round trip* works.

The common cause is that every readiness check to date is an observation of
rendered terminal output — the exact thing the mailbox exists to avoid trusting.
The fix is a positive test: **before any caller is allowed to use a session, the
supervisor sends it one real request and requires one real reply.**

That handshake is not a ping. It is `tool-brain-reply` executing under the real
permission rules, from the real cwd, with the real allowlist — the same path
every subsequent request takes. A successful handshake is proof that the
allowlist spelling, the system prompt's `{{TOOL_BRAIN_REPLY}}` substitution, the
mailbox permissions, and Claude's own onboarding state all line up. Nothing else
in the system tests those together, and each of them has already broken once.

For `spark-io` the handshake proves something more valuable still: that the
untrusted-input boundary is a working channel and not merely a configured one.
That session has exactly one tool. A handshake that succeeds there is the only
evidence that a one-tool envelope is usable at all.

## 2. The state machine

### 2.1 The marker

One file per session replaces the current `model` marker:

```
state/brain/<session>/validation.json
```

```json
{
  "state": "validating" | "validated",
  "request_id": "<uuid4>",
  "model": "claude-haiku-4-5-20251001",
  "attempt": 1,
  "updated_at": "2026-08-17T04:15:02Z"
}
```

The `model` field is authoritative, not advisory: it names the model that
answered the handshake that produced this marker. There is no state in which the
marker claims a model the session has not demonstrably answered on. The separate
`state/brain/<session>/model` file is removed — one fact, one file.

The marker is created **0644, not 0666**. The 1777 reasoning that governs the
mailbox directories and `state/health/` does not transfer, and it is worth
saying why rather than copying the mode across out of habit: those are
world-writable because they have *many* writers running as different uids. This
marker has exactly one writer — the supervisor, always `pi` — and every other
process only reads it. Handing write permission to uids that never write is
permission for no reason, and it would let a compromised or confused caller
forge a `validated` marker for a session that cannot answer, which is the exact
claim this whole design exists to make unforgeable.

### 2.2 States

`brain.session_state(session, model=None) -> str` derives state at read time,
never storing it — same discipline as `health.py`, and for the same reason: a
dead supervisor must not be able to leave a lying `validated` behind. `model` is
optional and usually omitted: a caller that accepts the session's own model asks
only whether the session can answer.

| State | Condition | Caller | Log | Health |
|---|---|---|---|---|
| `validated` | session exists, marker `validated`, and — if the caller named a model — the marker's model matches | inject | — | success |
| `validating` | session exists, marker `validating`, `updated_at` newer than `VALIDATION_CEILING_S` | fall back | info | untouched |
| `no_marker` | session exists, marker absent — or `validating` and older than `VALIDATION_CEILING_S` | fall back | error | failure |
| `session_absent` | `session_exists()` is false | fall back | error | failure |

`UNVALIDATED` is the umbrella for the last three: in all of them the caller does
the same thing, which is fall back quietly to the Ollama tiers. They differ only
in loudness and in what a human should go and do.

The split exists because `validating` is a **normal, expected state**. It covers
the whole startup window after every boot and every nightly recycle. A single
loud `no_marker` state would fire on healthy operation several times a day, and
an alarm that cries wolf on a working system is un-taught within a week. The
loud states are the ones that mean nobody is coming: a session that exists and
nobody is handshaking, or no session at all.

`session_absent` earns its own row beyond loudness. "px-brain is down or the
session is onboarding-blocked" is two different repairs, and `session_exists()`
separates them for free:

- **`session_absent`** — the supervisor is not doing its job. Check
  `systemctl status px-brain`, check the tmux server, check the socket dir mode.
- **`no_marker`** — the session is up and cannot answer. This is the onboarding
  wall (a fresh `claude` in `state/brain/io/` asking for trust), a permission
  dialog, or an allowlist mismatch. Attach and look:
  `tmux -S /tmp/tmux-1000/px-mind attach -r -t spark-io`.

A stale `validating` marker degrades to `no_marker` rather than getting its own
state: a supervisor that died mid-handshake leaves exactly that, and the repair
is the same as any other "nobody is working on it".

### 2.3 The handshake

Owned by the supervisor. Callers never handshake — `brain_daemon.py` exists
precisely so that no daemon has to drive session lifecycle on its own timing.

**Three triggers**, and the third is the one that makes the state machine
closed:

- on session (re)create;
- on any transition out of `validated` (model change, recycle);
- **on observing `no_marker` for a session that exists.**

The third is not redundant with the first two, because `no_marker` is the only
state you arrive at by *aging* rather than by an event. A supervisor killed
mid-handshake leaves a session that exists, was never validated, and whose
`validating` marker crosses `VALIDATION_CEILING_S` some time later with nobody
watching. Neither edge ever fires again: nothing recreates the session, because
tmux still has it. Without this trigger the session sits loud and unrepaired
until a human attaches — which is exactly the "up and cannot answer" case §2.2
writes a repair line for, with no automatic path to that repair. So the tick is
**level-triggered on state**, not edge-triggered on events: any tick that
observes `no_marker` on an existing session starts a handshake, subject to the
one-validation-per-tick rule in §2.6.

Then, in order:

1. **Sweep, at one of two widths.** On session creation, sweep *all* pending
   inbox entries to `dead/` (unlocked, per §2.8). On the level-triggered path
   there is no creation, so instead sweep **exactly one file**: the
   `inbox/<request_id>.json` named by the aged `validating` marker about to be
   replaced. Then delete `validation.json`.

   The narrow sweep is not tidiness. A supervisor killed mid-handshake leaves
   its request in the inbox; step 4 then mints a *fresh* id (§2.4), so nothing
   ever claims the old file, and nothing recreates the session to sweep it. The
   sweep is how that orphan gets **recorded** — `dead/` is the audit trail, and
   a request that vanished from the inbox without appearing there is a request
   nobody can later account for. It is not, on its own, what keeps the session
   recycling; that is the predicate change below.

   It is safe outside the lock for a stronger reason than the creation sweep's:
   the marker names the one file it may delete, so there is no glob and no
   discovery step, and that request belongs to a handshake whose only waiter —
   a previous supervisor — is dead. If the marker is absent entirely rather than
   aged, there is nothing to sweep: no handshake had been sent.
2. **Acquire the single-flight `FileLock`** for the session and hold it for the
   whole handshake. This is the same lock `ask_brain` takes, so no caller can
   inject into a session that is mid-handshake, and no handshake can splice
   itself into a caller's turn.
3. **Take `ensure_session()`'s word for the glyph, and do not wait again.**
   `tmux_claude.ensure_session()` already polls `pane_ready()` for up to its own
   `STARTUP_TIMEOUT_S` before returning (`tmux_claude.py:209-217`), so a second
   wait here would spend that budget twice — see §2.6. Note it returns `True` on
   `session_exists()` alone when the prompt never appeared, and that is fine
   *here specifically*: the glyph is now a best-effort hint about when to start
   typing, and the handshake is the authoritative readiness test. A session that
   never showed a prompt gets nudged anyway and fails validation on the merits.
4. **Settle** `SETTLE_S`, then generate one fresh `request_id` (uuid4) and write
   the marker as `validating`.
5. **Write the handshake request** to `inbox/<request_id>.json` with
   `kind: "handshake"`, and write `current.json`.
6. **Nudge**, and start the clock. The handshake deadline runs from *this
   moment* — the injection of the nudge — not from session creation.
7. **Await `outbox/<request_id>.json`** for `HANDSHAKE_TIMEOUT_S`.
   - Reply → rewrite the marker as `validated` with the model, clear
     `current.json`, `record_success()`, done.
   - Timeout → send Escape, increment `attempt`, re-nudge the **same**
     `request_id`, and wait again. Up to `HANDSHAKE_ATTEMPTS` per session.
   - Attempts exhausted → `record_failure()`, `kill_session()`, drop the holder,
     return. The next tick recreates the session, which sweeps and starts a
     handshake with a **new** `request_id`.

**`_is_idle()` stops asking about files and asks about live requests.** The
sweep records the orphan; this is what stops any orphan from pinning recycles
forever, and the two compose rather than substitute. Today the predicate asks
"is a request live?" and answers it with "does a file exist?"
(`brain_daemon.py:219`) — and those differ exactly when a writer died. Every
request carries `deadline` as wall-clock (`brain.py:470`, `:475`) and
`ask_brain` gives up precisely at it (`brain.py:495`); by the time `_is_idle()`
reaches the inbox glob it has already established that `current.json` is absent
(`:217`). A pending inbox entry that is **past its deadline with no
`current.json` has no waiter by construction** — that holds for a dead
handshake's request and for a killed caller's alike, which is why one predicate
closes both without the supervisor needing to know which it is looking at.

So the glob skips entries whose `deadline` has passed. An entry whose
`deadline` is missing or not a number counts as **live**, matching
`check_wedge()`'s existing guard (`brain_daemon.py:187`) — a predicate that
cannot read a deadline must not become a reason to recycle over a real request.

This deletes nothing and transfers no ownership. The supervisor still sweeps
only the one file it wrote itself, so the scoping argument in step 1 and §5
stands untouched; what changes is that a corpse the supervisor may *not* delete
no longer blocks every future recycle on that session.

**The predicate is now clock-dependent, and that is worth saying out loud so
nobody reads it as a fresh hazard.** The Pi 4 has no RTC, so NTP steps the clock
at boot, and a large enough forward step could make a live request look expired
and let a recycle land on it. This is not new exposure: `ask_brain`'s own wait
loop (`brain.py:495`) and `check_wedge()`'s comparison (`:187`) already trust
the same wall-clock `deadline`, and a step big enough to fool the predicate has
already ended the caller's wait for the same reason. The change adds a third
reader of an existing trust assumption, not a new one.

**The handshake does its own escalation and does not rely on `check_wedge()`.**
That path clears itself whenever `pane_ready()` is true (`brain_daemon.py:191`),
which is exactly the case the handshake exists to catch — a permission dialog
that renders a prompt glyph. Leaving `check_wedge()` as-is is deliberate: for a
*caller's* request it is still the right behaviour, because `ask_brain`'s
`finally:` removes `current.json` on timeout, so a lingering past-deadline
marker there means the caller process itself died. Recorded here as a known
limitation of that path rather than fixed by this spec.

**It carries a warning label in the code, not only in this document.** The
`pane_ready()` branch in `check_wedge()` gets a comment saying in as many words
that the glyph does not prove the session can answer, that a permission dialog
renders it, and that this branch is therefore trusted only because
`ask_brain`'s `finally:` makes a lingering `current.json` mean something
narrower. A limitation recorded only in a spec is a limitation the next reader
re-derives from scratch after it bites them a second time.

**A tmux server restart mid-handshake is self-recovering and needs no special
handling.** The session disappears, `session_exists()` goes false, the next tick
reads `session_absent`, recreates, sweeps, and handshakes with a fresh id. The
in-flight handshake's caller is the supervisor itself, which is not waiting on a
deadline it cannot abandon. Stated so nobody adds machinery for it.

### 2.4 Request ids: fresh per creation, reused across retries

A retry re-nudges the id that is already sitting in the inbox. It does not write
a second request — the session may simply have been slow, and two live requests
for one handshake is two turns billed for one answer.

But a **new session creation always gets a new uuid4**, because the sweep and
the new handshake would otherwise be handling the same id in both roles: the
sweep is moving `inbox/<id>.json` to `dead/`, while the new handshake expects
`inbox/<id>.json` to be the request it is waiting on. Whichever wins, the loser
is wrong — and `tool-brain-reply` rejects an id that does not name a *pending*
request, so a late reply to the swept id would be rejected rather than
mis-delivered, turning a race into a silent extra failed attempt. A fresh id
makes the two roles disjoint by construction.

### 2.5 Model changes are recycles

A `/model` injection is a keystroke into the pane, made outside the request
path, that changes the session's configuration after validation. It gets a state
transition rather than a side effect:

1. Inside the same critical section (the single-flight lock),
2. delete `validation.json` — the session is now `no_marker` to any reader,
3. inject `/model <model>`,
4. re-handshake per §2.3,
5. rewrite the marker with the new model.

So the marker's `model` field is only ever written by a handshake that the new
model actually answered. `_switch_model()` in `brain.py` is removed; `ask_brain`
no longer injects `/model` at all.

**What triggers a model change, then.** Not a request — the supervisor, on tick,
comparing the marker's model against the session's *configured* model (the
launcher default, `PX_CLAUDE_TMUX_MODEL`, per `spec_for_session`). A mismatch
means someone changed the configuration, and the supervisor performs the
transition above at the next idle moment. A caller that names a model the marker
does not carry simply falls back; it does not queue a switch, because a session's
model is a property of the session and one caller must not be able to retune the
mind out from under the next one.

The nightly and turn-count recycles in `maybe_recycle()` move inside the same
critical section for the same reason — today they inject `/clear` with no lock
held at all, which races any caller's nudge.

**Ordering: delete the marker before injecting `/clear` or `/model`.** This is
the crash-safe direction, and it composes with `FileLock`'s flock semantics: a
supervisor killed between the two steps drops the lock at process death and
leaves no marker, so the next reader sees `no_marker`, falls back, and the next
tick re-handshakes — rather than injecting a request into a session whose
context has just been cleared. The reverse order has a window in which the
marker vouches for a session that no longer holds its identity prompt.

### 2.6 The bound that has to hold

The inner deadline covers one turn only:

- `STARTUP_CEILING_S` is spent before the handshake clock starts (§2.3 step 6),
  and it is **counted once**. `STARTUP_CEILING_S` *is*
  `tmux_claude.STARTUP_TIMEOUT_S` — the wait that `ensure_session()` already
  performs internally — not a second wait layered on top of it. An earlier draft
  of §2.3 had the supervisor wait for the glyph again after `ensure_session()`
  returned, which spent the same 45s twice and put the true worst case at 212s,
  not 167s. There is one glyph wait in the system per session start, it lives
  inside `ensure_session()`, and this term accounts for it. Corollary: on the
  level-triggered path (§2.3, third trigger) the session already exists, and
  `ensure_session()` returns immediately on `session_exists()`
  (`tmux_claude.py:190-191`), so that handshake spends none of the 45s — its
  real cost is `SETTLE_S + HANDSHAKE_ATTEMPTS × HANDSHAKE_TIMEOUT_S` = 122s. The
  identity still holds as the upper bound; it is simply not tight on the path
  that fires most often once something is already wrong.
- `HANDSHAKE_TIMEOUT_S` covers a single first turn — read a small JSON file, run
  one Bash tool. It is generous for that (60s, matching the cold-start figure
  that forced `CLAUDE_TIMEOUT` 45→60 on the describe-scene path) because a first
  turn pays model warm-up and permission evaluation.

The bound that actually needs pinning is the outer one — total time one
validation can consume:

```
STARTUP_CEILING_S + SETTLE_S + HANDSHAKE_ATTEMPTS × HANDSHAKE_TIMEOUT_S
    ≤ VALIDATION_CEILING_S
```

with proposed values `45 + 2 + 2 × 60 = 167 ≤ 180`.

`VALIDATION_CEILING_S` is **not** derived from systemd. `systemd/px-brain.service`
is `Type=simple` with no `TimeoutStartSec` and no `WatchdogSec`, so systemd has
no slowness timeout to breach and cannot restart the unit mid-handshake. The
constraint that binds is the supervisor's own structure: `tick()` walks both
sessions in one thread, so time spent validating `spark-brain` is time
`px-brain-io` is not getting a health write, against its 300s `STALE_AFTER_S`.
So:

```
VALIDATION_CEILING_S = 0.6 × min(STALE_AFTER_S["px-brain"],
                                 STALE_AFTER_S["px-brain-io"])   # 180
```

Two consequences, both stated so a later reader is not surprised:

- **At most one session is validated per tick.** Two back-to-back validations in
  one tick would double the sibling's blackout.
- **And the one chosen is the session that has waited longest** since its last
  validation attempt — not the first in iteration order. This matters because
  the level trigger in §2.3 changed the failure shape: edge-triggered validation
  is self-limiting (an event fires once), while level-triggered validation is
  self-perpetuating, since a failing session re-qualifies on every tick. With
  `tick()`'s fixed iteration over an insertion-ordered dict
  (`brain_daemon.py:270`, built at `:288`), a `spark-brain` that fails
  validation forever would consume the per-tick budget forever and `spark-io`
  would never be attempted at all — reporting failure not because anything is
  wrong with it, but because it never got a turn. That is a different case from
  the both-crash-looping one above, and `stale` is *not* an accurate report of
  it. Longest-waited is the smallest rule that makes it impossible; alternating
  the starting index would also work, but degrades as soon as a third session
  exists.
- The 0.6 factor is the margin for the rest of the tick (holder checks, wedge
  checks, the sibling's own work). If `HANDSHAKE_ATTEMPTS` or
  `HANDSHAKE_TIMEOUT_S` ever grow past this budget, the answer is not a bigger
  number — it is moving validation to a state machine that advances one step per
  tick instead of blocking. A test asserts the inequality so that decision is
  forced rather than discovered.

**The inequality bounds one session's validation, not a run of them**, and that
distinction is worth stating because the arithmetic invites a wrong reading in
both directions. Summing two validations (167 + 167 = 334 > 300) looks like a
guaranteed stale sibling on boot, and it isn't: with one validation per tick,
session A's health lands at the end of its own iteration, B's a moment later,
and A's next write comes ~355s after its first — a gap of ~188s, inside the
300s window. The sum is only reached when *both* sessions handshake on every
tick, which means both are crash-looping, and in that state `stale` is an
accurate report rather than a false alarm. So: no boot-time breakage, but the
budget genuinely does not cover a sustained multi-session failure, and nobody
should later "prove" it does by adding the terms up.

`validating` deliberately does not write health success, so a session stuck in
validation goes `stale` on its own component after 300s rather than reporting
`ok`. That is the alarm that was missing.

One caveat on reading that alarm: **`stale` has two causes.**
`health._write_record` swallows `OSError` silently (`health.py:124-126`), so on a
full disk no record is written at all and every component reads `stale` —
indistinguishable from a supervisor that stopped working. The silent swallow is
correct behaviour (health reporting must never kill the daemon it reports on),
but an operator seeing `stale` should check `df` before concluding the brain is
wedged, and `bin/px-brain-status` prints free space on `state/` alongside the
session states for exactly that reason.

### 2.7 The handshake request

```json
{"id": "<uuid4>", "kind": "handshake", "payload": {"echo": "<uuid4>"},
 "deadline": 1755400000.0, "created_at": "2026-08-17T04:15:02Z"}
```

The expected reply is `{"echo": "<the same uuid4>"}`. Echoing the nonce rather
than accepting any reply means a stale reply from a previous handshake cannot
validate the current one.

Both system prompts (`docs/prompts/spark-brain-system.md`,
`spark-io-system.md`) gain a short paragraph: a `handshake` request is answered
by echoing `payload.echo` straight back through `{{TOOL_BRAIN_REPLY}}`, doing
nothing else. As with every other request, the nudge restates the protocol so a
drifted context still lands.

Handshakes are metered (`record_request("handshake")`) like any other request.
They are real Claude turns and cost real money, and a spike in the handshake
count is the visible symptom of a session restart-looping. They also count
toward `CONTEXT_TURNS`, because a handshake is a turn of context like any other.

### 2.8 One supervisor, and what the lock does not cover

`bin/px-brain` has no single-instance guard today — no PID file, no flock,
nothing. So two supervisors is not a hypothetical, and the obvious way to get
there is an operator running `bin/px-brain` in a shell to watch it while systemd
already has one. The damage is not subtle: `start_session`, `sweep_pending`,
`kill_session` and `check_wedge` all run *outside* the single-flight lock, so one
supervisor can sweep the other's in-flight handshake request into `dead/`, and
the handshake then times out against a request that no longer exists.

**The guard:** `fcntl.flock(LOCK_EX | LOCK_NB)` on
`state/brain/.supervisor.lock`, taken at startup and held for the process
lifetime. The loser exits non-zero.

**flock cannot tell the loser who holds the lock.** `LOCK_EX | LOCK_NB` fails
with `EWOULDBLOCK` and nothing else — there is no `F_GETLK` equivalent for
flock, so the holder's pid is not recoverable from the call. Since the guard's
entire value is the log line an operator reads at 3am, the winner writes its own
pid into the lock file after acquiring, and the loser reads that file and logs
it **as a hint that may be stale** — a crashed holder leaves its pid behind. The
log line says so, rather than asserting a pid that may name a process that no
longer exists. (POSIX record locks via `F_SETLK`/`F_GETLK` would report the
holder authoritatively, but carry the footgun that closing *any* fd on the file
drops the process's lock; a hint we label as a hint is the better trade for a
message that never gates a decision.)

Two deliberate choices there. It is flock rather than px-mind's PID-file-plus-
`/proc` pattern because a supervisor that can be SIGKILLed wants a guard the
kernel releases at death — no stale-PID window, no PID-reuse `cmdline` check to
get subtly wrong. And it is stdlib `fcntl` rather than `filelock` because
`bin/px-brain` runs under `/usr/bin/python3`, where `brain.py` already carries an
`ImportError` path that degrades to "no lock available"; a guard that can
silently degrade into no guard is not a guard.

The systemd interaction is the intended one: `StartLimitBurst=5` /
`StartLimitIntervalSec=300` means the losing copy gives up after five attempts
rather than restart-looping forever, and `px-brain`'s health goes `stale`, which
is visible. One supervisor wins, the other stops, and the fact that it happened
is legible.

**Which lifecycle operations take the per-session single-flight lock:**

| Operation | Lock | Why |
|---|---|---|
| Handshake (§2.3) | held | It is a request. Same splice hazard as any other. |
| Model change / recycle (§2.5) | held | Injects keystrokes into the pane. |
| `sweep_pending` on create | **not held** | Acquiring it would block the supervisor for the length of a caller's deadline — up to 1800s for `evolve`. The request being swept belongs to a session that no longer exists, so the caller is already doomed to fall back; blocking the supervisor to be tidy about it trades a lost request for a stalled brain. |
| Escape / `kill_session` on a wedge | **not held** | The wedged caller *is* the lock holder. Waiting for the lock would make unwedging structurally impossible — the one case where taking it is precisely wrong. |
| `check_wedge`, `_is_idle`, state reads | not held | Read-only. |

Note what that table means: the operations that could collide between two
supervisors are exactly the ones that must not take the lock. **The
dual-supervisor hazard is closed by the guard, not by the lock**, and no amount
of widening the lock's scope would close it without deadlocking the unwedge
path.

## 3. What callers and operators see

`ask_brain` gains **two** checks, and both are load-bearing.

**Before the lock**, as a fast path: if `session_state(session)` is not
`validated`, return `None` immediately — no lock, no injection, no request file.
This is the common case during startup — a caller that arrives while
`spark-brain` is validating gets its fallback in milliseconds instead of waiting
`LOCK_WAIT_S` behind the supervisor's lock.

**After `lock.acquire()` returns**, re-derive the state and bail if it is no
longer `validated`. The pre-lock check is a check-then-act across a lock
boundary, and the window is not theoretical: the supervisor holds the same lock
for a model change or a recycle, so a caller can pass the check, block on
`acquire()`, and wake up on the far side of a `/clear`. The failure depends on
timing in a way that gets worse as the system gets faster. If the supervisor is
slow, the caller times out at `LOCK_WAIT_S` and loses ten seconds. If the
supervisor is quick, the caller acquires the lock moments after the `/clear`
lands and injects a real request into a session that has just forgotten
everything, including its identity prompt — and gets back a confident answer
produced with no context. A wasted ten seconds is a performance bug; a plausible
wrong answer routed into SPARK's cognition is not, and only the second check
distinguishes them.

Logging follows §2.2 exactly. `brain_unavailable` gains a `state` field carrying
the derived state verbatim, so the log line names the repair:

```
brain_unavailable kind=research session=spark-brain state=no_marker
```

Health, per session component (`px-brain`, `px-brain-io`):

- Handshake succeeded → `record_success` with `detail={"model": ..., "attempt": n}`.
- Handshake exhausted its attempts → `record_failure("handshake failed after N attempts")`.
- Reader observes `no_marker` or `session_absent` → `record_failure` with that state.
- `validating` → nothing recorded. Not a success (it cannot serve) and not a
  failure (it is working on it); if it never resolves, staleness catches it.

`bin/px-brain-status` (new, small) prints the derived state, marker model,
attempt count and marker age for both sessions. The state names in the table are
the vocabulary a human uses to describe the fault, so the tool prints them
unchanged rather than prettifying them into a different set of words.

### 3.1 Every glyph site, named

The glyph is not being deleted — it is being demoted from "the session can
answer" to "the pane is accepting input", which is all `capture-pane` was ever
able to tell us. A demotion is only real if every site is accounted for, so all
seven are listed with their verdict. Two change, two go, three stay.

| # | Site | Verdict |
|---|---|---|
| 1 | `tmux_claude.pane_ready()` — `tmux_claude.py:154` | **Stays, docstring corrected.** It currently reads "True once Claude is actually listening, not merely once tmux returned", which is the claim this spec disproves. It means: the pane is accepting input. |
| 2 | `tmux_claude.ensure_session()` startup poll — `:212` | **Stays**, and becomes the *only* glyph wait per session start (§2.3 step 3, §2.6). |
| 3 | `brain._wait_ready()` poll loop — `brain.py:351` | **Removed** with `_switch_model` (§2.5). |
| 4 | `brain._wait_ready()` final check — `brain.py:354` | **Removed**. Callers no longer wait on readiness at all; they read the marker. |
| 5 | `brain_daemon.check_wedge()` — `:191` | **Stays, warning-labelled** (§2.3). The one site where the glyph's weakness is load-bearing and tolerated. |
| 6 | `brain_daemon._is_idle()` — `:221` | **Stays.** "Idle" really is a question about the pane, and it gates recycles. A recycle mistimed by a dialog is now recoverable rather than silent, because every recycle is followed by a handshake. (The *inbox* half of the same predicate, `:219`, does change — see §2.3 — but that half was never a glyph question.) |
| 7 | `brain_daemon.tick()` health success — `:277` | **Changed, and this is the bug.** This is the line that recorded `ok` for a session that could not answer a single request. Success becomes conditional on `session_state() == "validated"`, never on the glyph. |

A test asserts that `pane_ready` has no call sites in `brain.py` after this
change, so the removed pair cannot quietly come back.

## 4. Testing

Against the existing fake-brain harness (a fixture script that watches an inbox
and answers via the real `bin/tool-brain-reply`):

- **Round trip validates.** Handshake against a fake session → marker written
  `validated` with the model; `session_state()` returns `validated`.
- **Every state is reachable and derived correctly.** Marker absent + session
  present → `no_marker`. Marker `validating` fresh → `validating`. Marker
  `validating` aged past `VALIDATION_CEILING_S` → `no_marker`. No session →
  `session_absent`. Marker model ≠ requested model → not `validated`.
- **A silent session escalates.** Fake brain that never replies → Escape, retry
  on the *same* id, then kill after `HANDSHAKE_ATTEMPTS`.
- **Fresh id per creation, reused per retry.** Assert the retry re-nudges the
  same uuid, and that the id after a kill/recreate differs from the swept one.
- **Callers fall back without touching the pane.** `ask_brain` on an
  unvalidated session returns `None`, writes no inbox file, and never calls
  `tmux_claude.inject` (assert on the mock).
- **The recycle ordering.** Marker is deleted before `/clear` is injected, and
  both happen under the lock — assert the call order, and assert a caller
  holding the lock blocks the recycle.
- **`no_marker` is repaired without a human.** Aged `validating` marker + live
  session + no supervisor event → the next `tick()` starts a handshake. This is
  the closure test for the state machine; without it the loud state has a repair
  line and no path to it.
- **A failing session cannot starve a healthy one.** `spark-brain` fails
  validation on every tick; assert `spark-io` is still attempted, and that over
  N ticks each session gets roughly half the validations. The regression this
  guards is specific to the level trigger, so it must fail against a
  first-in-iteration-order implementation.
- **The dead handshake's request is swept, and recycling recovers.** Aged
  `validating` marker plus its orphaned `inbox/<old-id>.json` → after the
  level-triggered handshake, that one file is gone, a differently-named request
  is not, `_is_idle()` is true again, and a due recycle actually fires. The last
  assertion is the point: a test that only checks the file vanished would pass
  against a fix that swept the wrong file.
- **`_is_idle()` reads deadlines, not filenames.** Three cases against one
  pending `inbox/<id>.json` and no `current.json`: a **past** `deadline` →
  `_is_idle()` is true and a due recycle fires; a **future** `deadline` → false
  and the recycle is withheld; a `deadline` that is **absent or non-numeric** →
  false. The middle and last cases are what make the test meaningful — asserting
  only the first would pass against an implementation that dropped the inbox
  check altogether. This one uses a caller-shaped orphan with no marker to sweep,
  so it exercises the predicate on its own, disentangled from the sweep.
- **The post-lock re-check fires.** Caller passes the pre-lock check, the
  supervisor invalidates the marker while the caller is blocked in
  `acquire()`, and on acquiring the caller returns `None` **without** calling
  `tmux_claude.inject` — asserted on the mock, because the symptom being
  prevented is a confident answer, not an error.
- **One supervisor.** A second `brain_daemon.run()` against the same
  `state/brain/` exits non-zero rather than starting, and the guard is released
  when the first process dies (kill it and assert the second now starts).
- **Health tracks validation, not the glyph.** A session whose pane is ready but
  whose marker is absent records a *failure*, not a success — the regression
  test for the original bug (glyph site 7).
- **No glyph left in the request path.** `pane_ready` has zero call sites in
  `brain.py` (grep-style assertion over the source, per §3.1).
- **The bound holds.** `STARTUP_CEILING_S + SETTLE_S + HANDSHAKE_ATTEMPTS ×
  HANDSHAKE_TIMEOUT_S ≤ VALIDATION_CEILING_S`, with `VALIDATION_CEILING_S` read
  from `health.STALE_AFTER_S` and `STARTUP_CEILING_S` read from
  `tmux_claude.STARTUP_TIMEOUT_S` rather than literals — the same pattern as
  `test_describe_scene_timeout_has_margin_over_claude`. Reading the startup term
  from the module is what stops the double-count reappearing: if someone adds a
  second glyph wait, the identity `STARTUP_CEILING_S is STARTUP_TIMEOUT_S`
  stops describing the code.
- **Envelope, statically.** The io spec's `PX_CLAUDE_ALLOWED_TOOLS` is exactly
  `TOOL_BRAIN_REPLY_ALLOW` and nothing else.

**The io boundary test, live-marked** (`@pytest.mark.live`, run on the Pi
against the real sessions — the permission rules are enforced by Claude Code, so
no in-process harness can stand in for them):

1. Handshake against the real `spark-io` succeeds — the one-tool envelope is a
   working channel, not just a configured one.
2. A request to that same session whose fulfilment requires *any other* tool
   (e.g. "read /etc/hostname and reply with its contents") produces no reply
   before the deadline, and the pane shows a permission dialog rather than the
   contents.

Half two is the half that matters, and it is the reason this test exists rather
than a comment asserting the boundary: the io session is where untrusted text
lands, and an untested boundary is an aspiration. Half one is nearly free once
the harness is in place; half two costs one deliberately-rejected turn.

The suite must not leave that dialog on screen: the test sends Escape and
re-validates in teardown.

## 5. Not in scope

- Fixing `check_wedge()`'s `pane_ready()` branch (§2.3) — warning-labelled in
  the code, not fixed.
- **Widening the sweep** to a *caller's* abandoned inbox entry (§2.3). Sweeping
  stays creation-scoped plus the one file the supervisor wrote itself: a
  supervisor deleting requests it did not write is how that scoping got its
  reasoning in the first place, and nothing here changes it. The consequence is
  that a caller's corpse never reaches `dead/` — the audit trail covers what the
  supervisor owns, and only that. It is no longer a *recycle* problem, because
  §2.3's deadline predicate ignores it without needing permission to delete it;
  what remains out of scope is recording it.
- Making `health._write_record`'s silent `OSError` visible (§2.6). The swallow is
  correct; the ambiguity it creates between "disk full" and "daemon dead"
  belongs to `health.py` and would be a change to every component at once.
- Per-request model switching. It is removed, not redesigned; a resident
  session's model is a property of the session, and every kind that needs a
  different one either accepts the session default or waits for a supervisor
  recycle.
- Widening `PX_BRAIN_KINDS`. The rollout dial is unchanged by this spec; nothing
  new routes to the brain because handshaking exists.
