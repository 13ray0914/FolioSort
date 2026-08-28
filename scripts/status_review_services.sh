#!/usr/bin/env bash
set -u
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
echo "Docker:"; docker info >/dev/null 2>&1 && echo "  running" || echo "  not ready"
echo "GROBID:"; if [[ "$(curl -fsS http://127.0.0.1:8070/api/isalive 2>/dev/null || true)" == "true" ]]; then echo "  ready"; else echo "  not ready"; fi
echo "Qwen:"; curl -fsS http://127.0.0.1:8080/v1/models >/dev/null 2>&1 && echo "  ready" || echo "  not ready"
echo "Pipeline lock:"; if flock -n "$ROOT/logs/auto_pipeline.lock" -c true 2>/dev/null; then echo "  idle"; else echo "  running"; fi
if [[ -f "$ROOT/config.json" ]]; then
  echo "Adaptive chunking:"
  python3 - "$ROOT/config.json" <<'PY'
import json,sys
try:
    c=json.load(open(sys.argv[1],encoding='utf-8')); llm=c.get('llm',{})
    print('  chunk_max_chars =',llm.get('chunk_max_chars'))
    print('  max_tokens      =',llm.get('max_tokens'))
    print('  adaptive        =',llm.get('adaptive_chunking',{}))
except Exception as e: print('  config error:',e)
PY
fi
