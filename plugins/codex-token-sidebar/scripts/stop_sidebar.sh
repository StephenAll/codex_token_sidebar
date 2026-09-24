#!/bin/zsh
set -euo pipefail

PLUGIN_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
exec "$PYTHON_BIN" "$PLUGIN_DIR/runtime/lifecycle.py" stop "$@"
