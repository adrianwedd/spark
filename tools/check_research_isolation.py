#!/usr/bin/env python3
"""Delegated-research OS isolation invariant (issue #281 phase 2).

Phase 1 (`tools/check_investigator_agent.py`) pins the *tool* boundary: which
tools a subagent may call. It cannot pin the identity boundary, because the
boundary is not in the agent definition — it is in the unit properties that
`px-research-run` constructs and the sudoers line that lets `pi` invoke it.

That is exactly the kind of guarantee that decays by convenience: one
`--property=` added "so the worker can also see X", one `--setenv=` for a
token, one extra word on the sudoers line. None of those fail a test today,
and all of them silently widen a boundary whose entire value is that it holds
when somebody is careless. So this script checks the properties themselves.

Two things it deliberately does NOT do:

* It does not check the *behaviour* of the sandbox (that is the adversarial
  canary in docs/operations/agent-os-isolation-design.md, which has to run on
  the robot as the real uid).
* It does not fail if the artifacts are simply not installed — this repo
  ships them inert. The check is "if this ships, it ships closed", not
  "somebody installed it".
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = Path("systemd/sbin/px-research-run")
WORKER = Path("src/pxh/research_worker.py")
SUDOERS = Path("systemd/sudoers.d/picar-x-services")
# The acceptance artifact for phase 2, and the reason it is checked here: the
# real path can only exec `bin/px-research-worker` (no shell, no probes), so the
# property set has to be canaried by a root-side systemd-run with the *same*
# properties and a different program. "Same properties" is the load-bearing
# claim, and two lists in two files drift unless something compares them.
CANARY = Path("tools/prototypes/agent-os-isolation/canary-real-uid.sh")
CANARY_PROBE = Path("tools/prototypes/agent-os-isolation/canary-probe.sh")

# Properties the design requires, by the reason each one is there. A missing
# property is a hole; the map is printed with all of them so a change that
# drops one says *which* guarantee went with it.
REQUIRED_PROPERTIES = {
    "DynamicUser=yes": "identity is allocated, not a static account to drift",
    "User=spark-research": "the sandbox is a nameable, non-pi principal",
    "ProtectSystem=strict": "default-deny on the whole filesystem",
    "ProtectHome=yes": "~pi/.claude and other homes are not reachable",
    "PrivateDevices=yes": "no GPIO, I2C or audio device nodes",
    "NoNewPrivileges=yes": "no setuid transition, so sudo cannot escalate",
    "CapabilityBoundingSet=": "no capabilities at all",
    "RestrictSUIDSGID=yes": "cannot create a privileged binary to run later",
    "ProtectKernelTunables=yes": "no sysctl/procfs writes",
    "ProtectKernelModules=yes": "cannot load a module",
    "ProtectControlGroups=yes": "cannot reach another unit's cgroup",
    "RestrictNamespaces=yes": "cannot build itself a namespace to escape in",
    "RestrictRealtime=yes": "cannot pin the CPU against the robot's own loops",
    "SystemCallArchitectures=native": "no 32-bit syscall bypass of the above",
    "Type=oneshot": "no long-lived process to re-enter later",
}

# The three path guarantees, checked by their exact argument text rather than
# by "a ReadOnlyPaths line exists" — the argument is the part that matters.
REQUIRED_PATH_PROPERTIES = {
    'ReadOnlyPaths="$REPO"': "the production checkout is read-only, whole",
    'InaccessiblePaths="$REPO/state"': "state/ is hidden, not merely unwritable",
    'BindReadOnlyPaths=/dev/null:"$REPO/.env"': ".env is neutered",
    'BindPaths=/var/lib/px-research/outbox': "the outbox is the only writable path",
}

# The uuid check, in the launcher, verbatim. Both halves are required: the
# shape, and the rejection of anything but exactly one argument.
UUID_RE = "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"

# Things the launcher must never contain, each with the reason.
FORBIDDEN_IN_LAUNCHER = {
    "sudo ": "the launcher is the privileged side; it escalates nothing",
    "eval ": "no second parsing pass over caller input",
    "sh -c": "no shell re-entry with a caller-controlled string",
    "--setenv": "environment values set by a caller are a widening; secrets "
                "arrive via the root-owned EnvironmentFile",
    "User=pi": "the sandboxed unit must not run as the production uid",
    "--uid=": "uid must come from DynamicUser, not from a parameter",
    "--gid=": "same, for the group",
    "--shell": "no interactive shell",
    "--pipe": "no stdin plumbing that could carry an unvalidated command",
}

WORKER_WRITE_ALLOWANCE = 1  # exactly one write_text: the tmp file in write_result


def _property_lines(text: str) -> list[str]:
    """Every `--property=` line, stripped. Values are compared literally, which
    is why the canary uses the launcher's variable names (`$REPO`, `$INBOX`,
    `$TIER_ENV`) rather than its own."""
    out = []
    for line in text.splitlines():
        line = line.strip().rstrip("\\").strip()
        if line.startswith("--property="):
            out.append(line)
    return out


def _read(root: Path, rel: Path) -> str:
    path = root / rel
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """Drop comments and blank lines.

    The forbidden-needle checks are about what the script *does*. A comment
    that explains why `--setenv` is not used is documentation, and a check
    that fires on it teaches the next author to delete the explanation —
    which is the opposite of what this file is for.
    """
    return "\n".join(
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def check(root: Path | None = None) -> list[str]:
    """Return violations; empty means the artifacts ship closed."""
    base = root if root is not None else REPO_ROOT
    violations: list[str] = []

    launcher = _read(base, LAUNCHER)
    if not launcher:
        return [f"{LAUNCHER} is missing — phase 2 has no entry point to check"]
    code = _code_only(launcher)

    # 1. The launcher may not be a shell that takes a command.
    for needle, why in FORBIDDEN_IN_LAUNCHER.items():
        if needle in code:
            violations.append(f"{LAUNCHER}: contains {needle!r} — {why}")

    # 2. Exactly one argument, validated as a bare v4 uuid.
    if "[[ $# -eq 1 ]]" not in code:
        violations.append(
            f"{LAUNCHER}: does not require exactly one argument — the uuid is the "
            "only value that may reach a filename or a unit name"
        )
    if UUID_RE not in code:
        violations.append(
            f"{LAUNCHER}: the v4-uuid validation is missing or altered; it is the "
            "single caller-controlled input"
        )
    if "exec env -i /usr/bin/systemd-run" not in code:
        violations.append(
            f"{LAUNCHER}: does not exec systemd-run under `env -i` — the unit's "
            "environment must be exactly what the properties and EnvironmentFile= "
            "set, and systemd-run otherwise hands it the calling environment"
        )

    # 3. Every required property, by name.
    for prop, why in REQUIRED_PROPERTIES.items():
        if f"--property={prop}" not in launcher:
            violations.append(f"{LAUNCHER}: missing --property={prop} ({why})")
    for prop, why in REQUIRED_PATH_PROPERTIES.items():
        if f"--property={prop}" not in launcher:
            violations.append(f"{LAUNCHER}: missing --property={prop} ({why})")

    # 4. The caller's uuid may reach the unit *name* and the worker's argument,
    #    and nothing else. This is the property that makes the whole design
    #    hold: no sandbox property is caller-influenced, so there is no
    #    argument that could widen the sandbox even from a correct-looking call.
    property_lines = [
        line for line in code.splitlines() if line.strip().startswith("--property=")
    ]
    tainted = [l.strip() for l in property_lines
               if "$uuid" in l or "$1" in l or "$@" in l or "$*" in l]
    if tainted:
        violations.append(
            f"{LAUNCHER}: a unit property is built from caller input: {tainted} — "
            "properties must be constants, or the sandbox is parameterisable"
        )
    if '--unit="px-research-$uuid"' not in code:
        violations.append(
            f"{LAUNCHER}: the unit name must be the validated uuid and nothing else"
        )
    if '-- "$REPO/bin/px-research-worker" "$uuid"' not in code:
        violations.append(
            f"{LAUNCHER}: the unit must exec the repo's worker with the validated "
            'uuid as its own argument (expected: -- "$REPO/bin/px-research-worker" '
            '"$uuid")'
        )

    # 5. sudoers: one line, one command, no wildcard on the command itself.
    sudoers = _read(base, SUDOERS)
    if sudoers:
        research_lines = [
            line for line in sudoers.splitlines()
            if "px-research-run" in line and not line.strip().startswith("#")
        ]
        if len(research_lines) != 1:
            violations.append(
                f"{SUDOERS}: expected exactly one px-research-run grant, found "
                f"{len(research_lines)}"
            )
        grant_re = re.compile(
            r"^pi ALL=\(root\) NOPASSWD: /usr/local/sbin/px-research-run \*$")
        for line in research_lines:
            if "NOPASSWD: ALL" in line or "(ALL:ALL)" in line:
                violations.append(
                    f"{SUDOERS}: {line.strip()!r} grants more than the launcher")
            # Anchored at both ends on purpose: an appended `, /bin/sh` or
            # `, ALL` is the cheapest way to widen this line, and a substring
            # check would not see it.
            if not grant_re.match(line.strip()):
                violations.append(
                    f"{SUDOERS}: the grant must be exactly "
                    "'pi ALL=(root) NOPASSWD: /usr/local/sbin/px-research-run *' — "
                    "the uuid is validated inside the launcher, not enumerated here"
                )
    else:
        violations.append(f"{SUDOERS} is missing — the launcher has no granted caller")

    # 6. The canary must test the sandbox that ships, not one that resembles it.
    canary_text = _read(base, CANARY)
    if not canary_text:
        violations.append(
            f"{CANARY} is missing — phase 2 has no executable acceptance artifact")
    else:
        canary_code = _code_only(canary_text)
        launcher_props = sorted(_property_lines(code))
        canary_props = sorted(_property_lines(canary_code))
        if not canary_props:
            violations.append(f"{CANARY}: no --property= lines — it would test nothing")
        elif canary_props != launcher_props:
            missing = [p for p in launcher_props if p not in canary_props]
            extra = [p for p in canary_props if p not in launcher_props]
            violations.append(
                f"{CANARY}: its property set differs from {LAUNCHER}'s — the canary "
                f"must test the sandbox that ships. missing={missing} extra={extra}"
            )
        if "env -i /usr/bin/systemd-run" not in canary_code:
            violations.append(
                f"{CANARY}: does not use the launcher's exec context "
                "(`env -i /usr/bin/systemd-run`) — then it is not testing the "
                "sandbox that ships"
            )
        # The guard's *shape*, not the string `id -u`: phase 3 also reads
        # `id -u pi` to prove the sandbox is not running as pi, so a substring
        # check for `id -u` would pass a canary with the guard deleted.
        if '"$(id -u)" -eq 0 ]] || die' not in canary_code \
                or "must run as root" not in canary_code:
            violations.append(
                f"{CANARY}: no root guard — the property phase builds a root-side "
                "systemd-run and must refuse to run without one"
            )
        for line in canary_code.splitlines():
            if "--unit=" in line and "px-canary-" not in line:
                violations.append(
                    f"{CANARY}: creates a unit that is not px-canary-*: {line.strip()!r} "
                    "— the canary may never touch a production unit"
                )
        probe_text = _read(base, CANARY_PROBE)
        if not probe_text:
            violations.append(f"{CANARY_PROBE} is missing — the canary has nothing to run")
        elif "PX_CANARY_LEAK_PROBE" not in probe_text:
            violations.append(
                f"{CANARY_PROBE}: the calling-environment leak probe is gone — the "
                "only claim here that cannot be settled by reading the launcher"
            )
        elif "px-canary-nonexistent.service" not in probe_text:
            violations.append(
                f"{CANARY_PROBE}: the systemctl probe must name a *nonexistent* unit "
                "(zero blast radius), the same discipline as the prototype"
            )

    # 7. The worker writes in exactly one place.
    worker = _read(base, WORKER)
    if not worker:
        violations.append(f"{WORKER} is missing — nothing for the unit to run")
    else:
        writes = worker.count(".write_text(")
        if writes != WORKER_WRITE_ALLOWANCE:
            violations.append(
                f"{WORKER}: {writes} write_text call(s), expected "
                f"{WORKER_WRITE_ALLOWANCE} (the atomic tmp write in write_result) — "
                "every additional writer is another path out of the sandbox"
            )
        for needle in ("shutil", "os.system", "subprocess"):
            if needle in worker:
                violations.append(
                    f"{WORKER}: imports/uses {needle} — the worker runs shell-free "
                    "and tool-free on purpose"
                )

    return violations


def report(root: Path | None = None) -> list[str]:
    base = root if root is not None else REPO_ROOT
    lines = []
    launcher = _read(base, LAUNCHER)
    if launcher:
        lines.append(f"launcher: {LAUNCHER} ({len(launcher.splitlines())} lines)")
        for prop, why in REQUIRED_PROPERTIES.items():
            mark = "ok  " if f"--property={prop}" in launcher else "MISS"
            lines.append(f"  {mark} {prop:32s} {why}")
        for prop, why in REQUIRED_PATH_PROPERTIES.items():
            mark = "ok  " if f"--property={prop}" in launcher else "MISS"
            lines.append(f"  {mark} {prop:32s} {why}")
    sudoers = _read(base, SUDOERS)
    if sudoers:
        for line in sudoers.splitlines():
            if "px-research-run" in line and not line.strip().startswith("#"):
                lines.append(f"  ok   grant: {line.strip()}")
    worker = _read(base, WORKER)
    if worker:
        lines.append(f"worker:   {WORKER} ({len(worker.splitlines())} lines, "
                     f"{worker.count('.write_text(')} write path)")
    canary = _read(base, CANARY)
    if canary:
        lines.append(f"canary:   {CANARY} ({len(canary.splitlines())} lines, "
                     f"{len(_property_lines(_code_only(canary)))} properties, "
                     "compared below against the launcher)")
        lines.append(f"probe:    {CANARY_PROBE} "
                     f"({len(_read(base, CANARY_PROBE).splitlines())} lines)")
    return lines


if __name__ == "__main__":
    found = check()
    for line in report():
        print(line)
    if found:
        print(f"\n{len(found)} research-isolation violation(s):")
        for v in found:
            print(f"  {v}")
        sys.exit(1)
    print(
        "\ndelegated-research OS isolation artifacts OK "
        "(not installed — inert by design)")
