#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import itertools
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.curation import surface_key
from lib.pipeline_common import get_paths, load_config, read_json

try:
    from rapidfuzz.fuzz import token_set_ratio
except Exception:
    token_set_ratio = None


def extract_terms(path: Path, term_type: str) -> list[tuple[str, str]]:
    payload = read_json(path)
    rows = payload.get("studied_properties", []) if term_type == "property" else payload.get("methods", [])
    out = []
    for item in rows:
        raw = item.get("property_raw") if term_type == "property" else item.get("method_raw")
        norm = item.get("property_normalized") if term_type == "property" else item.get("method_normalized")
        if raw or norm:
            out.append((str(raw or norm), str(norm or raw)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit raw-vs-curated terminology and suggest unresolved near-duplicates. Suggestions are never auto-merged.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--suggest-threshold", type=float, default=72.0)
    args = ap.parse_args()
    config, root = load_config(args.config)
    paths = get_paths(config, root)
    curated_dir = paths.get("curated", root / "data/curated")
    out_dir = paths.get("curation_outputs", root / "outputs/curation")
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_rows: list[dict[str, Any]] = []
    all_canonical: dict[str, Counter] = {"property": Counter(), "method": Counter()}
    raw_forms: dict[str, defaultdict[str, set[str]]] = {
        "property": defaultdict(set), "method": defaultdict(set)
    }
    for raw_path in sorted(paths["extracted"].glob("P*.inventory.json")):
        paper_id = raw_path.name.split(".")[0]
        curated_path = curated_dir / raw_path.name
        if not curated_path.exists():
            continue
        for term_type in ["property", "method"]:
            raw_terms = extract_terms(raw_path, term_type)
            curated_terms = extract_terms(curated_path, term_type)
            raw_unique = {surface_key(x[1]) for x in raw_terms if x[1]}
            curated_unique = {surface_key(x[1]) for x in curated_terms if x[1]}
            summary_rows.append({
                "paper_id": paper_id,
                "term_type": term_type,
                "raw_unique": len(raw_unique),
                "curated_unique": len(curated_unique),
                "collapsed_count": max(0, len(raw_unique) - len(curated_unique)),
            })
            for raw, canonical in curated_terms:
                key = surface_key(canonical)
                if key:
                    all_canonical[term_type][canonical] += 1
                    raw_forms[term_type][canonical].add(raw)

    with (out_dir / "normalization_summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["paper_id", "term_type", "raw_unique", "curated_unique", "collapsed_count"]
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(summary_rows)

    with (out_dir / "canonical_terms.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["term_type", "canonical", "paper_mentions", "observed_raw_forms"]
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for term_type, counter in all_canonical.items():
            for canonical, count in counter.most_common():
                w.writerow({
                    "term_type": term_type,
                    "canonical": canonical,
                    "paper_mentions": count,
                    "observed_raw_forms": " | ".join(sorted(raw_forms[term_type][canonical])),
                })

    suggestions = []
    if token_set_ratio is not None:
        for term_type, counter in all_canonical.items():
            terms = list(counter)
            for a, b in itertools.combinations(terms, 2):
                score = float(token_set_ratio(surface_key(a), surface_key(b)))
                if score >= args.suggest_threshold and surface_key(a) != surface_key(b):
                    suggestions.append({"term_type": term_type, "term_a": a, "term_b": b, "similarity": round(score, 1)})
    suggestions.sort(key=lambda x: x["similarity"], reverse=True)
    with (out_dir / "possible_duplicate_terms.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["term_type", "term_a", "term_b", "similarity"]
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(suggestions)
    print(f"Audit: {out_dir}")
    print(f"  summary rows  : {len(summary_rows)}")
    print(f"  suggestions   : {len(suggestions)} (review only; no automatic merge)")


if __name__ == "__main__":
    main()
