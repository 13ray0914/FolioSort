#!/usr/bin/env python3
from __future__ import annotations

# Use the main project venv. This stage needs requests/jsonschema helpers, not igraph.
import os as _bootstrap_os
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_BOOT_ROOT = _BootstrapPath(__file__).resolve().parents[1]
_BOOT_VENV = _BOOT_ROOT / ".venv"
_BOOT_PY = _BOOT_VENV / "bin" / "python"
if _BOOT_PY.exists() and _BootstrapPath(_bootstrap_sys.prefix).resolve() != _BOOT_VENV.resolve():
    _bootstrap_os.execv(str(_BOOT_PY), [str(_BOOT_PY), str(_BootstrapPath(__file__).resolve()), *_bootstrap_sys.argv[1:]])

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    get_paths,
    load_config,
    load_schema,
    parse_json_content,
    read_json,
    sha256_file,
    stable_json_hash,
    validate_schema,
    write_json,
)
from lib.projects import normalize_project_slug, project_network_dir

SCRIPT_VERSION = "cluster-naming-v4.1.4-content-addressed-v1"


def compact_json(value: Any, max_chars: int) -> Any:
    """Deterministically shrink a structured artifact while preserving its shape."""
    max_chars = max(1000, int(max_chars))
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(text) <= max_chars:
        return value

    def shrink(obj: Any, string_limit: int, list_limit: int) -> Any:
        if isinstance(obj, str):
            return obj if len(obj) <= string_limit else obj[:string_limit].rstrip() + " …[truncated]"
        if isinstance(obj, list):
            return [shrink(x, string_limit, list_limit) for x in obj[:list_limit]]
        if isinstance(obj, dict):
            return {str(k): shrink(v, string_limit, list_limit) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
        return obj

    last = value
    for string_limit, list_limit in [
        (5000, 20), (3000, 16), (1800, 12), (1200, 10), (800, 8), (500, 6), (300, 5), (180, 4)
    ]:
        last = shrink(value, string_limit, list_limit)
        if len(json.dumps(last, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= max_chars:
            return last
    return last


def choose_artifact(curated: Path, raw: Path) -> Path:
    return curated if curated.exists() else raw


def paper_dossier(
    paper_id: str,
    node: dict[str, Any],
    *,
    root: Path,
    paths: dict[str, Path],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    curated_dir = paths.get("curated", root / "data/curated")
    metadata_dir = paths.get("metadata", root / "data/metadata")
    extracted_dir = paths.get("extracted", root / "data/extracted")
    summary_dir = paths.get("summary_memory", root / "data/summary_memory")

    inventory_path = choose_artifact(
        curated_dir / f"{paper_id}.inventory.json",
        extracted_dir / f"{paper_id}.inventory.json",
    )
    evidence_path = choose_artifact(
        curated_dir / f"{paper_id}.evidence.json",
        extracted_dir / f"{paper_id}.evidence.json",
    )
    metadata_path = choose_artifact(
        curated_dir / f"{paper_id}.metadata.json",
        metadata_dir / f"{paper_id}.metadata.json",
    )
    validation_path = extracted_dir / f"{paper_id}.validation.json"
    memory_path = summary_dir / f"{paper_id}.memory.json"
    paper_path = paths.get("paper_json", root / "data/papers") / f"{paper_id}.json"

    inventory = read_json(inventory_path) if inventory_path.exists() else {}
    evidence = read_json(evidence_path) if evidence_path.exists() else {}
    metadata = read_json(metadata_path) if metadata_path.exists() else {}
    validation = read_json(validation_path) if validation_path.exists() else {}
    memory = read_json(memory_path) if memory_path.exists() else {}
    parsed_paper = read_json(paper_path) if paper_path.exists() else {}

    return {
        "paper_id": paper_id,
        "network_node": {
            key: node.get(key)
            for key in [
                "display_label", "authors", "title", "year", "journal", "doi",
                "properties", "methods", "keywords", "claims", "central_question",
                "validation_status",
            ]
        },
        "publication_metadata": metadata,
        "parsed_paper_record": compact_json(parsed_paper, int(cfg.get("max_paper_json_chars_per_paper", 8000))),
        # Whole-paper summary memory is produced from the complete parsed paper and
        # is intentionally the whole-article representation used for naming.
        "whole_paper_summary_memory": compact_json(memory, int(cfg.get("max_summary_chars_per_paper", 14000))),
        "curated_or_raw_inventory": compact_json(inventory, int(cfg.get("max_inventory_chars_per_paper", 8000))),
        "curated_or_raw_evidence": compact_json(evidence, int(cfg.get("max_evidence_chars_per_paper", 12000))),
        "validation": validation,
    }


def technical_cluster_summary(
    cluster_id: int,
    paper_ids: list[str],
    node_lookup: dict[str, dict[str, Any]],
    technical_label: str,
) -> dict[str, Any]:
    props: Counter[str] = Counter()
    methods: Counter[str] = Counter()
    keywords: Counter[str] = Counter()
    titles: list[str] = []
    for pid in paper_ids:
        node = node_lookup.get(pid, {})
        props.update(str(x) for x in node.get("properties", []) if x)
        methods.update(str(x) for x in node.get("methods", []) if x)
        keywords.update(str(x) for x in node.get("keywords", []) if x)
        if node.get("title"):
            titles.append(str(node["title"]))
    return {
        "cluster_id": cluster_id,
        "technical_label": technical_label,
        "size": len(paper_ids),
        "top_properties": props.most_common(8),
        "top_methods": methods.most_common(8),
        "top_keywords": keywords.most_common(10),
        "paper_titles": titles[:12],
    }


def model_id(llm_cfg: dict[str, Any]) -> str:
    configured = str(llm_cfg.get("model") or "auto")
    if configured and configured != "auto":
        return configured
    base_url = str(llm_cfg["base_url"]).rstrip("/")
    response = requests.get(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {llm_cfg.get('api_key', 'no-key')}"},
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json().get("data") or []
    if not rows:
        raise RuntimeError("llama.cpp /v1/models returned no model")
    return str(rows[0]["id"])


def deterministic_chat_json(
    llm_cfg: dict[str, Any],
    naming_cfg: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Greedy, fixed-seed, schema-constrained llama.cpp call.

    Exact cross-run GPU determinism is not assumed. Content-addressed caching is
    therefore the reproducibility guarantee after the first successful result.
    """
    model = model_id(llm_cfg)
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    system = (
        system_prompt.rstrip()
        + "\n\nYou MUST return exactly one JSON value matching this schema. Do not add keys absent from the schema.\nJSON_SCHEMA:\n"
        + schema_text
    )
    payload_base: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "top_k": 1,
        "top_p": 1.0,
        "min_p": 0.0,
        "seed": int(naming_cfg.get("seed", 41413)),
        "cache_prompt": bool(naming_cfg.get("cache_prompt", False)),
        "max_tokens": int(naming_cfg.get("max_tokens_per_cluster", 1200)),
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    variants = [
        {"response_format": {"type": "json_schema", "schema": schema}},
        {"response_format": {"type": "json_object"}, "json_schema": schema},
        {"response_format": {"type": "json_object", "schema": schema}},
    ]
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {llm_cfg.get('api_key', 'no-key')}",
    }
    url = f"{str(llm_cfg['base_url']).rstrip('/')}/chat/completions"
    failures: list[str] = []
    for structured in variants:
        payload = dict(payload_base)
        payload.update(structured)
        response = requests.post(url, headers=headers, json=payload, timeout=int(llm_cfg.get("timeout_seconds", 1200)))
        if response.status_code == 400:
            failures.append(response.text[:300].replace("\n", " "))
            continue
        response.raise_for_status()
        raw = response.json()
        content = raw["choices"][0]["message"].get("content", "")
        data = parse_json_content(content)
        errors = validate_schema(data, schema)
        if not errors:
            return data, model
        failures.append("schema mismatch: " + " | ".join(errors[:4]))
    raise RuntimeError("No schema-valid cluster name response: " + " || ".join(failures[:4]))


def main() -> None:
    ap = argparse.ArgumentParser(description="Deterministically name FolioSort Literature Network clusters using local Qwen.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--project", required=True)
    ap.add_argument("--force", action="store_true", help="Ignore content-addressed name cache and call the LLM again")
    args = ap.parse_args()

    request = json.loads(sys.stdin.read() or "{}")
    config, root = load_config(args.config)
    paths = get_paths(config, root)
    slug = normalize_project_slug(args.project)
    out_dir = project_network_dir(root, slug)
    network_path = out_dir / "network.json"
    if not network_path.exists():
        raise SystemExit(f"Network data not found: {network_path}")
    network = read_json(network_path)

    naming_cfg = (config.get("multiplex_graph") or {}).get("cluster_naming") or {}
    if not bool(naming_cfg.get("enabled", True)):
        print(json.dumps({"ok": True, "enabled": False, "cluster_names": {}}, ensure_ascii=False))
        return

    node_lookup = {str(n["paper_id"]): dict(n) for n in network.get("nodes", []) if n.get("paper_id")}
    membership = {str(k): int(v) for k, v in (request.get("membership") or {}).items() if str(k) in node_lookup}
    if not membership:
        membership = {str(n["paper_id"]): int(n.get("cluster_id", 0)) for n in network.get("nodes", []) if n.get("paper_id")}

    grouped: dict[int, list[str]] = defaultdict(list)
    for pid, cid in membership.items():
        grouped[int(cid)].append(pid)
    for cid in grouped:
        grouped[cid].sort()

    request_clusters = {int(c.get("cluster_id", 0)): c for c in (request.get("clusters") or [])}
    base_clusters = {int(c.get("cluster_id", 0)): c for c in (network.get("clusters") or [])}
    technical_labels: dict[int, str] = {}
    for cid in grouped:
        c = request_clusters.get(cid) or base_clusters.get(cid) or {}
        technical_labels[cid] = str(c.get("technical_label") or c.get("label") or f"cluster {cid + 1}")

    selected_layers = [str(x) for x in (request.get("selected_layers") or [])]
    resolution = float(request.get("resolution", 1.0))
    network_signature = str(request.get("network_signature") or (network.get("provenance") or {}).get("network_signature") or "")

    global_context = [
        technical_cluster_summary(cid, grouped[cid], node_lookup, technical_labels[cid])
        for cid in sorted(grouped)
    ]
    global_context = compact_json(global_context, int(naming_cfg.get("max_global_context_chars", 30000)))

    prompt_path = root / "prompts/v4/cluster_naming_system.txt"
    schema_path = root / "schemas/v4/cluster_naming.schema.json"
    system_prompt = prompt_path.read_text(encoding="utf-8")
    schema = load_schema(schema_path)
    prompt_hash = sha256_file(prompt_path)
    schema_hash = sha256_file(schema_path)

    cache_root = root / "data/llm_raw/cluster_names"
    cache_root.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    warnings: list[str] = []

    for cid in sorted(grouped):
        paper_ids = grouped[cid]
        dossiers = [
            paper_dossier(pid, node_lookup[pid], root=root, paths=paths, cfg=naming_cfg)
            for pid in paper_ids
        ]
        # Keep every paper and every analysis category in the prompt, but compact
        # deterministically when a large cluster would exceed the local model's
        # context window. This is preferable to sampling only "representative" papers.
        cluster_budget = int(naming_cfg.get("max_cluster_dossier_chars", 140000))
        dossier_text_len = len(json.dumps(dossiers, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if dossier_text_len > cluster_budget:
            per_paper_budget = max(3500, cluster_budget // max(1, len(dossiers)))
            dossiers = [compact_json(item, per_paper_budget) for item in dossiers]
        scientific_input = {
            "project": slug,
            "cluster_id": cid,
            "technical_label": technical_labels[cid],
            "selected_layers": selected_layers,
            "resolution": resolution,
            "papers": dossiers,
            "global_cluster_context": global_context,
        }
        dossier_hash = stable_json_hash(scientific_input)
        cache_identity = {
            "script": SCRIPT_VERSION,
            "prompt_hash": prompt_hash,
            "schema_hash": schema_hash,
            "project": slug,
            "network_signature": network_signature,
            "selected_layers": sorted(selected_layers),
            "resolution": round(resolution, 8),
            "cluster_id": cid,
            "membership": paper_ids,
            "dossier_hash": dossier_hash,
        }
        signature = stable_json_hash(cache_identity)
        cache_path = cache_root / f"cluster_{signature[:28]}.json"
        cached = None
        if cache_path.exists() and not args.force:
            cached = read_json(cache_path)
            if cached.get("short_name"):
                results[str(cid)] = {**cached, "cache_hit": True}
                continue

        user_prompt = (
            "TARGET_CLUSTER:\n"
            + json.dumps({
                "cluster_id": cid,
                "technical_label": technical_labels[cid],
                "selected_layers": selected_layers,
                "resolution": resolution,
                "papers": dossiers,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n\nGLOBAL_CLUSTER_CONTEXT:\n"
            + json.dumps(global_context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        try:
            data, model = deterministic_chat_json(config["llm"], naming_cfg, system_prompt, user_prompt, schema)
            allowed = set(paper_ids)
            reps = [str(x) for x in data.get("representative_paper_ids", []) if str(x) in allowed]
            if not reps:
                reps = paper_ids[: min(5, len(paper_ids))]
            record = {
                **data,
                "representative_paper_ids": reps,
                "cluster_id": cid,
                "technical_label": technical_labels[cid],
                "paper_ids": paper_ids,
                "selected_layers": selected_layers,
                "resolution": resolution,
                "network_signature": network_signature,
                "cluster_signature": signature,
                "dossier_hash": dossier_hash,
                "prompt_hash": prompt_hash,
                "schema_hash": schema_hash,
                "model": model,
                "temperature": 0.0,
                "seed": int(naming_cfg.get("seed", 41413)),
                "cache_prompt": bool(naming_cfg.get("cache_prompt", False)),
                "cache_hit": False,
                "reproducibility_note": "Same content signature reuses this exact cached result; the LLM is not called again unless force regeneration is requested.",
            }
            write_json(cache_path, record)
            results[str(cid)] = record
        except Exception as exc:
            warnings.append(f"C{cid + 1}: {type(exc).__name__}: {exc}")
            results[str(cid)] = {
                "cluster_id": cid,
                "technical_label": technical_labels[cid],
                "short_name": technical_labels[cid],
                "review_section_title": technical_labels[cid],
                "rationale": "AI naming was unavailable; the deterministic technical label is shown instead.",
                "distinguishing_features": [],
                "representative_paper_ids": paper_ids[: min(5, len(paper_ids))],
                "confidence": 0.0,
                "paper_ids": paper_ids,
                "cluster_signature": signature,
                "source": "technical_fallback",
                "cache_hit": False,
            }

    current = {
        "ok": True,
        "script_version": SCRIPT_VERSION,
        "project": slug,
        "network_signature": network_signature,
        "selected_layers": selected_layers,
        "resolution": resolution,
        "membership_hash": stable_json_hash(membership),
        "cluster_names": results,
        "warnings": warnings,
        "reproducibility": {
            "leiden_membership_seed": int(((config.get("multiplex_graph") or {}).get("clustering") or {}).get("seed", 42)),
            "llm_temperature": 0.0,
            "llm_seed": int(naming_cfg.get("seed", 41413)),
            "cache_prompt": bool(naming_cfg.get("cache_prompt", False)),
            "cache_policy": "content-addressed; identical scientific input returns the stored name without an LLM call",
        },
    }
    write_json(out_dir / "cluster_names.current.json", current)
    print(json.dumps(current, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
