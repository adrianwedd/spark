#!/bin/sh
# Runs INSIDE the sandbox spawned by run-prototype.sh (issue #281 phase 2).
# Not wired into CI, not invoked by any production path -- a standalone
# adversarial-canary rehearsal for docs/operations/agent-os-isolation-design.md.
#
# Every write attempt targets a NEW filename, never an existing tracked
# file, so a sandbox-construction bug fails safe (a stray file to delete)
# instead of corrupting real repo/state content.
set -u
REPO="${PX_PROTOTYPE_REPO:-/home/pi/picar-x-hacking}"

echo "== identity =="
id
echo "== 1. sudo escalation (no_new_privs should block the setuid transition) =="
sudo -n true 2>&1; echo "exit=$?"
echo "== 2. write NEW file into production repo root =="
( echo canary > "$REPO/CANARY-281-DELETE-ME.tmp" ) 2>&1; echo "exit=$?"
echo "== 3. write NEW file into state/ =="
( echo canary > "$REPO/state/CANARY-281-DELETE-ME.tmp" ) 2>&1; echo "exit=$?"
echo "== 4. list state/ (should be hidden entirely) =="
ls "$REPO/state" 2>&1; echo "exit=$?"
echo "== 5. GPIO device node visible? =="
ls -la /dev/gpiomem 2>&1; echo "exit=$?"
echo "== 6. I2C device node visible? =="
ls -la /dev/i2c-1 2>&1; echo "exit=$?"
echo "== 7. audio device node visible? =="
ls -la /dev/snd 2>&1; echo "exit=$?"
echo "== 8. systemctl restart against a NONEXISTENT unit (probes authz path with zero blast radius) =="
systemctl restart px-281-canary-nonexistent.service 2>&1; echo "exit=$?"
echo "== 9. read source (should succeed, read-only bind) =="
head -3 "$REPO/src/pxh/state.py" 2>&1
echo "== 10. read .env (should be hidden/neutered) =="
cat "$REPO/.env" 2>&1; echo "exit=$?"
echo "== 11. list pi's Claude credentials dir (should be hidden) =="
ls /home/pi/.claude 2>&1; echo "exit=$?"
echo "== 12. sanity: sandbox has SOME writable path (proves it isn't just inert) =="
( echo x > /var/tmp/canary-writetest ) 2>&1; cat /var/tmp/canary-writetest 2>&1; echo "exit=$?"
