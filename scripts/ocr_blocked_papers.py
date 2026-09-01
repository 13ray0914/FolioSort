#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "bin" / "python"
if VENV_PY.exists() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
    os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])

import argparse
import json

sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    connect_db,
    get_paths,
    load_config,
    set_stage,
    sha256_text,
    stage_is_current,
)
from lib.projects import normalize_project_slug, project_paper_ids

STAGE = "ocr"
SCRIPT_VERSION = "ocr-blocked-v1"


def _ocr_text_chars(path: Path) -> int:
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf  # type: ignore
        document = pymupdf.open(path)
        try:
            return sum(len((page.get_text() or "").strip()) for page in document)
        finally:
            document.close()
    except Exception:
        return 0


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )


def _ocr_command(
    executable: str,
    source: Path,
    target: Path,
    *,
    languages: str,
    jobs: int,
) -> list[str]:
    return [
        executable,
        "--skip-text",
        "--rotate-pages",
        "--deskew",
        "--optimize",
        "1",
        "--jobs",
        str(max(1, jobs)),
        "-l",
        languages,
        str(source),
        str(target),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create non-destructive OCR derivatives for papers blocked before GROBID."
    )
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--ids-file", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    slug = normalize_project_slug(args.project)
    ids_path = Path(args.ids_file).resolve()
    ids_path.parent.mkdir(parents=True, exist_ok=True)
    ids_path.write_text("", encoding="utf-8")

    ocr_cfg = config.get("ocr") or {}
    languages = str(ocr_cfg.get("languages") or "eng+jpn")
    jobs = int(ocr_cfg.get("jobs") or max(1, min(4, (os.cpu_count() or 2) // 2)))
    timeout = int(ocr_cfg.get("timeout_seconds_per_pdf") or 1800)
    minimum_text_chars = int(ocr_cfg.get("minimum_text_chars") or 40)
    configured_command = str(ocr_cfg.get("command") or "ocrmypdf")
    executable = shutil.which(configured_command)
    if not executable and configured_command == "ocrmypdf":
        isolated = root / ".venv_ocr" / "bin" / "ocrmypdf"
        executable = str(isolated) if isolated.is_file() else None
    if not executable:
        raise SystemExit(
            "OCRmyPDF is not installed. Install ocrmypdf, tesseract-ocr-eng, and "
            "tesseract-ocr-jpn in WSL, then retry."
        )
    executable_dir = str(Path(executable).parent)
    os.environ["PATH"] = executable_dir + os.pathsep + os.environ.get("PATH", "")

    conn = connect_db(paths["database"])
    try:
        allowed = set(project_paper_ids(conn, slug, active_only=True))
        rows = conn.execute(
            """
            SELECT p.*
            FROM papers p
            JOIN stages s ON s.paper_id=p.paper_id AND s.stage='grobid_parse'
            WHERE p.active=1 AND s.status='error' AND s.error LIKE 'OCR_REQUIRED:%'
            ORDER BY p.paper_id
            """
        ).fetchall()
        rows = [row for row in rows if str(row["paper_id"]) in allowed]
        print(f"OCR-BLOCKED project={slug} papers={len(rows)} languages={languages}", flush=True)
        if not rows:
            print("OCR-DONE no blocked papers", flush=True)
            return

        output_dir = root / "data" / "ocr_pdfs"
        output_dir.mkdir(parents=True, exist_ok=True)
        successful: list[str] = []
        failed = 0
        for row in rows:
            paper_id = str(row["paper_id"])
            source = paths["raw_pdfs"] / str(row["source_relpath"])
            target = output_dir / f"{paper_id}.ocr.pdf"
            input_hash = sha256_text(
                "\n".join([SCRIPT_VERSION, str(row["source_sha256"]), languages, str(jobs)])
            )
            if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, target):
                print(f"OCR-SKIP {paper_id} derivative current", flush=True)
                successful.append(paper_id)
                continue

            print(f"OCR-START {paper_id}", flush=True)
            set_stage(
                conn,
                paper_id,
                STAGE,
                "running",
                input_hash=input_hash,
                meta={"source_sha256": str(row["source_sha256"]), "languages": languages},
            )
            temporary = target.with_suffix(".tmp.pdf")
            decrypted: Path | None = None
            temporary.unlink(missing_ok=True)
            try:
                result = _run(
                    _ocr_command(executable, source, temporary, languages=languages, jobs=jobs),
                    timeout=timeout,
                )
                detail = result.stdout or ""
                if result.returncode != 0 and any(
                    token in detail.lower() for token in ("encrypt", "password", "security handler")
                ):
                    qpdf = shutil.which("qpdf")
                    if qpdf:
                        descriptor, decrypted_name = tempfile.mkstemp(
                            prefix=f"foliosort-{paper_id}-", suffix=".pdf"
                        )
                        os.close(descriptor)
                        decrypted = Path(decrypted_name)
                        decrypt = _run(
                            [qpdf, "--password=", "--decrypt", str(source), str(decrypted)], timeout=300
                        )
                        if decrypt.returncode == 0:
                            temporary.unlink(missing_ok=True)
                            result = _run(
                                _ocr_command(executable, decrypted, temporary, languages=languages, jobs=jobs),
                                timeout=timeout,
                            )
                            detail = result.stdout or ""
                if result.returncode != 0:
                    raise RuntimeError(detail.strip()[-3000:] or f"OCRmyPDF exited with {result.returncode}")
                if not temporary.exists() or temporary.stat().st_size < 5 or temporary.read_bytes()[:5] != b"%PDF-":
                    raise RuntimeError("OCRmyPDF did not produce a valid PDF")
                text_chars = _ocr_text_chars(temporary)
                if text_chars < minimum_text_chars:
                    raise RuntimeError(
                        f"OCR output contained only {text_chars} extractable characters; minimum is {minimum_text_chars}"
                    )
                os.replace(temporary, target)
                set_stage(
                    conn,
                    paper_id,
                    STAGE,
                    "success",
                    input_hash=input_hash,
                    output_path=target,
                    meta={
                        "source_sha256": str(row["source_sha256"]),
                        "languages": languages,
                        "text_chars": text_chars,
                        "original_pdf_preserved": True,
                    },
                )
                successful.append(paper_id)
                print(f"OCR-DONE {paper_id} text_chars={text_chars}", flush=True)
            except Exception as exc:
                failed += 1
                temporary.unlink(missing_ok=True)
                set_stage(
                    conn,
                    paper_id,
                    STAGE,
                    "error",
                    input_hash=input_hash,
                    error=str(exc),
                    meta={"source_sha256": str(row["source_sha256"]), "languages": languages},
                )
                print(f"OCR-ERROR {paper_id}: {exc}", flush=True)
            finally:
                if decrypted is not None:
                    decrypted.unlink(missing_ok=True)

        ids_path.write_text(",".join(successful), encoding="utf-8")
        print(f"OCR-SUMMARY success={len(successful)} failed={failed}", flush=True)
        if not successful and failed:
            raise SystemExit(2)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
