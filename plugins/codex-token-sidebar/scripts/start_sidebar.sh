#!/bin/zsh
set -euo pipefail

PLUGIN_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" "$PLUGIN_DIR/runtime/startup.py" --check-python
if [[ "${1:-}" == "--background" ]]; then
  shift
  exec "$PYTHON_BIN" "$PLUGIN_DIR/runtime/lifecycle.py" start "$@"
fi

exec "$PYTHON_BIN" "$PLUGIN_DIR/runtime/codex_token_sidebar.py" "$@"
