#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
ENV="${NETWORK_VENV:-$ROOT/.venv_network}"
PYTHON_BIN="${NETWORK_PYTHON:-/usr/bin/python3}"
"$PYTHON_BIN" -m venv "$ENV"
"$ENV/bin/python" -m pip install --upgrade pip wheel
"$ENV/bin/python" -m pip install -r "$ROOT/requirements_network_v4.txt"
echo "Network environment ready: $ENV"
