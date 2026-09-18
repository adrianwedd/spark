#!/usr/bin/env bash
# Rehearsal: run the canary's probe INSIDE a sandbox, before the authorised root
# run (#281 phase 2).
#
# The probe (`canary-probe.sh`) is the artifact the real canary executes as the
# `DynamicUser` uid. Until this script and its `--self-test` existed, its first
# execution would have been the acceptance step, under `systemd-run`, as root,
# once. Everything that can be wrong about it independently of the identity
# layer is checked here instead:
#
#   * every claim's code path is executed, and its verdict recorded;
#   * the probe's JSON record reaches the outbox;
#   * the exit status the unit propagates is the number of failures, so
#     `systemd-run --wait` turns a violated claim into a non-zero exit;
#   * the assertion machinery can fail at all — a canary that cannot fail is
#     not a canary (`--self-test`).
#
# WHAT IT DOES NOT PROVE:
#   * the identity layer. This is the bwrap namespace remap, as in the other two
#     rehearsals, so claim 13 (no supplementary groups) and the default-deny
#     posture are only truly tested by the real `DynamicUser` unit;
#   * that every claim PASSES. **Two claims are expected to FAIL here, and each
#     failure is the point of the rehearsal:**
#
#       - claim 14 asserts `/etc/px-research/tier.env` is unreadable. On a host
#         where the install block has not run, that file does not exist, which
#         the probe records as FAIL correctly — "unreadable because absent" is
#         not the property.
#       - claim 18 asserts the invoking environment does not reach the sandbox.
#         bwrap passes the calling environment straight through (there is no
#         `env -i` equivalent here), so this script exports the probe variable
#         and the claim fails — which is the *demostration* that claim 18 is
#         live rather than vacuous, and the reason the launcher execs
#         `env -i /usr/bin/systemd-run`.
#
#     So the expected profile is 16 PASS, 2 FAIL (claims 14 and 18), exit 2, and
#     this script fails loudly if it sees anything else. A rehearsal that
#     tolerated any other profile would hide the probe breaking.
#
# Requires: no root, no password, no persistent state, no network egress.
set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_candidate="${PX_REHEARSAL_REPO:-$_here/../../..}"
REPO="$(cd "$_candidate" 2>/dev/null && pwd || true)"
if [[ -n "$REPO" && ! -d "$REPO/.git" ]]; then REPO="${PX_REHEARSAL_REPO:-}"; fi
if [[ -z "$REPO" || ! -d "$REPO/.git" ]]; then
    echo "run-probe-rehearsal: not a checkout: '$_candidate'" >&2
    echo "  set PX_REHEARSAL_REPO=/path/to/spark-checkout" >&2
    exit 2
fi
# The probe is the one in the checkout — unless this is being run from a copy
# elsewhere, which is how it is used on the robot before the commit lands (the
# same reason PX_REHEARSAL_REPO exists). The probe under rehearsal has to be the
# probe that will run.
PROBE="${PX_REHEARSAL_PROBE:-$REPO/tools/prototypes/agent-os-isolation/canary-probe.sh}"
[[ -f "$PROBE" ]] || { echo "run-probe-rehearsal: no probe at $PROBE" >&2; exit 2; }

SCRATCH="$(mktemp -d)"
if [[ "${PX_REHEARSAL_KEEP:-0}" == "1" ]]; then
    trap 'echo "scratch kept: $SCRATCH" >&2' EXIT
else
    trap 'rm -rf "$SCRATCH"' EXIT
fi
mkdir -p "$SCRATCH/run/inbox" "$SCRATCH/run/outbox" "$SCRATCH/run/state" "$SCRATCH/empty"
chmod -R 0777 "$SCRATCH/run"
chmod 0755 "$SCRATCH"

# The probe's own --self-test first: it is cheap, needs no sandbox, and proves
# the machinery can record a violation before we ask it to record seventeen
# non-violations.
if bash "$PROBE" --self-test >"$SCRATCH/selftest.out" 2>&1; then
    echo "self-test: PASS"
else
    echo "self-test: FAILED (the probe's assertions cannot fail) — output follows" >&2
    cat "$SCRATCH/selftest.out" >&2
    exit 1
fi

echo "########## the probe, inside the sandbox, as a non-pi uid ##########"
# Exported deliberately: bwrap hands the calling environment to the sandbox, so
# claim 18 must see this and fail. That is the check that the leak claim works.
export PX_CANARY_LEAK_PROBE="leak-probe-rehearsal"
set +e
PX_PROTOTYPE_REPO="$REPO" \
bwrap \
  --ro-bind / / \
  --dev /dev \
  --chdir "$REPO" \
  --ro-bind "$SCRATCH/empty" "$REPO/state" \
  --ro-bind "$SCRATCH/empty" /home/pi/.claude \
  --ro-bind /dev/null "$REPO/.env" \
  --bind "$SCRATCH/run" /var/tmp \
  --unshare-user --unshare-pid --unshare-ipc \
  --uid 65534 --gid 65534 \
  --die-with-parent \
  --setenv PX_RESEARCH_OUTBOX /var/tmp/outbox \
  --setenv PX_RESEARCH_INBOX /var/tmp/inbox \
  --setenv PX_STATE_DIR /var/tmp/state \
  --setenv HOME /var/tmp \
  -- /bin/bash "$PROBE"
PROBE_RC=$?
set -e
echo "probe exit=$PROBE_RC"

record="$(ls -t "$SCRATCH/run/outbox"/canary-*.json 2>/dev/null | head -1 || true)"
pass_count=0
fail_count=0
failed_claims=""
if [[ -n "$record" ]]; then
    pass_count="$(grep -o '"verdict": "PASS"' "$record" | wc -l)"
    fail_count="$(grep -o '"verdict": "FAIL"' "$record" | wc -l)"
    failed_claims="$(grep -o '"n": [0-9]*, "verdict": "FAIL"' "$record" | grep -o '[0-9]*' | tr '\n' ' ')"
fi

verdict=0
[[ -n "$record" ]] || { echo "FAIL  the probe wrote no record to the outbox"; verdict=1; }
if [[ "$fail_count" == "2" && "$pass_count" == "16" ]]; then
    echo "PASS  the probe evaluated 18 claims: 16 PASS, 2 FAIL (${failed_claims% })"
else
    echo "FAIL  expected 16 PASS / 2 FAIL, got $pass_count PASS / $fail_count FAIL"
    verdict=1
fi
if [[ "$failed_claims" == "14 18 " ]]; then
    echo "PASS  the failures are claim 14 (the credential file the install creates) and"
    echo "      claim 18 (the invoking environment, which bwrap passes through) —"
    echo "      both are real violations here, so neither claim is vacuous"
else
    echo "FAIL  the failing claim(s) were '${failed_claims}' — expected 14 and 18"
    verdict=1
fi
if [[ "$PROBE_RC" == "2" ]]; then
    echo "PASS  the exit status is the failure count (2), so the unit propagates it"
else
    echo "FAIL  exit status was $PROBE_RC, not 2 — a violated claim would not reach systemd-run --wait"
    verdict=1
fi

if [[ $verdict -eq 0 ]]; then
    echo "probe rehearsal: PASS — every claim evaluated, machinery proven able to fail"
    exit 0
fi
echo "probe rehearsal: FAIL" >&2
[[ -n "$record" ]] && cat "$record" >&2
exit 1
