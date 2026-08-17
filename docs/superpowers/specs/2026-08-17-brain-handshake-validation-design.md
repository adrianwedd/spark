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

The marker is created 0666 for the same reason the mailbox directories are 1777:
SPARK's daemons do not all run as the same uid, and a root-created 0644 marker
locks every `pi` reader out of rewriting it.

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

On session (re)create, and on any transition out of `validated`:

1. **Sweep** pending inbox entries to `dead/`, delete `validation.json`.
2. **Acquire the single-flight `FileLock`** for the session and hold it for the
   whole handshake. This is the same lock `ask_brain` takes, so no caller can
   inject into a session that is mid-handshake, and no handshake can splice
   itself into a caller's turn.
3. **Wait for `pane_ready()`**, bounded by `STARTUP_CEILING_S`. This is the weak
   signal — necessary but not sufficient. It only gates when we start typing.
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

**The handshake does its own escalation and does not rely on `check_wedge()`.**
That path clears itself whenever `pane_ready()` is true (`brain_daemon.py:191`),
which is exactly the case the handshake exists to catch — a permission dialog
that renders a prompt glyph. Leaving `check_wedge()` as-is is deliberate: for a
*caller's* request it is still the right behaviour, because `ask_brain`'s
`finally:` removes `current.json` on timeout, so a lingering past-deadline
marker there means the caller process itself died. Recorded here as a known
limitation of that path rather than fixed by this spec.

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

- `STARTUP_CEILING_S` is spent before the handshake clock starts (§2.3 step 6).
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
- The 0.6 factor is the margin for the rest of the tick (holder checks, wedge
  checks, the sibling's own work). If `HANDSHAKE_ATTEMPTS` or
  `HANDSHAKE_TIMEOUT_S` ever grow past this budget, the answer is not a bigger
  number — it is moving validation to a state machine that advances one step per
  tick instead of blocking. A test asserts the inequality so that decision is
  forced rather than discovered.

`validating` deliberately does not write health success, so a session stuck in
validation goes `stale` on its own component after 300s rather than reporting
`ok`. That is the alarm that was missing.

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

## 3. What callers and operators see

`ask_brain` gains one check, before the lock: if `session_state(session)` is not
`validated`, return `None` immediately — no lock, no injection, no request file.
This is the fast path, and it is the common one during startup: a caller that
arrives while `spark-brain` is validating gets its fallback in milliseconds
instead of waiting `LOCK_WAIT_S` behind the supervisor's lock.

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
- **The bound holds.** `STARTUP_CEILING_S + SETTLE_S + HANDSHAKE_ATTEMPTS ×
  HANDSHAKE_TIMEOUT_S ≤ VALIDATION_CEILING_S`, with `VALIDATION_CEILING_S` read
  from `health.STALE_AFTER_S` rather than a literal — the same pattern as
  `test_describe_scene_timeout_has_margin_over_claude`.
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

- Fixing `check_wedge()`'s `pane_ready()` branch (§2.3) — recorded as a known
  limitation.
- Per-request model switching. It is removed, not redesigned; a resident
  session's model is a property of the session, and every kind that needs a
  different one either accepts the session default or waits for a supervisor
  recycle.
- Widening `PX_BRAIN_KINDS`. The rollout dial is unchanged by this spec; nothing
  new routes to the brain because handshaking exists.
