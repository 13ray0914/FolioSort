#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
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

STAGE = "extract_evidence"
SCRIPT_VERSION = "evidence-v1.2"


def compact_inventory(inventory: dict) -> str:
    lines = [f"ARTICLE_TYPE: {inventory.get('article_type', 'unclear')}", "KNOWN SYSTEMS:"]
    for s in inventory.get("systems", []):
        attrs = s.get("attributes", {})
        descriptors = []
        for key, val in attrs.items():
            if val not in (None, "", []):
                descriptors.append(f"{key}={val}")
        lines.append(
            f"- {s.get('system_id')}: {s.get('system_name_raw')} | normalized={s.get('normalized_name')}"
            + (" | " + "; ".join(descriptors) if descriptors else "")
        )
    return "\n".join(lines)


def merge_outputs(outputs: list[dict]) -> dict:
    measurements = dedupe_dicts(
        [x for o in outputs for x in o.get("measurements", [])],
        lambda x: (
            normalize_key(x.get("property_normalized") or x.get("property_raw")),
            normalize_key(x.get("value_raw")),
            tuple(sorted(x.get("evidence_sids", []))),
        ),
    )
    claims = dedupe_dicts(
        [x for o in outputs for x in o.get("claims", [])],
        lambda x: (normalize_key(x.get("statement")), tuple(sorted(x.get("evidence_sids", [])))),
    )
    limitations = dedupe_dicts(
        [x for o in outputs for x in o.get("limitations", [])],
        lambda x: (normalize_key(x.get("statement")), tuple(sorted(x.get("evidence_sids", [])))),
    )
    citation_contexts = dedupe_dicts(
        [x for o in outputs for x in o.get("citation_contexts", [])],
        lambda x: (
            x.get("evidence_sid"),
            tuple(sorted(x.get("cited_ref_ids", []))),
            x.get("rhetorical_role"),
        ),
    )
    for i, x in enumerate(measurements, start=1):
        x["measurement_id"] = f"M{i:04d}"
    for i, x in enumerate(claims, start=1):
        x["claim_id"] = f"C{i:04d}"
    for i, x in enumerate(limitations, start=1):
        x["limitation_id"] = f"L{i:04d}"
    for i, x in enumerate(citation_contexts, start=1):
        x["citation_context_id"] = f"CIT{i:04d}"
    return {
        "measurements": measurements,
        "claims": claims,
        "limitations": limitations,
        "citation_contexts": citation_contexts,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract measurements, atomic claims, limitations, and citation contexts.")
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
    schema_path = profile_dir / "evidence.schema.json"
    prompt_path = profile_dir / "prompts" / "evidence_system.txt"
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
        inventory_path = paths["extracted"] / f"{paper_id}.inventory.json"
        out_path = paths["extracted"] / f"{paper_id}.evidence.json"
        if not paper_path.exists() or not inventory_path.exists():
            print(f"WAIT    {paper_id}: paper JSON or inventory missing")
            continue

        input_hash = sha256_text(
            sha256_file(paper_path)
            + sha256_file(inventory_path)
            + prompt_version
            + schema_version
            + llm_signature
        )
        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} evidence current")
            continue

        paper = read_json(paper_path)
        inventory = read_json(inventory_path)
        inventory_text = compact_inventory(inventory)
        article_type = inventory.get("article_type", "unclear")
        chunks = make_text_chunks(
            paper,
            max_chars=int(config["llm"].get("chunk_max_chars", 30000)),
            include_abstract=True,
        )
        print(f"EVID    {paper_id}: {len(chunks)} chunks")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        chunk_outputs = []
        adaptive_cfg = config["llm"].get("adaptive_chunking", {}) or {}
        adaptive_enabled = bool(adaptive_cfg.get("enabled", True))
        adaptive_max_depth = int(adaptive_cfg.get("max_depth", 5))
        adaptive_min_chars = int(adaptive_cfg.get("min_chars", 1800))
        adaptive_stats = {"splits": 0, "leaf_chunks": 0}

        def process_chunk(chunk: dict, depth: int = 0) -> list[dict]:
            roles = {x.get("role") for x in chunk.get("sections", [])}
            citations_only = article_type == "primary_research" and roles and roles.issubset({"background"})
            mode = "CITATIONS_ONLY" if citations_only else "EVIDENCE_AND_CITATIONS"

            chunk_dir = paths["llm_raw"] / paper_id / "evidence"
            chunk_path = chunk_dir / f"{chunk['chunk_id']}.json"
            meta_path = chunk_dir / f"{chunk['chunk_id']}.meta.json"
            user_prompt = (
                f"PAPER_ID: {paper_id}\n"
                f"MODE: {mode}\n"
                f"ARTICLE_TYPE: {article_type}\n\n"
                f"{inventory_text}\n\n"
                "ARTICLE TEXT WITH STABLE SENTENCE IDS:\n"
                + chunk["text"]
            )
            chunk_hash = sha256_text(user_prompt + prompt_version + schema_version + llm_signature)

            # If a previous run already discovered that this exact parent chunk
            # is too output-dense, immediately recurse into its deterministic
            # children instead of paying for another truncated generation.
            if not args.force and meta_path.exists():
                meta = read_json(meta_path)
                if meta.get("input_hash") == chunk_hash and meta.get("adaptive_split") and adaptive_enabled:
                    children = split_chunk_adaptive(chunk)
                    if children is not None:
                        print(f"  SPLIT-CACHE {paper_id} {chunk['chunk_id']} {mode} depth={depth}")
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
                        "mode": mode,
                        "adaptive_split": True,
                        "adaptive_depth": depth,
                        "children": [x["chunk_id"] for x in children],
                        "reason": "finish_reason=length",
                    },
                )
                print(
                    f"  SPLIT {paper_id} {chunk['chunk_id']} {mode} -> "
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
                "article_type": article_type,
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
            set_stage(
                conn,
                paper_id,
                STAGE,
                "success",
                input_hash,
                out_path,
                meta={
                    "chunks": len(chunks),
                    "adaptive_leaf_chunks": adaptive_stats["leaf_chunks"],
                    "adaptive_splits": adaptive_stats["splits"],
                    "measurements": len(merged["measurements"]),
                    "claims": len(merged["claims"]),
                    "citation_contexts": len(merged["citation_contexts"]),
                },
            )
        except Exception as e:
            set_stage(conn, paper_id, STAGE, "error", input_hash=input_hash, error=str(e))
            print(f"ERROR   {paper_id}: {e}")


if __name__ == "__main__":
    main()
