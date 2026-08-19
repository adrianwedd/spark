# Supervisor lock scope migration

**Status:** decided, awaiting green baseline · **Issue:** #221 · **Date:** 2026-08-19

## Decisions

Four open questions, now closed. Recorded here so the implementation is a
transcription rather than a re-argument.

| Question | Call | Why |
|---|---|---|
| The unclosable migration row | **Accept and document as written.** | An old binary in checkout A cannot be taught about a lock it has never heard of by code shipped in checkout B. The bridge must not claim otherwise. |
| Socket env naming | **`PX_BRAIN_TMUX_SOCKET` is the canonical seam.** `PX_CLAUDE_TMUX_SOCKET` stays as a fallback source only. | It is what `brain.py:223` actually consumes. Docs and tests name the primary; the fallback exists for compatibility, not for reference. |
| Keep `boot_id`? | **Keep.** | No RTC on this host, and the wall clock has already jumped ~49 minutes and materially confused one incident reconstruction. `boot_id + pid + instance` is the forensic identity tuple those records need. |
| Broaden the import-time env fix? | **No.** | Fix `logging`'s call-time resolution because item 4 requires it, and let `brain_socket()` route around the tmux constant for this path. If the general pattern still matters afterwards, it gets its own issue. |

## What #221 established

The 43 duplicated `start` records in `logs/tool-brain-daemon.log` were not two
supervisors. They were pytest, writing production-shaped records into the
production log while the autouse mailbox fixture silently relocated the
supervisor lock to a per-test tmp inode. The original attribution is refuted in
the issue; the evidence is preserved there.

Falsifying it surfaced a real structural defect underneath, which is what this
design addresses.

## The defect

The guard's scope does not match the resource it guards.

| | Expression | Scope |
|---|---|---|
| Guarded resource | `brain.spec_for_session().socket` | host-global (`/tmp/tmux-1000/px-mind`) |
| Guard | `brain.brain_root() / ".supervisor.lock"` | checkout-relative (`$PX_STATE_DIR/brain`) |

One tmux server, five checkouts on this host, and a private guard per checkout.
`flock` is per-inode, so two supervisors in different checkouts contend over
nothing. The same asymmetry is what let the autouse fixture disable the guard by
accident: relocating the *mailbox* silently relocated the *guard*, because one
was derived from the other.

## Design

### 1 · One resolution, two consumers

`spec_for_session` resolves the socket at `brain.py:223`. Extract it so the
guard is keyed off literally the same call, and the two cannot drift:

```python
# brain.py
def brain_socket() -> str:
    """The tmux socket every brain session runs on, and therefore the thing
    the supervisor guard is scoped to."""
    return (os.environ.get("PX_BRAIN_TMUX_SOCKET")
            or os.environ.get("PX_CLAUDE_TMUX_SOCKET")
            or tmux_claude.SOCKET)

# brain_daemon.py
def supervisor_lock_path() -> Path:
    return Path(brain.brain_socket() + ".supervisor.lock")
```

Production path is unchanged in value: `/tmp/tmux-1000/px-mind.supervisor.lock`.
`/tmp` is correct — the guard must not outlive the tmux server it guards, and
neither survives a reboot.

Reading `PX_CLAUDE_TMUX_SOCKET` at call time also repairs, for the brain path
only, the import-time-constant defect described under Known latent below.

### 2 · Migration bridge

Changing the lock key while PID 740 holds the legacy lock would itself open a
two-supervisor window. Operator ordering (`systemctl stop` before swap) is not
sufficient on its own, so the new binary acquires **both** locks — socket first,
then legacy `brain_root()/.supervisor.lock`, both `LOCK_NB` — and refuses unless
it holds both. A failed second acquire releases the first.

| Incumbent | Challenger | Outcome |
|---|---|---|
| old, same checkout | new | new takes socket, fails legacy, refuses ✓ |
| new | new, any checkout | socket lock decides ✓ |
| old | old | unchanged ✓ |
| old in checkout A | new in checkout B | both run ✗ |

The last row is the honest limit: a pre-bridge binary knows nothing about the
socket lock, so no bridge closes it. It is not a regression — old-vs-old across
checkouts is already unguarded today, which is the defect being fixed. It is
also not live: no checkout other than `/home/pi/picar-x-hacking` has ever
created a lock file, and one systemd unit exists, at that `WorkingDirectory`.

`LOCK_NB` throughout means the dual acquire cannot deadlock; a lost race fails
fast rather than blocking.

**Removal gate.** Legacy acquisition is deleted in a follow-up, permitted only
once no pre-bridge binary can be started — i.e. every deployed checkout and the
systemd unit point at a revision at or after the bridge commit. Until then the
legacy acquire stays, and `state/brain/.supervisor.lock` is deleted as part of
that follow-up, not this change.

### 3 · Instance identity in log records

Records carry `ts` and `event` and nothing that identifies the emitting process,
which is what allowed pytest output to masquerade as a concurrency event. In
`brain_daemon._log()` only — other tools' log schemas are untouched:

```python
_INSTANCE_ID = uuid.uuid4().hex[:12]   # survives PID reuse
_BOOT_ID = _read_boot_id()             # /proc/sys/kernel/random/boot_id
```

