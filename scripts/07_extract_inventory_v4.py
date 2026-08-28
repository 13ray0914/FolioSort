#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    LLMOutputTruncatedError,
    LlamaCppClient,
    connect_db,
    dedupe_dicts,
    get_paths,
    load_config,
    load_schema,
    log_llm_run_finish,
    log_llm_run_start,
    make_text_chunks,
    normalize_key,
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
from lib.v4_common import compact_memory, make_visual_chunks, visual_context_text

STAGE = "extract_inventory_v4"
SCRIPT_VERSION = "inventory-v4.0-memory"


def merge_systems(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        key = normalize_key(item.get("normalized_name") or item.get("system_name_raw"))
        if not key:
            key = stable_json_hash(item)[:16]
        if key not in groups:
            groups[key] = json.loads(json.dumps(item))
            continue
        dst = groups[key]
        dst["evidence_sids"] = sorted(set(dst.get("evidence_sids", []) + item.get("evidence_sids", [])))
        if not dst.get("system_name_raw") and item.get("system_name_raw"):
            dst["system_name_raw"] = item["system_name_raw"]
        if not dst.get("system_type") and item.get("system_type"):
            dst["system_type"] = item["system_type"]
        attrs = dst.setdefault("attributes", {})
        for attr_key, value in item.get("attributes", {}).items():
            if isinstance(value, list):
                attrs[attr_key] = sorted(set((attrs.get(attr_key) or []) + value))
            elif attrs.get(attr_key) in (None, "") and value not in (None, ""):
                attrs[attr_key] = value
    out = list(groups.values())
    for index, item in enumerate(out, start=1):
        item["system_id"] = f"SYS{index:03d}"
    return out


def merge_outputs(outputs: list[dict[str, Any]]) -> dict[str, Any]:
    types = [o.get("article_type") for o in outputs if o.get("article_type") not in (None, "unclear")]
    article_type = Counter(types).most_common(1)[0][0] if types else "unclear"
    objectives = dedupe_dicts(
        [x for output in outputs for x in output.get("objectives", [])],
        lambda x: normalize_key(x.get("text")),
    )
    systems = merge_systems([x for output in outputs for x in output.get("systems", [])])
    methods = dedupe_dicts(
        [x for output in outputs for x in output.get("methods", [])],
        lambda x: (
            normalize_key(x.get("method_normalized") or x.get("method_raw")),
            normalize_key(x.get("target_property")),
        ),
    )
    properties = dedupe_dicts(
        [x for output in outputs for x in output.get("studied_properties", [])],
        lambda x: normalize_key(x.get("property_normalized") or x.get("property_raw")),
    )
    conditions = dedupe_dicts(
        [x for output in outputs for x in output.get("global_conditions", [])],
        lambda x: (normalize_key(x.get("condition_type")), normalize_key(x.get("value_raw"))),
    )
    return {
        "article_type": article_type,
        "objectives": objectives,
        "systems": systems,
        "methods": methods,
        "studied_properties": properties,
        "global_conditions": conditions,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract a paper inventory using whole-paper memory plus local chunks.")
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
    conn = connect_db(paths["database"])
    rows = select_papers(conn, parse_ids(args.ids), args.limit)

    profile_dir = root / "profiles" / config["profile"]
    schema_path = profile_dir / "inventory.schema.json"
    prompt_path = profile_dir / "prompts/inventory_v4_system.txt"
    schema = load_schema(schema_path)
    system_prompt = prompt_path.read_text(encoding="utf-8")
    prompt_version = sha256_file(prompt_path)[:16]
    schema_version = sha256_file(schema_path)[:16]

    client = LlamaCppClient(config["llm"])
    model = client.healthcheck()
    print(f"LLM model: {model}")
    memory_cfg = config.get("summary_memory", {})
    memory_max_chars = int(memory_cfg.get("memory_context_max_chars", 9000))
    visual_max_chars = int(config.get("visual", {}).get("extraction_context_max_chars", 10000))
    llm_signature = stable_json_hash(
        {
            "model": model,
            "temperature": config["llm"].get("temperature", 0.0),
            "enable_thinking": config["llm"].get("enable_thinking", False),
            "max_tokens": config["llm"].get("max_tokens", 20000),
            "script": SCRIPT_VERSION,
            "memory_max_chars": memory_max_chars,
        }
    )

    for row in rows:
        paper_id = row["paper_id"]
        paper_path = paths["paper_json"] / f"{paper_id}.json"
        memory_path = memory_dir / f"{paper_id}.memory.json"
        visual_path = visual_dir / f"{paper_id}.visual.json"
        metadata_path = metadata_dir / f"{paper_id}.metadata.json"
        out_path = paths["extracted"] / f"{paper_id}.inventory.json"
        if not paper_path.exists() or not memory_path.exists():
            print(f"WAIT    {paper_id}: paper JSON or summary memory missing")
            continue

        hash_parts = [
            sha256_file(paper_path),
            sha256_file(memory_path),
            prompt_version,
            schema_version,
            llm_signature,
        ]
        if visual_path.exists():
            hash_parts.append(sha256_file(visual_path))
        if metadata_path.exists():
            hash_parts.append(sha256_file(metadata_path))
        input_hash = sha256_text("".join(hash_parts))
        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} inventory v4 current")
            continue

        paper = read_json(paper_path)
        memory = read_json(memory_path)
        visual = read_json(visual_path) if visual_path.exists() else {}
        metadata = read_json(metadata_path) if metadata_path.exists() else {}
        canonical = metadata.get("canonical") or paper.get("metadata") or {}
        memory_text = compact_memory(memory, memory_max_chars)
        visual_text = visual_context_text(visual, visual_max_chars)
        chunks = make_text_chunks(
            paper,
            max_chars=int(config["llm"].get("chunk_max_chars", 12000)),
            include_abstract=True,
        )
        chunks.extend(make_visual_chunks(visual, max_chars=int(config.get("visual", {}).get("visual_chunk_max_chars", 8000))))
        print(f"INV4    {paper_id}: {len(chunks)} chunks")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        adaptive_cfg = config["llm"].get("adaptive_chunking", {}) or {}
        adaptive_enabled = bool(adaptive_cfg.get("enabled", True))
        adaptive_max_depth = int(adaptive_cfg.get("max_depth", 5))
        adaptive_min_chars = int(adaptive_cfg.get("min_chars", 1800))
        stats = {"splits": 0, "leaf_chunks": 0}
        outputs: list[dict[str, Any]] = []

        def process_chunk(chunk: dict[str, Any], depth: int = 0) -> list[dict[str, Any]]:
            chunk_dir = paths["llm_raw"] / paper_id / "inventory_v4"
            chunk_path = chunk_dir / f"{chunk['chunk_id']}.json"
            meta_path = chunk_dir / f"{chunk['chunk_id']}.meta.json"
            user_prompt = (
                f"PAPER_ID: {paper_id}\n"
                f"TITLE: {canonical.get('title') or ''}\n"
                f"DOI: {canonical.get('doi') or ''}\n"
                f"JOURNAL: {canonical.get('journal') or ''}\n"
                f"YEAR: {canonical.get('year') or ''}\n"
                f"CHUNK_ID: {chunk['chunk_id']}\n\n"
                "WHOLE-PAPER MEMORY (orientation plus original evidence IDs):\n"
                + memory_text
                + ("\n\nGLOBAL VISUAL EVIDENCE INDEX:\n" + visual_text if visual_text else "")
                + "\n\nCURRENT EVIDENCE CHUNK:\n"
                + chunk["text"]
            )
            chunk_hash = sha256_text(user_prompt + prompt_version + schema_version + llm_signature)

            if not args.force and meta_path.exists():
                meta = read_json(meta_path)
                if meta.get("input_hash") == chunk_hash and meta.get("adaptive_split") and adaptive_enabled:
                    children = split_chunk_adaptive(chunk)
                    if children is not None:
                        print(f"  SPLIT-CACHE {paper_id} {chunk['chunk_id']} inv4 depth={depth}")
                        result: list[dict[str, Any]] = []
                        for child in children:
                            result.extend(process_chunk(child, depth + 1))
                        return result
            if not args.force and chunk_path.exists() and meta_path.exists():
                meta = read_json(meta_path)
                if meta.get("input_hash") == chunk_hash:
                    candidate = read_json(chunk_path)
                    if not validate_schema(candidate, schema):
                        stats["leaf_chunks"] += 1
                        print(f"  CACHE {paper_id} {chunk['chunk_id']} inv4")
                        return [candidate]

            run_id = log_llm_run_start(
                conn, paper_id, STAGE, chunk["chunk_id"], prompt_version, schema_version, model, chunk_hash
            )
            try:
                result = client.chat_json(system_prompt, user_prompt, schema)
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
                    raise RuntimeError(f"could not split truncated inventory chunk {chunk['chunk_id']}") from error
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
                    },
                )
                print(
                    f"  SPLIT {paper_id} {chunk['chunk_id']} inv4 -> "
                    f"{children[0]['chunk_id']}, {children[1]['chunk_id']}"
                )
                result: list[dict[str, Any]] = []
                for child in children:
                    result.extend(process_chunk(child, depth + 1))
                return result
            except Exception as error:
                log_llm_run_finish(conn, run_id, "error", error=str(error))
                raise

        try:
            for chunk in chunks:
                outputs.extend(process_chunk(chunk))
            merged = merge_outputs(outputs)
            final = {
                "paper_id": paper_id,
                "profile": config["profile"],
                "source_metadata": canonical,
                "provenance": {
                    "model": model,
                    "prompt_version": prompt_version,
                    "schema_version": schema_version,
                    "script_version": SCRIPT_VERSION,
                    "chunks": len(chunks),
                    "adaptive_leaf_chunks": stats["leaf_chunks"],
                    "adaptive_splits": stats["splits"],
                    "summary_memory": str(memory_path.relative_to(root)),
                    "visual_context": bool(visual),
                },
                **merged,
            }
            write_json(out_path, final)
            conn.execute("UPDATE papers SET article_type=? WHERE paper_id=?", (merged["article_type"], paper_id))
            conn.commit()
            set_stage(conn, paper_id, STAGE, "success", input_hash, out_path, meta=final["provenance"])
        except Exception as error:
            set_stage(conn, paper_id, STAGE, "error", input_hash=input_hash, error=str(error))
            print(f"ERROR   {paper_id}: {error}")


if __name__ == "__main__":
    main()
