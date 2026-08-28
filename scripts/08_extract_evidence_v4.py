#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
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

STAGE = "extract_evidence_v4"
SCRIPT_VERSION = "evidence-v4.0-memory-visual"


def compact_inventory(inventory: dict[str, Any]) -> str:
    lines = [f"ARTICLE_TYPE: {inventory.get('article_type', 'unclear')}", "KNOWN SYSTEMS:"]
    for system in inventory.get("systems", []):
        attrs = system.get("attributes", {})
        descriptors = []
        for key, value in attrs.items():
            if value not in (None, "", []):
                descriptors.append(f"{key}={value}")
        lines.append(
            f"- {system.get('system_id')}: {system.get('system_name_raw')} | "
            f"normalized={system.get('normalized_name')}"
            + (" | " + "; ".join(descriptors) if descriptors else "")
        )
    return "\n".join(lines)


def merge_outputs(outputs: list[dict[str, Any]]) -> dict[str, Any]:
    measurements = dedupe_dicts(
        [x for output in outputs for x in output.get("measurements", [])],
        lambda x: (
            normalize_key(x.get("property_normalized") or x.get("property_raw")),
            normalize_key(x.get("value_raw")),
            tuple(sorted(x.get("evidence_sids", []))),
        ),
    )
    claims = dedupe_dicts(
        [x for output in outputs for x in output.get("claims", [])],
        lambda x: (normalize_key(x.get("statement")), tuple(sorted(x.get("evidence_sids", [])))),
    )
    limitations = dedupe_dicts(
        [x for output in outputs for x in output.get("limitations", [])],
        lambda x: (normalize_key(x.get("statement")), tuple(sorted(x.get("evidence_sids", [])))),
    )
    citation_contexts = dedupe_dicts(
        [x for output in outputs for x in output.get("citation_contexts", [])],
        lambda x: (
            x.get("evidence_sid"),
            tuple(sorted(x.get("cited_ref_ids", []))),
            x.get("rhetorical_role"),
        ),
    )
    for index, item in enumerate(measurements, start=1):
        item["measurement_id"] = f"M{index:04d}"
    for index, item in enumerate(claims, start=1):
        item["claim_id"] = f"C{index:04d}"
    for index, item in enumerate(limitations, start=1):
        item["limitation_id"] = f"L{index:04d}"
    for index, item in enumerate(citation_contexts, start=1):
        item["citation_context_id"] = f"CIT{index:04d}"
    return {
        "measurements": measurements,
        "claims": claims,
        "limitations": limitations,
        "citation_contexts": citation_contexts,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract detailed evidence using whole-paper memory and visual evidence.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    memory_dir = paths.get("summary_memory", root / "data/summary_memory")
    visual_dir = paths.get("visual_analysis", root / "data/visual_analysis")
    conn = connect_db(paths["database"])
    rows = select_papers(conn, parse_ids(args.ids), args.limit)

    profile_dir = root / "profiles" / config["profile"]
    schema_path = profile_dir / "evidence.schema.json"
    prompt_path = profile_dir / "prompts/evidence_v4_system.txt"
    schema = load_schema(schema_path)
    system_prompt = prompt_path.read_text(encoding="utf-8")
    prompt_version = sha256_file(prompt_path)[:16]
    schema_version = sha256_file(schema_path)[:16]

    client = LlamaCppClient(config["llm"])
    model = client.healthcheck()
    print(f"LLM model: {model}")
    memory_max_chars = int(config.get("summary_memory", {}).get("memory_context_max_chars", 9000))
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
        inventory_path = paths["extracted"] / f"{paper_id}.inventory.json"
        visual_path = visual_dir / f"{paper_id}.visual.json"
        out_path = paths["extracted"] / f"{paper_id}.evidence.json"
        if not paper_path.exists() or not memory_path.exists() or not inventory_path.exists():
            print(f"WAIT    {paper_id}: paper JSON, summary memory, or inventory missing")
            continue

        hash_parts = [
            sha256_file(paper_path),
            sha256_file(memory_path),
            sha256_file(inventory_path),
            prompt_version,
            schema_version,
            llm_signature,
        ]
        if visual_path.exists():
            hash_parts.append(sha256_file(visual_path))
        input_hash = sha256_text("".join(hash_parts))
        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} evidence v4 current")
            continue

        paper = read_json(paper_path)
        memory = read_json(memory_path)
        inventory = read_json(inventory_path)
        visual = read_json(visual_path) if visual_path.exists() else {}
        memory_text = compact_memory(memory, memory_max_chars)
        inventory_text = compact_inventory(inventory)
        visual_text = visual_context_text(visual, visual_max_chars)
        article_type = inventory.get("article_type", "unclear")
        chunks = make_text_chunks(
            paper,
            max_chars=int(config["llm"].get("chunk_max_chars", 12000)),
            include_abstract=True,
        )
        chunks.extend(make_visual_chunks(visual, max_chars=int(config.get("visual", {}).get("visual_chunk_max_chars", 8000))))
        print(f"EVID4   {paper_id}: {len(chunks)} chunks")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        adaptive_cfg = config["llm"].get("adaptive_chunking", {}) or {}
        adaptive_enabled = bool(adaptive_cfg.get("enabled", True))
        adaptive_max_depth = int(adaptive_cfg.get("max_depth", 5))
        adaptive_min_chars = int(adaptive_cfg.get("min_chars", 1800))
        stats = {"splits": 0, "leaf_chunks": 0}
        outputs: list[dict[str, Any]] = []

        def process_chunk(chunk: dict[str, Any], depth: int = 0) -> list[dict[str, Any]]:
            roles = {x.get("role") for x in chunk.get("sections", [])}
            is_visual = str(chunk.get("chunk_id", "")).startswith("visual_")
            citations_only = article_type == "primary_research" and roles and roles.issubset({"background"})
            if is_visual:
                mode = "VISUAL_EVIDENCE"
            else:
                mode = "CITATIONS_ONLY" if citations_only else "EVIDENCE_AND_CITATIONS"

            chunk_dir = paths["llm_raw"] / paper_id / "evidence_v4"
            chunk_path = chunk_dir / f"{chunk['chunk_id']}.json"
            meta_path = chunk_dir / f"{chunk['chunk_id']}.meta.json"
            user_prompt = (
                f"PAPER_ID: {paper_id}\nMODE: {mode}\nARTICLE_TYPE: {article_type}\n\n"
                "WHOLE-PAPER MEMORY (orientation plus original evidence IDs):\n"
                + memory_text
                + "\n\nSYSTEM INVENTORY:\n"
                + inventory_text
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
                        print(f"  SPLIT-CACHE {paper_id} {chunk['chunk_id']} {mode} depth={depth}")
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
                        print(f"  CACHE {paper_id} {chunk['chunk_id']} {mode}")
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
                        "mode": mode,
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
                    raise RuntimeError(f"could not split truncated evidence chunk {chunk['chunk_id']}") from error
                stats["splits"] += 1
                write_json(
                    meta_path,
                    {
                        "input_hash": chunk_hash,
                        "paper_id": paper_id,
                        "chunk_id": chunk["chunk_id"],
                        "mode": mode,
                        "adaptive_split": True,
                        "adaptive_depth": depth,
                        "children": [x["chunk_id"] for x in children],
                    },
                )
                print(
                    f"  SPLIT {paper_id} {chunk['chunk_id']} {mode} -> "
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
                "article_type": article_type,
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
            set_stage(
                conn,
                paper_id,
                STAGE,
                "success",
                input_hash,
                out_path,
                meta={
                    **final["provenance"],
                    "measurements": len(merged["measurements"]),
                    "claims": len(merged["claims"]),
                    "citation_contexts": len(merged["citation_contexts"]),
                },
            )
        except Exception as error:
            set_stage(conn, paper_id, STAGE, "error", input_hash=input_hash, error=str(error))
            print(f"ERROR   {paper_id}: {error}")


if __name__ == "__main__":
    main()
