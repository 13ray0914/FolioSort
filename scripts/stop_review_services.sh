#!/usr/bin/env bash
set -u
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"

stop_pidfile() {
  local label="$1" pidfile="$2"
  if [[ -f "$pidfile" ]]; then
    local pid
    pid=$(cat "$pidfile" 2>/dev/null || true)
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" && echo "Stopped $label PID $pid"
    fi
    rm -f "$pidfile"
  else
    echo "No pipeline-owned $label PID file. A manually started server must be stopped manually."
  fi
}

stop_pidfile "text Qwen llama-server" "$ROOT/logs/qwen-server.pid"
stop_pidfile "vision llama-server" "$ROOT/logs/vision-server.pid"
stop_pidfile "curation UI" "$ROOT/logs/curation-server.pid"
if [[ -f "$ROOT/docker-compose.grobid.yml" ]]; then
  (cd "$ROOT" && docker compose -f docker-compose.grobid.yml stop) || true
fi
