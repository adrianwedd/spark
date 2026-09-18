#!/usr/bin/env bash
# The adversarial canary for #281 phase 2 — the acceptance bar, as a script.
#
# Runs on the robot *after* the install block, as root, and fails rather than
# printing something for a human to judge. Three phases:
#
#   1. PROPERTIES — the probe (canary-probe.sh) runs under systemd-run with the
#      *identical* property list the real launcher builds, so the sandbox is
#      tested as it actually is, with a real DynamicUser uid rather than a
#      namespace remap of pi. Nothing about this can be checked by reading the
#      unit file.
#   2. THE REAL PATH — a real request goes through /usr/local/sbin/px-research-run
#      and a result must come back to the outbox. This is the positive control
#      the property phase cannot give: it proves the whole chain
#      (launcher -> systemd-run -> DynamicUser -> worker -> outbox) works.
#   3. RELEASE — a transient unit is killed mid-run and DynamicUser's
#      allocation must be gone afterwards (`getent passwd spark-research`
#      empty). An identity that persists between invocations is a standing
#      attack surface the design claims not to have.
#
# Why root: the real path execs `bin/px-research-worker` and nothing else — no
# shell, no probes — so the property phase has to be built by a root-side
# `systemd-run`. The property lists are compared by
# tools/check_research_isolation.py, which fails CI if they drift, because
# "identical properties" is the whole load-bearing claim of phase 1 here.
#
# Inert by construction: every unit this script creates is named `px-canary-*`
# and collected on exit; the only production path it touches is the real
# launcher, once, with a uuid it generates. It never restarts or stops a
# production unit (the systemctl probe inside the sandbox uses a nonexistent
# name on purpose).
set -uo pipefail

REPO=/home/pi/picar-x-hacking
INBOX=/var/lib/px-research/inbox
OUTBOX=/var/lib/px-research/outbox
TIER_ENV=/etc/px-research/tier.env
LAUNCHER=/usr/local/sbin/px-research-run
PROBE="$REPO/tools/prototypes/agent-os-isolation/canary-probe.sh"

fails=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; fails=$((fails + 1)); }
note() { printf '      %s\n' "$*"; }
die()  { printf 'canary: %s\n' "$*" >&2; exit 2; }

[[ "$(id -u)" -eq 0 ]] || die "must run as root (the property phase builds a root-side systemd-run)"

