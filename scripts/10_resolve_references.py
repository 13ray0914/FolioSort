#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    connect_db,
    get_paths,
    load_config,
    normalize_ws,
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
    normalize_openalex_id,
    normalize_title,
    now_iso,
)

STAGE = "reference_resolution_v4"
SCRIPT_VERSION = "reference-resolution-v4.1-incremental"


def canonical_for(paper_id: str, paper_path: Path, metadata_path: Path) -> dict[str, Any]:
    paper = read_json(paper_path)
    if metadata_path.exists():
        metadata = read_json(metadata_path)
        canonical = dict(metadata.get("canonical") or {})
    else:
        canonical = dict(paper.get("metadata") or {})
    canonical["paper_id"] = paper_id
    canonical.setdefault("authors", paper.get("metadata", {}).get("authors") or [])
    canonical["doi"] = normalize_doi(canonical.get("doi")) or None
    canonical["openalex_id"] = normalize_openalex_id(canonical.get("openalex_id")) or None
    canonical["title_normalized"] = normalize_title(canonical.get("title"))
    return canonical


def ref_query(reference: dict[str, Any]) -> dict[str, Any]:
    authors = reference.get("authors") or []
    if authors and isinstance(authors[0], str):
        authors = [{"full_name": value} for value in authors]
    return {
        "title": reference.get("title"),
        "authors": authors,
        "year": reference.get("year"),
        "journal": reference.get("journal"),
        "doi": normalize_doi(reference.get("doi")),
        "raw_reference": reference.get("raw_reference"),
    }


