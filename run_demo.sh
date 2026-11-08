#!/usr/bin/env bash
set -euo pipefail
DEMO_ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$DEMO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m unittest discover -s "$DEMO_ROOT/tests" -v
python3 -m festive_offers.online.system_demo --output "$DEMO_ROOT/output" "$@"
