#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
PORT="${REVIEW_APP_PORT:-8766}"
URL="http://127.0.0.1:${PORT}/"
PYTHON_BIN="${REVIEW_PYTHON:-$ROOT/.venv/bin/python}"
_BASE_VERSION="$("$PYTHON_BIN" -c 'from foliosort import __version__; print(__version__)' 2>/dev/null || echo "unknown")"
EXPECTED_VERSION="${_BASE_VERSION}-security-hardened-network-workspace"
mkdir -p "$ROOT/logs"

health="$(curl -fsS "${URL}health" 2>/dev/null || true)"
if [[ -n "$health" ]] && printf '%s' "$health" | grep -Eq '"version"[[:space:]]*:[[:space:]]*"'"$EXPECTED_VERSION"'"'; then
  echo "FolioSort already running: $URL ($EXPECTED_VERSION)"
else
  if [[ -n "$health" ]]; then
    echo "Older FolioSort detected; restarting it for $EXPECTED_VERSION..."
    "$ROOT/scripts/stop_review_app.sh" >/dev/null 2>&1 || true
    sleep 0.5
  fi
  [[ -x "$PYTHON_BIN" ]] || { echo "ERROR: Python is not executable: $PYTHON_BIN"; exit 2; }
  nohup "$PYTHON_BIN" "$ROOT/scripts/review_app_server.py" \
    --host 127.0.0.1 --port "$PORT" \
    > "$ROOT/logs/review-app.log" 2>&1 &
  echo $! > "$ROOT/logs/review-app.pid"
  for _ in $(seq 1 80); do
    health="$(curl -fsS "${URL}health" 2>/dev/null || true)"
    if [[ -n "$health" ]] && printf '%s' "$health" | grep -Eq '"version"[[:space:]]*:[[:space:]]*"'"$EXPECTED_VERSION"'"'; then break; fi
    sleep 0.25
  done
  health="$(curl -fsS "${URL}health" 2>/dev/null || true)"
  if [[ -z "$health" ]] || ! printf '%s' "$health" | grep -Eq '"version"[[:space:]]*:[[:space:]]*"'"$EXPECTED_VERSION"'"'; then
    echo "ERROR: FolioSort did not start with expected version $EXPECTED_VERSION."
    tail -n 80 "$ROOT/logs/review-app.log" 2>/dev/null || true
    exit 1
  fi
  echo "FolioSort started: $URL ($EXPECTED_VERSION)"
fi

if command -v powershell.exe >/dev/null 2>&1; then
  powershell.exe -NoProfile -Command "\$edge=(Get-Command msedge.exe -ErrorAction SilentlyContinue).Source; if (\$edge) { Start-Process -FilePath \$edge -ArgumentList '--app=$URL' } else { Start-Process '$URL' }" >/dev/null 2>&1 || true
elif command -v explorer.exe >/dev/null 2>&1; then
  explorer.exe "$URL" >/dev/null 2>&1 || true
fi
