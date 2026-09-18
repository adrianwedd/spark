# OS-level delegated-agent isolation — design (#281 phase 2)

Status: **design + working prototype + the artifacts in this repo; not
installed.** No live user, group, sudoers, or systemd unit change has been
made for this phase — everything ships inert and nothing in the running system
reads it. The three artifacts are `systemd/sbin/px-research-run` (the root
launcher), `bin/px-research-worker` + `src/pxh/research_worker.py` (what runs
inside), and one line in `systemd/sudoers.d/picar-x-services`; their closure is
pinned by `tools/check_research_isolation.py`, which runs as its own CI step,
and the acceptance bar is now a script rather than a transcript to read
(`tools/prototypes/agent-os-isolation/canary-real-uid.sh`, root, after the
install).
See
[agent-authority.md](agent-authority.md) for the phase-1 tool-boundary work
that already shipped and is out of scope here.

## Why this phase exists

Phase 1 (`65424b88`) gave `spark-investigator` a harness-enforced `tools:`
allowlist and tightened sudoers. The phase-1 doc names its own limit
explicitly: `spark-investigator` still runs as the same Unix user (`pi`), in
the same process family, under the same Claude Code harness as every other
session on this box. That matters because of how in-process subagents work:
**a `.claude/agents/*.md` subagent is a prompt/tool-list restriction inside
one process — it does not change the OS credential the process runs
under.** If the harness's tool-dispatch layer ever has a bug, or a future
tool is added to an agent definition by mistake, or a subagent spawns a raw
subprocess some other way, the thing stopping it from touching GPIO, `state/`,
or systemd is *prose plus the current tool list* — exactly the failure mode
phase 1 was built to close for the *tool* dimension. This phase closes it for
the *identity* dimension: a delegated agent that needs a genuinely separate
security boundary should run as a genuinely separate OS principal, not merely
a same-uid process with a shorter tool list.

## Threat model

**In scope** — what a compromised, buggy, or simply mistaken delegated
research process should be mechanically unable to do, regardless of what its
prompt says or what tool-dispatch bug might exist:
- Gain root (`sudo`, setuid escalation, capability grant).
- Write anything in the production checkout (`/home/pi/picar-x-hacking`)
  outside a single narrow drop directory.
- Read or write `state/` (session data, conversation buffers, location
  data, thoughts — the same data `mind._REFLECTION_AWARENESS_KEYS` already
  treats as sensitive) or `.env`/`~/.claude` credentials.
- Open GPIO, I²C, or audio device nodes.
- Talk to systemd/dbus to start, stop, or restart any unit.
- Push to the git remote or otherwise mutate the repo's history.

**Out of scope / explicitly not defended against:**
- A malicious change to the *design itself* (the launcher script, the
  sudoers line, the unit properties) — that's a code-review/PR problem, the
  same as any other production change, not something the sandbox defends
  against from inside.
- Resource exhaustion (CPU/memory/disk) by the sandboxed process — no
  `MemoryMax`/`CPUQuota` figures are proposed here; add them at
  implementation time using the same measured-trace method as
  [resource-containment.md](resource-containment.md), don't guess.
- Side channels (timing, cache) — not a realistic threat on a single-tenant
  Pi with no other security domain sharing the hardware.
- Network egress restriction. Delegated research legitimately needs
  `WebSearch`/`WebFetch`/LLM API calls; this design does not attempt to
  firewall that. If a future use case needs network-isolated research too,
  that's a separate `RestrictAddressFamilies`/network-namespace addition on
  top of this, not a reason to withhold network from every delegated task.

## Why "genuinely separate identity," not just tighter sandboxing directives

The prototype below (unprivileged `bwrap`) achieves real isolation purely
through **mount-namespace and `no_new_privs` tricks**, while the process's
real, host-visible uid stays `pi`. That is a **default-allow** posture: it
blocks exactly the resources someone thought to hide (`/dev`, `state/`,
`.env`), and anything *not* explicitly hidden is still reachable with `pi`'s
real permissions, because the underlying credential never actually changed.
A forgotten resource — a new device added later, a new `state/` subdirectory,
a file someone drops with world-readable permissions and forgets about — is
silently exposed. That is precisely the class of bug issue #281 itself is:
one specific case (`kill -USR1 <any pid>`) nobody had enumerated until an
audit went looking.