# --- preconditions: the install must actually be there -----------------------
missing=()
[[ -x "$LAUNCHER" ]] || missing+=("$LAUNCHER (run the install block)")
[[ -x "$PROBE" ]]    || missing+=("$PROBE")
[[ -d "$INBOX" ]]    || missing+=("$INBOX")
[[ -d "$OUTBOX" ]]   || missing+=("$OUTBOX")
[[ -r "$TIER_ENV" ]] || missing+=("$TIER_ENV (the sandbox's two-variable credential)")
[[ -f "$REPO/bin/px-research-worker" ]] || missing+=("$REPO/bin/px-research-worker")
if ((${#missing[@]})); then
    printf 'canary: not installed yet:\n' >&2
    printf '  - %s\n' "${missing[@]}" >&2
    exit 2
fi
grep -qE '^pi ALL=\(root\) NOPASSWD: /usr/local/sbin/px-research-run \*$' /etc/sudoers.d/picar-x-services \
    || die "the sudoers grant for px-research-run is not installed"

uuid() { python3 -c 'import uuid;print(uuid.uuid4())'; }
uid_now() { getent passwd spark-research 2>/dev/null | cut -d: -f3; }

# The property list below is the launcher's, verbatim and in the same order,
# with the same variable names. tools/check_research_isolation.py compares the
# two sets of `--property=` lines and fails CI if either drifts.
sandbox_props() {
    printf '%s\n' \
    --property=DynamicUser=yes \
    --property=User=spark-research \
    --property=Type=oneshot \
    --property=EnvironmentFile="$TIER_ENV" \
    --property=WorkingDirectory="$REPO" \
    --property=RuntimeDirectory=px-research \
    --property=RuntimeDirectoryMode=0700 \
    --property=Environment=PX_STATE_DIR=/run/px-research/state \
    --property=Environment=HOME=/run/px-research \
    --property=Environment=PX_RESEARCH_INBOX="$INBOX" \
    --property=Environment=PX_RESEARCH_OUTBOX=/var/lib/px-research/outbox \
    --property=ProtectSystem=strict \
    --property=ProtectHome=yes \
    --property=PrivateDevices=yes \
    --property=PrivateTmp=yes \
    --property=NoNewPrivileges=yes \
    --property=RestrictSUIDSGID=yes \
    --property=CapabilityBoundingSet= \
    --property=LockPersonality=yes \
    --property=ProtectKernelTunables=yes \
    --property=ProtectKernelModules=yes \
    --property=ProtectControlGroups=yes \
    --property=RestrictNamespaces=yes \
    --property=RestrictRealtime=yes \
    --property=SystemCallArchitectures=native \
    --property=ReadOnlyPaths="$REPO" \
    --property=InaccessiblePaths="$REPO/state" \
    --property=BindReadOnlyPaths=/dev/null:"$REPO/.env" \
    --property=BindPaths=/var/lib/px-research/outbox
}

echo "########## phase 1: the same properties, a real uid ##########"
u1="$(uuid)"
marker="$(mktemp -u /var/tmp/px-canary-marker.XXXXXX)"
leak="leak-probe-$(uuid)"
mapfile -t props < <(sandbox_props)
set +e
# `env -i` here for the same reason as the launcher: this phase must build the
# sandbox that ships, exec context included. PX_CANARY_LEAK_PROBE is exported in
# *this* shell and passed through no property, so probe 18 tests the effect of
# that line rather than the text of it: remove `env -i` and the variable reaches
# the sandbox and the probe fails.
export PX_CANARY_LEAK_PROBE="$leak"
env -i /usr/bin/systemd-run --unit="px-canary-$u1" --collect --wait "${props[@]}" -- \
    /bin/bash "$PROBE"
probe_rc=$?
set -e
if [[ $probe_rc -eq 0 ]]; then
    pass "every probe in canary-probe.sh held under the real DynamicUser uid"
else
    fail "the probe reported $probe_rc failing claim(s) — see the lines above and the record in $OUTBOX"
fi

# The record the probe wrote is the durable half of this: it survives the unit.
record="$(ls -t "$OUTBOX"/canary-*.json 2>/dev/null | head -1 || true)"
if [[ -n "$record" ]]; then
    pass "the probe's verdict was written to the outbox ($record)"
    note "$(head -c 400 "$record")"
else
    fail "the probe's verdict did not reach the outbox"
fi

echo
echo "########## phase 2: the real path, end to end ##########"
u2="$(uuid)"
request="$INBOX/$u2.json"
printf '{"prompt": "Canary request: state one sentence about what you cannot reach."}\n' > "$request"
chmod 0644 "$request"
if "$LAUNCHER" "$u2" >/dev/null 2>&1; then
    pass "the launcher accepted a bare uuid and started the transient unit"
else
    fail "the launcher refused a valid uuid"
fi
result="$OUTBOX/$u2.json"
for _ in $(seq 60); do [[ -s "$result" ]] && break; sleep 1; done
if [[ -s "$result" ]]; then
    pass "a result came back through the real path ($result)"
    note "status: $(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("status"), d.get("status_detail"), d.get("backend") or "-")' "$result" 2>/dev/null || echo unreadable)"
    owner="$(stat -c '%U:%G' "$result")"
    if [[ "$owner" == "root:root" ]]; then
        fail "the result is owned by root:root — the work did not run as the sandbox uid"
    else
        pass "the result was written by the sandbox identity ($owner), not by root"
    fi
else
    fail "no result in the outbox within 60s — the operator would have been left with silence"
fi
if systemctl status "px-research-$u2" >/dev/null 2>&1; then
    fail "the transient unit px-research-$u2 is still present"
else
    pass "the transient unit was collected (--collect, nothing left on the host)"
fi

echo
echo "########## phase 3: the allocation is released ##########"
u3="$(uuid)"
mapfile -t props < <(sandbox_props)
env -i /usr/bin/systemd-run --unit="px-canary-$u3" --collect "${props[@]}" -- /bin/sleep 120 >/dev/null 2>&1
for _ in $(seq 20); do [[ -n "$(uid_now)" ]] && break; sleep 0.5; done
during="$(uid_now)"
if [[ -n "$during" ]]; then
    pass "an active unit has a real allocated uid ($during) with no /etc/passwd entry of its own"
else
    fail "no uid was allocated while the unit was running"
fi
systemctl kill --signal=SIGTERM "px-canary-$u3" >/dev/null 2>&1
sleep 3
systemctl reset-failed "px-canary-$u3" >/dev/null 2>&1
after="$(uid_now)"
if [[ -z "$after" ]]; then
    pass "spark-research does not exist once the unit is gone (no standing identity)"
else
    fail "spark-research still resolves after the unit stopped (uid $after)"
fi

echo
echo "########## phase 4: nothing leaked ##########"
if git -C "$REPO" status --porcelain | grep -q .; then
    fail "the canary left changes in the checkout:"
    git -C "$REPO" status --porcelain | sed 's/^/      /'
else
    pass "the production checkout is unchanged"
fi
for stray in "$REPO/CANARY-281-DELETE-ME.tmp" "$REPO/state/CANARY-281-DELETE-ME.tmp" /home/pi/CANARY-281-DELETE-ME.tmp; do
    if [[ -e "$stray" ]]; then fail "a probe write escaped the sandbox: $stray"; fi
done
rm -f "$marker" "$request"
[[ -n "$record" ]] && note "probe record kept: $record"
[[ -n "$result" ]] && note "real-path result kept: $result"

echo
if [[ $fails -eq 0 ]]; then
    echo "canary: PASS — every claim held"
    exit 0
fi
echo "canary: FAIL — $fails claim(s) did not hold"
exit 1
