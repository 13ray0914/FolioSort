#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
PIDFILE="$ROOT/logs/review-app.pid"
if [[ -f "$PIDFILE" ]]; then
  pid=$(cat "$PIDFILE" 2>/dev/null || true)
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
    echo "Stopped Review Literature App (PID $pid)."
  fi
  rm -f "$PIDFILE"
fi
pkill -f 'scripts/review_app_server.py' 2>/dev/null || true
