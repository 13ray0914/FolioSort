#!/usr/bin/env bash
set -u
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
echo "Docker:"; docker info >/dev/null 2>&1 && echo "  running" || echo "  not ready"
echo "GROBID:"; if [[ "$(curl -fsS http://127.0.0.1:8070/api/isalive 2>/dev/null || true)" == "true" ]]; then echo "  ready"; else echo "  not ready"; fi
echo "Text Qwen (8080):"; curl -fsS http://127.0.0.1:8080/v1/models >/dev/null 2>&1 && echo "  ready" || echo "  not ready"
echo "Optional vision server (8081):"; curl -fsS http://127.0.0.1:8081/v1/models >/dev/null 2>&1 && echo "  ready" || echo "  not running"
echo "Curation UI (8765):"; curl -fsS http://127.0.0.1:8765/health >/dev/null 2>&1 && echo "  ready" || echo "  not running"
echo "Review App (8766):"; curl -fsS http://127.0.0.1:8766/health >/dev/null 2>&1 && echo "  ready" || echo "  not running"
echo "Pipeline lock:"; if flock -n "$ROOT/logs/auto_pipeline.lock" -c true 2>/dev/null; then echo "  idle"; else echo "  running"; fi
if [[ -f "$ROOT/config.json" ]]; then
  python3 - "$ROOT/config.json" "$ROOT" <<'PY'
import json,os,sys
from pathlib import Path
try:
    c=json.load(open(sys.argv[1],encoding='utf-8')); root=Path(sys.argv[2]); llm=c.get('llm',{})
    print('V4 configuration:')
    print('  chunk_max_chars =',llm.get('chunk_max_chars'))
    print('  max_tokens      =',llm.get('max_tokens'))
    print('  adaptive        =',llm.get('adaptive_chunking',{}))
    print('  summary memory  =',bool(c.get('summary_memory')))
    print('  Crossref        =',c.get('metadata_enrichment',{}).get('crossref',{}).get('enabled'))
    print('  OpenAlex        =',c.get('metadata_enrichment',{}).get('openalex',{}).get('enabled'))
    print('  Crossref mailto =',bool(os.environ.get('CROSSREF_MAILTO') or c.get('metadata_enrichment',{}).get('crossref',{}).get('mailto')))
    print('  OpenAlex key    =',bool(os.environ.get('OPENALEX_API_KEY') or c.get('metadata_enrichment',{}).get('openalex',{}).get('api_key')))
    print('  vision LLM      =',c.get('visual',{}).get('vision_llm',{}).get('enabled'))
    print('  MinerU          =',c.get('visual',{}).get('mineru',{}).get('enabled'))
    print('  curation        =',c.get('curation',{}).get('enabled',False))
    print('  curated graphs  =',c.get('curation',{}).get('use_curated_for_graphs',False))
    print('Environments:')
    print('  main     =', (root/'.venv/bin/python').exists())
    print('  network  =', (root/'.venv_network/bin/python').exists())
    print('  SPECTER2 =', (root/'.venv_specter2/bin/python').exists())
    print('  MinerU   =', (root/'.venv_mineru/bin/mineru').exists())
    print('Outputs:')
    for name,pattern in [('metadata','data/metadata/*.metadata.json'),('memory','data/summary_memory/*.memory.json'),('visual','data/visual_analysis/*.visual.json'),('evidence','data/extracted/*.evidence.json'),('curated','data/curated/*.evidence.json'),('references','data/reference_matches/*.references.json')]:
        print(f'  {name:10s} =',len(list(root.glob(pattern))))
except Exception as e: print('  config error:',e)
PY
fi