merged into every record as `pid`, `instance`, `boot_id`. `boot_id` earns its
place on this host specifically: there is no RTC, timesyncd stepped the clock
~49 minutes forward at 11:17:37 on 2026-08-19, and `boot_id` is the one
identifier immune to a clock step.

### 4 · Observability isolation

`logging.LOG_DIR` is resolved once at import (`logging.py:45`) and read as a
module global by `log_event`, so the documented `LOG_DIR` override cannot take
effect in-process. `isolated_project` sets it only in a subprocess `env` dict.

- `log_event` calls a `log_dir()` resolver; `LOG_DIR` stays as a back-compat
  module attribute.
- A fifth autouse fixture in `conftest.py`, same shape and same `live`-marker
  escape hatch as `_isolate_session`, sets `LOG_DIR` **and**
  `PX_BRAIN_TMUX_SOCKET` to per-test tmp paths.

Setting both in one fixture is the point: a synthetic socket implies a synthetic
guard by construction, so a test cannot acquire a namespace without also
acquiring the guard that belongs to it. Bypassing the production guard becomes
explicit — a test that wants the real socket must be marked `live`.

Pinned by a test asserting the fixture actually moved both, mirroring the
existing `test_a_test_that_sets_its_own_session_path_still_wins`.

Two documentation claims are corrected in the same change, because believing
them is what made #221 hard to see:

- `CLAUDE.md:38` states these env vars are "auto-set via `conftest.py`
  `isolated_project` fixture". `isolated_project` is opt-in, not autouse, and it
  sets `LOG_DIR` only inside a subprocess `env` dict — so the sentence is wrong
  about both *when* it applies and *what* it reaches.
- `CLAUDE.md` describes the supervisor guard as `state/brain/.supervisor.lock`,
  which item 1 changes.

Note that `test_a_second_supervisor_refuses_to_start` is currently green
*because of* the defect: the autouse mailbox fixture gives it a tmp inode to
contend on. Under the new key it contends with the live daemon unless the socket
fixture lands in the same change. Items 1 and 4 must therefore ship together.

## Sequencing

`pyproject.toml` records that "the local run is this project's real gate (there
is no pytest CI workflow)". That is why pytest runs on SPARK, and it is the
structural pressure that produced #221.

1. **PR 1 — CI only.** `.github/workflows/tests.yml`, `ubuntu-latest`,
   `pytest -m "not live"`. Deps are four pure-Python packages. Establishes a
   baseline against unchanged code, so any pre-existing failures are triaged on
   their own merits. **Shipped as #222.**

   It earned its keep before merging. Run 1 exposed six tests that asserted
   properties of this robot rather than of the code; run 2 exposed two more
   whose mocks did not outlive the background job they mocked, and which on the
   losing path spawned the real `bin/px-race`. All eight were test defects; no
   production code was wrong. Both classes are written up in
   [docs/testing.md](../testing.md).

2. **PR 2 — this design.** Rebased onto the green #222 baseline. A red run is
   then unambiguously attributable to the change.

No pytest is run on SPARK for either.

## Acceptance criteria

1. Two supervisors on the same socket cannot both acquire authority, including
   from different checkouts or state roots.
2. The namespace a supervisor claims is the pair (socket, checkout) while the
   bridge holds, not the socket alone. Stated as four cases:
   - same socket, different checkouts — **must contend**, on the socket lock;
   - different sockets, different state roots — **may coexist**;
   - different sockets, same checkout — **intentionally contend**, on the
     legacy lock. This is the bridge's cost, and it is compatibility
     behaviour rather than a defect: a pre-bridge binary in that checkout
     guards on exactly that inode and knows nothing about the socket;
   - after the legacy lock is removed, the socket alone becomes the complete
     namespace and case 3 inverts. **Removal is gated by #224**, which owns
     the proof that no pre-bridge binary remains runnable.

   The suite relies on case 2, not case 3: the autouse fixtures give every
   test a tmp_path socket *and* a tmp_path `brain_root`, so both halves of
   the pair differ per test and tests do not contend.
3. Ordinary tests write no production logs.
4. Every brain-daemon record identifies its emitting process and instance.
5. Mixed-version migration cannot create a two-supervisor window, for every
   case except old-in-checkout-A vs new-in-checkout-B, which is documented
   above as unreachable by any bridge and not a regression.

## Known latent, not fixed here

- **Import-time env constants.** `logging.LOG_DIR` (line 45),
  `tmux_claude.SOCKET` (line 43) and `SessionSpec.socket = SOCKET` (line 74, a
  dataclass default bound at class-definition time) all read the environment
  once at import. Each looks overridable and is not. Item 4 fixes the first and
  item 1 routes around the second for the brain path; `SessionSpec`'s default is
  left alone to keep the blast radius small.
- **Deliberate production-log writers.** `tests/test_alive_frigate.py:32` and
  `tests/test_post.py:32` pass `LOG_DIR=PROJECT_ROOT/logs` into subprocess envs
  on purpose. Out of scope for "ordinary tests"; worth revisiting.

## Invariant this earns

> Test isolation must cover observability as well as state and effects. A test
> that writes production-shaped logs can falsify later forensics even if it
> never touches production state.

Carried into the #215 docs refactor.
