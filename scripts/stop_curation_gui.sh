#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
PIDFILE="$ROOT/logs/curation-server.pid"
if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE" || true)"
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then kill "$PID" || true; fi
  rm -f "$PIDFILE"
fi
pkill -f "$ROOT/scripts/curation_server.py" 2>/dev/null || true
echo "Curation GUI stopped."
