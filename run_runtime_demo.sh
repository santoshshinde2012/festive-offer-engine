#!/usr/bin/env bash
set -euo pipefail
DEMO_ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$DEMO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m festive_offers.online.runtime_demo --output "$DEMO_ROOT/output" "$@"
