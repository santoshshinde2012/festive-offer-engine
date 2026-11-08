#!/usr/bin/env bash
set -euo pipefail
DEMO_ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$DEMO_ROOT:$DEMO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
python3 -m tools.record_results "$@"
