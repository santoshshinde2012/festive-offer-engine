#!/usr/bin/env bash
# Start a new synthetic customer UI/API and a separately running durable worker.
set -euo pipefail
DEMO_ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$DEMO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
RUNTIME_ENV="$(python3 "$DEMO_ROOT/tools/prepare_local_runtime.py")"
# This file was just generated locally with shell-quoted values and mode 0600.
source "$RUNTIME_ENV"
RUNTIME_DIR="$(dirname "$RUNTIME_ENV")"
python3 -m festive_offers.online.runtime publish --bundle "$RUNTIME_DIR/bundle.json" --funding "$RUNTIME_DIR/funding.json" --activate
python3 -m festive_offers.online.runtime seed --data "$DEMO_ROOT/data/dataset.json"
python3 - <<'PY'
import json,os
print('Synthetic E-Shop only; no real payment. Open http://127.0.0.1:8080')
print('Customer token:', next(iter(json.loads(os.environ['FESTIVE_AUTH_TOKENS_JSON']))))
print('Checkout ID: diwali_ananya')
print('Runtime configuration:', os.environ['FESTIVE_SQLITE_PATH'].replace('application.sqlite','runtime.env'))
print('Emit fake capture/payout from another terminal using the guide in docs/runtime.md.')
PY
python3 -m festive_offers.online.runtime worker > "$RUNTIME_DIR/worker.log" 2>&1 &
WORKER_PID=$!
trap 'kill "$WORKER_PID" 2>/dev/null || true; wait "$WORKER_PID" 2>/dev/null || true' EXIT
python3 -m festive_offers.online.runtime serve --port 8080
