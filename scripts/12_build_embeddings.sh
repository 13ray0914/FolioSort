#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
ENV="${SPECTER2_VENV:-$ROOT/.venv_specter2}"
if [[ ! -x "$ENV/bin/python" ]]; then
  echo "WARNING: SPECTER2 environment is missing: $ENV"
  echo "Run: $ROOT/scripts/install_specter2_env.sh"
  exit 0
fi
exec "$ENV/bin/python" "$ROOT/scripts/specter2_embed.py" --config "$ROOT/config.json" "$@"
