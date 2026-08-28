#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

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


def esc(text) -> str:
    return str(text or "").replace("|", "\\|")


def evidence_text(sids, sentences, max_chars=700) -> str:
    text = " ".join(f"[{sid}] {sentences[sid]['text']}" for sid in sids if sid in sentences)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def generate_report(paper_id: str, row, paths) -> dict | None:
    paper_path = paths["paper_json"] / f"{paper_id}.json"
    inv_path = paths["extracted"] / f"{paper_id}.inventory.json"
    ev_path = paths["extracted"] / f"{paper_id}.evidence.json"
    val_path = paths["extracted"] / f"{paper_id}.validation.json"
    if not all(p.exists() for p in [paper_path, inv_path, ev_path, val_path]):
        print(f"WAIT    {paper_id}: report inputs missing")
        return None

    paper = read_json(paper_path)
    inv = read_json(inv_path)
    ev = read_json(ev_path)
    val = read_json(val_path)
    sentences = flatten_sentences(paper)
    meta = paper.get("metadata", {})

    lines = [
        f"# Review report: {paper_id}",
        "",
        f"- **Original filename:** {row['original_filename']}",
        f"- **Title:** {meta.get('title') or '(not extracted)'}",
        f"- **DOI:** {meta.get('doi') or '(not extracted)'}",
        f"- **Journal / year:** {meta.get('journal') or '?'} / {meta.get('year') or '?'}",
        f"- **Article type:** {inv.get('article_type')}",
        f"- **Automatic validation:** {val.get('overall_status')}",
        "",
        "## Paper inventory",
        "",
        "### Objectives",
    ]
    for x in inv.get("objectives", []):
        lines.append(f"- {x.get('text')}  ")
        lines.append(f"  Evidence: {evidence_text(x.get('evidence_sids', []), sentences)}")
    if not inv.get("objectives"):
        lines.append("- None extracted")

    lines += ["", "### Systems", "", "| ID | Raw name | Normalized | Key attributes |", "|---|---|---|---|"]
    for x in inv.get("systems", []):
        attrs = "; ".join(f"{k}={v}" for k, v in x.get("attributes", {}).items() if v not in (None, "", []))
        lines.append(f"| {esc(x.get('system_id'))} | {esc(x.get('system_name_raw'))} | {esc(x.get('normalized_name'))} | {esc(attrs)} |")
    if not inv.get("systems"):
        lines.append("| - | None extracted | - | - |")

    lines += ["", "### Methods / properties", ""]
    for x in inv.get("methods", []):
        lines.append(f"- **{x.get('method_normalized')}** — {x.get('target_property') or ''}")
    props = [x.get("property_normalized") for x in inv.get("studied_properties", [])]
    lines.append(f"- Properties: {', '.join(p for p in props if p) or '(none extracted)'}")

    lines += ["", "## Measurements", ""]
    if ev.get("measurements"):
        for x in ev["measurements"]:
            lines.append(
                f"### {x.get('measurement_id')} — {x.get('property_normalized') or x.get('property_raw')}"
            )
            lines.append(f"- Value: `{x.get('value_raw')}`")
            lines.append(f"- Systems: {', '.join(x.get('system_refs', [])) or '(none)'}")
            lines.append(f"- Conditions: {x.get('conditions_text') or '(not extracted)'}")
            lines.append(f"- Status: {x.get('status')}")
            lines.append(f"- Evidence: {evidence_text(x.get('evidence_sids', []), sentences)}")
            lines.append("")
    else:
        lines.append("No measurements extracted.")

    lines += ["", "## Atomic claims", ""]
    if ev.get("claims"):
        for x in ev["claims"]:
            lines.append(f"### {x.get('claim_id')} — {x.get('claim_type')}")
            lines.append(f"- Claim: {x.get('statement')}")
            lines.append(f"- Origin: {x.get('claim_origin')}")
            lines.append(f"- Systems: {', '.join(x.get('system_refs', [])) or '(none)'}")
            lines.append(f"- Evidence: {evidence_text(x.get('evidence_sids', []), sentences)}")
            lines.append("")
    else:
        lines.append("No claims extracted.")

    lines += ["", "## Automatic validation issues", ""]
    if not val.get("errors") and not val.get("warnings"):
        lines.append("No automatic errors or warnings.")
    for item in val.get("errors", []):
        lines.append(f"- **ERROR** `{item.get('type')}` — {item}")
    for item in val.get("warnings", []):
        lines.append(f"- **WARNING** `{item.get('type')}` — {item}")

    profile_dir = paths.get("profile_dir")
    checklist_path = profile_dir / "review_checklist.txt" if profile_dir else None
    checklist = []
    if checklist_path and checklist_path.exists():
        checklist = [x.strip() for x in checklist_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not checklist:
        checklist = [
            "Bibliographic metadata are correct",
            "Important systems/materials are correctly identified",
            "Important experimental conditions are preserved",
            "Numerical values and units match the PDF",
            "Claims are supported by the cited sentence IDs",
            "Important conclusions were not missed",
        ]
    lines += ["", "## Human review checklist", ""]
    lines.extend(f"- [ ] {item}" for item in checklist)
    lines += [
        "",
        f"When satisfied, run: `python scripts/07_review_report.py --approve {paper_id}`",
        "",
    ]

    out_path = paths["review_reports"] / f"{paper_id}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "paper_id": paper_id,
        "original_filename": row["original_filename"],
        "title": meta.get("title") or "",
        "article_type": inv.get("article_type") or "",
        "validation_status": val.get("overall_status") or "",
        "errors": len(val.get("errors", [])),
        "warnings": len(val.get("warnings", [])),
        "systems": len(inv.get("systems", [])),
        "measurements": len(ev.get("measurements", [])),
        "claims": len(ev.get("claims", [])),
        "report_path": str(out_path),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate human-readable review reports or record human approval/rejection.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--approve", nargs="+", help="Mark one or more paper IDs approved")
    ap.add_argument("--reject", nargs="+", help="Mark one or more paper IDs rejected")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    paths["profile_dir"] = root / "profiles" / config["profile"]
    conn = connect_db(paths["database"])

    if args.approve or args.reject:
        decision = "approved" if args.approve else "rejected"
        ids = args.approve or args.reject
        for paper_id in [x.upper() for x in ids]:
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
        item = generate_report(row["paper_id"], row, paths)
        if item:
            summary.append(item)
            print(f"REPORT  {row['paper_id']}")

    queue_path = paths["review_queue"]
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "paper_id", "original_filename", "title", "article_type", "validation_status",
        "errors", "warnings", "systems", "measurements", "claims", "report_path"
    ]
    with queue_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary)
    export_manifest(conn, paths["manifest"])
    print(f"Review queue: {queue_path}")


if __name__ == "__main__":
    main()
