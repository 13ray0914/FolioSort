#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
ENV="${SPECTER2_VENV:-$ROOT/.venv_specter2}"
PYTHON_BIN="${SPECTER2_PYTHON:-/usr/bin/python3}"
"$PYTHON_BIN" -m venv "$ENV"
"$ENV/bin/python" -m pip install --upgrade pip wheel
"$ENV/bin/python" -m pip install -r "$ROOT/requirements_specter2.txt"
echo "SPECTER2 environment ready: $ENV"
echo "The model weights will be downloaded from Hugging Face on first embedding run."