A **genuinely different, low-privilege uid** (via a root-mediated
`systemd-run --uid=`/`runuser`, not a namespace remap) is **default-deny**:
anything not explicitly granted to that uid/group is unreadable/unwritable
by ordinary Unix permission bits, with no dependency on anyone having
thought to hide it. That is the primary control. The mount-namespace/
`no_new_privs` sandboxing is then layered *on top* as defence in depth, the
same "layered, not single point" pattern this repo already uses for the
cliff guard and `policy.py`'s three enforcement sites — not a substitute for
the identity change, a second independent line behind it.

**Recommendation: `DynamicUser=yes` under a pinned `User=spark-research`
name, not a persistent `useradd` account.** This still gives the "dedicated
account" the goal asked for — `ps`, logs, and `journalctl -u <unit>` all show
`spark-research` — but systemd allocates the uid dynamically, with **zero
supplementary groups by construction** (no risk of someone later adding it to
`gpio` "just for one debug session" and forgetting to remove it), no
`/etc/passwd`/`/etc/shadow` entry to misconfigure, no home directory to
accumulate stale files, and automatic cleanup when the unit's not running.
It is less machinery than a static account, not more, and it structurally
can't drift the way a static account's group memberships can.

## Recommended invocation design

```
operator (pi, this session)
   │  1. writes task JSON to a drop directory the launcher can read
   ▼
/var/lib/px-research/inbox/<uuid>.json      (pi:pi, 0644 — root-helper-readable)
   │
   │  2. sudo -n /usr/local/sbin/px-research-run <uuid>
   │     (mirrors px-signal-alive: root-owned, fixed script, the ONLY
   │      caller-controlled input is a bare-uuid4 that becomes a filename —
   │      no sandbox parameter, uid, path, or command is caller-supplied)
   ▼
px-research-run (root, /usr/local/sbin)
   │  validates uuid, confirms inbox/<uuid>.json exists,
   │  then runs a hardcoded systemd-run invocation (see below)
   ▼
systemd-run --unit=px-research-<uuid> --collect \
   --property=Type=oneshot \
   --property=EnvironmentFile=/etc/px-research/tier.env \
   --property=DynamicUser=yes --property=User=spark-research \
   --property=RuntimeDirectory=px-research \
   --property=Environment=PX_STATE_DIR=/run/px-research/state \
   --property=ProtectSystem=strict --property=ProtectHome=yes \
   --property=PrivateDevices=yes --property=PrivateTmp=yes \
   --property=NoNewPrivileges=yes --property=RestrictSUIDSGID=yes \
   --property=CapabilityBoundingSet= --property=LockPersonality=yes \
   --property=ProtectKernelTunables=yes --property=ProtectKernelModules=yes \
   --property=ProtectControlGroups=yes --property=RestrictNamespaces=yes \
   --property=ReadOnlyPaths=/home/pi/picar-x-hacking \
   --property=InaccessiblePaths=/home/pi/picar-x-hacking/state \
   --property=BindReadOnlyPaths=/dev/null:/home/pi/picar-x-hacking/.env \
   --property=BindPaths=/var/lib/px-research/outbox \
   -- /home/pi/picar-x-hacking/bin/px-research-worker <uuid>
   │
   │  3. worker (pi-owned script, spark-research can only execute it,
   │     not modify it — RO bind covers the whole checkout) reads the
   │     request, runs the actual investigation, writes ONE file
   ▼
/var/lib/px-research/outbox/<uuid>.json     (only path spark-research can write)
   │
   │  4. operator (pi) reads/polls the outbox file
   ▼
operator applies findings by hand — spark-research never touches the
production checkout or state/ directly, exactly like px-evolve's PR gate:
proposals, never auto-applied.
```

### The credential, which the design originally left implicit

`BindReadOnlyPaths=/dev/null:.env` is correct and stays, but it has a
consequence the first draft did not name: `pxh.m5` needs a model name
(`PX_M5_SPARK_MODEL`) and a key (`OLLAMA_CLOUD_API_KEY`) for a hosted tier, and
both live in `.env`. A sandbox that cannot read `.env` and has no other route
to them can make exactly zero model calls, which would make the whole mechanism
a $0 experiment.

Three ways out, and only one is acceptable:

| route | why not |
|---|---|
| `--setenv=PX_M5_SPARK_MODEL=... --setenv=OLLAMA_CLOUD_API_KEY=...` on the launcher | the values become unit properties, and `systemctl show -p Environment` (and `Environment=`) is **world-readable** — the key would be readable by every user on the box, including the one this design is isolating from |
| point the unit at the real `.env` | defeats `BindReadOnlyPaths=/dev/null`; the sandbox gets the HA token, the admin PIN, the relay token, the Bluesky app password — everything, for the sake of two variables |
| **a root-owned two-variable file the unit *inherits* and never reads** | chosen |

