# The Resident Brain

**Owns:** SPARK's persistent Claude Code sessions — `src/pxh/brain.py`,
`src/pxh/brain_daemon.py`, `src/pxh/tmux_claude.py`, `bin/px-brain`,
`bin/px-claude-session`, `bin/px-brain-status`, `bin/tool-brain-reply`.

---

## Invariant

### SPARK's Claude calls are migrating off `claude -p` onto a resident session

This is a settled direction, not a tradeoff to re-argue. A one-shot subprocess
throws away context on every call and cannot use SPARK's own tools.

The migration is incomplete and that is expected. `PX_BRAIN_KINDS` (default
`research,compose,post_qa,reflection`) selects which kinds route to the brain;
everything else still takes the old path. It is read at call time so the
rollout can be widened or rolled back live, and there is one dial rather than
two that can disagree — `bin/px-post` consults the same one for its QA gate.

`evolve` cannot move until the brain can work inside a git worktree: a resident
session's tool envelope is fixed at launch and cannot be widened per call.

Remaining `claude -p` call sites are listed in
[operations/llm-routing](../operations/llm-routing.md).

### Replies come back through the filesystem, never the pane

`capture-pane` returns *rendered* terminal output — wrapping, spinners, ANSI
escapes, a finite scrollback. An answer scraped from it is at the mercy of the
terminal. The session answers by running a tool instead.

**Pane for humans, filesystem for machines.**

Mailbox at `state/brain/<session>/`:

| Path | Meaning |
|---|---|
| `inbox/<uuid>.json` | request |
| `outbox/<uuid>.json` | reply, written by `bin/tool-brain-reply` |
| `dead/` | swept on session recreate |
| `current.json` | the in-flight request — what wedge detection keys on |
| `validation.json` | proof a real handshake landed — what readiness means |

### Readiness is a proven round trip, never the prompt glyph

The glyph renders identically for a session that is listening and for one
sitting behind a permission dialog it cannot answer. That collapse was a real
bug, and it is why readiness is defined by evidence instead.

`bin/px-brain` sends one real request through `tool-brain-reply` and requires
one real reply echoing a nonce, recording the outcome in `validation.json`.
`brain.session_state()` derives one of four strings from that marker **at read
time, never stored**:

| State | Meaning |
|---|---|
| `validated` | a real round trip landed, on the model the marker records |
| `validating` | a handshake is in flight (or aged out) |
| `no_marker` | session is up but has never proven it can answer |
| `session_absent` | tmux has no such session |

`ask_brain()` proceeds only on `validated`. Noticing that the *configured*
model has since changed is `handshake_reason`'s separate job, and is what
triggers a re-handshake.

`bin/px-brain-status` prints all four states plus model and marker age in one
command. **Start there before attaching to a pane.**

`run_handshake` deliberately does *not* gate on `pane_ready()` — the real
reply-with-nonce is itself the authoritative test, so a glyph check would add
only a redundant, misleading gate. `handshake_reason` is different: inside the
bounded window right after a recycle it *does* consult `_is_idle`, because
there the supervisor already knows a real turn (the recycle's own
journal-append-then-`/clear`) is in flight, and the glyph is what says that
turn has finished. Injecting mid-turn splices two prompts into one and produces
a plausible-looking wrong answer.

### `ask_brain()` returns `None` on every failure and never raises

`None` means "fall back" — callers drop to the Ollama tiers exactly as they do
when Claude is unreachable. There is deliberately no exception path; this sits
under daemons.

### Single-flight lock per session

Two concurrent `send-keys` runs do not queue, they interleave into one garbled
prompt. The failure mode is not "slow", it is "both answers wrong". A caller
that cannot get the `FileLock` within `LOCK_WAIT_S` falls back rather than
queueing.

The supervisor itself is guarded by an `fcntl` flock
(`state/brain/.supervisor.lock`) so a second copy started by hand refuses to
run rather than racing the systemd-managed one.

### There is exactly one spelling of `tool-brain-reply`, and it is absolute

Claude Code matches a `Bash(...)` allowlist rule against the command by
*prefix*. `Bash($PROJECT_ROOT/bin/tool-brain-reply:*)` admits an absolute
invocation and nothing else — a bare or repo-relative spelling misses it and
raises a permission dialog nobody is attached to answer, which is a wedge.
Relative also cannot work for the io session, whose cwd is outside the repo.

`brain.TOOL_BRAIN_REPLY` is the constant. The nudge and both allowlists use it,
and both system prompts carry a `{{TOOL_BRAIN_REPLY}}` placeholder that
`bin/px-claude-session` substitutes at launch. **Never write a literal
`tool-brain-reply` into a prompt** — pinned by
`test_launcher_renders_one_absolute_reply_spelling`.

### `tool-brain-reply` validates everything

It is reachable from the untrusted io session, so it checks: a bare uuid4 id
(it becomes a filename), that the id names a *pending* request (otherwise a
valid uuid is a write primitive aimed at the outbox), and a JSON payload under
`MAX_REPLY_BYTES`.

### Mailbox directories are `1777` and the lock file `0666`

Same reasoning as `state/health/`, and load-bearing for the same reason:
SPARK's daemons do not all run as the same user, and a root-created `0755`
directory locks every `pi` daemon out of `atomic_write`'s `mkstemp`.
**Do not tighten either.** See
[operations/state-and-runtime](../operations/state-and-runtime.md).

### The supervisor's first job is holding an attached client

tmux 3.3a's `send-keys` fails outright when no client is attached, so without a
read-only attached client per session, injection fails precisely when nobody is
watching. `TERM` must be set in the unit (`tmux attach` refuses without one).

`KillMode=process` is deliberate: restarting the supervisor must not kill the
sessions it supervises.

The supervisor also sweeps pending requests to `dead/` on session (re)create,
unwedges (Escape, then kill after `ESCAPE_GRACE_S`), and recycles context on
turn count plus nightly at 02:00 Hobart — **always at an idle moment**, since a
`/clear` between nudge and reply loses the request. Wedge detection keys on
`current.json`, never on stale inbox files: an abandoned inbox entry means a
caller gave up, not that the session is stuck.

### Editing a system prompt requires killing the session

`docs/prompts/spark-brain-system.md` and `spark-io-system.md` bake in at
launch. `KillMode=process` means restarting `px-brain.service` will not reload
them — the session must be killed.

### Reflection through the brain returns the thought; it does not act

`docs/prompts/spark-brain-system.md` tells the session that a `reflection` turn
is answered by *returning* the thought. The caller dispatches the `action`
field itself, so a session that speaks during reflection makes it happen twice.

---

## Why it looks like this

*History, not rule.*

Readiness used to be defined as "the prompt glyph is showing", and this file
once documented that as the design. It was wrong in the one case that mattered:
a session parked behind a permission dialog renders the same glyph as an idle
one, so the supervisor cheerfully injected requests into a session that could
not answer, and the caller waited out its deadline.

The absolute-path rule for `tool-brain-reply` came from the same class of
failure — a repo-relative spelling missed the allowlist prefix and raised a
dialog in an unattended pane.

`px-brain.service` was, for about eleven hours, believed to be an
authentication problem. It had simply never been installed.

Design: [2026-08-01 px-brain design](../superpowers/specs/2026-08-01-px-brain-design.md)
and [2026-08-17 handshake validation](../superpowers/specs/2026-08-17-brain-handshake-validation-design.md).
Both are decision fossils — see [the fossil banner](../superpowers/README.md).
