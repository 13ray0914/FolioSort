#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
PORT="${REVIEW_CURATION_PORT:-8765}"
URL="http://127.0.0.1:$PORT/"
EXPECTED_VERSION="4.2.2-security-hardening"
PYTHON_BIN="${REVIEW_PYTHON:-$ROOT/.venv/bin/python}"
mkdir -p "$ROOT/logs"
health="$(curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
if [[ -z "$health" ]] || ! printf '%s' "$health" | grep -Eq '"version"[[:space:]]*:[[:space:]]*"'"$EXPECTED_VERSION"'"'; then
  if [[ -n "$health" ]]; then
    "$ROOT/scripts/stop_curation_gui.sh" >/dev/null 2>&1 || true
    sleep 0.4
  fi
  [[ -x "$PYTHON_BIN" ]] || { echo "ERROR: Python is not executable: $PYTHON_BIN"; exit 2; }
  nohup "$PYTHON_BIN" -u "$ROOT/scripts/curation_server.py" --port "$PORT" > "$ROOT/logs/curation-server.log" 2>&1 &
  echo $! > "$ROOT/logs/curation-server.pid"
  for _ in $(seq 1 40); do
    health="$(curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
    [[ -n "$health" ]] && printf '%s' "$health" | grep -Eq '"version"[[:space:]]*:[[:space:]]*"'"$EXPECTED_VERSION"'"' && break
    sleep 0.25
  done
fi
if command -v cmd.exe >/dev/null 2>&1; then
  cmd.exe /C start "" "$URL" >/dev/null 2>&1 || true
else
  echo "$URL"
fi
echo "Curation GUI: $URL ($EXPECTED_VERSION)"
