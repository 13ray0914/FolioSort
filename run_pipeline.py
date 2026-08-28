#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STEPS = [
    (1, "scripts/01_make_manifest.py", "Scan PDFs and preserve stable paper IDs"),
    (2, "scripts/02_grobid_parse.py", "GROBID full-text/coordinates parsing"),
    (3, "scripts/03_tei_to_json.py", "TEI to stable sentence/visual JSON"),
    (4, "scripts/04_enrich_metadata.py", "Crossref/OpenAlex metadata enrichment"),
    (5, "scripts/05_extract_visual_assets.py", "Figure/table/equation extraction"),
    (6, "scripts/06_build_summary_memory.py", "Whole-paper summary memory"),
    (7, "scripts/07_extract_inventory_v4.py", "Memory-aware inventory extraction"),
    (8, "scripts/08_extract_evidence_v4.py", "Memory-aware evidence extraction"),
    (9, "scripts/09_validate_extraction_v4.py", "Text and visual evidence validation"),
    (10, "scripts/10_resolve_references.py", "Reference-to-local-paper resolution"),
    (11, "scripts/11_review_report_v4.py", "Human review report"),
]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Run the resumable v4 literature pipeline. Current hash-matched stages are skipped."
    )
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--from-step", type=int, default=1, choices=range(1, 12))
    ap.add_argument("--to-step", type=int, default=11, choices=range(1, 12))
    ap.add_argument("--ids", help="Comma-separated IDs. Step 1 always scans all PDFs.")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--local-references-only", action="store_true")
    args = ap.parse_args()
    if args.from_step > args.to_step:
        raise SystemExit("--from-step must be <= --to-step")

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    for number, script, label in STEPS:
        if number < args.from_step or number > args.to_step:
            continue
        cmd = [sys.executable, "-u", str(ROOT / script), "--config", args.config]
        if number != 1:
            if args.ids:
                cmd += ["--ids", args.ids]
            if args.limit is not None:
                cmd += ["--limit", str(args.limit)]
            if args.force and number <= 10:
                cmd += ["--force"]
        if number == 10 and args.local_references_only:
            cmd += ["--local-only"]
        print(f"\n=== STEP {number}/11: {label} ({script}) ===", flush=True)
        result = subprocess.run(cmd, cwd=ROOT, env=env)
        if result.returncode != 0:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
