#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
VENV="${NETWORK_VENV:-$ROOT/.venv_network}"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "WARNING: network environment missing: $VENV"
  echo "Run: $ROOT/scripts/install_network_env.sh"
  exit 0
fi
exec "$VENV/bin/python" "$ROOT/scripts/14_build_knowledge_graph.py" --config "$ROOT/config.json" "$@"
