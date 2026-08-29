#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
PROJECT="${1:-${REVIEW_PROJECT:-default}}"
"$ROOT/scripts/start_review_app.sh" >/dev/null
if command -v powershell.exe >/dev/null 2>&1; then
  URL="http://127.0.0.1:8766/knowledge?project=$PROJECT"
  powershell.exe -NoProfile -Command "Start-Process '$URL'" >/dev/null 2>&1 || true
else
  echo "Open: http://127.0.0.1:8766/knowledge?project=$PROJECT"
fi
