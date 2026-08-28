#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
python3 - "$ROOT/config.json" <<'PY'
import json,sys
path=sys.argv[1]
with open(path,encoding='utf-8') as f: c=json.load(f)
c.setdefault('visual',{}).setdefault('mineru',{})['enabled']=False
c.setdefault('visual',{}).setdefault('vision_llm',{})['enabled']=False
with open(path,'w',encoding='utf-8') as f: json.dump(c,f,ensure_ascii=False,indent=2)
print('MinerU and vision LLM disabled.')
PY
