#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
PID_FILE="$ROOT/logs/vision-server.pid"
if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "Stopped vision server PID $PID"
  fi
  rm -f "$PID_FILE"
else
  echo "No pipeline-managed vision server PID file found."
fi
