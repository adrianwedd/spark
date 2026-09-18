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
#
# `--dry-run` prints the exact systemd-run invocations this host would execute,
# reports what the install is missing, and exits without touching systemd. It is
# the half that can be checked *before* the root run — a 200-line script whose
# first execution happens at the acceptance step is a script whose first
# execution is untested, and its argument construction is the part that can be
# wrong without systemd being involved at all.
set -uo pipefail

DRY_RUN=0
case "${1:-}" in
    --dry-run) DRY_RUN=1 ;;
    "") ;;
    *) printf 'usage: %s [--dry-run]\n' "$0" >&2; exit 2 ;;
esac

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

if [[ $DRY_RUN -eq 0 ]]; then
    [[ "$(id -u)" -eq 0 ]] || die "must run as root (the property phase builds a root-side systemd-run)"
fi

# --- preconditions: the install must actually be there -----------------------
missing=()
[[ -x "$LAUNCHER" ]] || missing+=("$LAUNCHER (run the install block)")
[[ -x "$PROBE" ]]    || missing+=("$PROBE")
[[ -d "$INBOX" ]]    || missing+=("$INBOX")
[[ -d "$OUTBOX" ]]   || missing+=("$OUTBOX")
[[ -r "$TIER_ENV" ]] || missing+=("$TIER_ENV (the sandbox's two-variable credential)")
[[ -f "$REPO/bin/px-research-worker" ]] || missing+=("$REPO/bin/px-research-worker")
[[ -x /usr/bin/systemd-run ]] || missing+=("/usr/bin/systemd-run")
[[ -x /usr/bin/env ]] || missing+=("/usr/bin/env")
[[ -x /bin/bash ]]    || missing+=("/bin/bash")
# Absolute, like every other binary this script names. `command -v runuser`
# depends on the caller's PATH — measured on the robot, where /usr/sbin/runuser
# is present but /usr/sbin is not on a non-login shell's PATH, and the check
# therefore reported a false missing piece on the acceptance step.
[[ -x /usr/sbin/runuser ]] || missing+=("/usr/sbin/runuser (phase 2 drives the real path as pi)")
grep -qE '^pi ALL=\(root\) NOPASSWD: /usr/local/sbin/px-research-run \*$' \
    /etc/sudoers.d/picar-x-services 2>/dev/null \
    || missing+=("the sudoers grant for px-research-run in /etc/sudoers.d/picar-x-services")

