#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
BASE_URL="${VISION_BASE_URL:-http://127.0.0.1:8081/v1}"
if ! curl -fsS "$BASE_URL/models" >/dev/null 2>&1; then
  echo "ERROR: no vision server is responding at $BASE_URL"
  echo "Start it first with: $ROOT/scripts/start_vision_server.sh"
  exit 2
fi
python3 - "$ROOT/config.json" "$BASE_URL" <<'PY'
import json,sys
path,url=sys.argv[1:]
with open(path,encoding='utf-8') as f: c=json.load(f)
vision=c.setdefault('visual',{}).setdefault('vision_llm',{})
vision['enabled']=True
vision['base_url']=url.rstrip('/')
with open(path,'w',encoding='utf-8') as f: json.dump(c,f,ensure_ascii=False,indent=2)
print('Vision analysis enabled:', vision['base_url'])
PY
