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
import math
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

SCRIPT_VERSION = "cluster-naming-v4.1.5-hierarchical-content-addressed-v2"


class ContextOverflowError(RuntimeError):
    pass


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def middle_truncate(text: str, max_chars: int, marker: str = " …[compacted]… ") -> str:
    text = str(text or "")
    max_chars = max(64, int(max_chars))
    if len(text) <= max_chars:
        return text
    room = max_chars - len(marker)
    if room <= 16:
        return text[:max_chars]
    left = max(8, int(room * 0.72))
    right = max(8, room - left)
    return text[:left].rstrip() + marker + text[-right:].lstrip()


def hard_compact(value: Any, max_chars: int) -> Any:
    """Deterministically compact JSON-like data with a hard serialized-size bound.

    Dict keys are retained whenever feasible; lists keep early and late items. If a
    deeply nested value still cannot fit, it becomes a head/tail JSON excerpt string.
    """
    max_chars = max(256, int(max_chars))
    if len(json_text(value)) <= max_chars:
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return middle_truncate(value, max_chars)
    if isinstance(value, list):
        if not value:
            return []
        # Keep both early and late evidence when possible.
        keep_n = min(len(value), max(1, max_chars // 700))
        if keep_n >= len(value):
            chosen = value
        elif keep_n == 1:
            chosen = [value[0]]
        else:
            head_n = (keep_n + 1) // 2
            tail_n = keep_n - head_n
            chosen = list(value[:head_n]) + list(value[-tail_n:] if tail_n else [])
        per = max(180, (max_chars - 120) // max(1, len(chosen)))
        out = [hard_compact(x, per) for x in chosen]
        if len(chosen) < len(value):
            out.insert(min(len(out), (len(out) + 1) // 2), f"[... {len(value)-len(chosen)} list items compacted ...]")
        if len(json_text(out)) <= max_chars:
            return out
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: str(kv[0]))
        if not items:
            return {}
        overhead = sum(len(json.dumps(str(k), ensure_ascii=False)) + 4 for k, _ in items) + 16
        per = max(180, (max_chars - overhead) // max(1, len(items)))
        out = {str(k): hard_compact(v, per) for k, v in items}
        if len(json_text(out)) <= max_chars:
            return out
        # A second, tighter pass keeps the same top-level keys.
        per2 = max(96, int(per * 0.55))
        out = {str(k): hard_compact(v, per2) for k, v in items}
        if len(json_text(out)) <= max_chars:
            return out
    # Guaranteed final bound. The field containing this string still identifies
    # which analysis artifact it came from in the parent paper dossier.
    excerpt = middle_truncate(json_text(value), max_chars - 24, " …[JSON compacted]… ")
    return excerpt


def choose_artifact(curated: Path, raw: Path) -> Path:
    return curated if curated.exists() else raw


_RUNTIME_FIELDS = {
    "cache_hit", "candidate_cache_hit", "harmonize_cache_hit", "group_cache_hits",
    "input_tokens", "token_count_mode", "model", "source", "reproducibility_note",
}

def scientific_view(value: Any) -> Any:
    """Remove execution/cache metadata before downstream hashing and prompting."""
    if isinstance(value, dict):
        return {str(k): scientific_view(v) for k, v in value.items() if str(k) not in _RUNTIME_FIELDS}
    if isinstance(value, list):
        return [scientific_view(x) for x in value]
    return value


def paper_dossier(
    paper_id: str,
    node: dict[str, Any],
    *,
    root: Path,
    paths: dict[str, Path],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Build a bounded dossier covering every analysis category for one paper.

    The full parsed article text is intentionally represented by whole-paper summary
    memory, which was itself generated from the complete parsed paper. This avoids
    duplicating hundreds of thousands of raw characters while still retaining the
    article-level interpretation together with curated claims/evidence.
    """
    curated_dir = paths.get("curated", root / "data/curated")
    metadata_dir = paths.get("metadata", root / "data/metadata")
    extracted_dir = paths.get("extracted", root / "data/extracted")
    summary_dir = paths.get("summary_memory", root / "data/summary_memory")

    inventory_path = choose_artifact(curated_dir / f"{paper_id}.inventory.json", extracted_dir / f"{paper_id}.inventory.json")
    evidence_path = choose_artifact(curated_dir / f"{paper_id}.evidence.json", extracted_dir / f"{paper_id}.evidence.json")
    metadata_path = choose_artifact(curated_dir / f"{paper_id}.metadata.json", metadata_dir / f"{paper_id}.metadata.json")
    validation_path = extracted_dir / f"{paper_id}.validation.json"
    memory_path = summary_dir / f"{paper_id}.memory.json"

    inventory = read_json(inventory_path) if inventory_path.exists() else {}
    evidence = read_json(evidence_path) if evidence_path.exists() else {}
    metadata = read_json(metadata_path) if metadata_path.exists() else {}
    validation = read_json(validation_path) if validation_path.exists() else {}
    memory = read_json(memory_path) if memory_path.exists() else {}

    dossier = {
        "paper_id": paper_id,
        "network_node": {
            key: node.get(key)
            for key in [
                "display_label", "authors", "title", "year", "journal", "doi",
                "properties", "methods", "keywords", "claims", "central_question",
                "validation_status",
            ]
        },
        "publication_metadata": hard_compact(metadata, int(cfg.get("metadata_chars_per_paper", 3000))),
        "whole_paper_summary_memory": hard_compact(memory, int(cfg.get("summary_chars_per_paper", 9000))),
        "curated_or_raw_inventory": hard_compact(inventory, int(cfg.get("inventory_chars_per_paper", 5000))),
        "curated_or_raw_evidence": hard_compact(evidence, int(cfg.get("evidence_chars_per_paper", 6500))),
        "validation": hard_compact(validation, int(cfg.get("validation_chars_per_paper", 1800))),
        "coverage_note": "Whole-paper summary memory represents the complete parsed article; curated/raw inventory and evidence add structured scientific details and provenance.",
    }
    return hard_compact(dossier, int(cfg.get("max_paper_dossier_chars", 12000)))


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
    questions: list[str] = []
    for pid in paper_ids:
        node = node_lookup.get(pid, {})
        props.update(str(x) for x in node.get("properties", []) if x)
        methods.update(str(x) for x in node.get("methods", []) if x)
        keywords.update(str(x) for x in node.get("keywords", []) if x)
        if node.get("title"):
            titles.append(str(node["title"]))
        if node.get("central_question"):
            questions.append(str(node["central_question"]))
    return {
        "cluster_id": cluster_id,
        "technical_label": technical_label,
        "size": len(paper_ids),
        "top_properties": props.most_common(10),
        "top_methods": methods.most_common(10),
        "top_keywords": keywords.most_common(12),
        "paper_titles": titles[:20],
        "central_questions": questions[:12],
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


def schema_augmented_system(system_prompt: str, schema: dict[str, Any]) -> str:
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return (
        system_prompt.rstrip()
        + "\n\nYou MUST return exactly one JSON value matching this schema. Do not add keys absent from the schema.\nJSON_SCHEMA:\n"
        + schema_text
    )


def count_tokens_for_llama(llm_cfg: dict[str, Any], text: str) -> tuple[int, str]:
    base_url = str(llm_cfg["base_url"]).rstrip("/")
    root_url = base_url[:-3] if base_url.endswith("/v1") else base_url
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {llm_cfg.get('api_key', 'no-key')}",
    }
    try:
        response = requests.post(f"{root_url}/tokenize", headers=headers, json={"content": text}, timeout=60)
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
    return max(1, math.ceil(len(text) / 3.0)), "estimated"


def prompt_token_count(llm_cfg: dict[str, Any], system_prompt: str, user_prompt: str, schema: dict[str, Any]) -> tuple[int, str]:
    joined = schema_augmented_system(system_prompt, schema) + "\n\nUSER:\n" + user_prompt
    return count_tokens_for_llama(llm_cfg, joined)


def deterministic_chat_json(
    llm_cfg: dict[str, Any],
    naming_cfg: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    *,
    max_tokens: int,
    input_token_budget: int,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    token_count, token_mode = prompt_token_count(llm_cfg, system_prompt, user_prompt, schema)
    if token_count > int(input_token_budget):
        raise ContextOverflowError(f"prompt budget exceeded before LLM call: {token_count} > {input_token_budget} tokens ({token_mode})")

    model = model_id(llm_cfg)
    system = schema_augmented_system(system_prompt, schema)
    payload_base: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}],
        "temperature": 0.0,
        "top_k": 1,
        "top_p": 1.0,
        "min_p": 0.0,
        "seed": int(naming_cfg.get("seed", 41413)),
        "cache_prompt": bool(naming_cfg.get("cache_prompt", False)),
        "max_tokens": int(max_tokens),
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    variants = [
        {"response_format": {"type": "json_schema", "schema": schema}},
        {"response_format": {"type": "json_object"}, "json_schema": schema},
        {"response_format": {"type": "json_object", "schema": schema}},
    ]
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {llm_cfg.get('api_key', 'no-key')}"}
    url = f"{str(llm_cfg['base_url']).rstrip('/')}/chat/completions"
    failures: list[str] = []
    for structured in variants:
        payload = dict(payload_base)
        payload.update(structured)
        response = requests.post(url, headers=headers, json=payload, timeout=int(llm_cfg.get("timeout_seconds", 1200)))
        if response.status_code == 400:
            text = response.text[:1000].replace("\n", " ")
            if "exceed_context_size_error" in text or "exceeds the available context size" in text:
                raise ContextOverflowError(text)
            failures.append(text[:400])
            continue
        response.raise_for_status()
        raw = response.json()
        content = raw["choices"][0]["message"].get("content", "")
        data = parse_json_content(content)
        errors = validate_schema(data, schema)
        if not errors:
            return data, model, {"input_tokens": token_count, "token_count_mode": token_mode}
        failures.append("schema mismatch: " + " | ".join(errors[:4]))
    raise RuntimeError("No schema-valid response: " + " || ".join(failures[:4]))


def cache_load(path: Path, required_key: str, *, force: bool) -> dict[str, Any] | None:
    if force or not path.exists():
        return None
    try:
        data = read_json(path)
    except Exception:
        return None
    return data if data.get(required_key) else None


def cache_write(path: Path, record: dict[str, Any], *, force: bool) -> None:
    # Force regeneration is intentionally non-destructive: it does not replace the
    # canonical content-addressed result used by future reproducible runs.
    if not force:
        write_json(path, record)


def group_prompt(cluster_id: int, technical_label: str, dossiers: list[dict[str, Any]], group_index: int, group_count: int) -> str:
    payload = {
        "cluster_id": cluster_id,
        "technical_label": technical_label,
        "group_index": group_index,
        "group_count": group_count,
        "paper_dossiers": dossiers,
    }
    return "PAPER_GROUP:\n" + json_text(payload)


def split_dossiers_for_budget(
    dossiers: list[dict[str, Any]],
    *,
    cluster_id: int,
    technical_label: str,
    llm_cfg: dict[str, Any],
    system_prompt: str,
    schema: dict[str, Any],
    token_budget: int,
    max_group_papers: int,
) -> list[list[dict[str, Any]]]:
    # Start with small deterministic groups. Then verify exact/estimated tokenizer
    # budget and recursively split any oversized group.
    initial = [dossiers[i:i + max(1, max_group_papers)] for i in range(0, len(dossiers), max(1, max_group_papers))]
    out: list[list[dict[str, Any]]] = []
    queue = list(initial)
    while queue:
        group = queue.pop(0)
        prompt = group_prompt(cluster_id, technical_label, group, 0, 1)
        count, _ = prompt_token_count(llm_cfg, system_prompt, prompt, schema)
        if count <= token_budget:
            out.append(group)
            continue
        if len(group) > 1:
            mid = max(1, len(group) // 2)
            queue.insert(0, group[mid:])
            queue.insert(0, group[:mid])
            continue
        # A single dossier should already be bounded. Tighten it until safe.
        item = group[0]
        target = max(2500, int(len(json_text(item)) * 0.65))
        fitted = item
        for _ in range(8):
            fitted = hard_compact(fitted, target)
            prompt = group_prompt(cluster_id, technical_label, [fitted], 0, 1)
            count, _ = prompt_token_count(llm_cfg, system_prompt, prompt, schema)
            if count <= token_budget:
                break
            target = max(1800, int(target * 0.72))
        out.append([fitted])
    return out


def fallback_group_summary(group: list[dict[str, Any]]) -> dict[str, Any]:
    signals = []
    for dossier in group:
        pid = str(dossier.get("paper_id") or "")
        node = dossier.get("network_node") if isinstance(dossier.get("network_node"), dict) else {}
        contribution = str(node.get("central_question") or node.get("title") or "Paper included in this cluster")
        signals.append({"paper_id": pid, "contribution": middle_truncate(contribution, 300)})
    return {
        "group_summary": "LLM group compression was unavailable; deterministic paper-level signals are retained for fallback interpretation.",
        "shared_scientific_questions": [],
        "systems_scope": [],
        "methods": [],
        "properties_phenomena": [],
        "major_findings": [],
        "important_claims": [],
        "limitations_or_boundaries": [],
        "paper_signals": signals,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Name FolioSort Literature Network clusters with hierarchical, reproducible local-Qwen analysis.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--project", required=True)
    ap.add_argument("--force", action="store_true", help="Bypass canonical content-addressed caches without overwriting them")
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

    global_context = [technical_cluster_summary(cid, grouped[cid], node_lookup, technical_labels[cid]) for cid in sorted(grouped)]
    global_context = hard_compact(global_context, int(naming_cfg.get("max_global_context_chars", 24000)))

    cluster_prompt_path = root / "prompts/v4/cluster_naming_system.txt"
    cluster_schema_path = root / "schemas/v4/cluster_naming.schema.json"
    group_prompt_path = root / "prompts/v4/cluster_group_summary_system.txt"
    group_schema_path = root / "schemas/v4/cluster_group_summary.schema.json"
    harmonize_prompt_path = root / "prompts/v4/cluster_naming_harmonize_system.txt"
    harmonize_schema_path = root / "schemas/v4/cluster_naming_harmonize.schema.json"

    cluster_system = cluster_prompt_path.read_text(encoding="utf-8")
    cluster_schema = load_schema(cluster_schema_path)
    group_system = group_prompt_path.read_text(encoding="utf-8")
    group_schema = load_schema(group_schema_path)
    harmonize_system = harmonize_prompt_path.read_text(encoding="utf-8")
    harmonize_schema = load_schema(harmonize_schema_path)

    prompt_hashes = {
        "cluster": sha256_file(cluster_prompt_path),
        "group": sha256_file(group_prompt_path),
        "harmonize": sha256_file(harmonize_prompt_path),
    }
    schema_hashes = {
        "cluster": sha256_file(cluster_schema_path),
        "group": sha256_file(group_schema_path),
        "harmonize": sha256_file(harmonize_schema_path),
    }

    cache_root = root / "data/llm_raw/cluster_names"
    cache_root.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    candidates: dict[int, dict[str, Any]] = {}
    cluster_evidence: dict[int, dict[str, Any]] = {}
    group_cache_hits_total = 0
    group_calls_total = 0

    group_token_budget = int(naming_cfg.get("group_input_token_budget", 26000))
    candidate_token_budget = int(naming_cfg.get("candidate_input_token_budget", 30000))
    harmonize_token_budget = int(naming_cfg.get("harmonize_input_token_budget", 36000))
    max_group_papers = int(naming_cfg.get("max_group_papers", 5))

    for cid in sorted(grouped):
        paper_ids = grouped[cid]
        dossiers = [paper_dossier(pid, node_lookup[pid], root=root, paths=paths, cfg=naming_cfg) for pid in paper_ids]
        groups = split_dossiers_for_budget(
            dossiers,
            cluster_id=cid,
            technical_label=technical_labels[cid],
            llm_cfg=config["llm"],
            system_prompt=group_system,
            schema=group_schema,
            token_budget=group_token_budget,
            max_group_papers=max_group_papers,
        )
        group_summaries: list[dict[str, Any]] = []
        group_hits = 0
        for gi, group in enumerate(groups):
            group_ids = [str(x.get("paper_id") or "") for x in group]
            group_identity = {
                "script": SCRIPT_VERSION,
                "stage": "group_summary",
                "prompt_hash": prompt_hashes["group"],
                "schema_hash": schema_hashes["group"],
                "project": slug,
                "cluster_id": cid,
                "group_index": gi,
                "paper_ids": group_ids,
                "dossiers_hash": stable_json_hash(group),
                "model_hint": str((config.get("llm") or {}).get("model") or "auto"),
            }
            group_signature = stable_json_hash(group_identity)
            group_cache = cache_root / f"group_{group_signature[:32]}.json"
            cached = cache_load(group_cache, "group_summary", force=args.force)
            if cached is not None:
                summary = dict(cached)
                summary["cache_hit"] = True
                group_hits += 1
                group_cache_hits_total += 1
                group_summaries.append(scientific_view(summary))
                continue
            group_calls_total += 1
            user_prompt = group_prompt(cid, technical_labels[cid], group, gi, len(groups))
            try:
                data, model, token_meta = deterministic_chat_json(
                    config["llm"], naming_cfg, group_system, user_prompt, group_schema,
                    max_tokens=int(naming_cfg.get("group_max_tokens", 900)),
                    input_token_budget=group_token_budget,
                )
                allowed = set(group_ids)
                signals = [x for x in data.get("paper_signals", []) if str(x.get("paper_id") or "") in allowed]
                seen = {str(x.get("paper_id")) for x in signals}
                for dossier in group:
                    pid = str(dossier.get("paper_id") or "")
                    if pid and pid not in seen:
                        node = dossier.get("network_node") if isinstance(dossier.get("network_node"), dict) else {}
                        signals.append({"paper_id": pid, "contribution": middle_truncate(str(node.get("central_question") or node.get("title") or "Included paper"), 300)})
                record = {
                    **data,
                    "paper_signals": signals,
                    "cluster_id": cid,
                    "group_index": gi,
                    "paper_ids": group_ids,
                    "group_signature": group_signature,
                    "model": model,
                    "input_tokens": token_meta["input_tokens"],
                    "token_count_mode": token_meta["token_count_mode"],
                    "cache_hit": False,
                    "source": "ai_group_summary",
                }
                cache_write(group_cache, record, force=args.force)
                group_summaries.append(scientific_view(record))
            except Exception as exc:
                warnings.append(f"C{cid + 1} group {gi + 1}/{len(groups)}: {type(exc).__name__}: {exc}")
                record = {
                    **fallback_group_summary(group),
                    "cluster_id": cid,
                    "group_index": gi,
                    "paper_ids": group_ids,
                    "group_signature": group_signature,
                    "cache_hit": False,
                    "source": "deterministic_group_fallback",
                }
                group_summaries.append(scientific_view(record))

        evidence = {
            "cluster_id": cid,
            "technical_summary": technical_cluster_summary(cid, paper_ids, node_lookup, technical_labels[cid]),
            "paper_count": len(paper_ids),
            "paper_ids": paper_ids,
            "selected_layers": selected_layers,
            "resolution": resolution,
            "group_summaries": group_summaries,
            "coverage": {
                "groups": len(groups),
                "all_papers_included": sorted({pid for g in groups for pid in [str(x.get('paper_id') or '') for x in g] if pid}) == paper_ids,
                "group_cache_hits": group_hits,
            },
        }
        cluster_evidence[cid] = evidence

        candidate_input = {
            "project": slug,
            "target_cluster": hard_compact(scientific_view(evidence), int(naming_cfg.get("candidate_cluster_context_chars", 60000))),
            "global_cluster_context": global_context,
        }
        candidate_identity = {
            "script": SCRIPT_VERSION,
            "stage": "candidate_name",
            "prompt_hash": prompt_hashes["cluster"],
            "schema_hash": schema_hashes["cluster"],
            "project": slug,
            "network_signature": network_signature,
            "selected_layers": sorted(selected_layers),
            "resolution": round(resolution, 8),
            "cluster_id": cid,
            "membership": paper_ids,
            "input_hash": stable_json_hash(candidate_input),
            "model_hint": str((config.get("llm") or {}).get("model") or "auto"),
        }
        candidate_signature = stable_json_hash(candidate_identity)
        candidate_cache = cache_root / f"candidate_{candidate_signature[:32]}.json"
        cached_candidate = cache_load(candidate_cache, "short_name", force=args.force)
        if cached_candidate is not None:
            rec = dict(cached_candidate)
            rec["candidate_cache_hit"] = True
            candidates[cid] = rec
            continue

        user_prompt = "HIERARCHICAL_CLUSTER_EVIDENCE:\n" + json_text(candidate_input)
        try:
            data, model, token_meta = deterministic_chat_json(
                config["llm"], naming_cfg, cluster_system, user_prompt, cluster_schema,
                max_tokens=int(naming_cfg.get("candidate_max_tokens", 1000)),
                input_token_budget=candidate_token_budget,
            )
            allowed = set(paper_ids)
            reps = [str(x) for x in data.get("representative_paper_ids", []) if str(x) in allowed]
            if not reps:
                reps = paper_ids[: min(5, len(paper_ids))]
            rec = {
                **data,
                "representative_paper_ids": reps,
                "cluster_id": cid,
                "technical_label": technical_labels[cid],
                "paper_ids": paper_ids,
                "cluster_signature": candidate_signature,
                "model": model,
                "input_tokens": token_meta["input_tokens"],
                "token_count_mode": token_meta["token_count_mode"],
                "candidate_cache_hit": False,
                "group_cache_hits": group_hits,
                "source": "ai_candidate",
            }
            cache_write(candidate_cache, rec, force=args.force)
            candidates[cid] = rec
        except Exception as exc:
            warnings.append(f"C{cid + 1} candidate: {type(exc).__name__}: {exc}")
            candidates[cid] = {
                "cluster_id": cid,
                "technical_label": technical_labels[cid],
                "short_name": technical_labels[cid],
                "review_section_title": technical_labels[cid],
                "rationale": "AI cluster naming was unavailable; the deterministic technical label is shown instead.",
                "distinguishing_features": [],
                "representative_paper_ids": paper_ids[: min(5, len(paper_ids))],
                "confidence": 0.0,
                "paper_ids": paper_ids,
                "cluster_signature": candidate_signature,
                "candidate_cache_hit": False,
                "group_cache_hits": group_hits,
                "source": "technical_fallback",
            }

    # Final pass sees all clusters together, so names can be made mutually distinct.
    harmonize_clusters: list[dict[str, Any]] = []
    per_cluster_chars = int(naming_cfg.get("harmonize_context_chars_per_cluster", 11000))
    for cid in sorted(grouped):
        harmonize_clusters.append({
            "cluster_id": cid,
            "allowed_paper_ids": grouped[cid],
            "preliminary_name": scientific_view(candidates[cid]),
            "evidence_summary": hard_compact(scientific_view(cluster_evidence[cid]), per_cluster_chars),
        })
    harmonize_input = {
        "project": slug,
        "network_signature": network_signature,
        "selected_layers": selected_layers,
        "resolution": resolution,
        "clusters": harmonize_clusters,
    }

    # If a project has many communities, tighten each context until the all-cluster
    # comparison fits the configured token budget. No cluster is omitted.
    for _ in range(8):
        hp = "ALL_CLUSTER_ANALYSES:\n" + json_text(harmonize_input)
        count, _mode = prompt_token_count(config["llm"], harmonize_system, hp, harmonize_schema)
        if count <= harmonize_token_budget:
            break
        per_cluster_chars = max(2200, int(per_cluster_chars * 0.70))
        harmonize_clusters = [{
            "cluster_id": x["cluster_id"],
            "allowed_paper_ids": x["allowed_paper_ids"],
            "preliminary_name": hard_compact(x["preliminary_name"], max(1400, per_cluster_chars // 3)),
            "evidence_summary": hard_compact(scientific_view(cluster_evidence[x["cluster_id"]]), per_cluster_chars),
        } for x in harmonize_clusters]
        harmonize_input["clusters"] = harmonize_clusters

    harmonize_identity = {
        "script": SCRIPT_VERSION,
        "stage": "harmonize_names",
        "prompt_hash": prompt_hashes["harmonize"],
        "schema_hash": schema_hashes["harmonize"],
        "project": slug,
        "network_signature": network_signature,
        "selected_layers": sorted(selected_layers),
        "resolution": round(resolution, 8),
        "membership_hash": stable_json_hash(membership),
        "input_hash": stable_json_hash(harmonize_input),
        "model_hint": str((config.get("llm") or {}).get("model") or "auto"),
    }
    harmonize_signature = stable_json_hash(harmonize_identity)
    harmonize_cache = cache_root / f"harmonized_{harmonize_signature[:32]}.json"
    harmonized_cache_hit = False
    harmonized_data = cache_load(harmonize_cache, "clusters", force=args.force)
    if harmonized_data is not None:
        harmonized_cache_hit = True
    else:
        try:
            hprompt = "ALL_CLUSTER_ANALYSES:\n" + json_text(harmonize_input)
            harmonized_data, model, token_meta = deterministic_chat_json(
                config["llm"], naming_cfg, harmonize_system, hprompt, harmonize_schema,
                max_tokens=int(naming_cfg.get("harmonize_max_tokens", 3000)),
                input_token_budget=harmonize_token_budget,
            )
            harmonized_data = {
                **harmonized_data,
                "harmonize_signature": harmonize_signature,
                "model": model,
                "input_tokens": token_meta["input_tokens"],
                "token_count_mode": token_meta["token_count_mode"],
            }
            cache_write(harmonize_cache, harmonized_data, force=args.force)
        except Exception as exc:
            warnings.append(f"harmonization: {type(exc).__name__}: {exc}")
            harmonized_data = None

    results: dict[str, Any] = {str(cid): dict(candidates[cid]) for cid in sorted(grouped)}
    if isinstance(harmonized_data, dict):
        rows = harmonized_data.get("clusters") or []
        row_by_id = {int(row.get("cluster_id")): row for row in rows if isinstance(row, dict) and isinstance(row.get("cluster_id"), int)}
        expected = set(grouped)
        if set(row_by_id) == expected:
            for cid in sorted(grouped):
                row = row_by_id[cid]
                allowed = set(grouped[cid])
                reps = [str(x) for x in row.get("representative_paper_ids", []) if str(x) in allowed]
                if not reps:
                    reps = results[str(cid)].get("representative_paper_ids") or grouped[cid][: min(5, len(grouped[cid]))]
                results[str(cid)] = {
                    **results[str(cid)],
                    **row,
                    "representative_paper_ids": reps,
                    "technical_label": technical_labels[cid],
                    "paper_ids": grouped[cid],
                    "harmonize_signature": harmonize_signature,
                    "harmonize_cache_hit": harmonized_cache_hit,
                    "cache_hit": bool(harmonized_cache_hit),
                    "source": "ai_harmonized",
                }
        else:
            warnings.append("harmonization: returned cluster IDs did not exactly match the current partition; preliminary names retained")

    ai_named = sum(1 for x in results.values() if str(x.get("source") or "").startswith("ai_"))
    fallback = sum(1 for x in results.values() if x.get("source") == "technical_fallback")
    candidate_hits = sum(1 for x in results.values() if x.get("candidate_cache_hit"))
    final_hits = sum(1 for x in results.values() if x.get("harmonize_cache_hit"))
    naming_summary = {
        "total_clusters": len(results),
        "ai_named": ai_named,
        "fallback_labels": fallback,
        "group_summaries": group_calls_total + group_cache_hits_total,
        "group_cache_hits": group_cache_hits_total,
        "candidate_cache_hits": candidate_hits,
        "final_cache_hits": final_hits,
        "warnings": len(warnings),
        "all_papers_covered": all(bool(cluster_evidence[cid]["coverage"]["all_papers_included"]) for cid in cluster_evidence),
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
        "naming_summary": naming_summary,
        "warnings": warnings,
        "reproducibility": {
            "leiden_membership_seed": int(((config.get("multiplex_graph") or {}).get("clustering") or {}).get("seed", 42)),
            "llm_temperature": 0.0,
            "llm_seed": int(naming_cfg.get("seed", 41413)),
            "cache_prompt": bool(naming_cfg.get("cache_prompt", False)),
            "cache_policy": "hierarchical content-addressed caches for group summaries, candidate names, and final all-cluster harmonization; identical canonical input returns stored JSON without a new LLM call",
            "force_policy": "force regeneration bypasses but does not overwrite canonical reproducibility caches",
            "token_budgeting": {
                "group_input_token_budget": group_token_budget,
                "candidate_input_token_budget": candidate_token_budget,
                "harmonize_input_token_budget": harmonize_token_budget,
                "tokenizer": "llama.cpp /tokenize when available; conservative 3 chars/token estimate otherwise",
            },
        },
        "hierarchical_pipeline": {
            "paper_representation": "metadata + whole-paper summary memory + curated/raw inventory + curated/raw evidence + validation + network descriptors",
            "map_stage": "bounded groups containing every paper",
            "reduce_stage": "cluster candidate naming from all group summaries",
            "harmonization_stage": "all clusters compared together for mutually distinct final names",
        },
    }
    write_json(out_dir / "cluster_names.current.json", current)
    print(json.dumps(current, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
