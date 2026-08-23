#!/usr/bin/env bash
# Unprivileged-bwrap stand-in for the identity/sandbox layer proposed in
# docs/operations/agent-os-isolation-design.md (issue #281 phase 2).
#
# Requires no root, no password, no persistent account or group, and makes
# NO live change: bwrap builds a private mount+user namespace for the
# lifetime of this one process and tears it down completely on exit.
#
# What this proves: the mount-namespace / no_new_privs mechanism the design
# doc's systemd properties (ProtectSystem, PrivateDevices, ReadOnlyPaths,
# InaccessiblePaths, NoNewPrivileges, ...) rely on for their defense-in-depth
# layer actually works on this box.
#
# What this does NOT prove: the primary default-deny/uid-separation layer
# (DynamicUser=yes + a genuinely different uid) -- this script maps the
# sandbox to uid/gid 65534 (nobody) purely via a namespace remap, so the
# REAL host-side credential crossing back out of the namespace is still
# pi. Anything not explicitly hidden below (via --tmpfs/--ro-bind/--dev)
# stays reachable with pi's real permissions. See the design doc's
# "Why genuinely separate identity" section and its adversarial canary
# plan item 2 (probe a resource NOT enumerated here) for why that gap
# matters and how the real implementation closes it.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
mkdir -p "$SCRATCH/empty"

PX_PROTOTYPE_REPO="$REPO" bwrap \
  --ro-bind / / \
  --dev /dev \
  --ro-bind "$SCRATCH/empty" "$REPO/state" \
  --ro-bind "$SCRATCH/empty" /home/pi/.claude \
  --ro-bind /dev/null "$REPO/.env" \
  --tmpfs /var/tmp \
  --unshare-user --unshare-pid --unshare-ipc \
  --uid 65534 --gid 65534 \
  --die-with-parent \
  --setenv PX_PROTOTYPE_REPO "$REPO" \
  -- "$(dirname "${BASH_SOURCE[0]}")/probe.sh"

echo "--- post-run leak check (should be empty) ---" >&2
ls "$REPO"/CANARY-281-DELETE-ME.tmp 2>/dev/null && echo "LEAK: repo root write escaped the sandbox" >&2
ls "$REPO"/state/CANARY-281-DELETE-ME.tmp 2>/dev/null && echo "LEAK: state/ write escaped the sandbox" >&2
git -C "$REPO" status --porcelain
