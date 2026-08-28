#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
VENV="${REVIEW_VENV:-$ROOT/.venv}"
QWEN_SERVER="${QWEN_SERVER:-$HOME/desktop/llm/llama.cpp/build/bin/llama-server}"
QWEN_MODEL="${QWEN_MODEL:-$HOME/models/Qwen3.8-27B/Qwen3.8-27B-Q4_K_M.gguf}"
QWEN_DRAFT_MODEL="${QWEN_DRAFT_MODEL:-$HOME/models/Qwen3.8-27B/mtp-Qwen3.8-27B-Q4_0.gguf}"
QWEN_URL="${QWEN_URL:-http://127.0.0.1:8080/v1/models}"
GROBID_URL="${GROBID_URL:-http://127.0.0.1:8070/api/isalive}"
WAIT_SECONDS="${REVIEW_SERVICE_WAIT:-240}"

mkdir -p "$ROOT/logs"
LOG="$ROOT/logs/auto_pipeline_$(date +%Y%m%d).log"
LOCK="$ROOT/logs/auto_pipeline.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] Another review pipeline is already running." | tee -a "$LOG"
  exit 0
fi
exec > >(tee -a "$LOG") 2>&1

echo ""
echo "========== Review pipeline $(date '+%F %T') =========="
cd "$ROOT"
if [[ ! -f "$VENV/bin/activate" ]]; then
  echo "ERROR: virtualenv not found: $VENV"
  exit 2
fi
# shellcheck disable=SC1090
source "$VENV/bin/activate"

echo "Adaptive chunking:"
python - <<'PYCFG'
import json
from pathlib import Path
p=Path("config.json")
try:
    c=json.loads(p.read_text(encoding="utf-8"))
    llm=c.get("llm", {})
    print("  chunk_max_chars =", llm.get("chunk_max_chars"))
    print("  max_tokens      =", llm.get("max_tokens"))
    print("  adaptive        =", llm.get("adaptive_chunking", {}))
except Exception as e:
    print("  WARNING: could not read config:", e)
PYCFG

wait_until() {
  local description="$1"; shift
  local started now
  started=$(date +%s)
  while true; do
    if "$@" >/dev/null 2>&1; then
      echo "OK: $description"
      return 0
    fi
    now=$(date +%s)
    if (( now - started >= WAIT_SECONDS )); then
      echo "ERROR: timeout waiting for $description"
      return 1
    fi
    sleep 3
  done
}

ensure_docker() {
  if docker info >/dev/null 2>&1; then
    echo "OK: Docker daemon"
    return
  fi
  echo "Docker Desktop is not ready. Trying to start it from Windows..."
  if command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -Command '$p = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"; if (Test-Path $p) { Start-Process $p }' >/dev/null 2>&1 || true
  fi
  wait_until "Docker daemon" docker info
}

ensure_grobid() {
  docker compose -f docker-compose.grobid.yml up -d
  wait_until "GROBID" bash -c "[[ \"\$(curl -fsS '$GROBID_URL' 2>/dev/null)\" == 'true' ]]"
}

ensure_qwen() {
  if curl -fsS "$QWEN_URL" >/dev/null 2>&1; then
    echo "OK: Qwen llama-server already running"
    return
  fi
  echo "Starting Qwen llama-server..."
  if [[ ! -x "$QWEN_SERVER" ]]; then echo "ERROR: llama-server not found: $QWEN_SERVER"; exit 3; fi
  if [[ ! -f "$QWEN_MODEL" ]]; then echo "ERROR: Qwen model not found: $QWEN_MODEL"; exit 3; fi
  if [[ ! -f "$QWEN_DRAFT_MODEL" ]]; then echo "ERROR: MTP draft model not found: $QWEN_DRAFT_MODEL"; exit 3; fi
  nohup "$QWEN_SERVER" \
    -m "$QWEN_MODEL" \
    -md "$QWEN_DRAFT_MODEL" \
    -ngl all \
    -c 65536 \
    -np 1 \
    --flash-attn on \
    --spec-type draft-mtp \
    --spec-draft-n-max 4 \
    --host 127.0.0.1 \
    --port 8080 \
    > "$ROOT/logs/qwen-server.log" 2>&1 &
  echo $! > "$ROOT/logs/qwen-server.pid"
  wait_until "Qwen llama-server" curl -fsS "$QWEN_URL"
}

ensure_docker
ensure_grobid
ensure_qwen

echo "--- Scan raw_pdfs and update stable IDs ---"
python scripts/01_make_manifest.py

echo "--- Run resumable stages 2-7 ---"
# Every active paper is checked, but current stages are hash-checked and skipped.
# New, changed, or interrupted papers are the only ones that do expensive work.
python run_pipeline.py --from-step 2 --to-step 7

echo "--- Rebuild relation network / clustering GUI ---"
if [[ -f scripts/08_build_network.py ]]; then
  python scripts/08_build_network.py || echo "WARNING: network GUI build failed; paper extraction itself is complete."
fi

echo "========== Completed $(date '+%F %T') =========="
