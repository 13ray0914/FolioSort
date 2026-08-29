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
    export_manifest,
    get_paths,
    load_config,
    next_paper_id,
    now_iso,
    reset_stages,
    sha256_file,
)


def append_duplicate_event(root: Path, payload: dict) -> None:
    log_path = root / "logs" / "duplicate_pdfs.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def delete_exact_duplicate(
    *,
    pdf: Path,
    rel: str,
    digest: str,
    kept_paper_id: str,
    kept_relpath: str,
    root: Path,
    note: str | None = None,
) -> bool:
    event = {
        "created_at": now_iso(),
        "event_type": "exact_pdf_duplicate_deleted",
        "deleted_relpath": rel,
        "kept_paper_id": kept_paper_id,
        "kept_relpath": kept_relpath,
        "sha256": digest,
    }
    if note:
        event["note"] = note
    try:
        pdf.unlink()
    except OSError as exc:
        event["event_type"] = "exact_pdf_duplicate_delete_failed"
        event["error"] = f"{type(exc).__name__}: {exc}"
        append_duplicate_event(root, event)
        print(
            f"DUPLICATE-DELETE-FAILED {rel} == {kept_paper_id} ({kept_relpath}) "
            f"[{type(exc).__name__}: {exc}]"
        )
        return False

    append_duplicate_event(root, event)
    suffix = f" | {note}" if note else ""
    print(f"DUPLICATE-DELETED {rel} -> kept {kept_paper_id} ({kept_relpath}){suffix}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Scan raw_pdfs, assign stable Pxxxx IDs, update manifest/SQLite, and delete only "
            "byte-identical duplicate PDFs (SHA-256 exact match). Filenames are never parsed as metadata."
        )
    )
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    raw_dir = paths["raw_pdfs"]
    manifest_path = paths["manifest"]
    conn = connect_db(paths["database"])

    pdfs = sorted(p for p in raw_dir.rglob("*.pdf") if p.is_file())
    seen_relpaths: set[str] = set()
    added = 0
    changed = 0
    unchanged = 0
    duplicates_deleted = 0
    duplicate_delete_failed = 0
    renamed = 0

    for pdf in pdfs:
        # A previous duplicate in the same scan may already have been deleted.
        if not pdf.exists():
            continue
        rel = pdf.relative_to(raw_dir).as_posix()
        digest = sha256_file(pdf)
        stat = pdf.stat()

        by_path = conn.execute("SELECT * FROM papers WHERE source_relpath = ?", (rel,)).fetchone()

        # Existing path, unchanged bytes: keep it as the canonical copy.
        if by_path and by_path["source_sha256"] == digest:
            seen_relpaths.add(rel)
            conn.execute(
                "UPDATE papers SET last_seen_at=?, active=1, file_size=?, original_filename=? WHERE paper_id=?",
                (now_iso(), stat.st_size, pdf.name, by_path["paper_id"]),
            )
            conn.commit()
            unchanged += 1
            continue

        # Look for the same exact bytes under another stable paper ID/path.
        if by_path:
            by_hash = conn.execute(
                "SELECT * FROM papers WHERE source_sha256 = ? AND paper_id <> ? "
                "ORDER BY active DESC, paper_id LIMIT 1",
                (digest, by_path["paper_id"]),
            ).fetchone()
        else:
            by_hash = conn.execute(
                "SELECT * FROM papers WHERE source_sha256 = ? ORDER BY active DESC, paper_id LIMIT 1",
                (digest,),
            ).fetchone()

        if by_hash:
            kept_path = raw_dir / by_hash["source_relpath"]
            if kept_path.exists():
                # The already-registered, still-present copy wins. Delete only the exact duplicate.
                note = None
                if by_path:
                    note = (
                        f"path previously belonged to {by_path['paper_id']}; that paper is now inactive "
                        "because its source PDF was replaced by an exact duplicate"
                    )
                deleted = delete_exact_duplicate(
                    pdf=pdf,
                    rel=rel,
                    digest=digest,
                    kept_paper_id=by_hash["paper_id"],
                    kept_relpath=by_hash["source_relpath"],
                    root=root,
                    note=note,
                )
                if deleted:
                    duplicates_deleted += 1
                    if by_path:
                        conn.execute(
                            "UPDATE papers SET active=0, last_seen_at=? WHERE paper_id=?",
                            (now_iso(), by_path["paper_id"]),
                        )
                        conn.commit()
                    # Deliberately do not add this deleted path to seen_relpaths.
                else:
                    duplicate_delete_failed += 1
                    # Keep it visible for this scan if deletion failed, but do not analyze it as a new paper.
                    seen_relpaths.add(rel)
                continue

            # Same bytes are known, but the old source path disappeared. Preserve the old stable ID as a rename/move.
            if by_path and by_path["paper_id"] != by_hash["paper_id"]:
                conn.execute(
                    "UPDATE papers SET active=0, last_seen_at=? WHERE paper_id=?",
                    (now_iso(), by_path["paper_id"]),
                )
            conn.execute(
                """
                UPDATE papers
                SET source_relpath=?, original_filename=?, file_size=?, last_seen_at=?, active=1
                WHERE paper_id=?
                """,
                (rel, pdf.name, stat.st_size, now_iso(), by_hash["paper_id"]),
            )
            conn.commit()
            seen_relpaths.add(rel)
            print(f"RENAMED {by_hash['paper_id']}  {by_hash['source_relpath']} -> {rel}")
            renamed += 1
            unchanged += 1
            continue

        if by_path:
            # Same path, genuinely different bytes and not a duplicate of another paper.
            seen_relpaths.add(rel)
            conn.execute(
                """
                UPDATE papers
                SET source_sha256=?, file_size=?, original_filename=?, last_seen_at=?, active=1
                WHERE paper_id=?
                """,
                (digest, stat.st_size, pdf.name, now_iso(), by_path["paper_id"]),
            )
            conn.commit()
            reset_stages(conn, by_path["paper_id"])
            print(f"CHANGED {by_path['paper_id']}  {rel} -> downstream stages reset")
            changed += 1
            continue

        paper_id = next_paper_id(conn)
        conn.execute(
            """
            INSERT INTO papers(
                paper_id, source_relpath, original_filename, source_sha256, file_size,
                first_seen_at, last_seen_at, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (paper_id, rel, pdf.name, digest, stat.st_size, now_iso(), now_iso()),
        )
        conn.commit()
        seen_relpaths.add(rel)
        print(f"ADDED   {paper_id}  {rel}")
        added += 1

    # Files that disappeared from raw_pdfs remain in the database for provenance but become inactive.
    for row in conn.execute("SELECT paper_id, source_relpath FROM papers WHERE active=1").fetchall():
        if row["source_relpath"] not in seen_relpaths:
            conn.execute("UPDATE papers SET active=0, last_seen_at=? WHERE paper_id=?", (now_iso(), row["paper_id"]))
    conn.commit()

    export_manifest(conn, manifest_path)
    total_active = conn.execute("SELECT COUNT(*) FROM papers WHERE active=1").fetchone()[0]
    print(
        f"\nDone. active={total_active}, added={added}, changed={changed}, renamed={renamed}, "
        f"unchanged={unchanged}, duplicate_files_deleted={duplicates_deleted}, "
        f"duplicate_delete_failed={duplicate_delete_failed}"
    )
    if duplicates_deleted or duplicate_delete_failed:
        print(f"Duplicate audit log: {root / 'logs' / 'duplicate_pdfs.jsonl'}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
