#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
PORT="${REVIEW_CURATION_PORT:-8765}"
URL="http://127.0.0.1:$PORT/"
mkdir -p "$ROOT/logs"
if ! curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  nohup "$ROOT/.venv/bin/python" -u "$ROOT/scripts/curation_server.py" --port "$PORT" > "$ROOT/logs/curation-server.log" 2>&1 &
  echo $! > "$ROOT/logs/curation-server.pid"
  for _ in $(seq 1 40); do
    curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 0.25
  done
fi
if command -v cmd.exe >/dev/null 2>&1; then
  cmd.exe /C start "" "$URL" >/dev/null 2>&1 || true
else
  echo "$URL"
fi
echo "Curation GUI: $URL"