if ((${#missing[@]})); then
    printf 'canary: %s\n' "$([[ $DRY_RUN -eq 1 ]] && echo "the install is incomplete:" || echo "not installed yet:")" >&2
    printf '  - %s\n' "${missing[@]}" >&2
    # A dry run is *for* the incomplete case: it reports and still shows the
    # commands it would build. A real run must not start against a partial
    # install, because a canary that passes over a missing piece is the worst
    # kind of green.
    [[ $DRY_RUN -eq 1 ]] || exit 2
fi

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

if [[ $DRY_RUN -eq 1 ]]; then
    mapfile -t props < <(sandbox_props)
    printf '\nproperties (%s):\n' "${#props[@]}"
    printf '  %s\n' "${props[@]}"
    cat <<EOF

phase 1 would run:
  env -i /usr/bin/systemd-run --unit=px-canary-<uuid1> --collect --wait \\
$(printf '      %s \\\n' "${props[@]}")
      -- /bin/bash $PROBE

phase 2 would run (as pi, through the sudoers grant — the documented invocation):
  /usr/sbin/runuser -u pi -- sudo -n $LAUNCHER <uuid2>
  then: poll $OUTBOX/<uuid2>.json, assert it exists, is not root:root, and the unit is collected

phase 3 would run:
  env -i /usr/bin/systemd-run --unit=px-canary-<uuid3> --collect \\
$(printf '      %s \\\n' "${props[@]}")
      -- /bin/sleep 120
  then: observe the allocated uid, SIGTERM the unit, assert it is released

phase 0 would check: every tier variable name in $REPO/.env is present in $TIER_ENV
phase 4 would check: git status on $REPO, and that no probe write escaped
EOF
    if ((${#missing[@]})); then
        echo
        echo "dry run: ${#missing[@]} piece(s) of the install are missing — a real run would exit 2 above"
        exit 2
    fi
    echo
    echo "dry run: install looks complete; no unit was started"
    exit 0
fi

# Phase 0: the credential file must carry every tier variable production uses.
# Names only — the values stay in the file and in the unit's environment — and
# the claim matters because a missing name is *silent*: no `PX_M5_SPARK_HOST` in
# here means the sandbox talks to the default host while the robot talks to the
# configured one, and no `PX_M5_SPARK_TIMEOUT_S` means a different deadline.
echo "########## phase 0: the credential covers production's tier variables ##########"
tier_names() { grep -oE '^(PX_M5_SPARK_[A-Z_]+|OLLAMA_[A-Z_]*API_KEY)=' "$1" 2>/dev/null | sort -u; }
prod_names="$(tier_names "$REPO/.env")"
sandbox_names="$(tier_names "$TIER_ENV")"
missing_names="$(comm -23 <(printf '%s\n' "$prod_names") <(printf '%s\n' "$sandbox_names") | tr -d '=')"
if [[ -z "$prod_names" ]]; then
    fail "could not read any tier variable names out of $REPO/.env — is the coverage check meaningful?"
elif [[ -z "$missing_names" ]]; then
    pass "every tier variable production sets is present in the sandbox's credential file ($(printf '%s\n' "$prod_names" | wc -l) names)"
else
    fail "the sandbox's credential file is missing: $(printf '%s' "$missing_names" | tr '\n' ' ')"
fi

status_before="$(git -C "$REPO" status --porcelain)"

echo "########## phase 1: the same properties, a real uid ##########"
u1="$(uuid)"
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
# As pi, through `sudo -n`: that is the documented invocation, and the only one
# the sudoers grant exists for. Running it as root here would pass even with a
# broken grant, which is the false green this phase should not produce.
if launch_out="$(/usr/sbin/runuser -u pi -- sudo -n "$LAUNCHER" "$u2" 2>&1)"; then
    pass "the real invocation (pi -> sudo -n -> launcher) started the transient unit"
else
    fail "the real invocation failed: ${launch_out:0:160}"
fi
result="$OUTBOX/$u2.json"
# 120s, not 60: the tier's own deadline is PX_M5_SPARK_TIMEOUT_S (60s default),
# and this poll is for whether the *flow-back* works, not for how fast the model
# is. A canary that fails on a slow provider would be measuring the wrong thing.
for _ in $(seq 120); do [[ -s "$result" ]] && break; sleep 1; done
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
    fail "no result in the outbox within 120s — the operator would have been left with silence"
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
if [[ -z "$during" ]]; then
    # nss-systemd is what makes `getent` answer for a DynamicUser. If it is not
    # in play, read the uid from the process itself rather than declaring the
    # allocation absent — the claim is about the *identity*, not about nss.
    run_pid="$(systemctl show -p MainPID --value "px-canary-$u3" 2>/dev/null)"
    if [[ -n "$run_pid" && -r "/proc/$run_pid/status" ]]; then
        during="$(awk '/^Uid:/{print $2}' "/proc/$run_pid/status")"
        [[ "$during" == "$(id -u pi 2>/dev/null)" ]] && during=""
    fi
fi
if [[ -n "$during" ]]; then
    if [[ "$during" == "$(id -u pi 2>/dev/null)" ]]; then
        fail "the sandboxed unit ran as pi (uid $during)"
    else
        pass "an active unit runs as a genuinely different principal (uid $during)"
    fi
else
    fail "no allocated uid was observable while the unit was running"
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

status_after="$(git -C "$REPO" status --porcelain)"

echo
echo "########## phase 4: nothing leaked ##########"
# Compare against a snapshot taken before phase 1: the claim is "the canary left
# nothing behind", not "this host happened to be clean when it ran".
if [[ "$status_after" == "$status_before" ]]; then
    pass "the production checkout is unchanged ($([[ -z "$status_before" ]] && echo "and was clean before" || echo "$(printf '%s' "$status_before" | wc -l) pre-existing entr(y|ies)"))"
else
    fail "the canary changed the checkout:"
    diff <(printf '%s\n' "$status_before") <(printf '%s\n' "$status_after") | sed 's/^/      /'
fi
for stray in "$REPO/CANARY-281-DELETE-ME.tmp" "$REPO/state/CANARY-281-DELETE-ME.tmp" /home/pi/CANARY-281-DELETE-ME.tmp; do
    if [[ -e "$stray" ]]; then fail "a probe write escaped the sandbox: $stray"; fi
done
rm -f "$request"
[[ -n "$record" ]] && note "probe record kept: $record"
[[ -n "$result" ]] && note "real-path result kept: $result"

echo
if [[ $fails -eq 0 ]]; then
    echo "canary: PASS — every claim held"
    exit 0
fi
echo "canary: FAIL — $fails claim(s) did not hold"
exit 1
