#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
ENV="${NETWORK_VENV:-$ROOT/.venv_network}"
if [[ ! -x "$ENV/bin/python" ]]; then
  echo "WARNING: network environment is missing: $ENV"
  echo "Run: $ROOT/scripts/install_network_env.sh"
  exit 0
fi
exec "$ENV/bin/python" "$ROOT/scripts/13_build_multiplex_network.py" --config "$ROOT/config.json" "$@"
