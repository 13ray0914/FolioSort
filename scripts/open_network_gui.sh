#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
"$ROOT/scripts/start_review_app.sh" >/dev/null
# start_review_app.sh already opens the dashboard; open the network as well.
if command -v powershell.exe >/dev/null 2>&1; then
  powershell.exe -NoProfile -Command "Start-Process 'http://127.0.0.1:8766/network'" >/dev/null 2>&1 || true
fi
