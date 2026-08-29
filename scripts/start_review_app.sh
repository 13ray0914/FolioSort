#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
PORT="${REVIEW_APP_PORT:-8766}"
URL="http://127.0.0.1:${PORT}/"
mkdir -p "$ROOT/logs"

if curl -fsS "$URL/health" >/dev/null 2>&1; then
  echo "Review Literature App already running: $URL"
else
  nohup "$ROOT/.venv/bin/python" "$ROOT/scripts/review_app_server.py" \
    --host 127.0.0.1 --port "$PORT" \
    > "$ROOT/logs/review-app.log" 2>&1 &
  echo $! > "$ROOT/logs/review-app.pid"
  for _ in $(seq 1 80); do
    if curl -fsS "$URL/health" >/dev/null 2>&1; then break; fi
    sleep 0.25
  done
  curl -fsS "$URL/health" >/dev/null 2>&1 || {
    echo "ERROR: Review Literature App did not start."
    tail -n 80 "$ROOT/logs/review-app.log" 2>/dev/null || true
    exit 1
  }
  echo "Review Literature App started: $URL"
fi

if command -v powershell.exe >/dev/null 2>&1; then
  # Prefer Microsoft Edge app mode so the dashboard looks like a standalone Windows app.
  powershell.exe -NoProfile -Command "\$edge=(Get-Command msedge.exe -ErrorAction SilentlyContinue).Source; if (\$edge) { Start-Process -FilePath \$edge -ArgumentList '--app=$URL' } else { Start-Process '$URL' }" >/dev/null 2>&1 || true
elif command -v explorer.exe >/dev/null 2>&1; then
  explorer.exe "$URL" >/dev/null 2>&1 || true
fi
