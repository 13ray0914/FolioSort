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
    LLMOutputTruncatedError,
    LlamaCppClient,
    connect_db,
    get_paths,
    load_config,
    load_schema,
    log_llm_run_finish,
    log_llm_run_start,
    make_text_chunks,
    parse_ids,
    read_json,
    select_papers,
    set_stage,
    sha256_file,
    sha256_text,
    stable_json_hash,
    stage_is_current,
    split_chunk_adaptive,
    validate_schema,
    write_json,
)

STAGE = "summary_memory_v4"
SCRIPT_VERSION = "summary-memory-v4.0"


def visual_chunk_memory(visual: dict[str, Any]) -> dict[str, Any] | None:
    evidence = visual.get("visual_evidence") or []
    if not evidence:
        return None
    section_summaries = []
    findings = []
    definitions = []
    for item in evidence:
        evidence_id = item.get("evidence_id")
        text = str(item.get("text") or "").strip()
        caption = str(item.get("caption") or "").strip()
        if not evidence_id or not (text or caption):
            continue
        summary = text or caption
        section_summaries.append(
            {
                "heading": f"Visual asset {item.get('asset_id') or evidence_id}",
                "summary": summary,
                "evidence_sids": [evidence_id],
            }
        )
        findings.append(
            {
                "statement": summary,
                "conditions": f"visual asset on page {item.get('page')}" if item.get("page") else "visual asset",
                "evidence_sids": [evidence_id],
            }
        )
    if not section_summaries:
        return None
    return {
        "article_type_guess": "unclear",
        "section_summaries": section_summaries,
        "systems_overview": [],
        "method_map": [],
        "definitions": definitions,
        "major_findings": findings,
        "interpretations": [],
        "limitations": [],
        "cross_chunk_dependencies": [],
        "global_constraints": [],
    }


