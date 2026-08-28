#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
import time

import requests
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
SCRIPT_VERSION = "summary-memory-v4.0.2-direct-retry"


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


def count_tokens_for_llama(llm_cfg: dict[str, Any], text: str) -> tuple[int, str]:
    """Count tokens with llama.cpp /tokenize; fall back conservatively if unavailable."""
    base_url = str(llm_cfg["base_url"]).rstrip("/")
    root_url = base_url[:-3] if base_url.endswith("/v1") else base_url
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {llm_cfg.get('api_key', 'no-key')}",
    }
    try:
        response = requests.post(
            f"{root_url}/tokenize",
            headers=headers,
            json={"content": text},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        tokens = payload.get("tokens")
        if isinstance(tokens, list):
            return len(tokens), "exact"
        count = payload.get("count")
        if isinstance(count, int):
            return count, "exact"
    except Exception:
        pass

    # Scientific text often tokenizes less efficiently than ordinary prose.
    # 3 chars/token is intentionally conservative for routing decisions.
    return max(1, math.ceil(len(text) / 3.0)), "estimated"


def schema_augmented_system(system_prompt: str, schema: dict[str, Any]) -> str:
    """Mirror LlamaCppClient.chat_json's schema instruction for routing token counts."""
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return (
        system_prompt.rstrip()
        + "\n\nYou MUST return exactly one JSON value matching this schema. "
          "Do not add keys that are absent from the schema.\nJSON_SCHEMA:\n"
        + schema_text
    )


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
    direct_prompt_path = root / "prompts/v4/summary_direct_system.txt"
    chunk_schema = load_schema(chunk_schema_path)
    memory_schema = load_schema(memory_schema_path)
    chunk_system = chunk_prompt_path.read_text(encoding="utf-8")
    merge_system = merge_prompt_path.read_text(encoding="utf-8")
    direct_system = direct_prompt_path.read_text(encoding="utf-8")

    cfg = config.get("summary_memory", {})
    chunk_max_chars = int(cfg.get("chunk_max_chars", config["llm"].get("chunk_max_chars", 12000)))
    merge_max_chars = int(cfg.get("merge_max_chars", 24000))
    merge_output_tokens = int(cfg.get("merge_max_tokens", config["llm"].get("max_tokens", 20000)))
    direct_enabled = bool(cfg.get("direct_enabled", True))
    direct_max_input_tokens = int(cfg.get("direct_max_input_tokens", 40000))
    direct_max_output_tokens = int(cfg.get("direct_max_output_tokens", 4000))
    direct_retry_enabled = bool(cfg.get("direct_retry_enabled", True))
    direct_retry_output_tokens = int(cfg.get("direct_retry_output_tokens", 6000))
    adaptive_cfg = cfg.get("adaptive_chunking") or config["llm"].get("adaptive_chunking", {}) or {}
    adaptive_enabled = bool(adaptive_cfg.get("enabled", True))
    adaptive_max_depth = int(adaptive_cfg.get("max_depth", 5))
    adaptive_min_chars = int(adaptive_cfg.get("min_chars", 1800))

    client = LlamaCppClient(config["llm"])
    model = client.healthcheck()
    print(f"LLM model: {model}")
    prompt_version = sha256_text(
        sha256_file(chunk_prompt_path)
        + sha256_file(merge_prompt_path)
        + sha256_file(direct_prompt_path)
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
        visual_memory = visual_chunk_memory(visual)
        whole_text = "\n\n".join(chunk["text"] for chunk in chunks)
        visual_context = (
            json.dumps(visual_memory, ensure_ascii=False, separators=(",", ":"))
            if visual_memory else "none"
        )
        direct_user_prompt = (
            f"PAPER_ID: {paper_id}\n"
            f"TITLE: {canonical.get('title') or ''}\n"
            f"YEAR: {canonical.get('year') or ''}\n"
            f"JOURNAL: {canonical.get('journal') or ''}\n"
            f"DOI: {canonical.get('doi') or ''}\n\n"
            "ARTICLE TEXT WITH STABLE EVIDENCE IDS:\n"
            + whole_text
            + "\n\nSTRUCTURED VISUAL EVIDENCE SUMMARY (may be none):\n"
            + visual_context
        )
        direct_count_text = (
            schema_augmented_system(direct_system, memory_schema)
            + "\n\n"
            + direct_user_prompt
        )
        direct_input_tokens, token_count_source = count_tokens_for_llama(
            config["llm"], direct_count_text
        )
        route = (
            "direct"
            if direct_enabled and direct_input_tokens <= direct_max_input_tokens
            else "hierarchical"
        )
        print(
            f"MEMORY  {paper_id}: {len(chunks)} text chunks | "
            f"direct_input={direct_input_tokens} tokens ({token_count_source}) | route={route}"
        )
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        stats = {
            "splits": 0,
            "leaf_chunks": 0,
            "merge_calls": 0,
            "direct_calls": 0,
            "direct_attempts": 0,
            "direct_retries": 0,
            "direct_cache_hits": 0,
        }

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
                started = time.monotonic()
                print(
                    f"  CHUNK-START {paper_id} {chunk['chunk_id']} "
                    f"depth={depth} chars={len(chunk['text'])}",
                    flush=True,
                )
                result = client.chat_json(chunk_system, user_prompt, chunk_schema)
                elapsed = time.monotonic() - started
                print(
                    f"  CHUNK-DONE  {paper_id} {chunk['chunk_id']} "
                    f"{elapsed:.1f}s",
                    flush=True,
                )
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
            started = time.monotonic()
            print(
                f"  MERGE-START {paper_id} {merge_id} items={len(items)} depth={depth}",
                flush=True,
            )
            try:
                result = client.chat_json(
                    merge_system,
                    user_prompt,
                    memory_schema,
                    max_tokens=merge_output_tokens,
                )
                elapsed = time.monotonic() - started
                write_json(out, result.data)
                write_json(meta, {"input_hash": merge_hash, "items": len(items), "depth": depth})
                log_llm_run_finish(conn, run_id, "success", out)
                stats["merge_calls"] += 1
                print(
                    f"  MERGE-DONE  {paper_id} {merge_id} {elapsed:.1f}s",
                    flush=True,
                )
                return result.data
            except LLMOutputTruncatedError as error:
                elapsed = time.monotonic() - started
                log_llm_run_finish(conn, run_id, "truncated", error=str(error))
                print(
                    f"  MERGE-TRUNCATED {paper_id} {merge_id} {elapsed:.1f}s",
                    flush=True,
                )
                if len(items) <= 1:
                    raise RuntimeError(
                        f"summary merge {merge_id} truncated even with one item; increase merge_max_tokens"
                    ) from error
                midpoint = max(1, len(items) // 2)
                left = merge_group(items[:midpoint], merge_id + "a", depth + 1)
                right = merge_group(items[midpoint:], merge_id + "b", depth + 1)
                return merge_group([left, right], merge_id + "c", depth + 1)
            except Exception as error:
                elapsed = time.monotonic() - started
                log_llm_run_finish(conn, run_id, "error", error=str(error))
                print(
                    f"  MERGE-ERROR {paper_id} {merge_id} {elapsed:.1f}s: "
                    f"{type(error).__name__}: {error}",
                    flush=True,
                )
                raise

        try:
            final_memory: dict[str, Any] | None = None
            if route == "direct":
                direct_dir = paths["llm_raw"] / paper_id / "summary_memory" / "direct"
                direct_path = direct_dir / "whole_paper.json"
                direct_meta_path = direct_dir / "whole_paper.meta.json"
                direct_hash = sha256_text(
                    direct_user_prompt + prompt_version + schema_version + signature
                )
                if (
                    not args.force
                    and direct_path.exists()
                    and direct_meta_path.exists()
                    and read_json(direct_meta_path).get("input_hash") == direct_hash
                ):
                    candidate = read_json(direct_path)
                    if not validate_schema(candidate, memory_schema):
                        final_memory = candidate
                        stats["direct_cache_hits"] += 1
                        print(f"  DIRECT-CACHE {paper_id}", flush=True)

                if final_memory is None:
                    def run_direct_attempt(
                        max_output_tokens: int,
                        *,
                        retry: bool,
                    ) -> tuple[dict[str, Any] | None, Exception | None]:
                        attempt_name = "direct_whole_paper_retry" if retry else "direct_whole_paper"
                        attempt_hash = sha256_text(
                            direct_hash
                            + f"|max_output_tokens={max_output_tokens}|retry={int(retry)}"
                        )
                        run_id = log_llm_run_start(
                            conn, paper_id, STAGE, attempt_name,
                            prompt_version, schema_version, model, attempt_hash
                        )
                        stats["direct_attempts"] += 1
                        if retry:
                            stats["direct_retries"] += 1
                            print(
                                f"  DIRECT-RETRY {paper_id} max_output={max_output_tokens}",
                                flush=True,
                            )
                        else:
                            print(
                                f"  DIRECT-START {paper_id} input={direct_input_tokens} tokens "
                                f"max_output={max_output_tokens}",
                                flush=True,
                            )
                        started = time.monotonic()
                        try:
                            result = client.chat_json(
                                direct_system,
                                direct_user_prompt,
                                memory_schema,
                                max_tokens=max_output_tokens,
                            )
                            elapsed = time.monotonic() - started
                            write_json(direct_path, result.data)
                            write_json(
                                direct_meta_path,
                                {
                                    "input_hash": direct_hash,
                                    "paper_id": paper_id,
                                    "model": result.model,
                                    "input_tokens": direct_input_tokens,
                                    "token_count_source": token_count_source,
                                    "max_output_tokens": max_output_tokens,
                                    "attempt": "retry" if retry else "initial",
                                    "direct_attempts": stats["direct_attempts"],
                                },
                            )
                            log_llm_run_finish(conn, run_id, "success", direct_path)
                            stats["direct_calls"] += 1
                            print(f"  DIRECT-DONE  {paper_id} {elapsed:.1f}s", flush=True)
                            return result.data, None
                        except LLMOutputTruncatedError as error:
                            elapsed = time.monotonic() - started
                            log_llm_run_finish(conn, run_id, "truncated", error=str(error))
                            suffix = " retry" if retry else ""
                            print(
                                f"  DIRECT-TRUNCATED {paper_id}{suffix} {elapsed:.1f}s",
                                flush=True,
                            )
                            return None, error
                        except Exception as error:
                            elapsed = time.monotonic() - started
                            log_llm_run_finish(conn, run_id, "error", error=str(error))
                            suffix = " retry" if retry else ""
                            print(
                                f"  DIRECT-ERROR {paper_id}{suffix} {elapsed:.1f}s: "
                                f"{type(error).__name__}: {error}",
                                flush=True,
                            )
                            return None, error

                    final_memory, direct_error = run_direct_attempt(
                        direct_max_output_tokens, retry=False
                    )

                    if (
                        final_memory is None
                        and isinstance(direct_error, LLMOutputTruncatedError)
                        and direct_retry_enabled
                        and direct_retry_output_tokens > direct_max_output_tokens
                    ):
                        final_memory, direct_error = run_direct_attempt(
                            direct_retry_output_tokens, retry=True
                        )

                    if final_memory is None:
                        print(
                            f"  DIRECT-FALLBACK {paper_id}: "
                            f"{type(direct_error).__name__ if direct_error else 'unknown error'}: "
                            f"{direct_error or 'direct mode did not produce a valid memory'}",
                            flush=True,
                        )

            if final_memory is None:
                chunk_memories: list[dict[str, Any]] = []
                for chunk in chunks:
                    chunk_memories.extend(process_summary_chunk(chunk))
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
                    "direct_calls": stats["direct_calls"],
                    "direct_attempts": stats["direct_attempts"],
                    "direct_retries": stats["direct_retries"],
                    "direct_cache_hits": stats["direct_cache_hits"],
                    "memory_route": (
                        "direct"
                        if stats["direct_calls"] or stats["direct_cache_hits"]
                        else "hierarchical"
                    ),
                    "direct_input_tokens": direct_input_tokens,
                    "token_count_source": token_count_source,
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
