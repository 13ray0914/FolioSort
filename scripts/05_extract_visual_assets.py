#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    connect_db,
    get_paths,
    load_config,
    load_schema,
    parse_ids,
    read_json,
    select_papers,
    set_stage,
    sha256_file,
    sha256_text,
    stable_json_hash,
    stage_is_current,
    write_json,
)
from lib.v4_common import OpenAICompatibleVisionClient

STAGE = "visual_analysis_v4"
SCRIPT_VERSION = "visual-v4.0"


def parse_coords(coords: str | None) -> dict[int, list[tuple[float, float, float, float]]]:
    pages: dict[int, list[tuple[float, float, float, float]]] = {}
    if not coords:
        return pages
    for part in coords.split(";"):
        fields = part.split(",")
        if len(fields) != 5:
            continue
        try:
            page = int(fields[0])
            x, y, width, height = map(float, fields[1:])
        except Exception:
            continue
        pages.setdefault(page, []).append((x, y, x + width, y + height))
    return pages


def union_rect(boxes: list[tuple[float, float, float, float]], padding: float) -> tuple[float, float, float, float]:
    return (
        min(x[0] for x in boxes) - padding,
        min(x[1] for x in boxes) - padding,
        max(x[2] for x in boxes) + padding,
        max(x[3] for x in boxes) + padding,
    )


def sentences_on_page(paper: dict[str, Any], page_number: int | None, limit: int = 12) -> list[str]:
    if page_number is None:
        return []
    out: list[str] = []
    for paragraph in paper.get("abstract", []):
        for sentence in paragraph.get("sentences", []):
            if sentence.get("page") == page_number:
                out.append(f"[{sentence['sid']}] {sentence.get('text', '')}")
    for section in paper.get("sections", []):
        for paragraph in section.get("paragraphs", []):
            for sentence in paragraph.get("sentences", []):
                if sentence.get("page") == page_number:
                    out.append(f"[{sentence['sid']}] {sentence.get('text', '')}")
    return out[:limit]


