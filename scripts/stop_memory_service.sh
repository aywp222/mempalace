#!/usr/bin/env bash
set -euo pipefail

LABEL="${MEMPALACE_LAUNCH_LABEL:-com.mempalace.mcp-http}"
RUN_DIR="${MEMPALACE_RUN_DIR:-$HOME/.mempalace/run}"
PID_FILE="$RUN_DIR/memory-service.pid"
HOST="${MEMPALACE_HTTP_HOST:-127.0.0.1}"
PORT="${MEMPALACE_HTTP_PORT:-47291}"
HEALTH_URL="http://$HOST:$PORT/health"
FORCE="false"
UNINSTALL="false"

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
    --force)
      FORCE="true"
      shift
      ;;
    --uninstall)
      UNINSTALL="true"
      shift
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

HEALTH_URL="http://$HOST:$PORT/health"

if [[ "$(uname -s)" == "Darwin" ]]; then
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    echo "launch agent stopped: $LABEL"
  fi
  if [[ "$UNINSTALL" == "true" ]]; then
    rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
    echo "launch agent plist removed"
  fi
fi

if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" || true)"
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
  if [[ "$FORCE" == "true" ]]; then
    PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "$PIDS" ]]; then
      for P in $PIDS; do
        kill "$P" 2>/dev/null || true
      done
      echo "memory service force-stopped by port ($PORT)"
      exit 0
    fi
  fi
  echo "memory service is still reachable; use --force to stop by port"
  exit 0
fi

echo "memory service stopped"