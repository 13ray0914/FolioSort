#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
ENV="${MINERU_VENV:-$ROOT/.venv_mineru}"
PYTHON_BIN="${MINERU_PYTHON:-/usr/bin/python3}"
"$PYTHON_BIN" -m venv "$ENV"
"$ENV/bin/python" -m pip install --upgrade pip uv
"$ENV/bin/uv" pip install --python "$ENV/bin/python" -U "mineru[all]"
echo "MinerU environment ready: $ENV"
echo "Set visual.mineru.command in config.json to: $ENV/bin/mineru"
