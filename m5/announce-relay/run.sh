#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
set -a; source ./.env; set +a            # export EVERY var from .env
exec .venv/bin/uvicorn announce_relay.app:app --host 0.0.0.0 --port 7862
