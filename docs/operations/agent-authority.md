# Delegated-agent authority boundary (#281)

## Why

During the 2026-08-23 silence investigation, a delegated Claude Code fork
was given a scoped, prose-only investigation
task and took live physical action on the robot anyway — outside the scope
it was actually given. The prompt said not to; the agent had every tool
needed to do it regardless, and nothing mechanical stood in the way. That
gap is issue [#281](https://github.com/adrianwedd/spark/issues/281):
delegated/research/investigation agents had no boundary against production
systemd, GPIO, live audio/wake hardware, or state writes other than
instructions in their prompt — the same category of failure `src/pxh/policy.py`
was built to close for SPARK's own dispatchers (a persona-swapped system
prompt can silently drop a safety rule that only lived in prose; a sink gate
cannot be swapped out from under it).

Two more mechanical checks landed alongside the agent-boundary fix, found
while auditing what a compromised or simply-mistaken agent could reach:

- **`/etc/sudoers.d/010_pi-nopasswd` granted `pi ALL=(ALL) NOPASSWD: ALL`**
  — unrestricted passwordless root for the same Unix user every Claude Code
  session (main or delegated) runs as. Any agent with `Bash` could already
  become root with zero friction, which made a tool-level restriction alone
  incomplete: a delegated agent that kept `Bash` could `sudo` around it.
- **`kill -USR1`/`kill` calls from `bin/px-env`'s `yield_alive()` and
  `bin/px-wake-listen`'s `_stop_alive()` took a caller-supplied PID.** A
  sudoers rule broad enough to cover that (`kill -USR1 *`) would have
  matched *any* PID argument, not just px-alive's.

## What this PR does

**1. Restricted subagent type — `spark-investigator`**
(`.claude/agents/spark-investigator.md`). `tools:` is a hard,
harness-enforced allowlist: `Read, Grep, Glob, WebSearch, WebFetch`. No
`Bash`, `Write`, `Edit`, `NotebookEdit`, or `Agent` — the omission is not
a suggestion the model can route around, it is tools that do not exist for
it to call. `permissionMode: plan` on top, as defence in depth. Pinned by
`tools/check_investigator_agent.py` (parses the frontmatter, fails if any
of `{Bash, Write, Edit, NotebookEdit, Agent}` ever reappears in the list)
and `tests/test_agent_authority_invariant.py`, wired into CI as its own
step (same reasoning as the resident-only Claude check: a red build here
should read as "the boundary was reopened," not as one failing test among
1200). Both the checker and the agent definition are blacklisted from
px-evolve (`claude_session.BLACKLIST_FILES`) for the same reason the
resident-only pair is — an evolution PR that can edit the thing that
checks the rule can satisfy the rule by weakening the check.

**2. Sudoers, tightened fail-closed.** `010_pi-nopasswd`'s blanket grant is
removed (backed up, not deleted). The replacement grants exact absolute
commands/units only — no `ALL`, no unit wildcards, no argument wildcards:

- The five dashboard-managed services (`_MANAGED_SERVICES` in `api.py`) —
  `start`/`stop`/`restart` named individually per unit, nothing broader.
- `/usr/local/sbin/px-signal-alive USR1` and `... TERM` — a new root-owned
  helper (not a raw `kill`) that resolves px-alive's *real* current PID via
  `systemctl show -p MainPID --value px-alive.service` rather than trusting
  any PID an agent could supply. `bin/px-env`'s `yield_alive()` and
  `bin/px-wake-listen`'s `_stop_alive()` now call it instead of `kill`.
  Fixed signal set only (`USR1`/`TERM`) — the sudoers rule matches the two
  literal invocations, not a wildcard argument.
- `shutdown -h now` stays NOPASSWD — this is SPARK's own autonomous
  battery-emergency path (`mind.py`, fires unattended at ≤10% battery), not
  an operator convenience, so it does not move behind a password.
- `apt` and `reboot` move to a separate, password-required operator rule.
  Neither is called from any autonomous/cron/timer path (`px-update-reboot`
  is human-invoked only) — moving them off `pi`'s NOPASSWD set costs
  nothing in automation and removes root-without-a-password from the two
  commands most capable of taking the robot fully offline.

Installed via `visudo -cf` validation before activation, atomic install,
and audited afterward with `sudo -l -U pi` plus explicit negative probes
(`sudo sh`, `sudo python3`, an unlisted `systemctl` unit/verb) confirmed to
fail, alongside the legitimate helper/systemctl calls confirmed to still
succeed.

## What this does NOT do

This is defence in depth, not a complete security boundary.
`spark-investigator` still runs as the same Unix user (`pi`), in the same
process family, under the same Claude Code harness as every other session
on this box. The sudoers tightening closes the specific `sudo` escape this
audit found; it does not remove `pi`'s native GPIO/I²C/audio group
membership (`gpio`, `i2c`, `spi`, `audio`, `video`, `plugdev` — unrelated to
sudo) or give a delegated agent a separate OS identity. **Issue #281 stays
open** for that stronger work — a genuinely separate identity or sandbox
(container, dedicated low-privilege user, or read-only worktree checkout)
that removes GPIO/I²C/audio/sudo/production-write authority at the OS
level, not just at the tool-dispatch level.

## Acceptance

Proven with an adversarial canary, not trusted from the prompt: a real
`spark-investigator` subagent was spawned and instructed to attempt sudo,
production state writes, systemd manipulation, GPIO/audio access, and
checkout mutation. Each was mechanically denied — the agent had no tool
capable of attempting most of them, and the ones it could reach for
(`Bash`) simply were not in its allowlist to call.
