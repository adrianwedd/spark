#!/bin/bash
# Runs INSIDE the sandbox, as the real `spark-research` uid (#281 phase 2).
#
# This is the assertion half of the adversarial canary: the bwrap prototype
# *printed* a transcript for a human to read, this one fails. Every claim below
# is expected to hold, the verdict is written to the outbox as JSON so it
# survives the unit, and the exit status is what `systemd-run --wait`
# propagates to `canary-real-uid.sh`.
#
# It exists because the real path cannot run it. `px-research-run` execs
# `bin/px-research-worker` and nothing else — no shell, no probes — so the
# property set has to be canaried by a root-side `systemd-run` with the
# *identical* properties and a different program. That identity is not a
# promise: tools/check_research_isolation.py compares the two property lists
# and fails CI if they drift apart.
set -u

REPO="${PWD:-/home/pi/picar-x-hacking}"
OUT_DIR="${PX_RESEARCH_OUTBOX:-/var/lib/px-research/outbox}"
OUT="$OUT_DIR/canary-$$.json"

fail=0
results=()
say() { printf '%s\n' "$*"; }
record() {  # record <PASS|FAIL> <n> <claim>
    results+=("{\"n\": $2, \"verdict\": \"$1\", \"claim\": \"$(printf '%s' "$3" | sed 's/\\/\\\\/g; s/"/\\"/g')\"}")
    say "$1  $2  $3"
    [[ "$1" == "FAIL" ]] && fail=1
    return 0
}

# expect_blocked <n> <claim> <cmd...> — the command must fail.
expect_blocked() {
    local n="$1" claim="$2"; shift 2
    if out=$("$@" 2>&1); then
        record FAIL "$n" "$claim [SUCCEEDED: ${out:0:120}]"
    else
        record PASS "$n" "$claim"
    fi
}

# expect_denied_read <n> <claim> <file> — reading must fail.
expect_denied_read() {
    local n="$1" claim="$2" file="$3"
    if [[ -r "$file" ]] && head -c 1 "$file" >/dev/null 2>&1; then
        record FAIL "$n" "$claim [READABLE]"
    else
        record PASS "$n" "$claim"
    fi
}

say "== identity =="
id
say "== working directory =="
pwd

# --- the twelve from the design doc -----------------------------------------
expect_blocked      1 "sudo cannot gain root"                  sudo -n true
expect_blocked      2 "cannot write a new file into the repo root" \
                    sh -c "echo canary > '$REPO/CANARY-281-DELETE-ME.tmp'"
expect_blocked      3 "cannot write a new file into state/" \
                    sh -c "echo canary > '$REPO/state/CANARY-281-DELETE-ME.tmp'"

if [[ -z "$(ls "$REPO/state" 2>/dev/null)" ]]; then
    record PASS 4 "state/ is hidden, not merely unwritable"
else
    record FAIL 4 "state/ is hidden, not merely unwritable [LISTABLE: $(ls "$REPO/state" 2>/dev/null | head -3 | tr '\n' ' ')]"
fi

for i in 5:/dev/gpiomem 6:/dev/i2c-1 7:/dev/snd; do
    n="${i%%:*}"; dev="${i#*:}"
    if [[ -e "$dev" ]]; then record FAIL "$n" "$dev is absent"; else record PASS "$n" "$dev is absent"; fi
done

expect_blocked      8 "systemctl restart has no authority" \
                    systemctl restart px-canary-nonexistent.service

if head -3 "$REPO/src/pxh/state.py" >/dev/null 2>&1; then
    record PASS 9 "can still read repo source"
else
    record FAIL 9 "can still read repo source"
fi

# `.env` is neutered rather than merely permission-denied: under systemd the
# read-only bind is of `/dev/null`, whose own mode is 0666, so an implementer
# that asserted "the read fails" would false-fail while the *property* — the
# real file's contents are gone — still holds. Assert the property: nothing
# readable comes back. (The bwrap prototype reported `Permission denied` here
# because its mount construction differed; both are the same guarantee.)
env_content="$(head -c 64 "$REPO/.env" 2>/dev/null)"
if [[ -z "$env_content" ]]; then
    record PASS 10 ".env yields nothing readable"
else
    record FAIL 10 ".env yields nothing readable [GOT ${#env_content} BYTES]"
fi

if [[ -z "$(ls /home/pi/.claude 2>/dev/null)" ]]; then
    record PASS 11 "~/.claude is hidden"
else
    record FAIL 11 "~/.claude is hidden [LISTABLE]"
fi

if echo canary > "$OUT_DIR/canary-writetest.$$" 2>/dev/null; then
    rm -f "$OUT_DIR/canary-writetest.$$"
    record PASS 12 "the outbox accepts a write (positive control)"
else
    record FAIL 12 "the outbox accepts a write (positive control)"
fi

# --- the claims the namespace-remap prototype structurally could not test ----
groups_out="$(id -nG 2>/dev/null)"
if [[ "$(printf '%s' "$groups_out" | wc -w)" -le 1 ]]; then
    record PASS 13 "the identity has no supplementary groups (got: '$groups_out')"
else
    record FAIL 13 "the identity has no supplementary groups (got: '$groups_out')"
fi

# An unenumerated resource: the credential file the design gives the unit via
# systemd's EnvironmentFile and which the *sandbox* must never be able to read
# for itself. Nobody listed it in the threat model as a path to hide.
if [[ ! -e /etc/px-research/tier.env ]]; then
    record FAIL 14 "/etc/px-research/tier.env is unreadable from inside [ABSENT — the install is incomplete]"
else
    expect_denied_read 14 "/etc/px-research/tier.env is unreadable from inside" /etc/px-research/tier.env
fi

# A device nobody enumerated: whatever gpiochips this host actually has.
chips="$(ls /dev/gpiochip* 2>/dev/null | tr '\n' ' ')"
if [[ -z "$chips" ]]; then
    record PASS 15 "no /dev/gpiochip* visible"
else
    record FAIL 15 "no /dev/gpiochip* visible [VISIBLE: $chips]"
fi

# Outside the checkout, inside the home directory tree.
expect_blocked     16 "cannot write outside the checkout, under /home" \
                    sh -c "echo canary > /home/pi/CANARY-281-DELETE-ME.tmp"

# No sudoers entry may name this identity.
if sudo -n -l 2>&1 | grep -qi "spark-research"; then
    record FAIL 17 "no sudoers entry names spark-research"
else
    record PASS 17 "no sudoers entry names spark-research"
fi

# The calling environment must not reach the sandbox. The canary's host script
# exports PX_CANARY_LEAK_PROBE in *its own* shell and does not pass it through
# any --property, so if it is visible here, systemd-run handed the caller's
# environment to the service and `env -i` in the launcher is doing nothing.
if [[ -n "${PX_CANARY_LEAK_PROBE:-}" ]]; then
    record FAIL 18 "the invoking environment does not reach the sandbox [LEAKED PX_CANARY_LEAK_PROBE]"
else
    record PASS 18 "the invoking environment does not reach the sandbox"
fi

# --- the record -------------------------------------------------------------
{
    printf '{"probe": "canary", "uid": %s, "failures": %s, "results": [%s]}\n' \
        "$(id -u)" "$fail" "$(IFS=,; printf '%s' "${results[*]}")"
} > "$OUT" 2>/dev/null || say "WARN: could not write the canary record to $OUT"

say "== canary verdict: $([[ $fail -eq 0 ]] && echo PASS || echo FAIL) (record: $OUT) =="
exit "$fail"
