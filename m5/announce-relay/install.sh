#!/usr/bin/env bash
set -euo pipefail
# Run on M5 from a checkout of m5/announce-relay/. Requires .env to exist.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

[[ -f .env ]] || { echo "create .env first (cp .env.example .env)"; exit 1; }
chmod +x run.sh

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Validate config + create data dirs (run.sh sources .env at runtime; the token
# and all other vars live ONLY in .env, never baked into the plist).
set -a; source .env; set +a
mkdir -p "${RELAY_DATA_DIR:?set RELAY_DATA_DIR in .env}/cache" "${RELAY_DATA_DIR}/priv"

PLIST="$HOME/Library/LaunchAgents/com.spark.announce-relay.plist"
sed -e "s#<m5user>#$USER#g" com.spark.announce-relay.plist > "$PLIST"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Loaded com.spark.announce-relay. Health:"
sleep 2
curl -fsS "http://127.0.0.1:7862/health" && echo
