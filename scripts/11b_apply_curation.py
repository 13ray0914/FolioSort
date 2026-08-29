#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.curation import materialize_all
from lib.pipeline_common import connect_db, get_paths, load_config, select_papers


def parse_ids(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [x.strip() for x in value.split(",") if x.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply controlled-vocabulary normalization and human curation overlays without modifying raw extraction JSON.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    curation_cfg = config.get("curation", {})
    if not curation_cfg.get("enabled", True):
        print("CURATE  disabled in config")
        return
    curated_dir = paths.get("curated", root / "data/curated")
    curation_dir = paths.get("curation", root / "data/curation")
    events_path = curation_dir / "events.jsonl"
    ontology_path = Path(curation_cfg.get("ontology_path", f"profiles/{config['profile']}/ontology/terms.json"))
    if not ontology_path.is_absolute():
        ontology_path = root / ontology_path

    conn = connect_db(paths["database"])
    requested = parse_ids(args.ids)
    rows = select_papers(conn, requested, args.limit)
    paper_ids = [row["paper_id"] for row in rows]
    results = materialize_all(
        paper_ids,
        extracted_dir=paths["extracted"],
        curated_dir=curated_dir,
        ontology_path=ontology_path,
        events_path=events_path,
    )
    written = sum(len(item["written"]) for item in results)
    print(f"CURATE  papers={len(paper_ids)} files_written={written}")
    print(f"Raw     {paths['extracted']}")
    print(f"Curated {curated_dir}")
    print(f"Events  {events_path}")


if __name__ == "__main__":
    main()
