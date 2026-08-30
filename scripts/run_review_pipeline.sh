#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
VENV="${REVIEW_VENV:-$ROOT/.venv}"
QWEN_SERVER="${QWEN_SERVER:-$HOME/desktop/llm/llama.cpp/build/bin/llama-server}"
QWEN_MODEL="${QWEN_MODEL:-$HOME/models/Qwen3.8-27B/Qwen3.8-27B-Q4_K_M.gguf}"
QWEN_DRAFT_MODEL="${QWEN_DRAFT_MODEL:-$HOME/models/Qwen3.8-27B/mtp-Qwen3.8-27B-Q4_0.gguf}"
QWEN_URL="${QWEN_URL:-http://127.0.0.1:8080/v1/models}"
GROBID_URL="${GROBID_URL:-http://127.0.0.1:8070/api/isalive}"
WAIT_SECONDS="${REVIEW_SERVICE_WAIT:-300}"
export PYTHONUNBUFFERED=1

mkdir -p "$ROOT/logs"
LOG="$ROOT/logs/auto_pipeline_$(date +%Y%m%d).log"
LOCK="$ROOT/logs/auto_pipeline.lock"

# Hold the lock in the external flock parent, but close the lock FD in the
# pipeline process (-o). Long-lived children such as Qwen, D-Bus, or tee can
# therefore never inherit the pipeline lock.
if [[ "${REVIEW_PIPELINE_LOCKED:-0}" != "1" ]]; then
  if flock -n -o -E 200 "$LOCK" env REVIEW_PIPELINE_LOCKED=1 "$0" "$@"; then
    exit 0
  else
    rc=$?
    if [[ "$rc" -eq 200 ]]; then
      echo "[$(date '+%F %T')] Another review pipeline is already running."
      exit 0
    fi
    exit "$rc"
  fi
fi

exec > >(tee -a "$LOG") 2>&1

echo
echo "========== Review pipeline v4.1.7 $(date '+%F %T') =========="
echo "Project scope: ${REVIEW_PROJECT:-all papers}"
cd "$ROOT"
if [[ ! -f "$VENV/bin/activate" ]]; then
  echo "ERROR: virtualenv not found: $VENV"
  exit 2
fi
# shellcheck disable=SC1090
source "$VENV/bin/activate"

python - <<'PYCFG'
import json
from pathlib import Path
c=json.loads(Path('config.json').read_text(encoding='utf-8'))
print('Core extraction:')
print('  chunk_max_chars =', c.get('llm',{}).get('chunk_max_chars'))
print('  max_tokens      =', c.get('llm',{}).get('max_tokens'))
print('  adaptive        =', c.get('llm',{}).get('adaptive_chunking'))
print('  summary memory  =', c.get('summary_memory',{}))
print('  Crossref        =', c.get('metadata_enrichment',{}).get('crossref',{}).get('enabled'))
print('  OpenAlex        =', c.get('metadata_enrichment',{}).get('openalex',{}).get('enabled'))
print('  Vision LLM      =', c.get('visual',{}).get('vision_llm',{}).get('enabled'))
print('  MinerU          =', c.get('visual',{}).get('mineru',{}).get('enabled'))
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
  echo "Starting text Qwen llama-server..."
  [[ -x "$QWEN_SERVER" ]] || { echo "ERROR: llama-server not found: $QWEN_SERVER"; exit 3; }
  [[ -f "$QWEN_MODEL" ]] || { echo "ERROR: Qwen model not found: $QWEN_MODEL"; exit 3; }
  [[ -f "$QWEN_DRAFT_MODEL" ]] || { echo "ERROR: MTP draft model not found: $QWEN_DRAFT_MODEL"; exit 3; }
  nohup "$QWEN_SERVER" \
    -m "$QWEN_MODEL" \
    -md "$QWEN_DRAFT_MODEL" \
    -ngl all -c 65536 -np 1 \
    --flash-attn on \
    --spec-type draft-mtp --spec-draft-n-max 4 \
    --host 127.0.0.1 --port 8080 \
    > "$ROOT/logs/qwen-server.log" 2>&1 &
  echo $! > "$ROOT/logs/qwen-server.pid"
  wait_until "Qwen llama-server" curl -fsS "$QWEN_URL"
}

ensure_docker
ensure_grobid
ensure_qwen

echo "--- Scan raw_pdfs and update stable IDs ---"
python -u scripts/01_make_manifest.py

echo "--- Run resumable v4 stages 2-11 ---"
python -u run_pipeline.py --from-step 2 --to-step 11

echo "--- Controlled vocabulary + human curation overlay ---"
python -u scripts/11b_apply_curation.py
python -u scripts/16_curation_audit.py || echo "WARNING: curation audit failed; curated data itself is still available."

if [[ "${REVIEW_SKIP_EMBEDDINGS:-0}" != "1" ]]; then
  echo "--- Incremental SPECTER2 embeddings ---"
  scripts/12_build_embeddings.sh || echo "WARNING: SPECTER2 stage skipped/failed; run scripts/install_specter2_env.sh once."
fi

if [[ "${REVIEW_SKIP_NETWORKS:-0}" != "1" ]]; then
  echo "--- Multiplex graph + Leiden clustering ---"
  scripts/13_build_multiplex_network.sh || echo "WARNING: multiplex graph stage skipped/failed; run scripts/install_network_env.sh once."
  echo "--- Scientific knowledge graph ---"
  scripts/14_build_knowledge_graph.sh || echo "WARNING: knowledge graph stage skipped/failed."
fi

echo "========== Completed v4.1.7 $(date '+%F %T') project=${REVIEW_PROJECT:-all} =========="
