#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

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

STAGE = "validate_extraction"
SCRIPT_VERSION = "validator-v1.2"


def numeric_tokens(text: str | None) -> list[str]:
    if not text:
        return []
    text = text.replace(",", "")
    return re.findall(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", text)


def sid_locations(paper: dict) -> dict[str, dict]:
    out = {}
    for p in paper.get("abstract", []):
        for s in p.get("sentences", []):
            out[s["sid"]] = {"heading": "Abstract", "role": "abstract"}
    for sec in paper.get("sections", []):
        role = section_role(sec.get("heading", ""))
        for p in sec.get("paragraphs", []):
            for s in p.get("sentences", []):
                out[s["sid"]] = {"heading": sec.get("heading"), "role": role}
    for s in paper.get("auxiliary_text", []):
        out[s["sid"]] = {"heading": s.get("heading"), "role": "figure_table"}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Deterministic validation of LLM-extracted inventory/evidence.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    conn = connect_db(paths["database"])
    rows = select_papers(conn, parse_ids(args.ids), args.limit)

    for row in rows:
        paper_id = row["paper_id"]
        paper_path = paths["paper_json"] / f"{paper_id}.json"
        inventory_path = paths["extracted"] / f"{paper_id}.inventory.json"
        evidence_path = paths["extracted"] / f"{paper_id}.evidence.json"
        out_path = paths["extracted"] / f"{paper_id}.validation.json"
        if not all(p.exists() for p in [paper_path, inventory_path, evidence_path]):
            print(f"WAIT    {paper_id}: required inputs missing")
            continue

        input_hash = sha256_text(
            sha256_file(paper_path)
            + sha256_file(inventory_path)
            + sha256_file(evidence_path)
            + SCRIPT_VERSION
        )
        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} validation current")
            continue

        print(f"CHECK   {paper_id}")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        try:
            paper = read_json(paper_path)
            inventory = read_json(inventory_path)
            evidence = read_json(evidence_path)
            sentences = flatten_sentences(paper)
            locations = sid_locations(paper)
            valid_sids = set(sentences)
            system_ids = {s.get("system_id") for s in inventory.get("systems", []) if s.get("system_id")}
            ref_ids = {r.get("ref_id") for r in paper.get("references", []) if r.get("ref_id")}

            errors = []
            warnings = []
            checks = {
                "inventory_evidence_sid_checks": 0,
                "evidence_sid_checks": 0,
                "system_ref_checks": 0,
                "citation_ref_checks": 0,
                "measurement_number_checks": 0,
            }

            # Inventory evidence IDs.
            for category in ["objectives", "systems", "methods", "studied_properties", "global_conditions"]:
                for item in inventory.get(category, []):
                    for sid in item.get("evidence_sids", []):
                        checks["inventory_evidence_sid_checks"] += 1
                        if sid not in valid_sids:
                            errors.append({"type": "missing_sentence_id", "category": category, "sid": sid})

            # Measurements.
            for m in evidence.get("measurements", []):
                mid = m.get("measurement_id")
                evidence_text = " ".join(sentences[sid]["text"] for sid in m.get("evidence_sids", []) if sid in sentences)
                for sid in m.get("evidence_sids", []):
                    checks["evidence_sid_checks"] += 1
                    if sid not in valid_sids:
                        errors.append({"type": "missing_sentence_id", "item_id": mid, "sid": sid})
                for sys_id in m.get("system_refs", []):
                    checks["system_ref_checks"] += 1
                    if sys_id not in system_ids:
                        errors.append({"type": "unknown_system_ref", "item_id": mid, "system_ref": sys_id})
                nums = numeric_tokens(m.get("value_raw"))
                if nums:
                    checks["measurement_number_checks"] += 1
                    evidence_nums = set(numeric_tokens(evidence_text))
                    missing_nums = [n for n in nums if n not in evidence_nums]
                    if missing_nums and m.get("status") == "explicitly_reported":
                        warnings.append(
                            {
                                "type": "measurement_number_not_found_verbatim",
                                "item_id": mid,
                                "value_raw": m.get("value_raw"),
                                "missing_numeric_tokens": missing_nums,
                            }
                        )

            # Claims and limitations.
            for category in ["claims", "limitations"]:
                for item in evidence.get(category, []):
                    item_id = item.get("claim_id") or item.get("limitation_id")
                    roles = []
                    for sid in item.get("evidence_sids", []):
                        checks["evidence_sid_checks"] += 1
                        if sid not in valid_sids:
                            errors.append({"type": "missing_sentence_id", "item_id": item_id, "sid": sid})
                        elif sid in locations:
                            roles.append(locations[sid]["role"])
                    for sys_id in item.get("system_refs", []):
                        checks["system_ref_checks"] += 1
                        if sys_id not in system_ids:
                            errors.append({"type": "unknown_system_ref", "item_id": item_id, "system_ref": sys_id})
                    if (
                        category == "claims"
                        and inventory.get("article_type") == "primary_research"
                        and item.get("claim_origin") == "this_paper_result"
                        and roles
                        and all(r == "background" for r in roles)
                    ):
                        errors.append(
                            {"type": "primary_result_supported_only_by_background", "item_id": item_id, "roles": roles}
                        )

            # Citation contexts.
            for c in evidence.get("citation_contexts", []):
                cid = c.get("citation_context_id")
                sid = c.get("evidence_sid")
                checks["evidence_sid_checks"] += 1
                if sid not in valid_sids:
                    errors.append({"type": "missing_sentence_id", "item_id": cid, "sid": sid})
                    continue
                sentence_refs = set(sentences[sid].get("citation_ref_ids", []))
                for rid in c.get("cited_ref_ids", []):
                    checks["citation_ref_checks"] += 1
                    if rid not in ref_ids:
                        warnings.append({"type": "citation_ref_not_in_reference_list", "item_id": cid, "ref_id": rid})
                    if rid not in sentence_refs:
                        errors.append(
                            {
                                "type": "citation_ref_not_present_in_evidence_sentence",
                                "item_id": cid,
                                "sid": sid,
                                "ref_id": rid,
                            }
                        )

            # Simple duplicate warning for claim statements.
            seen_claims = {}
            for c in evidence.get("claims", []):
                key = normalize_key(c.get("statement"))
                if key and key in seen_claims:
                    warnings.append(
                        {
                            "type": "possible_duplicate_claim",
                            "item_id": c.get("claim_id"),
                            "duplicate_of": seen_claims[key],
                        }
                    )
                elif key:
                    seen_claims[key] = c.get("claim_id")

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
                },
                "checks": checks,
                "errors": errors,
                "warnings": warnings,
            }
            write_json(out_path, report)
            set_stage(conn, paper_id, STAGE, "success", input_hash, out_path, meta=report["counts"])
        except Exception as e:
            set_stage(conn, paper_id, STAGE, "error", input_hash=input_hash, error=str(e))
            print(f"ERROR   {paper_id}: {e}")


if __name__ == "__main__":
    main()
