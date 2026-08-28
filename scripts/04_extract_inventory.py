#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    LlamaCppClient,
    LLMOutputTruncatedError,
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

STAGE = "extract_inventory"
SCRIPT_VERSION = "inventory-v1.2"


def merge_systems(items: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
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
        for k, v in item.get("attributes", {}).items():
            if isinstance(v, list):
                attrs[k] = sorted(set((attrs.get(k) or []) + v))
            elif attrs.get(k) in (None, "") and v not in (None, ""):
                attrs[k] = v
    out = list(groups.values())
    for i, item in enumerate(out, start=1):
        item["system_id"] = f"SYS{i:03d}"
    return out


def merge_outputs(outputs: list[dict]) -> dict:
    types = [o.get("article_type") for o in outputs if o.get("article_type") not in (None, "unclear")]
    article_type = Counter(types).most_common(1)[0][0] if types else "unclear"

    objectives = dedupe_dicts(
        [x for o in outputs for x in o.get("objectives", [])],
        lambda x: normalize_key(x.get("text")),
    )
    systems = merge_systems([x for o in outputs for x in o.get("systems", [])])

    methods_raw = [x for o in outputs for x in o.get("methods", [])]
    methods = dedupe_dicts(
        methods_raw,
        lambda x: (normalize_key(x.get("method_normalized") or x.get("method_raw")), normalize_key(x.get("target_property"))),
    )
    properties = dedupe_dicts(
        [x for o in outputs for x in o.get("studied_properties", [])],
        lambda x: normalize_key(x.get("property_normalized") or x.get("property_raw")),
    )
    conditions = dedupe_dicts(
        [x for o in outputs for x in o.get("global_conditions", [])],
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
    ap = argparse.ArgumentParser(description="Use local llama.cpp/Qwen to extract a paper inventory.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    conn = connect_db(paths["database"])
    rows = select_papers(conn, parse_ids(args.ids), args.limit)

    profile_dir = root / "profiles" / config["profile"]
    schema_path = profile_dir / "inventory.schema.json"
    prompt_path = profile_dir / "prompts" / "inventory_system.txt"
    schema = load_schema(schema_path)
    system_prompt = prompt_path.read_text(encoding="utf-8")
    prompt_version = sha256_file(prompt_path)[:16]
    schema_version = sha256_file(schema_path)[:16]

    client = LlamaCppClient(config["llm"])
    model = client.healthcheck()
    print(f"LLM model: {model}")

    llm_signature = stable_json_hash(
        {
            "model": model,
            "temperature": config["llm"].get("temperature", 0.0),
            "enable_thinking": config["llm"].get("enable_thinking", False),
            "max_tokens": config["llm"].get("max_tokens", 8192),
            "script": SCRIPT_VERSION,
        }
    )

    for row in rows:
        paper_id = row["paper_id"]
        paper_path = paths["paper_json"] / f"{paper_id}.json"
        out_path = paths["extracted"] / f"{paper_id}.inventory.json"
        if not paper_path.exists():
            print(f"WAIT    {paper_id}: paper JSON missing")
            continue

        input_hash = sha256_text(
            sha256_file(paper_path) + prompt_version + schema_version + llm_signature
        )
        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} inventory current")
            continue

        paper = read_json(paper_path)
        chunks = make_text_chunks(
            paper,
            max_chars=int(config["llm"].get("chunk_max_chars", 30000)),
            include_abstract=True,
        )
        print(f"INV     {paper_id}: {len(chunks)} chunks")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        chunk_outputs = []
        adaptive_cfg = config["llm"].get("adaptive_chunking", {}) or {}
        adaptive_enabled = bool(adaptive_cfg.get("enabled", True))
        adaptive_max_depth = int(adaptive_cfg.get("max_depth", 5))
        adaptive_min_chars = int(adaptive_cfg.get("min_chars", 1800))
        adaptive_stats = {"splits": 0, "leaf_chunks": 0}

        def process_chunk(chunk: dict, depth: int = 0) -> list[dict]:
            chunk_dir = paths["llm_raw"] / paper_id / "inventory"
            chunk_path = chunk_dir / f"{chunk['chunk_id']}.json"
            meta_path = chunk_dir / f"{chunk['chunk_id']}.meta.json"
            user_prompt = (
                f"PAPER_ID: {paper_id}\n"
                f"TITLE: {paper.get('metadata', {}).get('title') or ''}\n"
                f"DOI: {paper.get('metadata', {}).get('doi') or ''}\n"
                f"JOURNAL: {paper.get('metadata', {}).get('journal') or ''}\n"
                f"YEAR: {paper.get('metadata', {}).get('year') or ''}\n"
                f"CHUNK_ID: {chunk['chunk_id']}\n\n"
                "ARTICLE TEXT WITH STABLE SENTENCE IDS:\n"
                + chunk["text"]
            )
            chunk_hash = sha256_text(user_prompt + prompt_version + schema_version + llm_signature)

            # Resume a previously chosen adaptive split without repeating the
            # known-too-large parent request. Child IDs are deterministic.
            if not args.force and meta_path.exists():
                meta = read_json(meta_path)
                if meta.get("input_hash") == chunk_hash and meta.get("adaptive_split") and adaptive_enabled:
                    children = split_chunk_adaptive(chunk)
                    if children is not None:
                        print(f"  SPLIT-CACHE {paper_id} {chunk['chunk_id']} depth={depth}")
                        out: list[dict] = []
                        for child in children:
                            out.extend(process_chunk(child, depth + 1))
                        return out

            if not args.force and chunk_path.exists() and meta_path.exists():
                meta = read_json(meta_path)
                if meta.get("input_hash") == chunk_hash:
                    candidate = read_json(chunk_path)
                    if not validate_schema(candidate, schema):
                        adaptive_stats["leaf_chunks"] += 1
                        print(f"  CACHE {paper_id} {chunk['chunk_id']}")
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
                        "prompt_version": prompt_version,
                        "schema_version": schema_version,
                        "adaptive_depth": depth,
                    },
                )
                log_llm_run_finish(conn, run_id, "success", chunk_path)
                adaptive_stats["leaf_chunks"] += 1
                return [result.data]
            except LLMOutputTruncatedError as e:
                log_llm_run_finish(conn, run_id, "truncated", error=str(e))
                if not adaptive_enabled:
                    raise
                if depth >= adaptive_max_depth:
                    raise RuntimeError(
                        f"adaptive chunking reached max_depth={adaptive_max_depth} at {chunk['chunk_id']} "
                        f"({len(chunk['text'])} chars)"
                    ) from e
                if len(chunk["text"]) <= adaptive_min_chars:
                    raise RuntimeError(
                        f"adaptive chunking cannot shrink {chunk['chunk_id']} below min_chars="
                        f"{adaptive_min_chars}; current={len(chunk['text'])}"
                    ) from e
                children = split_chunk_adaptive(chunk)
                if children is None:
                    raise RuntimeError(f"could not split truncated chunk {chunk['chunk_id']}") from e
                adaptive_stats["splits"] += 1
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
                    f"  SPLIT {paper_id} {chunk['chunk_id']} -> "
                    f"{children[0]['chunk_id']} ({len(children[0]['text'])} chars), "
                    f"{children[1]['chunk_id']} ({len(children[1]['text'])} chars)"
                )
                out: list[dict] = []
                for child in children:
                    out.extend(process_chunk(child, depth + 1))
                return out
            except Exception as e:
                log_llm_run_finish(conn, run_id, "error", error=str(e))
                raise

        try:
            for chunk in chunks:
                chunk_outputs.extend(process_chunk(chunk))

            merged = merge_outputs(chunk_outputs)
            final = {
                "paper_id": paper_id,
                "profile": config["profile"],
                "source_metadata": paper.get("metadata", {}),
                "provenance": {
                    "model": model,
                    "prompt_version": prompt_version,
                    "schema_version": schema_version,
                    "script_version": SCRIPT_VERSION,
                    "chunks": len(chunks),
                    "adaptive_leaf_chunks": adaptive_stats["leaf_chunks"],
                    "adaptive_splits": adaptive_stats["splits"],
                },
                **merged,
            }
            write_json(out_path, final)
            conn.execute("UPDATE papers SET article_type=? WHERE paper_id=?", (merged["article_type"], paper_id))
            conn.commit()
            set_stage(
                conn, paper_id, STAGE, "success", input_hash, out_path,
                meta={
                    "chunks": len(chunks),
                    "adaptive_leaf_chunks": adaptive_stats["leaf_chunks"],
                    "adaptive_splits": adaptive_stats["splits"],
                },
            )
        except Exception as e:
            set_stage(conn, paper_id, STAGE, "error", input_hash=input_hash, error=str(e))
            print(f"ERROR   {paper_id}: {e}")


if __name__ == "__main__":
    main()
