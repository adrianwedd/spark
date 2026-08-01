#!/bin/zsh
# Push Find Hub tracker locations from M5 to the Pi, atomically.
#
# Runs from cron every 5 min. The previous crontab piped query stdout straight
# into `ssh 'cat > findmyhub.json'`, which truncates the file at connection-open
# and leaves it empty until the query prints — or for the whole 5-min window if
# the query fails. px-mind reads every 60s and logged "read error: Expecting
# value" every time it caught the empty window.
#
# Rules:
#  - Buffer the query output locally; never open the remote file until we have
#    complete, valid JSON in hand.
#  - Push to a temp file and mv into place — readers see old-complete or
#    new-complete, never empty.
#  - On query failure, leave the previous file alone (px-mind's own staleness
#    check handles aging data).
#  - Address the Pi by IP: picar.local mDNS is unreliable from the Mac.

set -o pipefail

PI=pi@192.168.0.27
DEST=/home/pi/picar-x-hacking/state/findmyhub.json
TOOLS_DIR="$HOME/GoogleFindMyTools"
LOG="$TOOLS_DIR/push_findmyhub.log"

cd "$TOOLS_DIR" || exit 1

out=$(venv/bin/python3 query_findmyhub.py 2>>"$LOG")
if [ -z "$out" ]; then
    echo "$(date -Iseconds) query produced no output — keeping previous file" >>"$LOG"
    exit 1
fi
if ! printf '%s' "$out" | /usr/bin/python3 -c 'import json,sys; json.load(sys.stdin)' 2>>"$LOG"; then
    echo "$(date -Iseconds) query output is not valid JSON — keeping previous file" >>"$LOG"
    exit 1
fi

printf '%s' "$out" | ssh -o BatchMode=yes -o ConnectTimeout=10 "$PI" \
    "cat > $DEST.tmp && mv $DEST.tmp $DEST" \
    || echo "$(date -Iseconds) push to Pi failed" >>"$LOG"
