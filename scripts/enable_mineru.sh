#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
COMMAND="${MINERU_COMMAND:-$ROOT/.venv_mineru/bin/mineru}"
[[ -x "$COMMAND" ]] || { echo "ERROR: MinerU not found: $COMMAND"; echo "Run: $ROOT/scripts/install_mineru_env.sh"; exit 2; }
python3 - "$ROOT/config.json" "$COMMAND" <<'PY'
import json,sys
path,command=sys.argv[1:]
with open(path,encoding='utf-8') as f: c=json.load(f)
c.setdefault('visual',{}).setdefault('mineru',{})['enabled']=True
c['visual']['mineru']['command']=command
with open(path,'w',encoding='utf-8') as f: json.dump(c,f,ensure_ascii=False,indent=2)
print('MinerU enabled:', command)
PY
