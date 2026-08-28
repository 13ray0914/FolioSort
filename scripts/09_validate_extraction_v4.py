#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    connect_db,
    flatten_sentences,
    get_paths,
    load_config,
    normalize_key,
    parse_ids,
    read_json,
    section_role,
    select_papers,
    set_stage,
    sha256_file,
    sha256_text,
    stage_is_current,
    write_json,
)
from lib.v4_common import visual_evidence_map

STAGE = "validate_extraction_v4"
SCRIPT_VERSION = "validator-v4.0-visual-memory"


def numeric_tokens(text: str | None) -> list[str]:
    if not text:
        return []
    normalized = text.replace(",", "")
    return re.findall(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", normalized)


def sid_locations(paper: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for paragraph in paper.get("abstract", []):
        for sentence in paragraph.get("sentences", []):
            out[sentence["sid"]] = {"heading": "Abstract", "role": "abstract"}
    for section in paper.get("sections", []):
        role = section_role(section.get("heading", ""))
        for paragraph in section.get("paragraphs", []):
            for sentence in paragraph.get("sentences", []):
                out[sentence["sid"]] = {"heading": section.get("heading"), "role": role}
    for sentence in paper.get("auxiliary_text", []):
        out[sentence["sid"]] = {"heading": sentence.get("heading"), "role": "figure_table"}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate v4 text, memory, and visual evidence extraction deterministically.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    visual_dir = paths.get("visual_analysis", root / "data/visual_analysis")
    memory_dir = paths.get("summary_memory", root / "data/summary_memory")
    conn = connect_db(paths["database"])
    rows = select_papers(conn, parse_ids(args.ids), args.limit)

    for row in rows:
        paper_id = row["paper_id"]
        paper_path = paths["paper_json"] / f"{paper_id}.json"
        inventory_path = paths["extracted"] / f"{paper_id}.inventory.json"
        evidence_path = paths["extracted"] / f"{paper_id}.evidence.json"
        visual_path = visual_dir / f"{paper_id}.visual.json"
        memory_path = memory_dir / f"{paper_id}.memory.json"
        out_path = paths["extracted"] / f"{paper_id}.validation.json"
        required = [paper_path, inventory_path, evidence_path, memory_path]
        if not all(path.exists() for path in required):
            print(f"WAIT    {paper_id}: required v4 inputs missing")
            continue

        parts = [sha256_file(path) for path in required]
        if visual_path.exists():
            parts.append(sha256_file(visual_path))
        input_hash = sha256_text("".join(parts) + SCRIPT_VERSION)
        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} validation v4 current")
            continue

        print(f"CHECK4  {paper_id}")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        try:
            paper = read_json(paper_path)
            inventory = read_json(inventory_path)
            evidence = read_json(evidence_path)
            visual = read_json(visual_path) if visual_path.exists() else {}
            sentences = flatten_sentences(paper)
            locations = sid_locations(paper)
            visual_map = visual_evidence_map(visual)
            evidence_map: dict[str, dict[str, Any]] = {
                evidence_id: {"text": sentence.get("text", ""), "kind": "text", "page": sentence.get("page")}
                for evidence_id, sentence in sentences.items()
            }
            evidence_map.update(visual_map)
            valid_ids = set(evidence_map)
            system_ids = {x.get("system_id") for x in inventory.get("systems", []) if x.get("system_id")}
            ref_ids = {x.get("ref_id") for x in paper.get("references", []) if x.get("ref_id")}

            errors: list[dict[str, Any]] = []
            warnings: list[dict[str, Any]] = []
            checks = {
                "inventory_evidence_id_checks": 0,
                "evidence_id_checks": 0,
                "visual_evidence_checks": 0,
                "system_ref_checks": 0,
                "citation_ref_checks": 0,
                "measurement_number_checks": 0,
            }

            for category in ["objectives", "systems", "methods", "studied_properties", "global_conditions"]:
                for item in inventory.get(category, []):
                    for evidence_id in item.get("evidence_sids", []):
                        checks["inventory_evidence_id_checks"] += 1
                        if str(evidence_id).startswith("vis:"):
                            checks["visual_evidence_checks"] += 1
                        if evidence_id not in valid_ids:
                            errors.append({"type": "missing_evidence_id", "category": category, "evidence_id": evidence_id})

            for measurement in evidence.get("measurements", []):
                item_id = measurement.get("measurement_id")
                ids = measurement.get("evidence_sids", [])
                evidence_text = " ".join(evidence_map[x].get("text", "") for x in ids if x in evidence_map)
                for evidence_id in ids:
                    checks["evidence_id_checks"] += 1
                    if str(evidence_id).startswith("vis:"):
                        checks["visual_evidence_checks"] += 1
                    if evidence_id not in valid_ids:
                        errors.append({"type": "missing_evidence_id", "item_id": item_id, "evidence_id": evidence_id})
                for system_id in measurement.get("system_refs", []):
                    checks["system_ref_checks"] += 1
                    if system_id not in system_ids:
                        errors.append({"type": "unknown_system_ref", "item_id": item_id, "system_ref": system_id})
                numbers = numeric_tokens(measurement.get("value_raw"))
                if numbers:
                    checks["measurement_number_checks"] += 1
                    evidence_numbers = set(numeric_tokens(evidence_text))
                    missing = [number for number in numbers if number not in evidence_numbers]
                    if missing and measurement.get("status") == "explicitly_reported":
                        warnings.append(
                            {
                                "type": "measurement_number_not_found_verbatim",
                                "item_id": item_id,
                                "value_raw": measurement.get("value_raw"),
                                "missing_numeric_tokens": missing,
                            }
                        )
                if any(str(x).startswith("vis:") for x in ids) and measurement.get("status") == "explicitly_reported":
                    warnings.append(
                        {
                            "type": "visual_measurement_marked_explicit",
                            "item_id": item_id,
                            "recommended_status": "estimated_from_figure unless the value is explicitly printed in the visual/table",
                        }
                    )

            for category in ["claims", "limitations"]:
                for item in evidence.get(category, []):
                    item_id = item.get("claim_id") or item.get("limitation_id")
                    roles: list[str] = []
                    for evidence_id in item.get("evidence_sids", []):
                        checks["evidence_id_checks"] += 1
                        if str(evidence_id).startswith("vis:"):
                            checks["visual_evidence_checks"] += 1
                        if evidence_id not in valid_ids:
                            errors.append({"type": "missing_evidence_id", "item_id": item_id, "evidence_id": evidence_id})
                        elif evidence_id in locations:
                            roles.append(locations[evidence_id]["role"])
                    for system_id in item.get("system_refs", []):
                        checks["system_ref_checks"] += 1
                        if system_id not in system_ids:
                            errors.append({"type": "unknown_system_ref", "item_id": item_id, "system_ref": system_id})
                    if (
                        category == "claims"
                        and inventory.get("article_type") == "primary_research"
                        and item.get("claim_origin") == "this_paper_result"
                        and roles
                        and all(role == "background" for role in roles)
                    ):
                        errors.append(
                            {"type": "primary_result_supported_only_by_background", "item_id": item_id, "roles": roles}
                        )

            for context in evidence.get("citation_contexts", []):
                item_id = context.get("citation_context_id")
                evidence_id = context.get("evidence_sid")
                checks["evidence_id_checks"] += 1
                if evidence_id not in sentences:
                    errors.append({"type": "citation_context_requires_text_sentence", "item_id": item_id, "evidence_id": evidence_id})
                    continue
                sentence_refs = set(sentences[evidence_id].get("citation_ref_ids", []))
                for ref_id in context.get("cited_ref_ids", []):
                    checks["citation_ref_checks"] += 1
                    if ref_id not in ref_ids:
                        warnings.append({"type": "citation_ref_not_in_reference_list", "item_id": item_id, "ref_id": ref_id})
                    if ref_id not in sentence_refs:
                        errors.append(
                            {
                                "type": "citation_ref_not_present_in_evidence_sentence",
                                "item_id": item_id,
                                "evidence_id": evidence_id,
                                "ref_id": ref_id,
                            }
                        )

            seen_claims: dict[str, str] = {}
            for claim in evidence.get("claims", []):
                key = normalize_key(claim.get("statement"))
                if key and key in seen_claims:
                    warnings.append(
                        {
                            "type": "possible_duplicate_claim",
                            "item_id": claim.get("claim_id"),
                            "duplicate_of": seen_claims[key],
                        }
                    )
                elif key:
                    seen_claims[key] = claim.get("claim_id")

            overall = "review_required" if errors or warnings else "pass"
            report = {
                "paper_id": paper_id,
                "overall_status": overall,
                "counts": {
                    "errors": len(errors),
                    "warnings": len(warnings),
                    "systems": len(inventory.get("systems", [])),
                    "measurements": len(evidence.get("measurements", [])),
                    "claims": len(evidence.get("claims", [])),
                    "citation_contexts": len(evidence.get("citation_contexts", [])),
                    "visual_assets": len(visual.get("assets", [])),
                },
                "checks": checks,
                "errors": errors,
                "warnings": warnings,
                "provenance": {"script_version": SCRIPT_VERSION},
            }
            write_json(out_path, report)
            set_stage(conn, paper_id, STAGE, "success", input_hash, out_path, meta=report["counts"])
        except Exception as error:
            set_stage(conn, paper_id, STAGE, "error", input_hash=input_hash, error=str(error))
            print(f"ERROR   {paper_id}: {error}")


if __name__ == "__main__":
    main()
