#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    connect_db,
    get_paths,
    load_config,
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
from lib.v4_common import (
    CrossrefClient,
    OpenAlexClient,
    choose_best_candidate,
    ensure_v4_schema,
    metadata_match_score,
    normalize_doi,
    now_iso,
)

STAGE = "metadata_enrichment_v4"
SCRIPT_VERSION = "metadata-v4.1-bibliography"


def accepted(
    score: dict | None,
    margin: float,
    cfg: dict,
    *,
    direct: bool = False,
    query_has_title: bool = True,
) -> bool:
    if not score:
        return False
    if direct:
        # A DOI singleton lookup is authoritative for that DOI, but a malformed
        # DOI extracted from an old PDF should not silently overwrite an
        # unrelated title. When no local title is available, DOI is the only
        # usable identifier and the singleton lookup is accepted for review.
        return (not query_has_title) or score.get("title", 0.0) >= float(
            cfg.get("direct_doi_min_title_similarity", 0.60)
        )
    return (
        score.get("overall", 0.0) >= float(cfg.get("auto_accept_threshold", 0.91))
        and score.get("title", 0.0) >= float(cfg.get("min_title_similarity", 0.86))
        and margin >= float(cfg.get("min_candidate_margin", 0.025))
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Enrich local-paper metadata with Crossref and OpenAlex.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    metadata_dir = paths.get("metadata", root / "data/metadata")
    metadata_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_db(paths["database"])
    ensure_v4_schema(conn)
    rows = select_papers(conn, parse_ids(args.ids), args.limit)

    cfg = config.get("metadata_enrichment", {})
    cr_cfg = cfg.get("crossref", {})
    oa_cfg = cfg.get("openalex", {})
    crossref = CrossrefClient(conn, cr_cfg) if cr_cfg.get("enabled", True) else None
    openalex = OpenAlexClient(conn, oa_cfg) if oa_cfg.get("enabled", True) else None
    signature = stable_json_hash({"script": SCRIPT_VERSION, "config": cfg})

    for row in rows:
        paper_id = row["paper_id"]
        paper_path = paths["paper_json"] / f"{paper_id}.json"
        out_path = metadata_dir / f"{paper_id}.metadata.json"
        if not paper_path.exists():
            print(f"WAIT    {paper_id}: paper JSON missing")
            continue
        input_hash = sha256_text(sha256_file(paper_path) + signature)
        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} metadata current")
            continue

        print(f"META    {paper_id}")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        try:
            paper = read_json(paper_path)
            original = dict(paper.get("metadata") or {})
            query = {
                "title": original.get("title") or row["title"],
                "authors": original.get("authors") or [],
                "year": original.get("year") or row["year"],
                "journal": original.get("journal") or row["journal"],
                "doi": normalize_doi(original.get("doi") or row["doi"]),
            }

            cr_best = None
            cr_score = None
            cr_margin = 0.0
            cr_direct = False
            cr_candidates = []
            if crossref:
                if query["doi"]:
                    cr_best = crossref.by_doi(query["doi"], force=args.force)
                    cr_direct = cr_best is not None
                    if cr_best:
                        cr_score = metadata_match_score(query, cr_best)
                if cr_best is None:
                    cr_candidates = crossref.search(query, force=args.force)
                    cr_best, cr_score, cr_margin = choose_best_candidate(query, cr_candidates)

            cr_ok = accepted(
                cr_score, cr_margin, cfg, direct=cr_direct, query_has_title=bool(query.get("title"))
            )
            resolved_doi = normalize_doi(cr_best.get("doi") if cr_ok and cr_best else query["doi"])

            oa_best = None
            oa_score = None
            oa_margin = 0.0
            oa_direct = False
            oa_candidates = []
            if openalex:
                if resolved_doi:
                    oa_best = openalex.by_doi(resolved_doi, force=args.force)
                    oa_direct = oa_best is not None
                    if oa_best:
                        oa_score = metadata_match_score(query, oa_best)
                if oa_best is None:
                    oa_candidates = openalex.search(query, force=args.force)
                    oa_best, oa_score, oa_margin = choose_best_candidate(query, oa_candidates)

            oa_ok = accepted(
                oa_score, oa_margin, cfg, direct=oa_direct, query_has_title=bool(query.get("title"))
            )

            accepted_sources = []
            if cr_ok and cr_best:
                accepted_sources.append(cr_best)
            if oa_ok and oa_best:
                accepted_sources.append(oa_best)

            doi_values = {normalize_doi(x.get("doi")) for x in accepted_sources if normalize_doi(x.get("doi"))}
            conflict = len(doi_values) > 1
            if conflict:
                status = "conflict"
            elif accepted_sources:
                status = "resolved"
            elif cr_best or oa_best:
                status = "manual_review"
            else:
                status = "unresolved"

            preferred = None
            if not conflict:
                if cr_ok and cr_best:
                    preferred = cr_best
                elif oa_ok and oa_best:
                    preferred = oa_best
            canonical = {
                "title": (preferred or {}).get("title") or query["title"],
                "doi": normalize_doi((preferred or {}).get("doi") or query["doi"]) or None,
                "year": (preferred or {}).get("year") or query["year"],
                "journal": (preferred or {}).get("journal") or query["journal"],
                "authors": (preferred or {}).get("authors") or query["authors"],
                "journal_abbreviation": (preferred or {}).get("journal_abbreviation"),
                "volume": (preferred or {}).get("volume"),
                "issue": (preferred or {}).get("issue"),
                "pages": (preferred or {}).get("pages"),
                "article_number": (preferred or {}).get("article_number"),
                "issn": (preferred or {}).get("issn") or [],
                "openalex_id": (oa_best or {}).get("openalex_id") if oa_ok else None,
                "referenced_works": (oa_best or {}).get("referenced_works", []) if oa_ok else [],
                "cited_by_count": (oa_best or {}).get("cited_by_count") if oa_ok else None,
            }
            confidence = max(
                (cr_score or {}).get("overall", 0.0) if cr_ok else 0.0,
                (oa_score or {}).get("overall", 0.0) if oa_ok else 0.0,
            )
            canonical_source = "crossref" if preferred is cr_best else "openalex" if preferred is oa_best else "grobid"

            def candidate_summary(candidate: dict, score: dict | None) -> dict:
                return {
                    "title": candidate.get("title"),
                    "doi": candidate.get("doi"),
                    "year": candidate.get("year"),
                    "journal": candidate.get("journal"),
                    "journal_abbreviation": candidate.get("journal_abbreviation"),
                    "volume": candidate.get("volume"),
                    "issue": candidate.get("issue"),
                    "pages": candidate.get("pages"),
                    "article_number": candidate.get("article_number"),
                    "openalex_id": candidate.get("openalex_id"),
                    "score": score,
                }

            payload = {
                "paper_id": paper_id,
                "match_status": status,
                "confidence": round(float(confidence), 6),
                "canonical_source": canonical_source,
                "original": query,
                "canonical": canonical,
                "crossref": {
                    "accepted": bool(cr_ok),
                    "direct_doi_lookup": cr_direct,
                    "score": cr_score,
                    "margin": cr_margin,
                    "selected": candidate_summary(cr_best, cr_score) if cr_best else None,
                    "candidates": [candidate_summary(c, metadata_match_score(query, c)) for c in cr_candidates[:5]],
                },
                "openalex": {
                    "accepted": bool(oa_ok),
                    "direct_doi_lookup": oa_direct,
                    "score": oa_score,
                    "margin": oa_margin,
                    "selected": candidate_summary(oa_best, oa_score) if oa_best else None,
                    "candidates": [candidate_summary(c, metadata_match_score(query, c)) for c in oa_candidates[:5]],
                },
                "provenance": {"script_version": SCRIPT_VERSION},
            }
            write_json(out_path, payload)

            conn.execute(
                """
                INSERT INTO paper_metadata_v4(
                    paper_id,title,doi,year,journal,openalex_id,crossref_score,openalex_score,
                    confidence,match_status,canonical_source,metadata_json,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(paper_id) DO UPDATE SET
                    title=excluded.title, doi=excluded.doi, year=excluded.year,
                    journal=excluded.journal, openalex_id=excluded.openalex_id,
                    crossref_score=excluded.crossref_score, openalex_score=excluded.openalex_score,
                    confidence=excluded.confidence, match_status=excluded.match_status,
                    canonical_source=excluded.canonical_source, metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    paper_id,
                    canonical["title"],
                    canonical["doi"],
                    canonical["year"],
                    canonical["journal"],
                    canonical["openalex_id"],
                    (cr_score or {}).get("overall"),
                    (oa_score or {}).get("overall"),
                    confidence,
                    status,
                    canonical_source,
                    json.dumps(payload, ensure_ascii=False),
                    now_iso(),
                ),
            )
            if status == "resolved":
                conn.execute(
                    "UPDATE papers SET title=?, doi=?, year=?, journal=? WHERE paper_id=?",
                    (canonical["title"], canonical["doi"], canonical["year"], canonical["journal"], paper_id),
                )
            conn.commit()
            set_stage(
                conn,
                paper_id,
                STAGE,
                "success",
                input_hash,
                out_path,
                meta={"status": status, "confidence": confidence, "doi": canonical["doi"]},
            )
        except Exception as error:
            set_stage(conn, paper_id, STAGE, "error", input_hash=input_hash, error=str(error))
            print(f"ERROR   {paper_id}: {error}")


if __name__ == "__main__":
    main()
