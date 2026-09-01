#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
PROJECT="${REVIEW_PROJECT:-default}"
NETWORK_PYTHON="${NETWORK_PYTHON:-$ROOT/.venv_network/bin/python}"
LOG_DIR="$ROOT/logs"
LOG="$LOG_DIR/network_rebuild_$(date +%Y%m%d).log"
LOCK="$LOG_DIR/auto_pipeline.lock"
mkdir -p "$LOG_DIR"

# Share the full Process lock so analysis and a network-only rebuild can never
# overwrite project outputs at the same time.
if [[ "${REVIEW_PIPELINE_LOCKED:-0}" != "1" ]]; then
  if flock -n -o -E 200 "$LOCK" env REVIEW_PIPELINE_LOCKED=1 "$0" "$@"; then
    exit 0
  fi
  rc=$?
  if [[ "$rc" -eq 200 ]]; then
    echo "[$(date '+%F %T')] Another FolioSort process is already running."
    exit 200
  fi
  exit "$rc"
fi

exec > >(tee -a "$LOG") 2>&1
cd "$ROOT"

[[ -x "$NETWORK_PYTHON" ]] || {
  echo "ERROR: network Python is not executable: $NETWORK_PYTHON"
  exit 2
}

_FS_VERSION="$("$NETWORK_PYTHON" -c 'from foliosort import __version__; print(__version__)' 2>/dev/null || echo "unknown")"
echo
echo "========== Literature Network rebuild v${_FS_VERSION} $(date '+%F %T') =========="
echo "Project scope: $PROJECT"
echo "NETWORK-BUILD-START project=$PROJECT"

"$NETWORK_PYTHON" -u "$ROOT/scripts/13_build_multiplex_network.py" \
  --config "$ROOT/config.json" --project "$PROJECT" \
  --skip-ai-cluster-naming

echo "NETWORK-BUILD-DONE project=$PROJECT"
echo "========== Literature Network rebuild completed $(date '+%F %T') project=$PROJECT =========="
