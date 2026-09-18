#!/usr/bin/env bash
# Rehearsal: does the real `bin/px-research-worker` run inside the sandbox the
# phase-2 unit builds, and does the one-readable/one-writable contract hold
# there? (issue #281 phase 2)
#
# WHY THIS EXISTS. The artifacts (systemd/sbin/px-research-run,
# bin/px-research-worker) are reviewed, merged and pinned by
# tools/check_research_isolation.py — and none of that says the worker *runs*
# inside the sandbox. Their tests use pytest tmpdirs on a normal filesystem;
# the sandbox is a read-only checkout, a hidden state/, a neutered .env, no
# device nodes and a different uid. The parts that can only fail there —
# bytecode writes into a read-only tree, the mailbox path, the tier client's
# state dir, the network path — are exactly the parts an install would discover
# at 2am otherwise.
#
# WHAT IT PROVES:
#   * the worker executes inside the sandbox as a different uid, reads only its
#     request, and writes exactly one result file (no .tmp left behind);
#   * the negative probes hold in the *same* sandbox it runs in, not in a
#     separate rehearsal;
#   * the tier path works through the sandbox when a tier is reachable — run 1
#     answers against a throwaway local stand-in for the tier, so the positive
#     control is a real HTTP round trip, not a mock;
#   * when there is no credential, the operator still gets the reason (run 2).
#
# WHAT IT DOES NOT PROVE, stated so the transcript is not read as more:
#   * the primary identity layer. This is the same bwrap namespace remap the
#     other prototype uses (uid 65534), so the host-side credential crossing
#     back out is still `pi`. The design doc's canary plan item 1 needs the real
#     `DynamicUser` unit for that;
#   * the real mailbox paths: /var/lib/px-research does not exist until the
#     install block runs, so the rehearsal points the same two environment
#     variables at /var/tmp inside the sandbox. The unit sets them to the real
#     paths;
#   * `EnvironmentFile=` — so run 2's missing-credential result is the shape to
#     expect if that file is ever absent on the host.
#
# Requires: no root, no password, no persistent state, no network egress.
set -euo pipefail

# The repo under rehearsal is the one this script lives in — unless it is being
# run from a copy elsewhere (which is how it is used on the robot, before the
# commit lands), in which case PX_REHEARSAL_REPO says which. Validated rather
# than assumed: a wrong path here silently rehearses the wrong tree, and the
# `/tmp/../../..` case resolves to `/` and tries to bind over `/state`.
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_candidate="${PX_REHEARSAL_REPO:-$_here/../../..}"
REPO="$(cd "$_candidate" 2>/dev/null && pwd || true)"
if [[ -z "$REPO" || ! -d "$REPO/.git" ]]; then
    REPO="${PX_REHEARSAL_REPO:-}"
fi
if [[ -z "$REPO" || ! -d "$REPO/.git" ]]; then
    echo "run-worker-rehearsal: not a checkout: '$_candidate'" >&2
    echo "  set PX_REHEARSAL_REPO=/path/to/spark-checkout" >&2
    exit 2
fi
SCRATCH="$(mktemp -d)"
if [[ "${PX_REHEARSAL_KEEP:-0}" == "1" ]]; then
    trap 'echo "scratch kept: $SCRATCH" >&2' EXIT
else
    trap 'rm -rf "$SCRATCH"' EXIT
fi
mkdir -p "$SCRATCH/run/inbox" "$SCRATCH/run/outbox" "$SCRATCH/run/state" "$SCRATCH/empty"
chmod -R 0777 "$SCRATCH/run"
chmod 0755 "$SCRATCH"

