#!/usr/bin/env bash
set -euo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
FILE="$ROOT/outputs/knowledge_graph/knowledge.html"
if [[ ! -f "$FILE" ]]; then
  echo "Knowledge graph GUI not found. Run: $ROOT/scripts/14_build_knowledge_graph.sh"
  exit 1
fi
if command -v explorer.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
  explorer.exe "$(wslpath -w "$FILE")" >/dev/null 2>&1 &
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$FILE" >/dev/null 2>&1 &
else
  echo "$FILE"
fi
