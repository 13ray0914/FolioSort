#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    connect_db,
    get_paths,
    load_config,
    parse_ids,
    retry_request,
    select_papers,
    set_stage,
    sha256_text,
    stable_json_hash,
    stage_is_current,
)

STAGE = "grobid_parse"


def main() -> None:
    ap = argparse.ArgumentParser(description="Send pending PDFs to GROBID processFulltextDocument.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids", help="Comma-separated paper IDs, e.g. P0001,P0004")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    conn = connect_db(paths["database"])
    rows = select_papers(conn, parse_ids(args.ids), args.limit)

    grobid = config["grobid"]
    base = grobid["base_url"].rstrip("/")
    health = retry_request("GET", f"{base}/api/isalive", timeout=30)
    if health.text.strip().lower() != "true":
        raise SystemExit(f"GROBID is not ready: {health.text[:200]}")

    params_for_hash = {
        "generateIDs": "1",
        "segmentSentences": "1",
        "includeRawCitations": "1",
        "consolidateHeader": str(grobid.get("consolidate_header", 0)),
        "consolidateCitations": str(grobid.get("consolidate_citations", 0)),
        "teiCoordinates": ["s", "figure", "biblStruct"],
    }
    parser_signature = stable_json_hash(params_for_hash)

    for row in rows:
        paper_id = row["paper_id"]
        pdf_path = paths["raw_pdfs"] / row["source_relpath"]
        out_path = paths["tei"] / f"{paper_id}.tei.xml"
        input_hash = sha256_text(row["source_sha256"] + parser_signature)

        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} already current")
            continue

        print(f"GROBID  {paper_id}  {row['original_filename']}")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        try:
            data = [
                ("generateIDs", "1"),
                ("segmentSentences", "1"),
                ("includeRawCitations", "1"),
                ("consolidateHeader", str(grobid.get("consolidate_header", 0))),
                ("consolidateCitations", str(grobid.get("consolidate_citations", 0))),
                ("teiCoordinates", "s"),
                ("teiCoordinates", "figure"),
                ("teiCoordinates", "biblStruct"),
            ]
            attempts = int(grobid.get("attempts", 4))
            wait_seconds = float(grobid.get("retry_wait_seconds", 5))
            response = None
            for attempt in range(1, attempts + 1):
                with pdf_path.open("rb") as f:
                    response = requests.post(
                        f"{base}/api/processFulltextDocument",
                        files={"input": (pdf_path.name, f, "application/pdf")},
                        data=data,
                        timeout=int(grobid.get("timeout_seconds", 900)),
                    )
                if response.status_code in {429, 503} and attempt < attempts:
                    time.sleep(wait_seconds * attempt)
                    continue
                response.raise_for_status()
                break
            if response is None or not response.content.strip():
                raise RuntimeError("GROBID returned an empty response")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(response.content)
            set_stage(conn, paper_id, STAGE, "success", input_hash, out_path)
        except Exception as e:
            set_stage(conn, paper_id, STAGE, "error", input_hash=input_hash, error=str(e))
            print(f"ERROR   {paper_id}: {e}")


if __name__ == "__main__":
    main()
