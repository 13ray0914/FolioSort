#!/usr/bin/env bash
set -euo pipefail
BASHRC="$HOME/.bashrc"
python3 - "$BASHRC" <<'PY'
import sys
from pathlib import Path
p=Path(sys.argv[1]); text=p.read_text(encoding='utf-8')
a='# >>> review-literature-pipeline >>>'; b='# <<< review-literature-pipeline <<<'
if a in text and b in text:
    before, rest=text.split(a,1); _, after=rest.split(b,1)
    p.write_text(before.rstrip()+"\n"+after.lstrip(), encoding='utf-8')
    print('Removed review pipeline autostart from', p)
else:
    print('Autostart block not found.')
PY
