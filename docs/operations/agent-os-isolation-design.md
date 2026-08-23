# OS-level delegated-agent isolation — design (#281 phase 2)

Status: **design + working prototype, not implemented.** No live user, group,
sudoers, or systemd unit change has been made for this phase. See
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
   --property=DynamicUser=yes --property=User=spark-research \
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
  `tool-brain-reply` already applies, for the same reason: a valid uuid that
  wasn't checked against a real pending request is a write primitive.
- **No new sudoers wildcard.** The sudoers line would be exactly one entry:
  `pi ALL=(root) NOPASSWD: /usr/local/sbin/px-research-run` — no argument
  wildcard beyond the uuid itself being validated inside the script, same
  shape as the existing `px-signal-alive` grant.

## Exact filesystem/group/sudo changes required (not yet made)

1. `useradd`: **none.** `DynamicUser=yes` needs no `/etc/passwd` entry.
2. `groupadd`: **none.**
3. `/etc/sudoers.d/picar-x-services` (existing file, one line added):
   `pi ALL=(root) NOPASSWD: /usr/local/sbin/px-research-run`
4. New root-owned file `/usr/local/sbin/px-research-run` (0755, root:root) —
   validates the uuid, checks the inbox entry exists, execs the fixed
   `systemd-run` invocation above. No caller-supplied sandbox parameters.
5. New directories: `/var/lib/px-research/{inbox,outbox}`, root:root 1777
   (sticky, world-writable-by-anyone-who-can-reach-it, matching the existing
   `state/health/` and `state/brain/` mailbox precedent and its documented
   reasoning: different Unix users write here, a locked-down parent dir
   would exclude one of them).
6. New pi-owned file `bin/px-research-worker` (tracked in the repo like any
   other `bin/tool-*`) — reads the inbox request, performs the investigation,
   writes the outbox result. Runs *as* `spark-research` (via the
   `DynamicUser` unit) but is *owned* by `pi` — `spark-research` can execute
   it (covered by the `ReadOnlyPaths` bind of the whole checkout) but never
   modify it, since the mount is read-only for that uid regardless of any
   file-mode bit.

## Result flow-back

`bin/px-research-worker` writes exactly one JSON file to
`/var/lib/px-research/outbox/<uuid>.json` and nothing else. The operator
session is the only actor with write access to the production checkout or
`state/`; applying a finding is a manual, reviewed step by the operator (or,
eventually, a PR the way `px-evolve` already gates self-proposed changes) —
never an automatic action taken by the sandboxed process itself. This mirrors
`ask_brain()`'s mailbox pattern and `px-evolve`'s "changes never auto-apply"
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

## Adversarial canary plan (for the real implementation, once shipped)

Same discipline as the phase-1 canary — actually attempt each escalation
through the real `px-research-run` → `systemd-run` → `spark-research` path,
not just read the unit file and assume:

1. From inside a `px-research-worker` invocation (or a manual `systemd-run`
   using the exact same properties), attempt all 12 probes above again —
   this time the identity is a **real** `DynamicUser`-allocated uid, not a
   namespace remap of `pi`, so this also validates the primary default-deny
   layer the prototype couldn't reach.
2. Attempt to reach a resource **not explicitly enumerated above** — e.g. a
   second GPIO chip added after this design was written, or a new `state/`
   subdirectory — to confirm the default-deny posture actually holds for
   things nobody thought to list, not just the specific paths this doc
   named. (This is the check the bwrap prototype's default-allow posture
   structurally cannot pass, and the reason the identity layer matters.)
3. Confirm `sudo -l` for `spark-research` shows no entries at all (there
   should be no sudoers line naming it — only `pi` gets the one-line grant
   to invoke the launcher).
4. Confirm the outbox write path works (positive control) and that
   `spark-research` cannot write anywhere else under `/var/lib/px-research/`
   or `/home/pi/picar-x-hacking` (negative control, including the inbox
   directory itself — read-only from the worker's side once the request is
   picked up).
5. Kill the transient unit mid-run and confirm `DynamicUser`'s allocation is
   released (`getent passwd spark-research` shows nothing outside an active
   unit) — the account should not persist as a standing attack surface
   between invocations.
6. Run the full CI suite's negative-control pattern from
   `feedback_containment_tests_must_be_inert` (memory) — the canary itself
   must never touch a real production unit; use a nonexistent unit name for
   the systemctl probe exactly as the prototype did here.

Once (1)–(6) pass against the real implementation, that's the equivalent
acceptance bar phase 1 used ("proven with an adversarial canary, not trusted
from the prompt") — at that point issue #281 can be reassessed for closure,
though the "out of scope" items above (resource limits, network isolation)
would still be open follow-ups, not blockers.
