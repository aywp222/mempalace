#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${MEMPALACE_ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
HOST="${MEMPALACE_HTTP_HOST:-127.0.0.1}"
PORT="${MEMPALACE_HTTP_PORT:-47291}"
L1_DIR="${MEMPALACE_L1_DIR:-$HOME/.claude/memory-bridge}"
PALACE_PATH="${MEMPALACE_PALACE_PATH:-}"

if [[ -n "${MEMPALACE_PYTHON:-}" ]]; then
  PYTHON_BIN="$MEMPALACE_PYTHON"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

CMD=("$PYTHON_BIN" "$ROOT_DIR/http_server.py" --host "$HOST" --port "$PORT")
if [[ -n "$PALACE_PATH" ]]; then
  CMD+=(--palace "$PALACE_PATH")
fi

exec env MEMPALACE_L1_DIR="$L1_DIR" MEMPALACE_SERVICE_MANAGER="launchd" "${CMD[@]}"