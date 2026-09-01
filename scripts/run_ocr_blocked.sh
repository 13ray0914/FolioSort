#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
PROJECT="${REVIEW_PROJECT:-default}"
PYTHON_BIN="${REVIEW_PYTHON:-$ROOT/.venv/bin/python}"
LOG_DIR="$ROOT/logs"
LOG="$LOG_DIR/ocr_$(date +%Y%m%d).log"
LOCK="$LOG_DIR/auto_pipeline.lock"
mkdir -p "$LOG_DIR" "$LOG_DIR/process-snapshots"

if [[ "${REVIEW_PIPELINE_LOCKED:-0}" != "1" ]]; then
  if flock -n -o -E 200 "$LOCK" env REVIEW_PIPELINE_LOCKED=1 "$0" "$@"; then
    exit 0
  fi
  rc=$?
  if [[ "$rc" -eq 200 ]]; then
    echo "[$(date '+%F %T')] Another FolioSort process is already running."
  fi
  exit "$rc"
fi

exec > >(tee -a "$LOG") 2>&1
cd "$ROOT"

IDS_FILE="$LOG_DIR/ocr-success-${PROJECT}-$$.txt"
cleanup() { rm -f -- "$IDS_FILE"; }
trap cleanup EXIT

echo
echo "========== OCR blocked papers $(date '+%F %T') project=$PROJECT =========="
"$PYTHON_BIN" -u scripts/ocr_blocked_papers.py \
  --config "$ROOT/config.json" --project "$PROJECT" --ids-file "$IDS_FILE"

REVIEW_IDS="$(tr -d '\r\n' < "$IDS_FILE")"
if [[ -z "$REVIEW_IDS" ]]; then
  echo "OCR produced no papers requiring downstream processing."
  exit 0
fi

export REVIEW_IDS
PIPE_SOURCE="$ROOT/scripts/run_review_pipeline.sh"
PIPE_SNAPSHOT="$LOG_DIR/process-snapshots/run_review_pipeline.ocr.$$.sh"
cp -- "$PIPE_SOURCE" "$PIPE_SNAPSHOT"
chmod 700 "$PIPE_SNAPSHOT"
trap 'rm -f -- "$IDS_FILE" "$PIPE_SNAPSHOT"' EXIT

echo "OCR-DOWNSTREAM ids=$REVIEW_IDS"
"$PIPE_SNAPSHOT"
echo "========== OCR Process completed $(date '+%F %T') project=$PROJECT =========="