`/etc/px-research/tier.env` is `root:root 0600`, containing exactly
`PX_M5_SPARK_MODEL` and `OLLAMA_CLOUD_API_KEY`. systemd (PID 1, already root)
reads `EnvironmentFile=` and injects the values into the unit's environment;
the sandboxed process never opens the file, and `systemctl show` on a unit with
`EnvironmentFile=` reports the *path*, not the values. The cost is honest and
worth stating: **rotating either value means editing two files**, and this file
must never grow a third variable — the file *is* the credential boundary, and
its size is the thing keeping the boundary narrow. `px-research-run` refuses to
start a unit when it is missing rather than starting one that will fail at the
first call with no key.

**The launcher execs `systemd-run` under `env -i`.** `sudo`'s `env_reset`
already keeps *pi's* environment out of a root shell, but nothing kept *root's*
out of the unit: `systemd-run` hands the calling environment to the service, so
"whatever happened to be exported by whoever invoked this" would have been a
reachable channel inside a sandbox whose entire purpose is to have no such
channel. Probe 18 above tests the *effect* of that line rather than its
presence in a file: without `env -i` the exported variable reaches the sandbox
and the probe fails.

Notes on specific choices:
- **`ReadOnlyPaths=` the whole checkout, not a separate worktree.** A
  read-only bind mount already prevents any write regardless of what's
  inside `.git` — no git push credentials work against a filesystem mounted
  read-only, and there's no separate worktree to keep in sync. Less
  machinery, same guarantee.
- **`InaccessiblePaths=state/`** — not merely non-writable. `state/` carries
  the same class of data `mind._REFLECTION_AWARENESS_KEYS` already treats as
  sensitive (location, conversation, thoughts); a research agent doesn't need
  it and shouldn't be able to read it, matching the existing
  allowlist-not-denylist philosophy from the Location Awareness section of
  `CLAUDE.md`.
- **`.env` is bind-read-only from `/dev/null`**, not merely made
  inaccessible — this exercises the exact same "neuter with `/dev/null`"
  technique already used for private-audio TTLs elsewhere in this codebase's
  culture of narrow, explicit denials rather than broad path exclusions that
  could accidentally also hide something legitimate nearby.
