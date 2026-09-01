#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    connect_db,
    get_stage,
    get_paths,
    load_config,
    parse_ids,
    retry_request,
    select_papers,
    set_stage,
    sha256_file,
    sha256_text,
    stable_json_hash,
    stage_is_current,
)

STAGE = "grobid_parse"
PARSER_VERSION = "grobid-v4.3-ocr-derivative-aware"


def preferred_pdf(conn, row, root: Path, raw_pdf: Path) -> tuple[Path, bool]:
    """Use a verified OCR derivative without ever replacing the canonical PDF."""
    stage = get_stage(conn, str(row["paper_id"]), "ocr")
    if not stage or stage["status"] != "success" or not stage["output_path"]:
        return raw_pdf, False
    try:
        meta = json.loads(stage["meta_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return raw_pdf, False
    candidate = Path(str(stage["output_path"])).resolve()
    expected_dir = (root / "data" / "ocr_pdfs").resolve()
    if (
        candidate.parent != expected_dir
        or not candidate.is_file()
        or str(meta.get("source_sha256") or "") != str(row["source_sha256"])
    ):
        return raw_pdf, False
    return candidate, True


def image_only_pdf_diagnostic(pdf_path: Path, *, sample_pages: int = 5) -> str | None:
    """Detect scans that GROBID cannot parse and return an actionable message."""
    try:
        try:
            import pymupdf
        except ImportError:  # PyMuPDF < 1.24 compatibility
            import fitz as pymupdf  # type: ignore
    except Exception:
        return None
    try:
        document = pymupdf.open(pdf_path)
        checked = min(max(1, int(sample_pages)), document.page_count)
        if checked <= 0:
            return "PDF_INVALID: the document has no pages."
        text_chars = 0
        image_pages = 0
        for page_index in range(checked):
            page = document[page_index]
            text_chars += len((page.get_text() or "").strip())
            image_pages += int(bool(page.get_images(full=True)))
        if text_chars == 0 and image_pages == checked:
            encryption = str((document.metadata or {}).get("encryption") or "")
            suffix = f" PDF metadata reports {encryption}." if encryption else ""
            return (
                f"OCR_REQUIRED: sampled {checked} page(s); every page is an image and the PDF has no text layer."
                f" Run OCR (for example OCRmyPDF with Japanese+English as appropriate) and retry GROBID.{suffix}"
            )
    except Exception:
        # A diagnostic failure must not replace GROBID's own parser result.
        return None
    finally:
        try:
            document.close()
        except Exception:
            pass
    return None


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
        "parser_version": PARSER_VERSION,
        "generateIDs": "1",
        "segmentSentences": "1",
        "includeRawCitations": "1",
        "consolidateHeader": str(grobid.get("consolidate_header", 0)),
        "consolidateCitations": str(grobid.get("consolidate_citations", 0)),
        "teiCoordinates": ["s", "p", "head", "figure", "formula", "ref", "biblStruct"],
    }
    parser_signature = stable_json_hash(params_for_hash)

    for row in rows:
        paper_id = row["paper_id"]
        raw_pdf_path = paths["raw_pdfs"] / row["source_relpath"]
        pdf_path, using_ocr = preferred_pdf(conn, row, root, raw_pdf_path)
        out_path = paths["tei"] / f"{paper_id}.tei.xml"
        input_hash = sha256_text(sha256_file(pdf_path) + parser_signature)

        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} already current")
            continue

        source_label = "verified OCR derivative" if using_ocr else row["original_filename"]
        print(f"GROBID  {paper_id}  {source_label}")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        try:
            if not using_ocr and bool(grobid.get("detect_image_only_pdfs", True)):
                diagnostic = image_only_pdf_diagnostic(
                    pdf_path,
                    sample_pages=int(grobid.get("image_only_sample_pages", 5)),
                )
                if diagnostic:
                    raise RuntimeError(diagnostic)
            data = [
                ("generateIDs", "1"),
                ("segmentSentences", "1"),
                ("includeRawCitations", "1"),
                ("consolidateHeader", str(grobid.get("consolidate_header", 0))),
                ("consolidateCitations", str(grobid.get("consolidate_citations", 0))),
                ("teiCoordinates", "s"),
                ("teiCoordinates", "p"),
                ("teiCoordinates", "head"),
                ("teiCoordinates", "figure"),
                ("teiCoordinates", "formula"),
                ("teiCoordinates", "ref"),
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
