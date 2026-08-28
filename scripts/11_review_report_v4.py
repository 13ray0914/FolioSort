#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    connect_db,
    export_manifest,
    flatten_sentences,
    get_paths,
    load_config,
    now_iso,
    parse_ids,
    read_json,
    select_papers,
)
from lib.v4_common import visual_evidence_map


def esc(value: Any) -> str:
    return str(value or "").replace("|", "\\|")


def evidence_text(ids: list[str], evidence_map: dict[str, dict[str, Any]], max_chars: int = 900) -> str:
    text = " ".join(f"[{eid}] {evidence_map[eid].get('text', '')}" for eid in ids if eid in evidence_map)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def generate_report(paper_id: str, row, paths: dict[str, Path], root: Path) -> dict[str, Any] | None:
    paper_path = paths["paper_json"] / f"{paper_id}.json"
    inventory_path = paths["extracted"] / f"{paper_id}.inventory.json"
    evidence_path = paths["extracted"] / f"{paper_id}.evidence.json"
    validation_path = paths["extracted"] / f"{paper_id}.validation.json"
    memory_path = paths["summary_memory"] / f"{paper_id}.memory.json"
    metadata_path = paths["metadata"] / f"{paper_id}.metadata.json"
    visual_path = paths["visual_analysis"] / f"{paper_id}.visual.json"
    references_path = paths["reference_matches"] / f"{paper_id}.references.json"
    required = [paper_path, inventory_path, evidence_path, validation_path, memory_path]
    if not all(path.exists() for path in required):
        print(f"WAIT    {paper_id}: v4 report inputs missing")
        return None

    paper = read_json(paper_path)
    inventory = read_json(inventory_path)
    evidence = read_json(evidence_path)
    validation = read_json(validation_path)
    memory = read_json(memory_path)
    metadata = read_json(metadata_path) if metadata_path.exists() else {}
    visual = read_json(visual_path) if visual_path.exists() else {}
    references = read_json(references_path) if references_path.exists() else {}
    canonical = metadata.get("canonical") or paper.get("metadata") or {}
    sentences = flatten_sentences(paper)
    evidence_map = {
        eid: {"text": sentence.get("text", ""), "kind": "text", "page": sentence.get("page")}
        for eid, sentence in sentences.items()
    }
    evidence_map.update(visual_evidence_map(visual))

    lines = [
        f"# Review report: {paper_id}",
        "",
        f"- **Original filename:** {row['original_filename']}",
        f"- **Canonical title:** {canonical.get('title') or '(unresolved)'}",
        f"- **DOI:** {canonical.get('doi') or '(unresolved)'}",
        f"- **Journal / year:** {canonical.get('journal') or '?'} / {canonical.get('year') or '?'}",
        f"- **OpenAlex ID:** {canonical.get('openalex_id') or '(unresolved)'}",
        f"- **Metadata status:** {metadata.get('match_status') or 'not run'}",
        f"- **Article type:** {inventory.get('article_type')}",
        f"- **Automatic validation:** {validation.get('overall_status')}",
        "",
        "## Whole-paper memory",
        "",
        f"- **Central question:** {memory.get('central_question') or ''}",
        f"- **Study design:** {memory.get('study_design') or ''}",
        f"- **Mechanistic model:** {memory.get('mechanistic_model') or ''}",
        "",
        "### Major findings",
    ]
    for item in memory.get("major_findings", []):
        lines.append(f"- {item.get('statement')}  ")
        lines.append(f"  Evidence: {evidence_text(item.get('evidence_sids', []), evidence_map)}")
    if not memory.get("major_findings"):
        lines.append("- None extracted")

    lines += ["", "## Paper inventory", "", "### Objectives"]
    for item in inventory.get("objectives", []):
        lines.append(f"- {item.get('text')}  ")
        lines.append(f"  Evidence: {evidence_text(item.get('evidence_sids', []), evidence_map)}")
    if not inventory.get("objectives"):
        lines.append("- None extracted")

    lines += ["", "### Systems", "", "| ID | Raw name | Normalized | Key attributes |", "|---|---|---|---|"]
    for item in inventory.get("systems", []):
        attrs = "; ".join(f"{key}={value}" for key, value in item.get("attributes", {}).items() if value not in (None, "", []))
        lines.append(
            f"| {esc(item.get('system_id'))} | {esc(item.get('system_name_raw'))} | "
            f"{esc(item.get('normalized_name'))} | {esc(attrs)} |"
        )
    if not inventory.get("systems"):
        lines.append("| - | None extracted | - | - |")

    lines += ["", "### Methods / properties", ""]
    for item in inventory.get("methods", []):
        lines.append(f"- **{item.get('method_normalized')}** — {item.get('target_property') or ''}")
    properties = [item.get("property_normalized") for item in inventory.get("studied_properties", [])]
    lines.append(f"- Properties: {', '.join(x for x in properties if x) or '(none extracted)'}")

    lines += ["", "## Measurements", ""]
    if evidence.get("measurements"):
        for item in evidence["measurements"]:
            lines.append(f"### {item.get('measurement_id')} — {item.get('property_normalized') or item.get('property_raw')}")
            lines.append(f"- Value: `{item.get('value_raw')}`")
            lines.append(f"- Systems: {', '.join(item.get('system_refs', [])) or '(none)'}")
            lines.append(f"- Conditions: {item.get('conditions_text') or '(not extracted)'}")
            lines.append(f"- Status: {item.get('status')}")
            lines.append(f"- Evidence: {evidence_text(item.get('evidence_sids', []), evidence_map)}")
            lines.append("")
    else:
        lines.append("No measurements extracted.")

    lines += ["", "## Atomic claims", ""]
    if evidence.get("claims"):
        for item in evidence["claims"]:
            lines.append(f"### {item.get('claim_id')} — {item.get('claim_type')}")
            lines.append(f"- Claim: {item.get('statement')}")
            lines.append(f"- Origin: {item.get('claim_origin')}")
            lines.append(f"- Systems: {', '.join(item.get('system_refs', [])) or '(none)'}")
            lines.append(f"- Evidence: {evidence_text(item.get('evidence_sids', []), evidence_map)}")
            lines.append("")
    else:
        lines.append("No claims extracted.")

    lines += ["", "## Visual assets", ""]
    if visual.get("assets"):
        for item in visual["assets"]:
            lines.append(
                f"- **{item.get('evidence_id')}** ({item.get('kind')}, page {item.get('page') or '?'}) — "
                f"{item.get('summary_text') or item.get('caption') or '(crop only)'}"
            )
            if item.get("crop_path"):
                lines.append(f"  Crop: `{item.get('crop_path')}`")
    else:
        lines.append("No visual assets extracted.")

    lines += ["", "## Local citation resolution", ""]
    ref_summary = references.get("summary") or {}
    lines.append(
        f"- References: {ref_summary.get('total', 0)}; local matches: {ref_summary.get('matched_local', 0)}; "
        f"external metadata resolved: {ref_summary.get('resolved_external', 0)}; unresolved: {ref_summary.get('unresolved', 0)}"
    )
    for item in references.get("references", []):
        if item.get("target_paper_id"):
            resolved = item.get("resolved") or {}
            lines.append(
                f"- `{item.get('ref_id')}` → **{item.get('target_paper_id')}** "
                f"({item.get('match_method')}; {resolved.get('title') or ''})"
            )

    lines += ["", "## Automatic validation issues", ""]
    if not validation.get("errors") and not validation.get("warnings"):
        lines.append("No automatic errors or warnings.")
    for item in validation.get("errors", []):
        lines.append(f"- **ERROR** `{item.get('type')}` — {item}")
    for item in validation.get("warnings", []):
        lines.append(f"- **WARNING** `{item.get('type')}` — {item}")

    checklist_path = root / "profiles" / inventory.get("profile", "peg") / "review_checklist.txt"
    checklist = []
    if checklist_path.exists():
        checklist = [line.strip() for line in checklist_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not checklist:
        checklist = [
            "Canonical metadata match is correct",
            "Important systems/materials are correctly identified",
            "Cross-chunk definitions and conditions are preserved",
            "Numerical values and units match the PDF or visual",
            "Claims are supported by the cited original evidence IDs",
            "Figure/table/equation interpretation is conservative",
            "Local reference matches are correct",
            "Important conclusions were not missed",
        ]
    lines += ["", "## Human review checklist", ""]
    lines.extend(f"- [ ] {item}" for item in checklist)
    lines += ["", f"When satisfied, run: `python scripts/11_review_report_v4.py --approve {paper_id}`", ""]

    out_path = paths["review_reports"] / f"{paper_id}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "paper_id": paper_id,
        "original_filename": row["original_filename"],
        "title": canonical.get("title") or "",
        "doi": canonical.get("doi") or "",
        "year": canonical.get("year") or "",
        "journal": canonical.get("journal") or "",
        "article_type": inventory.get("article_type") or "",
        "validation_status": validation.get("overall_status") or "",
        "errors": len(validation.get("errors", [])),
        "warnings": len(validation.get("warnings", [])),
        "systems": len(inventory.get("systems", [])),
        "measurements": len(evidence.get("measurements", [])),
        "claims": len(evidence.get("claims", [])),
        "visual_assets": len(visual.get("assets", [])),
        "local_citations": ref_summary.get("matched_local", 0),
        "report_path": str(out_path),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate v4 human review reports or record approval/rejection.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--approve", nargs="+")
    ap.add_argument("--reject", nargs="+")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    conn = connect_db(paths["database"])
    if args.approve or args.reject:
        decision = "approved" if args.approve else "rejected"
        ids = args.approve or args.reject
        for paper_id in [value.upper() for value in ids]:
            conn.execute(
                """
                INSERT INTO human_reviews(paper_id, decision, notes, reviewed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(paper_id) DO UPDATE SET
                    decision=excluded.decision, notes=excluded.notes, reviewed_at=excluded.reviewed_at
                """,
                (paper_id, decision, args.note, now_iso()),
            )
            print(f"{decision.upper():8s} {paper_id}")
        conn.commit()
        export_manifest(conn, paths["manifest"])
        return

    rows = select_papers(conn, parse_ids(args.ids), args.limit)
    summary = []
    for row in rows:
        item = generate_report(row["paper_id"], row, paths, root)
        if item:
            summary.append(item)
            print(f"REPORT4 {row['paper_id']}")

    queue_path = paths["review_queue"]
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "paper_id", "original_filename", "title", "doi", "year", "journal", "article_type",
        "validation_status", "errors", "warnings", "systems", "measurements", "claims",
        "visual_assets", "local_citations", "report_path",
    ]
    with queue_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary)
    export_manifest(conn, paths["manifest"])
    print(f"Review queue: {queue_path}")


if __name__ == "__main__":
    main()
