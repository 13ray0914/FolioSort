#!/usr/bin/env bash
set -euo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
FILE="$ROOT/outputs/network_gui/network.html"
if [[ ! -f "$FILE" ]]; then
  echo "Network GUI not found. Run: python scripts/08_build_network.py"
  exit 1
fi
if command -v explorer.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
  explorer.exe "$(wslpath -w "$FILE")" >/dev/null 2>&1 &
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$FILE" >/dev/null 2>&1 &
else
  echo "$FILE"
fi
