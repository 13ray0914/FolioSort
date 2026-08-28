#!/usr/bin/env python3
from __future__ import annotations

import argparse
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


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Scan raw_pdfs, assign stable Pxxxx IDs, and update manifest/SQLite. Filenames are never parsed as metadata."
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
    duplicates = 0

    for pdf in pdfs:
        rel = pdf.relative_to(raw_dir).as_posix()
        seen_relpaths.add(rel)
        digest = sha256_file(pdf)
        stat = pdf.stat()

        by_path = conn.execute("SELECT * FROM papers WHERE source_relpath = ?", (rel,)).fetchone()
        if by_path:
            if by_path["source_sha256"] != digest:
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
            else:
                conn.execute(
                    "UPDATE papers SET last_seen_at=?, active=1, file_size=?, original_filename=? WHERE paper_id=?",
                    (now_iso(), stat.st_size, pdf.name, by_path["paper_id"]),
                )
                conn.commit()
                unchanged += 1
            continue

        by_hash = conn.execute("SELECT * FROM papers WHERE source_sha256 = ?", (digest,)).fetchone()
        if by_hash:
            old_path = raw_dir / by_hash["source_relpath"]
            if not old_path.exists():
                # Same bytes under a new filename/path: treat as a rename, preserving the stable paper ID.
                conn.execute(
                    """
                    UPDATE papers
                    SET source_relpath=?, original_filename=?, file_size=?, last_seen_at=?, active=1
                    WHERE paper_id=?
                    """,
                    (rel, pdf.name, stat.st_size, now_iso(), by_hash["paper_id"]),
                )
                conn.commit()
                print(f"RENAMED {by_hash['paper_id']}  {by_hash['source_relpath']} -> {rel}")
                unchanged += 1
            else:
                print(
                    f"DUPLICATE skipped: {rel} has the same SHA-256 as {by_hash['paper_id']} ({by_hash['source_relpath']})"
                )
                duplicates += 1
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
        print(f"ADDED   {paper_id}  {rel}")
        added += 1

    # Mark files that disappeared from raw_pdfs as inactive; do not delete their data.
    for row in conn.execute("SELECT paper_id, source_relpath FROM papers WHERE active=1").fetchall():
        if row["source_relpath"] not in seen_relpaths:
            conn.execute("UPDATE papers SET active=0, last_seen_at=? WHERE paper_id=?", (now_iso(), row["paper_id"]))
    conn.commit()

    export_manifest(conn, manifest_path)
    total_active = conn.execute("SELECT COUNT(*) FROM papers WHERE active=1").fetchone()[0]
    print(
        f"\nDone. active={total_active}, added={added}, changed={changed}, "
        f"unchanged={unchanged}, duplicate_files={duplicates}"
    )
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