uuid() { python3 -c 'import uuid;print(uuid.uuid4())'; }
U1="$(uuid)"; U2="$(uuid)"
cat > "$SCRATCH/run/inbox/$U1.json" <<'JSON'
{"prompt": "Rehearsal request: name one thing this sandbox cannot reach."}
JSON
cat > "$SCRATCH/run/inbox/$U2.json" <<'JSON'
{"prompt": "Rehearsal request with no credential in the sandbox."}
JSON
chmod 0666 "$SCRATCH/run/inbox"/*.json

# A throwaway stand-in for the cognition tier: a local HTTP server that answers
# /api/generate the way the real one does. It exists so run 1's positive control
# is a real socket round trip from inside the sandbox — the sandbox's network
# namespace is the host's, so this also demonstrates that egress is not
# accidentally severed by the property set.
cat > "$SCRATCH/stub_tier.py" <<'PY'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            request = {}
        body = json.dumps({
            "model": request.get("model") or "rehearsal-probe",
            "response": ("Nothing outside this request is reachable here: the checkout is "
                         "read-only, state/ and .env are absent, and there are no devices."),
            "done": True,
            "prompt_eval_count": 12,
            "eval_count": 24,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
open(sys.argv[1], "w").write(str(server.server_port))
server.serve_forever()
PY
python3 "$SCRATCH/stub_tier.py" "$SCRATCH/port" &
STUB_PID=$!
if [[ "${PX_REHEARSAL_KEEP:-0}" == "1" ]]; then
    trap 'kill "$STUB_PID" 2>/dev/null || true; echo "scratch kept: $SCRATCH" >&2' EXIT
else
    trap 'kill "$STUB_PID" 2>/dev/null || true; rm -rf "$SCRATCH"' EXIT
fi
for _ in $(seq 50); do [[ -s "$SCRATCH/port" ]] && break; sleep 0.1; done
PORT="$(cat "$SCRATCH/port")"
echo "tier stand-in on 127.0.0.1:$PORT (pid $STUB_PID)"

cat > "$SCRATCH/run/inside.sh" <<'INSIDE'
#!/bin/sh
# Runs INSIDE the sandbox: probes first (so their result is about the same
# sandbox the worker runs in), then the worker.
set -u
REPO="${PX_PROTOTYPE_REPO:-/home/pi/picar-x-hacking}"
UUID="${PX_REHEARSAL_UUID:?}"

echo "== identity =="
id

echo "== P1 write NEW file into the production repo root (expect: read-only) =="
( echo canary > "$REPO/CANARY-281-WORKER-DELETE-ME.tmp" ) 2>&1; echo "exit=$?"
echo "== P2 write NEW file into state/ (expect: read-only) =="
( echo canary > "$REPO/state/CANARY-281-WORKER-DELETE-ME.tmp" ) 2>&1; echo "exit=$?"
echo "== P3 list state/ (expect: hidden) =="
ls "$REPO/state" 2>&1; echo "exit=$?"
echo "== P4 read .env (expect: neutered) =="
head -c 40 "$REPO/.env" 2>&1; echo "exit=$?"
echo "== P5 sudo -n (expect: blocked) =="
sudo -n true 2>&1; echo "exit=$?"
echo "== P6 device nodes (expect: absent) =="
for d in /dev/gpiomem /dev/i2c-1 /dev/snd; do printf '  %s: ' "$d"; ls -d "$d" 2>&1; done
echo "== P7 read the request (expect: readable) =="
head -c 80 "$PX_RESEARCH_INBOX/$UUID.json"; echo

echo "== P8 the worker, in this same sandbox =="
"$REPO/bin/px-research-worker" "$UUID"; echo "worker exit=$?"

echo "== P9 outbox after this run (expect: one new result for this uuid, no .tmp left) =="
ls -la "$PX_RESEARCH_OUTBOX"
printf '  results for this uuid: %s   .tmp files: %s\n' \
    "$(ls "$PX_RESEARCH_OUTBOX/$UUID.json" 2>/dev/null | wc -l)" \
    "$(ls "$PX_RESEARCH_OUTBOX"/.*.tmp 2>/dev/null | wc -l)"
echo "== P10 its contents =="
cat "$PX_RESEARCH_OUTBOX/$UUID.json"
echo
INSIDE
chmod 0755 "$SCRATCH/run/inside.sh"

sandbox() {  # sandbox <uuid> [extra --setenv pairs...]
    local id="$1"; shift
    PX_PROTOTYPE_REPO="$REPO" PX_REHEARSAL_UUID="$id" \
    bwrap \
      --ro-bind / / \
      --dev /dev \
      --ro-bind "$SCRATCH/empty" "$REPO/state" \
      --ro-bind "$SCRATCH/empty" /home/pi/.claude \
      --ro-bind /dev/null "$REPO/.env" \
      --bind "$SCRATCH/run" /var/tmp \
      --unshare-user --unshare-pid --unshare-ipc \
      --uid 65534 --gid 65534 \
      --die-with-parent \
      --setenv PX_PROTOTYPE_REPO "$REPO" \
      --setenv PX_REHEARSAL_UUID "$id" \
      --setenv PX_RESEARCH_INBOX /var/tmp/inbox \
      --setenv PX_RESEARCH_OUTBOX /var/tmp/outbox \
      --setenv PX_STATE_DIR /var/tmp/state \
      --setenv HOME /var/tmp \
      "$@" \
      -- /var/tmp/inside.sh
}

echo "########## run 1: a tier is reachable from inside the sandbox ##########"
sandbox "$U1" \
    --setenv PX_M5_SPARK_HOST "http://127.0.0.1:$PORT" \
    --setenv PX_M5_SPARK_MODEL rehearsal-probe

echo
# Explicitly unset rather than relying on the caller's environment being clean:
# this run is *about* the no-credential shape, so it has to be the same shape
# however the rehearsal was invoked.
echo "########## run 2: no credential in the sandbox ##########"
sandbox "$U2" \
    --unsetenv PX_M5_SPARK_MODEL --unsetenv PX_M5_SPARK_API_KEY \
    --unsetenv OLLAMA_API_KEY --unsetenv OLLAMA_CLOUD_API_KEY

echo "--- post-run leak check on the real tree (expect: empty) ---" >&2
ls "$REPO"/CANARY-281-WORKER-DELETE-ME.tmp 2>/dev/null && echo "LEAK: repo root write escaped" >&2
ls "$REPO"/state/CANARY-281-WORKER-DELETE-ME.tmp 2>/dev/null && echo "LEAK: state/ write escaped" >&2
git -C "$REPO" status --porcelain