def _mineru_text(value: Any) -> str:
    """Extract a compact textual representation from legacy or v2 MinerU blocks."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_mineru_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        preferred = []
        for key in [
            "text", "latex", "html", "markdown", "content", "table_body",
            "equation", "code_body", "chart_caption", "image_caption",
            "table_caption", "caption", "footnote", "list_items",
        ]:
            if key in value:
                text = _mineru_text(value.get(key))
                if text:
                    preferred.append(text)
        if preferred:
            return "\n".join(dict.fromkeys(preferred)).strip()
    return ""


def flatten_mineru(value: Any, out: list[dict[str, Any]], page_hint: int | None = None) -> None:
    """Flatten both legacy content_list and page-grouped content_list_v2 output."""
    if isinstance(value, dict):
        raw_page = value.get("page_idx")
        if raw_page is None:
            raw_page = value.get("page_index")
        if raw_page is None:
            raw_page = value.get("page")
        try:
            page = int(raw_page) if raw_page is not None else page_hint
        except Exception:
            page = page_hint

        type_value = str(
            value.get("type") or value.get("category") or value.get("block_type") or ""
        ).lower()
        content = value.get("content")
        content_dict = content if isinstance(content, dict) else {}
        recognized = any(
            term in type_value
            for term in ["table", "image", "figure", "equation", "formula", "chart"]
        )
        if recognized:
            text = _mineru_text(value)
            bbox = value.get("bbox") or value.get("box") or content_dict.get("bbox")
            img_path = (
                value.get("img_path")
                or value.get("image_path")
                or content_dict.get("img_path")
                or content_dict.get("image_path")
            )
            out.append(
                {
                    "type": type_value,
                    "page_idx": page,
                    "bbox": bbox,
                    "text": text or None,
                    "img_path": img_path,
                }
            )
        for child in value.values():
            flatten_mineru(child, out, page)
    elif isinstance(value, list):
        for child in value:
            flatten_mineru(child, out, page_hint)


def run_mineru(pdf_path: Path, output_root: Path, cfg: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if not cfg.get("enabled", False):
        return "disabled", []
    command = cfg.get("command") or "mineru"
    output_root.mkdir(parents=True, exist_ok=True)
    backend = str(cfg.get("backend", "pipeline"))
    args = [
        command,
        "-p",
        str(pdf_path),
        "-o",
        str(output_root),
        "-b",
        backend,
        "--client-side-output-generation",
        "true",
    ]
    if backend.startswith("hybrid"):
        args.extend(["--effort", str(cfg.get("effort", "high"))])
    if cfg.get("image_analysis", True) and backend != "pipeline":
        args.extend(["--image-analysis", "true"])

    # MinerU 3.x controls formula/table parsing through environment variables;
    # both are enabled by default, but set them explicitly for reproducibility.
    env = dict(__import__("os").environ)
    env["MINERU_FORMULA_ENABLE"] = "true"
    env["MINERU_TABLE_ENABLE"] = "true"
    timeout_seconds = int(cfg.get("timeout_seconds", 7200))
    env.setdefault("MINERU_TASK_RESULT_TIMEOUT_SECONDS", str(timeout_seconds))
    try:
        subprocess.run(args, check=True, timeout=timeout_seconds, env=env)
    except FileNotFoundError:
        return "command_not_found", []
    except Exception as error:
        return f"error:{error}", []

    candidates = sorted(output_root.rglob("*_content_list_v2.json"))
    if not candidates:
        candidates = sorted(output_root.rglob("*_content_list.json"))
    elements: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            flatten_mineru(read_json(candidate), elements)
        except Exception:
            continue

    # Content-list v2 may expose the same object through nested representations.
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str, str]] = set()
    for element in elements:
        signature = (
            str(element.get("type") or ""),
            int(element.get("page_idx") or 0),
            re.sub(r"\s+", " ", str(element.get("text") or "")).strip()[:300],
            Path(str(element.get("img_path") or "")).name,
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(element)
    return "success" if candidates else "no_content_list", deduped


def asset_entries(paper: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(paper.get("figures", []), start=1):
        entries.append(
            {
                "asset_id": f"fig{index:04d}",
                "kind": "figure",
                "source_id": item.get("id"),
                "page": item.get("page"),
                "coords": item.get("coords"),
                "caption": item.get("caption") or item.get("head") or item.get("label") or "",
                "structured_text": None,
            }
        )
    for index, item in enumerate(paper.get("tables", []), start=1):
        entries.append(
            {
                "asset_id": f"table{index:04d}",
                "kind": "table",
                "source_id": item.get("id"),
                "page": item.get("page"),
                "coords": item.get("coords"),
                "caption": item.get("caption") or item.get("head") or item.get("label") or "",
                "structured_text": item.get("table_text"),
                "rows": item.get("rows") or [],
            }
        )
    for index, item in enumerate(paper.get("formulas", []), start=1):
        entries.append(
            {
                "asset_id": f"eq{index:04d}",
                "kind": "equation",
                "source_id": item.get("id"),
                "page": item.get("page"),
                "coords": item.get("coords"),
                "caption": item.get("label") or "",
                "structured_text": item.get("raw_text"),
            }
        )
    return entries


def main() -> None:
    ap = argparse.ArgumentParser(description="Crop figures/tables/equations and optionally analyze them with MinerU and a vision model.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    visual_assets_root = paths.get("visual_assets", root / "data/visual_assets")
    visual_analysis_root = paths.get("visual_analysis", root / "data/visual_analysis")
    mineru_root = paths.get("mineru", root / "data/mineru")
    for path in [visual_assets_root, visual_analysis_root, mineru_root]:
        path.mkdir(parents=True, exist_ok=True)

    conn = connect_db(paths["database"])
    rows = select_papers(conn, parse_ids(args.ids), args.limit)
    cfg = config.get("visual", {})
    vision_cfg = cfg.get("vision_llm", {})
    schema_path = root / "schemas/v4/visual_asset.schema.json"
    prompt_path = root / "prompts/v4/visual_system.txt"
    schema = load_schema(schema_path)
    system_prompt = prompt_path.read_text(encoding="utf-8")
    signature = stable_json_hash(
        {
            "script": SCRIPT_VERSION,
            "visual": cfg,
            "schema": sha256_file(schema_path),
            "prompt": sha256_file(prompt_path),
        }
    )

    vision_client = None
    vision_model = None
    if vision_cfg.get("enabled", False):
        try:
            vision_client = OpenAICompatibleVisionClient(vision_cfg)
            vision_model = vision_client.healthcheck()
            print(f"Vision model: {vision_model}")
        except Exception as error:
            print(f"WARNING: vision server unavailable; crops will still be generated: {error}")
            vision_client = None

    try:
        import fitz  # PyMuPDF
    except Exception as error:
        raise SystemExit(f"PyMuPDF is required. Run: pip install pymupdf. Details: {error}")

    for row in rows:
        paper_id = row["paper_id"]
        paper_path = paths["paper_json"] / f"{paper_id}.json"
        pdf_path = paths["raw_pdfs"] / row["source_relpath"]
        out_path = visual_analysis_root / f"{paper_id}.visual.json"
        if not paper_path.exists() or not pdf_path.exists():
            print(f"WAIT    {paper_id}: paper JSON or PDF missing")
            continue
        input_hash = sha256_text(sha256_file(paper_path) + row["source_sha256"] + signature)
        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} visual current")
            continue

        print(f"VISUAL  {paper_id}")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        try:
            paper = read_json(paper_path)
            paper_asset_dir = visual_assets_root / paper_id
            paper_asset_dir.mkdir(parents=True, exist_ok=True)
            mineru_status, mineru_elements = run_mineru(pdf_path, mineru_root / paper_id, cfg.get("mineru", {}))
            document = fitz.open(pdf_path)
            results: list[dict[str, Any]] = []
            padding = float(cfg.get("crop_padding_points", 12))
            dpi = int(cfg.get("render_dpi", 180))
            zoom = dpi / 72.0

            for asset in asset_entries(paper):
                coords_by_page = parse_coords(asset.get("coords"))
                page_number = asset.get("page")
                if coords_by_page:
                    page_number = page_number or sorted(coords_by_page)[0]
                crop_path = None
                crop_status = "no_page"
                table_rows = list(asset.get("rows") or [])
                if page_number and 1 <= int(page_number) <= len(document):
                    page = document[int(page_number) - 1]
                    boxes = coords_by_page.get(int(page_number), [])
                    if boxes:
                        x0, y0, x1, y1 = union_rect(boxes, padding)
                        rect = fitz.Rect(max(0, x0), max(0, y0), min(page.rect.width, x1), min(page.rect.height, y1))
                    else:
                        rect = page.rect
                        crop_status = "full_page_fallback"
                    if rect.width > 2 and rect.height > 2:
                        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, alpha=False)
                        crop_path = paper_asset_dir / f"{asset['asset_id']}.png"
                        pixmap.save(crop_path)
                        if crop_status != "full_page_fallback":
                            crop_status = "cropped"
                        if asset["kind"] == "table" and not table_rows:
                            try:
                                finder = page.find_tables(clip=rect)
                                if finder.tables:
                                    table_rows = [["" if cell is None else str(cell) for cell in row] for row in finder.tables[0].extract()]
                            except Exception:
                                pass

                nearby = sentences_on_page(paper, int(page_number) if page_number else None)
                analysis = None
                analysis_status = "disabled"
                if vision_client and crop_path and crop_path.exists():
                    cache_path = paper_asset_dir / f"{asset['asset_id']}.analysis.json"
                    cache_meta = paper_asset_dir / f"{asset['asset_id']}.analysis.meta.json"
                    prompt = (
                        f"PAPER_ID: {paper_id}\nASSET_ID: {asset['asset_id']}\n"
                        f"EXPECTED_KIND: {asset['kind']}\nPAGE: {page_number}\n"
                        f"CAPTION: {asset.get('caption') or ''}\n"
                        f"STRUCTURED_TEXT: {asset.get('structured_text') or ''}\n"
                        f"NEARBY_TEXT:\n" + "\n".join(nearby)
                    )
                    request_hash = sha256_text(sha256_file(crop_path) + prompt + signature + str(vision_model))
                    if not args.force and cache_path.exists() and cache_meta.exists() and read_json(cache_meta).get("input_hash") == request_hash:
                        analysis = read_json(cache_path)
                        analysis_status = "cache"
                    else:
                        try:
                            result = vision_client.chat_image_json(system_prompt, prompt, crop_path, schema)
                            analysis = result.data
                            write_json(cache_path, analysis)
                            write_json(cache_meta, {"input_hash": request_hash, "model": result.model})
                            analysis_status = "success"
                        except Exception as error:
                            analysis_status = f"error:{error}"

                evidence_id = f"vis:{asset['asset_id']}"
                summary_text = ""
                if analysis:
                    summary_text = analysis.get("summary") or ""
                elif asset.get("structured_text"):
                    summary_text = str(asset.get("structured_text"))
                elif asset.get("caption"):
                    summary_text = str(asset.get("caption"))
                results.append(
                    {
                        **asset,
                        "evidence_id": evidence_id,
                        "crop_path": str(crop_path.relative_to(root)) if crop_path else None,
                        "crop_status": crop_status,
                        "nearby_text": nearby,
                        "table_rows": table_rows,
                        "analysis_status": analysis_status,
                        "analysis": analysis,
                        "summary_text": summary_text,
                    }
                )

            # MinerU is an optional second parser. Keep its formula/table/image
            # elements as independent visual evidence rather than silently
            # overwriting the GROBID/PyMuPDF results. This is particularly useful
            # for LaTeX equations and HTML tables that are absent from TEI.
            existing_signatures = {
                (str(item.get("kind") or ""), int(item.get("page") or 0),
                 re.sub(r"\s+", " ", str(item.get("structured_text") or item.get("caption") or "")).strip()[:240])
                for item in results
            }
            mineru_output_dir = mineru_root / paper_id
            for mineru_index, element in enumerate(mineru_elements, start=1):
                raw_type = str(element.get("type") or "visual").lower()
                if "table" in raw_type:
                    kind = "table"
                elif any(term in raw_type for term in ["equation", "formula"]):
                    kind = "equation"
                elif "chart" in raw_type:
                    kind = "graph"
                elif any(term in raw_type for term in ["image", "figure"]):
                    kind = "figure"
                else:
                    kind = "visual"
                raw_page = element.get("page_idx")
                try:
                    # MinerU page_idx is normally zero-based.
                    page_number = int(raw_page) + 1 if raw_page is not None else None
                except Exception:
                    page_number = None
                structured_text = str(element.get("text") or "").strip()
                signature_key = (kind, int(page_number or 0), re.sub(r"\s+", " ", structured_text).strip()[:240])
                if structured_text and signature_key in existing_signatures:
                    continue
                asset_id = f"mineru{mineru_index:04d}"
                crop_path = None
                source_image = element.get("img_path")
                if source_image:
                    candidate = Path(str(source_image))
                    if not candidate.is_absolute():
                        matches = list(mineru_output_dir.rglob(candidate.name))
                        candidate = matches[0] if matches else mineru_output_dir / candidate
                    if candidate.exists() and candidate.is_file():
                        suffix = candidate.suffix.lower() if candidate.suffix else ".png"
                        crop_path = paper_asset_dir / f"{asset_id}{suffix}"
                        try:
                            shutil.copy2(candidate, crop_path)
                        except Exception:
                            crop_path = None
                nearby = sentences_on_page(paper, page_number)
                results.append(
                    {
                        "asset_id": asset_id,
                        "kind": kind,
                        "source_id": None,
                        "page": page_number,
                        "coords": None,
                        "caption": "",
                        "structured_text": structured_text or None,
                        "evidence_id": f"vis:{asset_id}",
                        "crop_path": str(crop_path.relative_to(root)) if crop_path else None,
                        "crop_status": "mineru_image" if crop_path else "mineru_structured_only",
                        "nearby_text": nearby,
                        "table_rows": [],
                        "analysis_status": "mineru",
                        "analysis": None,
                        "summary_text": structured_text,
                        "mineru_type": raw_type,
                        "mineru_bbox": element.get("bbox"),
                    }
                )

            payload = {
                "paper_id": paper_id,
                "assets": results,
                "mineru": {"status": mineru_status, "elements": mineru_elements},
                "visual_evidence": [
                    {
                        "evidence_id": item["evidence_id"],
                        "asset_id": item["asset_id"],
                        "kind": item["kind"],
                        "page": item.get("page"),
                        "caption": item.get("caption"),
                        "text": item.get("summary_text") or "",
                    }
                    for item in results
                    if item.get("summary_text")
                ],
                "provenance": {
                    "script_version": SCRIPT_VERSION,
                    "vision_model": vision_model,
                    "mineru_status": mineru_status,
                },
            }
            write_json(out_path, payload)
            set_stage(
                conn,
                paper_id,
                STAGE,
                "success",
                input_hash,
                out_path,
                meta={
                    "assets": len(results),
                    "vision_analyzed": sum(1 for x in results if x.get("analysis")),
                    "mineru_status": mineru_status,
                },
            )
            document.close()
        except Exception as error:
            set_stage(conn, paper_id, STAGE, "error", input_hash=input_hash, error=str(error))
            print(f"ERROR   {paper_id}: {error}")


if __name__ == "__main__":
    main()
