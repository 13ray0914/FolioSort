#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
LLAMA_SERVER="${VISION_LLAMA_SERVER:-$HOME/desktop/llm/llama.cpp/build/bin/llama-server}"
HOST="${VISION_HOST:-127.0.0.1}"
PORT="${VISION_PORT:-8081}"
CONTEXT="${VISION_CONTEXT:-16384}"
NGL="${VISION_NGL:-all}"
LOG="$ROOT/logs/vision-server.log"
PID_FILE="$ROOT/logs/vision-server.pid"
mkdir -p "$ROOT/logs"

if curl -fsS "http://$HOST:$PORT/v1/models" >/dev/null 2>&1; then
  echo "Vision server is already ready at http://$HOST:$PORT/v1"
  exit 0
fi
[[ -x "$LLAMA_SERVER" ]] || { echo "ERROR: llama-server not found: $LLAMA_SERVER"; exit 2; }

args=("$LLAMA_SERVER" --host "$HOST" --port "$PORT" -c "$CONTEXT" -ngl "$NGL" --flash-attn on)
if [[ -n "${VISION_HF_REPO:-}" ]]; then
  args+=( -hf "$VISION_HF_REPO" )
elif [[ -n "${VISION_MODEL:-}" && -n "${VISION_MMPROJ:-}" ]]; then
  [[ -f "$VISION_MODEL" ]] || { echo "ERROR: VISION_MODEL not found: $VISION_MODEL"; exit 2; }
  [[ -f "$VISION_MMPROJ" ]] || { echo "ERROR: VISION_MMPROJ not found: $VISION_MMPROJ"; exit 2; }
  args+=( -m "$VISION_MODEL" --mmproj "$VISION_MMPROJ" )
else
  cat <<'EOF'
ERROR: no multimodal model was configured.

Use either:
  export VISION_HF_REPO='ggml-org/<supported-multimodal-GGUF-repo>'

or:
  export VISION_MODEL="$HOME/models/<vision-model>.gguf"
  export VISION_MMPROJ="$HOME/models/<matching-mmproj>.gguf"

Your current Qwen3.8-27B text GGUF on port 8080 is not automatically a
vision model. A compatible multimodal model/projector must be used here.
EOF
  exit 2
fi

nohup "${args[@]}" >"$LOG" 2>&1 &
echo $! > "$PID_FILE"
echo "Started vision server PID $(cat "$PID_FILE"). Log: $LOG"
for _ in $(seq 1 120); do
  if curl -fsS "http://$HOST:$PORT/v1/models" >/dev/null 2>&1; then
    echo "Vision server ready: http://$HOST:$PORT/v1"
    exit 0
  fi
  sleep 2
done
echo "ERROR: vision server did not become ready. Inspect: $LOG"
exit 1