def reference_search_issue(query: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Return an actionable reason when a reference is unsafe for text search."""
    title = normalize_ws(query.get("title") or "")
    raw = normalize_ws(query.get("raw_reference") or "")
    text = title or raw
    if not text:
        return "Reference has no searchable title or citation text; enter its DOI manually."
    max_chars = int(cfg.get("external_query_max_chars", 350))
    if len(text) > max_chars:
        return (
            f"Reference text is {len(text)} characters (limit {max_chars}) and looks like OCR/body text; "
            "external title search was skipped. Enter the DOI manually."
        )
    return None


def previous_reference_records(conn: Any, paper_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT ref_id,record_json FROM reference_matches_v4 WHERE citing_paper_id=?",
        (paper_id,),
    ).fetchall()
    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row["record_json"] or "{}")
        except Exception:
            continue
        if isinstance(payload, dict):
            records[str(row["ref_id"])] = payload
    return records


def apply_local_metadata(resolved: dict[str, Any], local: dict[str, Any]) -> None:
    resolved.update(
        {
            "title": local.get("title") or resolved.get("title"),
            "doi": local.get("doi") or resolved.get("doi"),
            "year": local.get("year") or resolved.get("year"),
            "journal": local.get("journal") or resolved.get("journal"),
            "openalex_id": local.get("openalex_id") or resolved.get("openalex_id"),
        }
    )


def best_local_match(
    query: dict[str, Any],
    local_metadata: dict[str, dict[str, Any]],
    *,
    citing_paper_id: str,
    oa_referenced_ids: set[str],
    cfg: dict[str, Any],
) -> tuple[str | None, dict[str, float] | None, str | None]:
    doi = normalize_doi(query.get("doi"))
    if doi:
        exact = [pid for pid, meta in local_metadata.items() if pid != citing_paper_id and normalize_doi(meta.get("doi")) == doi]
        if len(exact) == 1:
            return exact[0], {"overall": 1.0, "title": 1.0, "author": 1.0, "year": 1.0, "journal": 1.0}, "doi_exact"

    candidates: list[tuple[str, dict[str, float], float]] = []
    for paper_id, metadata in local_metadata.items():
        if paper_id == citing_paper_id:
            continue
        score = metadata_match_score(query, metadata)
        oa_bonus = 0.035 if normalize_openalex_id(metadata.get("openalex_id")) in oa_referenced_ids else 0.0
        candidates.append((paper_id, score, score["overall"] + oa_bonus))
    candidates.sort(key=lambda item: item[2], reverse=True)
    if not candidates:
        return None, None, None
    paper_id, score, adjusted = candidates[0]
    second = candidates[1][2] if len(candidates) > 1 else 0.0
    margin = adjusted - second
    if (
        score["title"] >= float(cfg.get("local_min_title_similarity", 0.90))
        and adjusted >= float(cfg.get("local_accept_threshold", 0.89))
        and margin >= float(cfg.get("local_min_margin", 0.02))
    ):
        method = "title_author_year"
        if normalize_openalex_id(local_metadata[paper_id].get("openalex_id")) in oa_referenced_ids:
            method = "title_author_year_plus_openalex_edge"
        return paper_id, {**score, "adjusted": round(adjusted, 6), "margin": round(margin, 6)}, method
    return None, {**score, "adjusted": round(adjusted, 6), "margin": round(margin, 6)}, None


def main() -> None:
    ap = argparse.ArgumentParser(description="Resolve references and identify cited local PDFs by DOI/title/OpenAlex edges.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--local-only", action="store_true", help="Do not query Crossref/OpenAlex for unresolved references.")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    metadata_dir = paths.get("metadata", root / "data/metadata")
    reference_dir = paths.get("reference_matches", root / "data/reference_matches")
    reference_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_db(paths["database"])
    ensure_v4_schema(conn)
    rows = select_papers(conn, parse_ids(args.ids), args.limit)
    cfg = config.get("reference_resolution", {})

    all_rows = conn.execute("SELECT * FROM papers WHERE active=1 ORDER BY paper_id").fetchall()
    local_metadata: dict[str, dict[str, Any]] = {}
    global_hash_parts: list[str] = []
    for row in all_rows:
        paper_path = paths["paper_json"] / f"{row['paper_id']}.json"
        metadata_path = metadata_dir / f"{row['paper_id']}.metadata.json"
        if not paper_path.exists():
            continue
        local_metadata[row["paper_id"]] = canonical_for(row["paper_id"], paper_path, metadata_path)
        global_hash_parts.append(sha256_file(paper_path))
        if metadata_path.exists():
            global_hash_parts.append(sha256_file(metadata_path))
    local_index_hash = sha256_text("".join(global_hash_parts))

    metadata_cfg = config.get("metadata_enrichment", {})
    crossref_cfg = metadata_cfg.get("crossref", {})
    openalex_cfg = metadata_cfg.get("openalex", {})
    crossref = None if args.local_only or not crossref_cfg.get("enabled", True) else CrossrefClient(conn, crossref_cfg)
    openalex = None if args.local_only or not openalex_cfg.get("enabled", True) else OpenAlexClient(conn, openalex_cfg)
    signature = stable_json_hash({"script": SCRIPT_VERSION, "config": cfg, "local_index": local_index_hash})

    for row in rows:
        paper_id = row["paper_id"]
        paper_path = paths["paper_json"] / f"{paper_id}.json"
        metadata_path = metadata_dir / f"{paper_id}.metadata.json"
        out_path = reference_dir / f"{paper_id}.references.json"
        if not paper_path.exists():
            print(f"WAIT    {paper_id}: paper JSON missing")
            continue
        override_rows = conn.execute(
            "SELECT ref_id,doi,updated_at FROM reference_doi_overrides_v4 WHERE citing_paper_id=? ORDER BY ref_id",
            (paper_id,),
        ).fetchall()
        overrides = {str(item["ref_id"]): normalize_doi(item["doi"]) for item in override_rows}
        override_hash = stable_json_hash([dict(item) for item in override_rows])
        input_hash = sha256_text(
            sha256_file(paper_path)
            + (sha256_file(metadata_path) if metadata_path.exists() else "")
            + signature
            + override_hash
        )
        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} references current")
            continue

        paper = read_json(paper_path)
        citing_meta = local_metadata.get(paper_id, {})
        oa_referenced_ids = {normalize_openalex_id(value) for value in citing_meta.get("referenced_works") or []}
        print(f"REFS    {paper_id}: {len(paper.get('references', []))} references")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        try:
            records: list[dict[str, Any]] = []
            external_queries = 0
            reused_external = 0
            provider_error_count = 0
            max_queries = int(cfg.get("max_external_queries_per_paper", 80))
            previous_by_ref = previous_reference_records(conn, paper_id) if not args.force else {}
            for ref_index, reference in enumerate(paper.get("references", []), start=1):
                ref_id = str(reference.get("ref_id") or f"ref-{ref_index:04d}")
                query = ref_query(reference)
                manual_doi = overrides.get(ref_id)
                if manual_doi:
                    query["doi"] = manual_doi
                target, local_score, method = best_local_match(
                    query,
                    local_metadata,
                    citing_paper_id=paper_id,
                    oa_referenced_ids=oa_referenced_ids,
                    cfg=cfg,
                )
                resolved: dict[str, Any] = {
                    "title": query.get("title"),
                    "doi": normalize_doi(query.get("doi")) or None,
                    "year": query.get("year"),
                    "journal": query.get("journal"),
                    "openalex_id": None,
                }
                provider_scores: dict[str, Any] = {}
                reused = False

                if target:
                    apply_local_metadata(resolved, local_metadata[target])
                    if manual_doi:
                        method = "manual_doi_to_local"
                elif manual_doi:
                    # A human-entered DOI is authoritative for this reference and
                    # intentionally bypasses fragile free-text provider searches.
                    resolved["doi"] = manual_doi
                    method = "manual_doi_override"
                else:
                    previous = previous_by_ref.get(ref_id)
                    if previous:
                        previous_resolved = dict(previous.get("resolved") or {})
                        enriched_query = dict(query)
                        for field in ("title", "doi", "year", "journal"):
                            if previous_resolved.get(field):
                                enriched_query[field] = previous_resolved[field]
                        cached_target, cached_score, cached_method = best_local_match(
                            enriched_query,
                            local_metadata,
                            citing_paper_id=paper_id,
                            oa_referenced_ids=oa_referenced_ids,
                            cfg=cfg,
                        )
                        resolved.update(previous_resolved)
                        provider_scores = dict(previous.get("provider_scores") or {})
                        reused = True
                        reused_external += 1
                        if cached_target:
                            target = cached_target
                            local_score = cached_score
                            method = "cached_external_to_local" if previous_resolved else cached_method
                            apply_local_metadata(resolved, local_metadata[target])
                        else:
                            method = previous.get("match_method")

                if not target and not manual_doi and not reused:
                    search_issue = reference_search_issue(query, cfg) if not query.get("doi") else None
                    if search_issue:
                        provider_scores["search"] = {
                            "error": search_issue,
                            "manual_doi_recommended": True,
                        }
                        provider_error_count += 1

                    # Crossref resolution. A singleton DOI lookup can be accepted
                    # without a title only when the returned DOI is exactly the one
                    # printed in the reference. Title-bearing references still get
                    # a sanity check to guard against malformed OCR DOI strings.
                    if crossref and external_queries < max_queries and not search_issue:
                        try:
                            candidate = None
                            score = None
                            margin = 0.0
                            direct = False
                            if query.get("doi"):
                                candidate = crossref.by_doi(query["doi"], force=args.force)
                                direct = candidate is not None
                                if candidate:
                                    score = metadata_match_score(query, candidate)
                            if candidate is None:
                                candidates = crossref.search(
                                    query,
                                    rows=int(cfg.get("crossref_rows", 5)),
                                    force=args.force,
                                )
                                external_queries += 1
                                candidate, score, margin = choose_best_candidate(query, candidates)
                            provider_scores["crossref"] = {
                                "score": score,
                                "margin": margin,
                                "direct_doi": direct,
                            }
                            exact_doi = bool(
                                direct
                                and normalize_doi(candidate.get("doi") if candidate else "")
                                == normalize_doi(query.get("doi"))
                            )
                            direct_ok = exact_doi and (
                                not query.get("title")
                                or (score or {}).get("title", 0.0)
                                >= float(cfg.get("direct_doi_min_title_similarity", 0.55))
                            )
                            search_ok = bool(
                                candidate
                                and score
                                and score["title"] >= float(cfg.get("external_min_title_similarity", 0.84))
                                and score["overall"] >= float(cfg.get("external_accept_threshold", 0.86))
                            )
                            if candidate and score and (direct_ok or search_ok):
                                resolved.update(
                                    {
                                        "title": candidate.get("title") or resolved["title"],
                                        "doi": candidate.get("doi") or resolved["doi"],
                                        "year": candidate.get("year") or resolved["year"],
                                        "journal": candidate.get("journal") or resolved["journal"],
                                    }
                                )
                                method = "crossref_doi" if direct_ok else "crossref_resolved"
                        except Exception as error:
                            provider_scores["crossref"] = {"error": str(error), "retryable": True}
                            provider_error_count += 1

                    # OpenAlex is used when Crossref did not resolve the citation.
                    # Confirmation of an already resolved DOI is optional because it
                    # doubles network traffic without changing ordinary graph edges.
                    should_query_openalex = bool(
                        openalex
                        and not search_issue
                        and (
                            not (method or "").startswith("crossref")
                            or bool(cfg.get("confirm_resolved_with_openalex", False))
                        )
                    )
                    if should_query_openalex:
                        try:
                            oa_candidate = None
                            oa_score = None
                            oa_margin = 0.0
                            oa_direct = False
                            if resolved.get("doi"):
                                oa_candidate = openalex.by_doi(resolved["doi"], force=args.force)
                                oa_direct = oa_candidate is not None
                                if oa_candidate:
                                    oa_score = metadata_match_score(query, oa_candidate)
                            if oa_candidate is None and external_queries < max_queries:
                                oa_candidates = openalex.search(query, force=args.force)
                                external_queries += 1
                                oa_candidate, oa_score, oa_margin = choose_best_candidate(query, oa_candidates)
                            provider_scores["openalex"] = {
                                "score": oa_score,
                                "margin": oa_margin,
                                "direct_doi": oa_direct,
                            }
                            oa_exact_doi = bool(
                                oa_direct
                                and normalize_doi(oa_candidate.get("doi") if oa_candidate else "")
                                == normalize_doi(resolved.get("doi") or query.get("doi"))
                            )
                            oa_direct_ok = oa_exact_doi and (
                                not query.get("title")
                                or (oa_score or {}).get("title", 0.0)
                                >= float(cfg.get("direct_doi_min_title_similarity", 0.55))
                            )
                            oa_search_ok = bool(
                                oa_candidate
                                and oa_score
                                and oa_score["title"] >= float(cfg.get("external_min_title_similarity", 0.84))
                                and oa_score["overall"] >= float(cfg.get("external_accept_threshold", 0.86))
                            )
                            if oa_candidate and oa_score and (oa_direct_ok or oa_search_ok):
                                resolved.update(
                                    {
                                        "title": oa_candidate.get("title") or resolved["title"],
                                        "doi": oa_candidate.get("doi") or resolved["doi"],
                                        "year": oa_candidate.get("year") or resolved["year"],
                                        "journal": oa_candidate.get("journal") or resolved["journal"],
                                        "openalex_id": oa_candidate.get("openalex_id"),
                                    }
                                )
                                if method and method.startswith("crossref"):
                                    method += "+openalex_confirmed"
                                elif oa_direct_ok:
                                    method = "openalex_doi"
                                else:
                                    method = "openalex_resolved"
                        except Exception as error:
                            provider_scores["openalex"] = {"error": str(error), "retryable": True}
                            provider_error_count += 1

                if not target and resolved.get("doi"):
                    doi_targets = [
                        pid for pid, meta in local_metadata.items()
                        if pid != paper_id and normalize_doi(meta.get("doi")) == normalize_doi(resolved.get("doi"))
                    ]
                    if len(doi_targets) == 1:
                        target = doi_targets[0]
                        method = "resolved_doi_to_local"
                if not target and resolved.get("openalex_id"):
                    oa_targets = [
                        pid for pid, meta in local_metadata.items()
                        if pid != paper_id and normalize_openalex_id(meta.get("openalex_id")) == normalize_openalex_id(resolved.get("openalex_id"))
                    ]
                    if len(oa_targets) == 1:
                        target = oa_targets[0]
                        method = "resolved_openalex_to_local"

                status = "matched_local" if target else "resolved_external" if resolved.get("doi") or resolved.get("openalex_id") else "unresolved"
                record = {
                    "citing_paper_id": paper_id,
                    "ref_id": ref_id,
                    "original": reference,
                    "query": query,
                    "target_paper_id": target,
                    "match_method": method,
                    "local_match_score": local_score,
                    "provider_scores": provider_scores,
                    "resolved": resolved,
                    "status": status,
                    "manual_doi_override": manual_doi,
                    "reused_external_resolution": reused,
                }
                records.append(record)
                conn.execute(
                    """
                    INSERT INTO reference_matches_v4(
                        citing_paper_id,ref_id,target_paper_id,match_method,match_score,
                        resolved_title,resolved_doi,resolved_year,resolved_journal,resolved_openalex_id,
                        status,record_json,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(citing_paper_id,ref_id) DO UPDATE SET
                        target_paper_id=excluded.target_paper_id,
                        match_method=excluded.match_method,
                        match_score=excluded.match_score,
                        resolved_title=excluded.resolved_title,
                        resolved_doi=excluded.resolved_doi,
                        resolved_year=excluded.resolved_year,
                        resolved_journal=excluded.resolved_journal,
                        resolved_openalex_id=excluded.resolved_openalex_id,
                        status=excluded.status,
                        record_json=excluded.record_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        paper_id,
                        ref_id,
                        target,
                        method,
                        (local_score or {}).get("adjusted") or (local_score or {}).get("overall"),
                        resolved.get("title"),
                        normalize_doi(resolved.get("doi")) or None,
                        resolved.get("year"),
                        resolved.get("journal"),
                        normalize_openalex_id(resolved.get("openalex_id")) or None,
                        status,
                        json.dumps(record, ensure_ascii=False),
                        now_iso(),
                    ),
                )
                if ref_index % 10 == 0:
                    print(
                        f"        {paper_id}: {ref_index}/{len(paper.get('references', []))} references "
                        f"(new external={external_queries}, reused={reused_external}, provider errors={provider_error_count})"
                    )
            conn.commit()

            # Add direct OpenAlex citation edges between local papers when the
            # local PDF reference list could not be matched to a particular ref.
            existing_targets = {x.get("target_paper_id") for x in records if x.get("target_paper_id")}
            oa_only_edges = []
            for target_id, metadata in local_metadata.items():
                if target_id == paper_id or target_id in existing_targets:
                    continue
                openalex_id = normalize_openalex_id(metadata.get("openalex_id"))
                if openalex_id and openalex_id in oa_referenced_ids:
                    oa_only_edges.append(
                        {
                            "citing_paper_id": paper_id,
                            "target_paper_id": target_id,
                            "match_method": "openalex_referenced_works",
                            "status": "matched_local",
                        }
                    )

            payload = {
                "paper_id": paper_id,
                "references": records,
                "openalex_only_local_edges": oa_only_edges,
                "summary": {
                    "total": len(records),
                    "matched_local": sum(x["status"] == "matched_local" for x in records),
                    "resolved_external": sum(x["status"] == "resolved_external" for x in records),
                    "unresolved": sum(x["status"] == "unresolved" for x in records),
                    "openalex_only_local_edges": len(oa_only_edges),
                    "external_queries": external_queries,
                    "reused_external_resolutions": reused_external,
                    "provider_errors": provider_error_count,
                    "manual_doi_overrides": sum(bool(x.get("manual_doi_override")) for x in records),
                },
                "provenance": {"script_version": SCRIPT_VERSION, "local_index_hash": local_index_hash},
            }
            write_json(out_path, payload)
            set_stage(conn, paper_id, STAGE, "success", input_hash, out_path, meta=payload["summary"])
            print(
                f"DONE    {paper_id}: new external={external_queries}, reused={reused_external}, "
                f"provider errors={provider_error_count}"
            )
        except Exception as error:
            conn.rollback()
            set_stage(conn, paper_id, STAGE, "error", input_hash=input_hash, error=str(error))
            print(f"ERROR   {paper_id}: {error}")


if __name__ == "__main__":
    main()