- **`ProtectHome=yes` hides `/home/*` and `/root` wholesale**, including
  `~pi/.claude` (Claude Code's own credential/session store) — no explicit
  bind needed to hide it; it's covered by the default-deny posture, and
  nothing punches a hole in it.
- **The outbox is the only writable path**, granted via `BindPaths=`
  (read-write bind) to a directory outside both the repo and `state/`,
  `1777`-style — mirroring the existing mailbox pattern documented in
  `CLAUDE.md` for `state/brain/` and `state/health/` (per-writer files, no
  shared-lock ownership hazard).
- **`px-research-run`'s only caller-controlled input is the uuid**, validated
  as bare-uuid4 before use as a filename component — the same discipline
  `tool-brain-reply` used to apply, for the same reason (it was deleted with
  the mailbox in #317 Phase 3): a valid uuid that
  wasn't checked against a real pending request is a write primitive.
- **No new sudoers wildcard.** The sudoers line would be exactly one entry:
  `pi ALL=(root) NOPASSWD: /usr/local/sbin/px-research-run` — no argument
  wildcard beyond the uuid itself being validated inside the script, same
  shape as the existing `px-signal-alive` grant.

## Exact filesystem/group/sudo changes required (not yet made)

Five things, of which two are already tracked in the repo and three are new
files on the host. `useradd` and `groupadd` are **not** among them —
`DynamicUser=yes` needs no `/etc/passwd` entry and no group.

| # | what | where it comes from |
|---|---|---|
| 1 | `/etc/sudoers.d/picar-x-services` gains one line: `pi ALL=(root) NOPASSWD: /usr/local/sbin/px-research-run *` | tracked: `systemd/sudoers.d/picar-x-services`. The wildcard is the one unavoidable one — the argument is a uuid, so it cannot be enumerated. Safety is in the launcher, which accepts exactly one argument and requires a bare v4 uuid |
| 2 | `/usr/local/sbin/px-research-run`, root:root 0755 | tracked: `systemd/sbin/px-research-run`, same install shape as `px-signal-alive` / `px-gpio-run` |
| 3 | `/etc/px-research/tier.env`, root:root 0600, two variables | generated on the host from `.env` (see "The credential" above) — **not** tracked, it holds a key |
| 4 | `/var/lib/px-research/{inbox,outbox}`, root:root 1777 sticky | the existing `state/health/` / `state/brain/` mailbox precedent: different Unix users write here, so a locked-down parent directory would exclude one of them |
| 5 | `bin/px-research-worker` (pi-owned, tracked) + `src/pxh/research_worker.py` | runs *as* `spark-research` but is *owned* by `pi`: the sandboxed uid can execute it (the whole checkout is bound read-only) and can never modify it, regardless of file-mode bits |

### The invariant, pinned

Two layers, because they fail differently:

* `tools/check_research_isolation.py` is a **structural** check on the
  artifacts as shipped — every `--property=` that carries a guarantee (with
  the reason it is there, printed as a map), the three path properties by
  their exact argument text, the uuid validation and the single-argument
  rule, and that no property is built from caller input. It runs as its own CI
  step, so "somebody added `--setenv=` for convenience" fails the build with
  the guarantee it removed named in the message.
* `tests/test_research_worker.py` is the **behavioural** half: the worker can
  name exactly one readable file and one writable one, refuses everything
  else, and always leaves the operator a reason.

Neither is the boundary. The boundary is the unit on the robot, which is what
the canary below is for.

## Result flow-back

`bin/px-research-worker` writes exactly one JSON file to
`/var/lib/px-research/outbox/<uuid>.json` and nothing else. The operator
session is the only actor with write access to the production checkout or
`state/`; applying a finding is a manual, reviewed step by the operator (or,
eventually, a PR the way `px-evolve` already gates self-proposed changes) —
never an automatic action taken by the sandboxed process itself. This mirrors
the retired mailbox's request/approve pattern and `px-evolve`'s "changes never auto-apply"
rule; delegated OS-isolated research inherits the same posture rather than
inventing a new one.

## Prototype — proven live, 2026-08-24, no live identity/sudoers change

Full isolation was demonstrated using **unprivileged `bwrap`** (user
namespaces — no root, no password, no persistent account, fully reversible,
nothing left mounted after the process exits) as a stand-in for the identity
piece, since creating the real `spark-research`/`DynamicUser` unit requires
root this session doesn't have non-interactively and the goal explicitly
said not to make that change yet. The prototype proves the *mechanism*
(mount-namespace isolation, `no_new_privs`) that the recommended systemd
properties above rely on for the second, defense-in-depth layer — it does
**not** by itself prove the primary default-deny/uid-separation layer, which
needs the real implementation to test (see canary plan below).

Scripts: `tools/prototypes/agent-os-isolation/probe.sh` (runs inside the
sandbox) and `tools/prototypes/agent-os-isolation/run-prototype.sh` (the
`bwrap` invocation). Reproducible, read-only, no root required.

Observed transcript (uid 65534/`nobody`, zero supplementary groups inside
the sandbox; `/home/pi/picar-x-hacking` read-only, `state/` and
`~/.claude` replaced with an empty read-only view, `.env` bind-read-only
from `/dev/null`, `/dev` replaced with bwrap's minimal private device set):

| # | Claim | Result |
|---|---|---|
| 1 | `sudo -n true` cannot gain root | **Blocked** — `no_new_privs` prevents the setuid transition; sudo refuses outright |
| 2 | Cannot write a new file into the repo root | **Blocked** — `Read-only file system` |
| 3 | Cannot write a new file into `state/` | **Blocked** — `Read-only file system` |
| 4 | Cannot list `state/` contents | **Blocked** — empty (real contents hidden) |
| 5 | `/dev/gpiomem` unreachable | **Blocked** — `No such file or directory` (node doesn't exist in the private `/dev`) |
| 6 | `/dev/i2c-1` unreachable | **Blocked** — same |
| 7 | `/dev/snd` unreachable | **Blocked** — same |
| 8 | `systemctl restart <unit>` has no authority | **Blocked** — `Interactive authentication required` (polkit denies before even checking whether the unit exists — tested against a nonexistent unit name so a bug could not have caused a real restart) |
| 9 | Can still read repo source | **Passes** — `src/pxh/state.py` read successfully |
| 10 | `.env` unreadable | **Blocked** — `Permission denied` |
| 11 | `~/.claude` (pi's Claude credentials) hidden | **Blocked** — empty |
| 12 | Sandbox has *some* writable path (proves it isn't just inert) | **Passes** — `/var/tmp` scratch mount accepted a write |

Post-run check: `git status --porcelain` on the real repo came back empty —
none of the blocked-write attempts leaked anything into tracked or untracked
production state. The two write attempts (checks 2–3) used new,
never-before-existing filenames rather than mutating any tracked file, so a
sandbox-construction bug would have failed safe (a stray file to delete)
rather than corrupting anything.

## Install (one block, needs root)

Nothing in this block has been run. It is the whole difference between
"designed" and "there".

```bash
# 1. the two tracked artifacts
sudo install -m 0755 systemd/sbin/px-research-run /usr/local/sbin/px-research-run
sudo install -m 0755 bin/px-research-worker /home/pi/picar-x-hacking/bin/px-research-worker

# 2. the two-variable credential, from .env — never tracked, never printed
sudo install -d -m 0755 /etc/px-research
sudo sh -c 'umask 077; grep -E "^(PX_M5_SPARK_MODEL|OLLAMA_CLOUD_API_KEY)=" \
  /home/pi/picar-x-hacking/.env > /etc/px-research/tier.env'
sudo chown root:root /etc/px-research/tier.env && sudo chmod 0600 /etc/px-research/tier.env

# 3. the mailbox pair — 1777 sticky, matching state/health/ and state/brain/
sudo install -d -m 1777 /var/lib/px-research/inbox /var/lib/px-research/outbox

# 4. the one sudoers line (the file is tracked; visudo -c first, always)
sudo visudo -c -f systemd/sudoers.d/picar-x-services
sudo install -m 0440 -o root -g root systemd/sudoers.d/picar-x-services \
  /etc/sudoers.d/picar-x-services
```

Then the canary below, then the first real request.

## Rehearsal — the worker itself, inside the sandbox (2026-09-18)

`tools/prototypes/agent-os-isolation/run-worker-rehearsal.sh`. Still no root,
no password, no persistent state. It is a different question from the prototype
above: that one proved the *mechanism* against a probe script, this one asks
whether the thing we actually intend to install — `bin/px-research-worker` —
runs inside the sandbox shape at all, and whether its one-readable/one-writable
contract holds there rather than in a pytest tmpdir.

Why it was worth doing before the install: the sandbox is a read-only checkout,
a hidden `state/`, a neutered `.env`, no devices and a different uid, and the
parts that can *only* fail under those conditions — bytecode writes into a
read-only tree, the mailbox path, the tier client's state directory, the
network path — are exactly the parts an install would discover at 2am.

Two runs, same sandbox, one request each:

| probe | result |
|---|---|
| identity | `uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)` |
| P1 write a new file into the repo root | **blocked** — `Read-only file system` |
| P2 write a new file into `state/` | **blocked** — `Read-only file system` |
| P3 list `state/` | **blocked** — empty (hidden, not merely unwritable) |
| P4 read `.env` | **blocked** — `Permission denied` |
| P5 `sudo -n true` | **blocked** — *"the no new privileges flag is set"* |
| P6 `/dev/gpiomem`, `/dev/i2c-1`, `/dev/snd` | **blocked** — absent |
| P7 read its own request | **passes** |
| P8 run 1: the worker, with a tier reachable | **exit 0** |
| P9 outbox: results for this uuid / `.tmp` left | **1 / 0** |
| P10 run 1 result | `status: ok`, `status_detail: available`, `backend: ollama-local`, a real HTTP round trip to a throwaway local stand-in for the tier (`duration_ms: 17`) |
| run 2: the same worker, no credential in the sandbox | **exit 4**, and the outbox carries the reason verbatim: `PX_M5_SPARK_MODEL must name a model explicitly (not auto)` |
| post-run `git status --porcelain` on the real tree | **empty** — nothing escaped |

Run 2 is the shape to expect if `EnvironmentFile=/etc/px-research/tier.env` is
ever missing on the host: the unit still produces a result file with the reason
in it, rather than a silent failure, which is what makes the install step's
credential file a *diagnosable* requirement rather than a mysterious one.

Two honest notes about the transcript:

* **`.env` reads as `Permission denied`, not as an empty file.** `/dev/null` is
  bound read-only over it, and the bind is what changes the access, not the
  file's mode. The property's intent — unreadable — is what the probe shows.
* **`sudo` complains that `/etc/sudo.conf` is owned by uid 65534.** That is an
  artefact of the uid *remap*: inside this namespace the real root-owned files
  appear owned by the remapped uid, and sudo says so before `no_new_privs`
  stops it. It is not a misconfiguration and it is not evidence about the real
  implementation — with a real `DynamicUser`, root-owned files are root-owned
  and the same `no_new_privs` block applies, which is what canary item 1 checks.

What remains unproven is unchanged, and it is the part that needs the install:

## Adversarial canary plan — a script, not a transcript

`tools/prototypes/agent-os-isolation/canary-real-uid.sh`, run as root on the
robot **after** the install block. One command; it fails rather than printing
something for a human to judge, and it refuses to start with exit 2 if any part
of the install is missing (launcher, probe, mailbox, credential file, sudoers
grant) — a canary that passes because the thing under test is absent is the
worst kind.

It is the equivalent of phase 1's canary: phase 1 was closed by *actually
attempting* the escalations, not by reading the agent definition, and this is
the same discipline applied to the unit properties.

### One correction to the plan this replaces

The previous version said to run all twelve probes "inside a
`px-research-worker` invocation (or a manual `systemd-run` using the exact same
properties)". The second half is the only executable one, and that is a
property of the design rather than an inconvenience: **the real path execs
`bin/px-research-worker` and nothing else** — no shell, no probes. A sandbox
that could run arbitrary probe code through its own entry point would not be
the sandbox this design claims. So the canary is split by what each half can
actually reach, and the *identity* of the property lists is what makes the
first half evidence about the real sandbox: `tools/check_research_isolation.py`
compares the canary's `--property=` lines against the launcher's and fails CI if
they drift.

### Phase 1 — the properties, with a real uid

`systemd-run --wait --collect` with the launcher's property list, verbatim,
running `canary-probe.sh` instead of the worker. That is a real
`DynamicUser`-allocated uid, not a namespace remap, so the following are
checked for the first time as *facts* rather than as arguments:

| # | claim |
|---|---|
| 1 | `sudo -n true` cannot gain root |
| 2–3 | cannot write a new file into the checkout, or into `state/` |
| 4 | `state/` is hidden, not merely unwritable |
| 5–7 | `/dev/gpiomem`, `/dev/i2c-1`, `/dev/snd` are absent |
| 8 | `systemctl restart` has no authority (against a **nonexistent** unit name: zero blast radius) |
| 9 | repo source is still readable |
| 10 | `.env` yields nothing readable — asserted as *no content*, because the read-only bind is of `/dev/null`, whose mode is 0666, so "the read fails" would be a false failure of a property that holds |
| 11 | `~/.claude` is hidden |
| 12 | the outbox accepts a write (positive control) |
| **13** | the identity has **no supplementary groups** — the claim `DynamicUser` is chosen for, which a namespace remap cannot test |
| **14** | `/etc/px-research/tier.env` is unreadable from inside — an **unenumerated** resource: the credential the unit *inherits* must not be readable by the sandbox for itself |
| **15** | no `/dev/gpiochip*` is visible — whatever this host actually has, not a list written when the doc was |
| **16** | cannot write outside the checkout, under `/home` |
| **17** | no sudoers entry names `spark-research` |
| **18** | the **invoking environment** does not reach the sandbox. The canary exports `PX_CANARY_LEAK_PROBE` in its own shell and passes it through no property; this probe is the *effect* of the launcher's `env -i`, so it is the check that fails if that line is ever removed |

13–17 are the ones the bwrap prototype structurally could not reach; the probe's
verdict is also written to the outbox as JSON, so the evidence outlives the
transient unit.

### Phase 2 — the real path, end to end

A real request through `/usr/local/sbin/px-research-run <uuid>`, then: a result
must appear in the outbox within 60 s, it must **not** be owned by `root:root`
(the work ran as the sandbox identity), and the transient unit must be gone
afterwards (`--collect` leaves nothing behind). This is the positive control
the property phase cannot give, and it is the chain an operator actually uses.

### Phase 3 — the allocation is released

A long-running unit under the same properties, killed mid-run: `getent passwd
spark-research` must resolve *while* it runs and must resolve to **nothing**
once it is stopped. An identity that persists between invocations is a standing
attack surface, and the design claims not to have one.

### Phase 4 — nothing leaked

`git status --porcelain` on the production checkout, plus explicit checks that
none of the probe's write attempts escaped into the repo, `state/`, or `/home`.

### What remains a human's job

Running one command as root after the install:
`sudo tools/prototypes/agent-os-isolation/canary-real-uid.sh`. Everything else
is mechanical, including the reading of the result.

Once it passes, this issue is at the bar phase 1 was closed on. The "out of
scope" items above (resource limits, network isolation) stay open follow-ups,
not blockers.
