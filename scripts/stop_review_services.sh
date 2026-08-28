#!/usr/bin/env bash
set -u
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
PIDFILE="$ROOT/logs/qwen-server.pid"
if [[ -f "$PIDFILE" ]]; then
  pid=$(cat "$PIDFILE" 2>/dev/null || true)
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" && echo "Stopped Qwen llama-server PID $pid"
  fi
  rm -f "$PIDFILE"
else
  echo "No pipeline-owned Qwen PID file. If Qwen was started manually, stop that terminal/process manually."
fi
if [[ -f "$ROOT/docker-compose.grobid.yml" ]]; then
  (cd "$ROOT" && docker compose -f docker-compose.grobid.yml stop) || true
fi
