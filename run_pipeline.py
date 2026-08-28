#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STEPS = [
    (1, "scripts/01_make_manifest.py"),
    (2, "scripts/02_grobid_parse.py"),
    (3, "scripts/03_tei_to_json.py"),
    (4, "scripts/04_extract_inventory.py"),
    (5, "scripts/05_extract_evidence.py"),
    (6, "scripts/06_validate_extraction.py"),
    (7, "scripts/07_review_report.py"),
]


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the resumable literature pipeline. Completed current stages are skipped.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--from-step", type=int, default=1, choices=range(1, 8))
    ap.add_argument("--to-step", type=int, default=7, choices=range(1, 8))
    ap.add_argument("--ids", help="Comma-separated IDs. Step 1 always scans all PDFs.")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.from_step > args.to_step:
        raise SystemExit("--from-step must be <= --to-step")

    for number, script in STEPS:
        if number < args.from_step or number > args.to_step:
            continue
        cmd = [sys.executable, str(ROOT / script), "--config", args.config]
        if number != 1:
            if args.ids:
                cmd += ["--ids", args.ids]
            if args.limit is not None:
                cmd += ["--limit", str(args.limit)]
            if args.force and number <= 6:
                cmd += ["--force"]
        print("\n=== STEP", number, script, "===")
        result = subprocess.run(cmd, cwd=ROOT)
        if result.returncode != 0:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