def batch_by_chars(items: list[dict[str, Any]], max_chars: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for item in items:
        size = len(json.dumps(item, ensure_ascii=False)) + 256
        if current and current_chars + size > max_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += size
    if current:
        batches.append(current)
    return batches


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a two-level whole-paper summary memory before detailed extraction.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    memory_dir = paths.get("summary_memory", root / "data/summary_memory")
    visual_dir = paths.get("visual_analysis", root / "data/visual_analysis")
    metadata_dir = paths.get("metadata", root / "data/metadata")
    memory_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_db(paths["database"])
    rows = select_papers(conn, parse_ids(args.ids), args.limit)

    chunk_schema_path = root / "schemas/v4/summary_chunk.schema.json"
    memory_schema_path = root / "schemas/v4/summary_memory.schema.json"
    chunk_prompt_path = root / "prompts/v4/summary_chunk_system.txt"
    merge_prompt_path = root / "prompts/v4/summary_merge_system.txt"
    chunk_schema = load_schema(chunk_schema_path)
    memory_schema = load_schema(memory_schema_path)
    chunk_system = chunk_prompt_path.read_text(encoding="utf-8")
    merge_system = merge_prompt_path.read_text(encoding="utf-8")

    cfg = config.get("summary_memory", {})
    chunk_max_chars = int(cfg.get("chunk_max_chars", config["llm"].get("chunk_max_chars", 12000)))
    merge_max_chars = int(cfg.get("merge_max_chars", 24000))
    merge_output_tokens = int(cfg.get("merge_max_tokens", config["llm"].get("max_tokens", 20000)))
    adaptive_cfg = cfg.get("adaptive_chunking") or config["llm"].get("adaptive_chunking", {}) or {}
    adaptive_enabled = bool(adaptive_cfg.get("enabled", True))
    adaptive_max_depth = int(adaptive_cfg.get("max_depth", 5))
    adaptive_min_chars = int(adaptive_cfg.get("min_chars", 1800))

    client = LlamaCppClient(config["llm"])
    model = client.healthcheck()
    print(f"LLM model: {model}")
    prompt_version = sha256_text(
        sha256_file(chunk_prompt_path) + sha256_file(merge_prompt_path)
    )[:16]
    schema_version = sha256_text(
        sha256_file(chunk_schema_path) + sha256_file(memory_schema_path)
    )[:16]
    signature = stable_json_hash(
        {
            "script": SCRIPT_VERSION,
            "model": model,
            "llm": {
                "temperature": config["llm"].get("temperature", 0.0),
                "max_tokens": config["llm"].get("max_tokens", 20000),
                "enable_thinking": config["llm"].get("enable_thinking", False),
            },
            "summary": cfg,
            "prompt": prompt_version,
            "schema": schema_version,
        }
    )

    for row in rows:
        paper_id = row["paper_id"]
        paper_path = paths["paper_json"] / f"{paper_id}.json"
        visual_path = visual_dir / f"{paper_id}.visual.json"
        metadata_path = metadata_dir / f"{paper_id}.metadata.json"
        out_path = memory_dir / f"{paper_id}.memory.json"
        if not paper_path.exists():
            print(f"WAIT    {paper_id}: paper JSON missing")
            continue

        hash_parts = [sha256_file(paper_path), signature]
        if visual_path.exists():
            hash_parts.append(sha256_file(visual_path))
        if metadata_path.exists():
            hash_parts.append(sha256_file(metadata_path))
        input_hash = sha256_text("".join(hash_parts))
        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} summary memory current")
            continue

        paper = read_json(paper_path)
        visual = read_json(visual_path) if visual_path.exists() else {}
        metadata = read_json(metadata_path) if metadata_path.exists() else {}
        canonical = metadata.get("canonical") or paper.get("metadata") or {}
        chunks = make_text_chunks(paper, max_chars=chunk_max_chars, include_abstract=True)
        print(f"MEMORY  {paper_id}: {len(chunks)} text chunks")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        stats = {"splits": 0, "leaf_chunks": 0, "merge_calls": 0}

        def process_summary_chunk(chunk: dict[str, Any], depth: int = 0) -> list[dict[str, Any]]:
            chunk_dir = paths["llm_raw"] / paper_id / "summary_memory" / "chunks"
            chunk_path = chunk_dir / f"{chunk['chunk_id']}.json"
            meta_path = chunk_dir / f"{chunk['chunk_id']}.meta.json"
            user_prompt = (
                f"PAPER_ID: {paper_id}\n"
                f"TITLE: {canonical.get('title') or ''}\n"
                f"YEAR: {canonical.get('year') or ''}\n"
                f"JOURNAL: {canonical.get('journal') or ''}\n"
                f"DOI: {canonical.get('doi') or ''}\n"
                f"CHUNK_ID: {chunk['chunk_id']}\n\n"
                "ARTICLE TEXT WITH STABLE EVIDENCE IDS:\n"
                + chunk["text"]
            )
            chunk_hash = sha256_text(user_prompt + prompt_version + schema_version + signature)

            if not args.force and meta_path.exists():
                meta = read_json(meta_path)
                if meta.get("input_hash") == chunk_hash and meta.get("adaptive_split") and adaptive_enabled:
                    children = split_chunk_adaptive(chunk)
                    if children is not None:
                        print(f"  SPLIT-CACHE {paper_id} {chunk['chunk_id']} memory depth={depth}")
                        out: list[dict[str, Any]] = []
                        for child in children:
                            out.extend(process_summary_chunk(child, depth + 1))
                        return out

            if not args.force and chunk_path.exists() and meta_path.exists():
                meta = read_json(meta_path)
                if meta.get("input_hash") == chunk_hash:
                    candidate = read_json(chunk_path)
                    if not validate_schema(candidate, chunk_schema):
                        stats["leaf_chunks"] += 1
                        print(f"  CACHE {paper_id} {chunk['chunk_id']} memory")
                        return [candidate]

            run_id = log_llm_run_start(
                conn, paper_id, STAGE, chunk["chunk_id"], prompt_version, schema_version, model, chunk_hash
            )
            try:
                result = client.chat_json(chunk_system, user_prompt, chunk_schema)
                write_json(chunk_path, result.data)
                write_json(
                    meta_path,
                    {
                        "input_hash": chunk_hash,
                        "paper_id": paper_id,
                        "chunk_id": chunk["chunk_id"],
                        "model": result.model,
                        "adaptive_depth": depth,
                    },
                )
                log_llm_run_finish(conn, run_id, "success", chunk_path)
                stats["leaf_chunks"] += 1
                return [result.data]
            except LLMOutputTruncatedError as error:
                log_llm_run_finish(conn, run_id, "truncated", error=str(error))
                if not adaptive_enabled or depth >= adaptive_max_depth or len(chunk["text"]) <= adaptive_min_chars:
                    raise
                children = split_chunk_adaptive(chunk)
                if children is None:
                    raise RuntimeError(f"could not split summary chunk {chunk['chunk_id']}") from error
                stats["splits"] += 1
                write_json(
                    meta_path,
                    {
                        "input_hash": chunk_hash,
                        "paper_id": paper_id,
                        "chunk_id": chunk["chunk_id"],
                        "adaptive_split": True,
                        "adaptive_depth": depth,
                        "children": [x["chunk_id"] for x in children],
                        "reason": "finish_reason=length",
                    },
                )
                print(
                    f"  SPLIT {paper_id} {chunk['chunk_id']} memory -> "
                    f"{children[0]['chunk_id']} ({len(children[0]['text'])}), "
                    f"{children[1]['chunk_id']} ({len(children[1]['text'])})"
                )
                out: list[dict[str, Any]] = []
                for child in children:
                    out.extend(process_summary_chunk(child, depth + 1))
                return out
            except Exception as error:
                log_llm_run_finish(conn, run_id, "error", error=str(error))
                raise

        def merge_group(items: list[dict[str, Any]], merge_id: str, depth: int = 0) -> dict[str, Any]:
            merge_dir = paths["llm_raw"] / paper_id / "summary_memory" / "merges"
            out = merge_dir / f"{merge_id}.json"
            meta = merge_dir / f"{merge_id}.meta.json"
            body = json.dumps(items, ensure_ascii=False, indent=2)
            user_prompt = (
                f"PAPER_ID: {paper_id}\nTITLE: {canonical.get('title') or ''}\n"
                f"MERGE_ID: {merge_id}\n\n"
                "EVIDENCE-LINKED CHUNK MEMORIES TO MERGE:\n" + body
            )
            merge_hash = sha256_text(user_prompt + prompt_version + schema_version + signature)
            if not args.force and out.exists() and meta.exists() and read_json(meta).get("input_hash") == merge_hash:
                candidate = read_json(out)
                if not validate_schema(candidate, memory_schema):
                    print(f"  MERGE-CACHE {paper_id} {merge_id}")
                    return candidate
            run_id = log_llm_run_start(
                conn, paper_id, STAGE, merge_id, prompt_version, schema_version, model, merge_hash
            )
            try:
                result = client.chat_json(
                    merge_system,
                    user_prompt,
                    memory_schema,
                    max_tokens=merge_output_tokens,
                )
                write_json(out, result.data)
                write_json(meta, {"input_hash": merge_hash, "items": len(items), "depth": depth})
                log_llm_run_finish(conn, run_id, "success", out)
                stats["merge_calls"] += 1
                return result.data
            except LLMOutputTruncatedError as error:
                log_llm_run_finish(conn, run_id, "truncated", error=str(error))
                if len(items) <= 1:
                    raise RuntimeError(
                        f"summary merge {merge_id} truncated even with one item; increase merge_max_tokens"
                    ) from error
                midpoint = max(1, len(items) // 2)
                left = merge_group(items[:midpoint], merge_id + "a", depth + 1)
                right = merge_group(items[midpoint:], merge_id + "b", depth + 1)
                return merge_group([left, right], merge_id + "c", depth + 1)
            except Exception as error:
                log_llm_run_finish(conn, run_id, "error", error=str(error))
                raise

        try:
            chunk_memories: list[dict[str, Any]] = []
            for chunk in chunks:
                chunk_memories.extend(process_summary_chunk(chunk))
            visual_memory = visual_chunk_memory(visual)
            if visual_memory:
                chunk_memories.append(visual_memory)

            current: list[dict[str, Any]] = []
            for index, group in enumerate(batch_by_chars(chunk_memories, merge_max_chars), start=1):
                current.append(merge_group(group, f"merge_l01_{index:04d}"))
            level = 2
            while len(current) > 1:
                next_level: list[dict[str, Any]] = []
                for index, group in enumerate(batch_by_chars(current, merge_max_chars), start=1):
                    next_level.append(merge_group(group, f"merge_l{level:02d}_{index:04d}"))
                if len(next_level) >= len(current) and len(current) > 1:
                    midpoint = max(1, len(current) // 2)
                    next_level = [
                        merge_group(current[:midpoint], f"merge_l{level:02d}_forced_a"),
                        merge_group(current[midpoint:], f"merge_l{level:02d}_forced_b"),
                    ]
                current = next_level
                level += 1
            if not current:
                raise RuntimeError("no chunk memories were generated")
            final_memory = current[0]
            payload = {
                "paper_id": paper_id,
                "profile": config["profile"],
                "canonical_metadata": canonical,
                "provenance": {
                    "model": model,
                    "script_version": SCRIPT_VERSION,
                    "prompt_version": prompt_version,
                    "schema_version": schema_version,
                    "source_text_chunks": len(chunks),
                    "adaptive_leaf_chunks": stats["leaf_chunks"],
                    "adaptive_splits": stats["splits"],
                    "merge_calls": stats["merge_calls"],
                    "visual_memory_included": bool(visual_memory),
                },
                **final_memory,
            }
            write_json(out_path, payload)
            set_stage(conn, paper_id, STAGE, "success", input_hash, out_path, meta=payload["provenance"])
        except Exception as error:
            set_stage(conn, paper_id, STAGE, "error", input_hash=input_hash, error=str(error))
            print(f"ERROR   {paper_id}: {error}")


if __name__ == "__main__":
    main()
