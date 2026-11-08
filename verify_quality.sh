#!/usr/bin/env bash
set -euo pipefail
QUALITY_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$QUALITY_ROOT"
if [[ -n "${FESTIVE_QUALITY_PYTHON:-}" ]]; then
  QUALITY_PYTHON="$FESTIVE_QUALITY_PYTHON"
elif [[ -x "$QUALITY_ROOT/.venv/bin/python" ]]; then
  QUALITY_PYTHON="$QUALITY_ROOT/.venv/bin/python"
else
  QUALITY_PYTHON=python3
fi
export PYTHONPATH="$QUALITY_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
"$QUALITY_PYTHON" -m ruff check --config pyproject.toml src tests tools notebooks
"$QUALITY_PYTHON" -m ruff format --check --config pyproject.toml src tests tools notebooks
"$QUALITY_PYTHON" -m mypy --config-file pyproject.toml
"$QUALITY_PYTHON" tools/check_source_layout.py
