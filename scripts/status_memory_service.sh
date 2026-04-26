#!/usr/bin/env bash
set -euo pipefail

LABEL="${MEMPALACE_LAUNCH_LABEL:-com.mempalace.mcp-http}"
HOST="${MEMPALACE_HTTP_HOST:-127.0.0.1}"
PORT="${MEMPALACE_HTTP_PORT:-47291}"
RUN_DIR="${MEMPALACE_RUN_DIR:-$HOME/.mempalace/run}"
PID_FILE="$RUN_DIR/memory-service.pid"
HEALTH_URL="http://$HOST:$PORT/health"

HEALTH="down"
HEALTH_JSON=""
if HEALTH_JSON="$(curl -fsS "$HEALTH_URL" 2>/dev/null)"; then
  HEALTH="up"
fi

LAUNCHD="unavailable"
LAUNCHD_PID=""
if [[ "$(uname -s)" == "Darwin" ]]; then
  if launchctl print "gui/$(id -u)/$LABEL" >/tmp/mempalace-launchctl-status.$$ 2>/dev/null; then
    LAUNCHD="loaded"
    LAUNCHD_PID="$(awk '/pid = / {print $3; exit}' /tmp/mempalace-launchctl-status.$$ || true)"
  else
    LAUNCHD="not_loaded"
  fi
  rm -f /tmp/mempalace-launchctl-status.$$
fi

PORT_PID="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
PID_FILE_PID=""
if [[ -f "$PID_FILE" ]]; then
  PID_FILE_PID="$(cat "$PID_FILE" || true)"
fi

MODE="down"
if [[ "$HEALTH" == "up" && "$LAUNCHD" == "loaded" ]]; then
  MODE="launchd"
elif [[ "$HEALTH" == "up" ]]; then
  MODE="external"
elif [[ "$LAUNCHD" == "loaded" ]]; then
  MODE="launchd_not_healthy"
fi

echo "health=$HEALTH"
echo "mode=$MODE"
echo "launch_label=$LABEL"
echo "launchd=$LAUNCHD"
if [[ -n "$LAUNCHD_PID" ]]; then
  echo "launchd_pid=$LAUNCHD_PID"
fi
if [[ -n "$PORT_PID" ]]; then
  echo "port_pid=$PORT_PID"
fi
if [[ -n "$PID_FILE_PID" ]]; then
  echo "pid_file_pid=$PID_FILE_PID"
fi
echo "mcp_endpoint=http://$HOST:$PORT/mcp"
echo "bridge_query_endpoint=http://$HOST:$PORT/api/bridge/query"
if [[ -n "$HEALTH_JSON" ]]; then
  echo "health_json=$HEALTH_JSON"
fi