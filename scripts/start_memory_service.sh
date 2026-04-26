#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${MEMPALACE_LAUNCH_LABEL:-com.mempalace.mcp-http}"
HOST="${MEMPALACE_HTTP_HOST:-127.0.0.1}"
PORT="${MEMPALACE_HTTP_PORT:-47291}"
PALACE_PATH="${MEMPALACE_PALACE_PATH:-}"
L1_DIR="${MEMPALACE_L1_DIR:-$HOME/.claude/memory-bridge}"
LOG_DIR="${MEMPALACE_LOG_DIR:-$HOME/.mempalace/logs}"
RUN_DIR="${MEMPALACE_RUN_DIR:-$HOME/.mempalace/run}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PID_FILE="$RUN_DIR/memory-service.pid"
OUT_LOG="$LOG_DIR/memory-service.out.log"
ERR_LOG="$LOG_DIR/memory-service.err.log"
HEALTH_URL="http://$HOST:$PORT/health"
DIRECT="false"
REINSTALL="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="$2"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --palace)
      PALACE_PATH="$2"
      shift 2
      ;;
    --l1-dir)
      L1_DIR="$2"
      shift 2
      ;;
    --direct)
      DIRECT="true"
      shift
      ;;
    --reinstall)
      REINSTALL="true"
      shift
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -n "${MEMPALACE_PYTHON:-}" ]]; then
  PYTHON_BIN="$MEMPALACE_PYTHON"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

mkdir -p "$LOG_DIR" "$RUN_DIR"

wait_for_health() {
  for _ in $(seq 1 30); do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      echo "memory service started"
      echo "launch_label: $LABEL"
      echo "mcp endpoint: http://$HOST:$PORT/mcp"
      echo "bridge config: http://$HOST:$PORT/api/bridge/config"
      return 0
    fi
    sleep 0.5
  done
  return 1
}

start_direct() {
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    echo "memory service is already reachable at $HEALTH_URL"
    echo "mcp endpoint: http://$HOST:$PORT/mcp"
    exit 0
  fi

  if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE" || true)"
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
      echo "memory service is already running (pid=$OLD_PID)"
      echo "health: http://$HOST:$PORT/health"
      exit 0
    fi
    rm -f "$PID_FILE"
  fi

  CMD=("$PYTHON_BIN" "$ROOT_DIR/http_server.py" --host "$HOST" --port "$PORT")
  if [[ -n "$PALACE_PATH" ]]; then
    CMD+=(--palace "$PALACE_PATH")
  fi

  echo "starting memory service directly..."
  echo "python: $PYTHON_BIN"
  echo "host: $HOST"
  echo "port: $PORT"
  echo "l1_dir: $L1_DIR"
  if [[ -n "$PALACE_PATH" ]]; then
    echo "palace: $PALACE_PATH"
  fi

  nohup env MEMPALACE_L1_DIR="$L1_DIR" "${CMD[@]}" >"$OUT_LOG" 2>"$ERR_LOG" &
  PID=$!
  echo "$PID" > "$PID_FILE"
}

write_launch_agent() {
  mkdir -p "$HOME/Library/LaunchAgents"
  ENTRY="$ROOT_DIR/scripts/mempalace_service_entry.sh"
  if [[ ! -x "$ENTRY" ]]; then
    chmod +x "$ENTRY"
  fi

  PALACE_ENV=""
  if [[ -n "$PALACE_PATH" ]]; then
    PALACE_ENV="    <key>MEMPALACE_PALACE_PATH</key>
    <string>$PALACE_PATH</string>"
  fi

  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ENTRY</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>MEMPALACE_HTTP_HOST</key>
    <string>$HOST</string>
    <key>MEMPALACE_HTTP_PORT</key>
    <string>$PORT</string>
    <key>MEMPALACE_L1_DIR</key>
    <string>$L1_DIR</string>
    <key>MEMPALACE_ROOT_DIR</key>
    <string>$ROOT_DIR</string>
$PALACE_ENV
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>$ROOT_DIR</string>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/mcp-http.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/mcp-http-error.log</string>
  <key>ThrottleInterval</key>
  <integer>10</integer>
</dict>
</plist>
EOF
}

start_launch_agent() {
  write_launch_agent
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null
  launchctl enable "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null
}

if [[ "$DIRECT" != "true" && "$(uname -s)" == "Darwin" ]]; then
  if [[ "$REINSTALL" == "true" || ! -f "$PLIST" ]]; then
    echo "installing launch agent: $PLIST"
    start_launch_agent
  elif curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    echo "memory service is already reachable at $HEALTH_URL"
    echo "mcp endpoint: http://$HOST:$PORT/mcp"
    exit 0
  else
    launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || start_launch_agent
  fi
else
  start_direct
fi

if wait_for_health; then
  exit 0
fi

echo "memory service start requested but health check timed out"
echo "check logs:"
echo "  $LOG_DIR/mcp-http.log"
echo "  $LOG_DIR/mcp-http-error.log"
exit 1