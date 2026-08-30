#!/usr/bin/env python3
from __future__ import annotations

# Re-exec user-facing scripts with the project's virtualenv.
# This avoids PATH/pyenv selecting a Python build without required stdlib extensions
# such as _sqlite3. The pipeline wrapper already activates this venv; this guard
# makes direct ./scripts/*.py invocation equally reliable.
import os as _bootstrap_os
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_BOOT_ROOT = _BootstrapPath(__file__).resolve().parents[1]
_BOOT_VENV = _BOOT_ROOT / ".venv_network"
_BOOT_PY = _BOOT_VENV / "bin" / "python"
if _BOOT_PY.exists() and _BootstrapPath(_bootstrap_sys.prefix).resolve() != _BOOT_VENV.resolve():
    _bootstrap_os.execv(str(_BOOT_PY), [str(_BOOT_PY), str(_BootstrapPath(__file__).resolve()), *_bootstrap_sys.argv[1:]])

import argparse
import csv
import html
import json
import math
import os
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import igraph as ig
import leidenalg as la
import networkx as nx
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    LlamaCppClient,
    get_paths,
    load_config,
    load_schema,
    normalize_key,
    read_json,
    sha256_file,
    sha256_text,
    stable_json_hash,
    write_json,
)
from lib.v4_common import normalize_doi, normalize_openalex_id, normalize_title
from lib.projects import ensure_project_schema, normalize_project_slug, project_name, project_network_dir, project_rows
from lib.network_runtime import LAYER_COLORS, compute_layout_positions

SCRIPT_VERSION = "multiplex-network-v4.1.3-selected-layer-recluster-fast-render"


def first_author_family(authors: list[Any] | None) -> str:
    authors = list(authors or [])
    if not authors:
        return ""
    first = authors[0]
    if isinstance(first, dict):
        explicit = str(first.get("family") or first.get("surname") or "").strip()
        full = str(first.get("full_name") or first.get("display_name") or "").strip()
        if explicit and full and full.lower().endswith(explicit.lower()):
            return full[len(full) - len(explicit):].strip()
        if explicit:
            return explicit
        if full:
            return full.rsplit(None, 1)[-1].strip(" ,.;")
        return ""
    text = str(first).strip()
    return text.rsplit(None, 1)[-1].strip(" ,.;") if text else ""


def paper_display_label(authors: list[Any] | None, year: Any, paper_id: str) -> str:
    family = first_author_family(authors)
    year_text = str(year) if year not in (None, "") else "?"
    if family:
        return f"{family}, {year_text}"
    return f"{paper_id}, {year_text}"


def merged_canonical_metadata(paper: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    merged = dict(paper.get("metadata") or {})
    merged.update(metadata.get("canonical") or {})
    return merged


def abstract_text(paper: dict[str, Any]) -> str:
    return " ".join(
        sentence.get("text", "")
        for paragraph in paper.get("abstract", [])
        for sentence in paragraph.get("sentences", [])
        if sentence.get("text")
    )


def normalized_set(items: list[dict[str, Any]], normalized_key: str, raw_key: str) -> set[str]:
    out = set()
    for item in items:
        value = item.get(normalized_key) or item.get(raw_key)
        value = normalize_key(str(value or ""))
        if value:
            out.add(value)
    return out


def topk_matrix_edges(matrix: np.ndarray, ids: list[str], *, top_k: int, threshold: float) -> dict[tuple[str, str], float]:
    edges: dict[tuple[str, str], float] = {}
    for i, source in enumerate(ids):
        row = matrix[i].copy()
        row[i] = -1.0
        order = np.argsort(row)[::-1][:top_k]
        for j in order:
            value = float(row[j])
            if value < threshold:
                continue
            key = tuple(sorted((source, ids[int(j)])))
            edges[key] = max(edges.get(key, 0.0), value)
    return edges


def jaccard_edges(values: dict[str, set[str]], ids: list[str], *, top_k: int, threshold: float) -> dict[tuple[str, str], float]:
    matrix = np.zeros((len(ids), len(ids)), dtype=np.float32)
    for i in range(len(ids)):
        left = values.get(ids[i], set())
        for j in range(i + 1, len(ids)):
            right = values.get(ids[j], set())
            union = left | right
            if union:
                matrix[i, j] = matrix[j, i] = len(left & right) / len(union)
    return topk_matrix_edges(matrix, ids, top_k=top_k, threshold=threshold)



def tfidf_text_edges(
    texts: dict[str, str],
    ids: list[str],
    *,
    top_k: int,
    threshold: float,
) -> dict[tuple[str, str], float]:
    corpus = [str(texts.get(paper_id) or "").strip() for paper_id in ids]
    if sum(bool(x) for x in corpus) < 2:
        return {}
    try:
        matrix = TfidfVectorizer(
            stop_words="english",
            ngram_range=(1, 2),
            min_df=1,
            max_features=30000,
            sublinear_tf=True,
        ).fit_transform(corpus)
    except ValueError:
        return {}
    sims = cosine_similarity(matrix).astype(np.float32)
    return topk_matrix_edges(sims, ids, top_k=top_k, threshold=threshold)


def claim_profile(evidence: dict[str, Any]) -> tuple[str, list[str]]:
    texts: list[str] = []
    display: list[str] = []
    for item in evidence.get("claims", []) or []:
        if str(item.get("review_status") or "").strip().lower() == "rejected":
            continue
        statement = str(item.get("statement") or "").strip()
        parts = [
            statement,
            item.get("subject"),
            item.get("relation"),
            item.get("object"),
            item.get("conditions_text"),
            " ".join(str(x) for x in (item.get("curated_tags") or []) if x),
        ]
        text = " ".join(str(x).strip() for x in parts if str(x or "").strip())
        if text:
            texts.append(text)
        if statement:
            display.append(statement)
    return "\n".join(texts), display


def keyword_candidate_pairs(
    keywords: list[str],
    keyword_to_papers: dict[str, set[str]],
    paper_vectors: dict[str, np.ndarray],
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    if len(keywords) < 2:
        return []
    lexical = np.zeros((len(keywords), len(keywords)), dtype=np.float32)
    try:
        lex_matrix = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=1).fit_transform(keywords)
        lexical = cosine_similarity(lex_matrix).astype(np.float32)
    except ValueError:
        pass

    centroids: dict[str, np.ndarray] = {}
    for keyword in keywords:
        vecs = [paper_vectors[p] for p in keyword_to_papers.get(keyword, set()) if p in paper_vectors]
        if vecs:
            centroid = np.mean(np.vstack(vecs), axis=0).astype(np.float32)
            norm = float(np.linalg.norm(centroid))
            if norm > 0:
                centroids[keyword] = centroid / norm

    candidate_top_k = max(1, int(cfg.get("candidate_top_k", 7)))
    lexical_min = float(cfg.get("lexical_candidate_min", 0.12))
    context_min = float(cfg.get("context_candidate_min", 0.72))
    max_pairs = max(1, int(cfg.get("max_candidate_pairs", 220)))
    by_pair: dict[tuple[int, int], dict[str, float]] = {}

    for i, keyword in enumerate(keywords):
        order = np.argsort(lexical[i])[::-1]
        accepted = 0
        for raw_j in order:
            j = int(raw_j)
            if j == i:
                continue
            score = float(lexical[i, j])
            if score < lexical_min:
                break
            key = (min(i, j), max(i, j))
            by_pair.setdefault(key, {"lexical": 0.0, "context": 0.0})["lexical"] = max(by_pair.get(key, {}).get("lexical", 0.0), score)
            accepted += 1
            if accepted >= candidate_top_k:
                break

    context_top_k = max(1, int(cfg.get("context_top_k", 4)))
    for i, left in enumerate(keywords):
        if left not in centroids:
            continue
        scored: list[tuple[float, int]] = []
        for j in range(i + 1, len(keywords)):
            right = keywords[j]
            if right not in centroids:
                continue
            left_papers = keyword_to_papers.get(left, set())
            right_papers = keyword_to_papers.get(right, set())
            # Two one-off terms extracted from exactly the same single paper have
            # identical context by construction; do not infer synonymy from that.
            if len(left_papers) == len(right_papers) == 1 and left_papers == right_papers:
                continue
            score = float(np.dot(centroids[left], centroids[right]))
            if score >= context_min:
                scored.append((score, j))
        for score, j in sorted(scored, reverse=True)[:context_top_k]:
            key = (i, j)
            rec = by_pair.setdefault(key, {"lexical": 0.0, "context": 0.0})
            rec["context"] = max(rec.get("context", 0.0), score)

    ranked = sorted(
        by_pair.items(),
        key=lambda item: max(float(item[1].get("lexical", 0.0)), float(item[1].get("context", 0.0))),
        reverse=True,
    )[:max_pairs]
    result = []
    for n, ((i, j), scores) in enumerate(ranked, start=1):
        result.append({
            "pair_id": "KW" + sha256_text(keywords[i] + "\0" + keywords[j])[:12],
            "left": keywords[i],
            "right": keywords[j],
            "lexical_similarity": round(float(scores.get("lexical", 0.0)), 6),
            "context_similarity": round(float(scores.get("context", 0.0)), 6),
            "left_papers": sorted(keyword_to_papers.get(keywords[i], set())),
            "right_papers": sorted(keyword_to_papers.get(keywords[j], set())),
        })
    return result


def fallback_keyword_relations(candidates: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    equivalent_min = float(cfg.get("fallback_equivalent_lexical_min", 0.82))
    related_lexical_min = float(cfg.get("fallback_related_lexical_min", 0.45))
    related_context_min = float(cfg.get("fallback_related_context_min", 0.82))
    out = []
    for item in candidates:
        lexical = float(item.get("lexical_similarity", 0.0))
        context = float(item.get("context_similarity", 0.0))
        if lexical >= equivalent_min:
            relation = "equivalent"
            confidence = min(0.94, 0.72 + 0.25 * lexical)
        elif lexical >= related_lexical_min or context >= related_context_min:
            relation = "related"
            confidence = min(0.88, 0.58 + 0.25 * max(lexical, context))
        else:
            continue
        out.append({
            "pair_id": item["pair_id"],
            "left": item["left"],
            "right": item["right"],
            "relation": relation,
            "confidence": round(confidence, 4),
            "rationale": "Automatic fallback from lexical/corpus-context similarity; not a manual synonym merge.",
            "source": "heuristic_fallback",
        })
    return out


def infer_keyword_relations(
    candidates: list[dict[str, Any]],
    cfg: dict[str, Any],
    root: Path,
    llm_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    schema_path = root / "schemas/v4/keyword_relations.schema.json"
    prompt_path = root / "prompts/v4/keyword_relations_system.txt"
    schema = load_schema(schema_path)
    system_prompt = prompt_path.read_text(encoding="utf-8")
    # Cache identity deliberately excludes the model name so an already-reviewed
    # keyword pair remains reusable when the Qwen server is temporarily offline.
    # The model actually used is stored inside each cache record for provenance.
    signature = stable_json_hash({
        "script": SCRIPT_VERSION,
        "schema": sha256_file(schema_path),
        "prompt": sha256_file(prompt_path),
    })
    cache_root = root / "data/llm_raw/keyword_relations"
    cache_root.mkdir(parents=True, exist_ok=True)
    lookup = {item["pair_id"]: item for item in candidates}
    output: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    cache_paths: dict[str, Path] = {}
    for item in candidates:
        pair_key = sha256_text(signature + "\n" + normalize_key(item["left"]) + "\n" + normalize_key(item["right"]))[:20]
        cache_path = cache_root / f"pair_{pair_key}.json"
        cache_paths[item["pair_id"]] = cache_path
        if cache_path.exists():
            cached = read_json(cache_path)
            if cached.get("relation"):
                output.append({**item, **cached, "source": "qwen_cache"})
                continue
        missing.append(item)
    if output:
        print(f"  KW-REL-CACHE {len(output)}/{len(candidates)} pairs")

    if missing and bool(cfg.get("llm_enabled", True)):
        try:
            client = LlamaCppClient(llm_cfg)
            model = client.healthcheck()
            batch_size = max(1, int(cfg.get("llm_batch_size", 24)))
            for start in range(0, len(missing), batch_size):
                batch = missing[start:start + batch_size]
                compact = [{
                    "pair_id": x["pair_id"],
                    "left": x["left"],
                    "right": x["right"],
                    "lexical_similarity": x["lexical_similarity"],
                    "corpus_context_similarity": x["context_similarity"],
                } for x in batch]
                user_prompt = "CLASSIFY THESE SCIENTIFIC KEYWORD PAIRS:\n" + json.dumps(compact, ensure_ascii=False, indent=2)
                print(f"  KW-REL-LLM pairs {start + 1}-{min(start + batch_size, len(missing))}/{len(missing)} uncached")
                data = client.chat_json(system_prompt, user_prompt, schema).data
                for rel in data.get("relations", []):
                    candidate = lookup.get(rel.get("pair_id"))
                    if not candidate:
                        continue
                    cache_payload = {
                        "pair_id": candidate["pair_id"],
                        "left": candidate["left"],
                        "right": candidate["right"],
                        "relation": rel.get("relation"),
                        "confidence": rel.get("confidence"),
                        "rationale": rel.get("rationale"),
                        "model": model,
                        "signature": signature,
                    }
                    write_json(cache_paths[candidate["pair_id"]], cache_payload)
                    output.append({**candidate, **cache_payload, "source": "qwen"})
        except Exception as exc:
            print(f"WARNING: automatic keyword semantic inference unavailable ({exc}); using conservative fallback for uncached pairs")
            output.extend(fallback_keyword_relations(missing, cfg))
    elif missing:
        output.extend(fallback_keyword_relations(missing, cfg))

    minimum = float(cfg.get("llm_min_confidence", 0.72))
    selected = []
    for item in output:
        if item.get("relation") not in {"equivalent", "related"}:
            continue
        # Heuristic fallback has its own conservative confidence construction;
        # use the same configurable floor to avoid creating weak edges.
        if float(item.get("confidence", 0.0)) < minimum:
            continue
        selected.append(item)
    return selected

def keyword_relation_map(relations: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[tuple[str, str], float]:
    equivalent_weight = float(cfg.get("equivalent_weight", 1.0))
    related_weight = float(cfg.get("related_weight", 0.55))
    out: dict[tuple[str, str], float] = {}
    for item in relations:
        left = normalize_key(str(item.get("left") or ""))
        right = normalize_key(str(item.get("right") or ""))
        if not left or not right or left == right:
            continue
        relation = item.get("relation")
        base = equivalent_weight if relation == "equivalent" else related_weight if relation == "related" else 0.0
        if base <= 0:
            continue
        confidence = float(item.get("confidence", 1.0))
        key = tuple(sorted((left, right)))
        out[key] = max(out.get(key, 0.0), base * confidence)
    return out


def soft_keyword_edges(
    values: dict[str, set[str]],
    ids: list[str],
    relations: dict[tuple[str, str], float],
    *,
    top_k: int,
    threshold: float,
) -> dict[tuple[str, str], float]:
    matrix = np.zeros((len(ids), len(ids)), dtype=np.float32)
    for i in range(len(ids)):
        left = values.get(ids[i], set())
        if not left:
            continue
        for j in range(i + 1, len(ids)):
            right = values.get(ids[j], set())
            if not right:
                continue
            def best_score(source: set[str], target: set[str]) -> float:
                total = 0.0
                for a in source:
                    best = 0.0
                    for b in target:
                        if a == b:
                            continue  # exact/manual canonical overlap belongs to the ordinary keyword layer
                        best = max(best, relations.get(tuple(sorted((a, b))), 0.0))
                    total += best
                return total / max(1, len(source))
            score = 0.5 * (best_score(left, right) + best_score(right, left))
            matrix[i, j] = matrix[j, i] = score
    return topk_matrix_edges(matrix, ids, top_k=top_k, threshold=threshold)

def reference_signatures(records: list[dict[str, Any]]) -> set[str]:
    out = set()
    for record in records:
        resolved = record.get("resolved") or {}
        doi = normalize_doi(resolved.get("doi") or (record.get("original") or {}).get("doi"))
        if doi:
            out.add("doi:" + doi)
            continue
        openalex_id = normalize_openalex_id(resolved.get("openalex_id"))
        if openalex_id:
            out.add("oa:" + openalex_id)
            continue
        title = normalize_title(resolved.get("title") or (record.get("original") or {}).get("title"))
        if len(title) >= 18:
            out.add("title:" + title)
    return out


def bibliographic_coupling_edges(
    signatures: dict[str, set[str]],
    ids: list[str],
    *,
    top_k: int,
    threshold: float,
    min_shared: int,
) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], int]]:
    matrix = np.zeros((len(ids), len(ids)), dtype=np.float32)
    shared_counts: dict[tuple[str, str], int] = {}
    for i in range(len(ids)):
        left = signatures.get(ids[i], set())
        for j in range(i + 1, len(ids)):
            right = signatures.get(ids[j], set())
            if not left or not right:
                continue
            shared = len(left & right)
            if shared >= min_shared:
                value = shared / math.sqrt(len(left) * len(right))
                matrix[i, j] = matrix[j, i] = value
                shared_counts[(ids[i], ids[j])] = shared
    return topk_matrix_edges(matrix, ids, top_k=top_k, threshold=threshold), shared_counts


def layer_graph(ids: list[str], edges: dict[tuple[str, str], float]) -> ig.Graph:
    index = {paper_id: i for i, paper_id in enumerate(ids)}
    graph = ig.Graph(n=len(ids), edges=[(index[a], index[b]) for a, b in edges], directed=False)
    graph.vs["name"] = ids
    if edges:
        graph.es["weight"] = [float(value) for value in edges.values()]
    return graph


def cluster_multiplex(
    ids: list[str],
    layers: dict[str, dict[tuple[str, str], float]],
    weights: dict[str, float],
    *,
    resolution: float,
    seed: int,
) -> list[int]:
    active_names = [name for name, edges in layers.items() if edges and float(weights.get(name, 0.0)) != 0.0]
    if not active_names:
        return list(range(len(ids)))
    graphs = [layer_graph(ids, layers[name]) for name in active_names]
    layer_weights = [float(weights.get(name, 1.0)) for name in active_names]
    kwargs = {"weights": "weight", "resolution_parameter": resolution}
    try:
        if len(graphs) == 1:
            partition = la.find_partition(
                graphs[0],
                la.RBConfigurationVertexPartition,
                n_iterations=-1,
                seed=seed,
                **kwargs,
            )
            return list(partition.membership)
        membership, _ = la.find_partition_multiplex(
            graphs,
            la.RBConfigurationVertexPartition,
            layer_weights=layer_weights,
            n_iterations=-1,
            seed=seed,
            **kwargs,
        )
        return list(membership)
    except Exception as error:
        print(f"WARNING: multiplex Leiden failed ({error}); falling back to weighted aggregate Leiden")
        combined: dict[tuple[str, str], float] = defaultdict(float)
        for name, edges in layers.items():
            weight = float(weights.get(name, 1.0))
            for key, value in edges.items():
                combined[key] += weight * float(value)
        graph = layer_graph(ids, dict(combined))
        partition = la.find_partition(
            graph,
            la.RBConfigurationVertexPartition,
            weights="weight",
            resolution_parameter=resolution,
            n_iterations=-1,
            seed=seed,
        )
        return list(partition.membership)




def graphml_safe(value: Any) -> str | int | float | bool:
    """Convert arbitrary node/edge attributes into GraphML-supported scalars.

    NetworkX GraphML cannot serialize None, containers, NumPy scalars, or
    non-finite floats. Keep the semantic information while guaranteeing that
    export never fails merely because metadata are missing.
    """
    if value is None:
        return ""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

def palette(count: int) -> list[str]:
    colors = [
        "#8b5cf6", "#14b8a6", "#f59e0b", "#ef4444", "#3b82f6", "#ec4899",
        "#84cc16", "#06b6d4", "#f97316", "#a855f7", "#22c55e", "#eab308",
        "#6366f1", "#10b981", "#fb7185", "#38bdf8", "#c084fc", "#facc15",
    ]
    return [colors[index % len(colors)] for index in range(max(1, count))]


def cluster_labels(nodes: dict[str, dict[str, Any]], membership: dict[str, int]) -> dict[int, str]:
    grouped: dict[int, list[str]] = defaultdict(list)
    for paper_id, cluster_id in membership.items():
        grouped[cluster_id].append(paper_id)
    labels = {}
    for cluster_id, paper_ids in grouped.items():
        counter = Counter()
        for paper_id in paper_ids:
            counter.update("property:" + x for x in nodes[paper_id]["properties"])
            counter.update("method:" + x for x in nodes[paper_id]["methods"])
            counter.update("keyword:" + x for x in nodes[paper_id].get("keywords", []))
        terms = [term.split(":", 1)[1] for term, _ in counter.most_common(4)]
        labels[cluster_id] = " / ".join(terms) if terms else f"cluster {cluster_id + 1}"
    return labels


def make_gui(out_path: Path, payload: dict[str, Any], local_vis_js: Path | None) -> None:
    clusters = payload["clusters"]
    colors = palette(max(1, len(clusters)))
    color_by_cluster = {int(item["cluster_id"]): colors[index] for index, item in enumerate(clusters)}
    positions = payload.get("positions") or {}
    vis_nodes = []
    for node in payload["nodes"]:
        cluster_id = int(node["cluster_id"])
        color = color_by_cluster.get(cluster_id, "#8b5cf6")
        border = "#f59e0b" if node.get("validation_status") == "review_required" else "#d1d5db"
        pos = positions.get(node["paper_id"]) or {}
        vis_nodes.append(
            {
                "id": node["paper_id"],
                "label": node.get("display_label") or node["paper_id"],
                "title": html.escape((node.get("title") or node["paper_id"])[:500]),
                "value": node["node_size"],
                "cluster": cluster_id,
                "x": float(pos.get("x", 0.0)),
                "y": float(pos.get("y", 0.0)),
                "color": {
                    "background": color,
                    "border": border,
                    "highlight": {"background": "#ffffff", "border": color},
                },
                "font": {"color": "#e5e7eb", "size": 13},
                "borderWidth": 1.4,
            }
        )

    script_src = "assets/vis-network.min.js" if local_vis_js else "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"
    html_doc = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Multiplex Literature Network</title>
<script src="__VIS_JS__"></script>
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#151515;color:#e5e7eb;font-family:Inter,Segoe UI,Arial,sans-serif}
#network{position:absolute;inset:0 420px 0 0;background:#151515}
#side{position:absolute;right:0;top:0;bottom:0;width:420px;background:rgba(22,22,24,.985);border-left:1px solid #333;padding:18px;box-sizing:border-box;overflow:auto;box-shadow:-12px 0 28px rgba(0,0,0,.22)}
h2{font-size:18px;margin:0 0 12px}.muted{color:#9ca3af;font-size:12px;line-height:1.45}.section{border-top:1px solid #333;margin-top:16px;padding-top:14px}
select,input,button{width:100%;box-sizing:border-box;background:#232326;color:#eee;border:1px solid #3c3c42;border-radius:7px;padding:9px;margin:5px 0}button{cursor:pointer}button:disabled{opacity:.5;cursor:wait}.row{display:flex;gap:7px}.row>*{flex:1}.check{display:flex;align-items:center;gap:7px;font-size:13px;margin:7px 0}.check input{width:auto;margin:0}.swatch{width:10px;height:10px;border-radius:2px;flex:0 0 auto}.badge{display:inline-block;padding:3px 7px;border-radius:12px;background:#2c2c32;margin:2px;font-size:11px}.claim{font-size:12px;line-height:1.45;margin:8px 0;color:#d1d5db}.legend{display:flex;align-items:flex-start;gap:7px;font-size:11px;margin:6px 0}.dot{width:10px;height:10px;border-radius:50%;margin-top:2px;flex:0 0 auto}.openpdf{margin-top:10px;background:#303037;border-color:#5b5b66;font-weight:600}.openpdf:hover{border-color:#a3a3b0}.primary{background:#35354a;border-color:#6666a4;font-weight:650}.secondary{background:#29292e}.compact{font-size:11px;padding:7px}.rangeRow{display:grid;grid-template-columns:1fr 48px;gap:8px;align-items:center}.rangeRow input{margin:0}.help{font-size:11px;color:#a3a3ad;line-height:1.4}.chip{display:inline-block;padding:2px 6px;border-radius:10px;background:#25252a;font-size:10px;color:#b8b8c3;margin:1px}
#status{position:absolute;left:12px;bottom:10px;background:rgba(20,20,20,.78);padding:7px 10px;border-radius:7px;font-size:11px;color:#bbb;pointer-events:none;max-width:65vw}
</style>
</head>
<body>
<div id="network"></div><div id="status">Loading network…</div>
<div id="side">
  <h2>Multiplex Literature Network</h2>
  <div class="muted"><b>Project:</b> __PROJECT_LABEL__<br>
    Leiden clustering uses the complete selected layers. The canvas uses a sparse maximum-spanning-forest + strongest-neighbor backbone so visualization settings do not discard edges from clustering.
  </div>

  <div class="section">
    <label>Find paper</label><select id="paperSearch"></select>
    <label>Cluster</label><select id="clusterFilter"></select>
    <div class="row"><button id="fitBtn">Fit</button><button id="relaxBtn">Relax layout</button></div>
    <button id="resetBtn" class="secondary">Reset view and clusters</button>
  </div>

  <div class="section"><b>Layers</b>
    <label class="check" title="Directed research genealogy: A → B means A cites B"><input type="checkbox" data-rel="citation" checked><span class="swatch" style="background:#f97316"></span>Citation</label>
    <label class="check" title="Whole-paper semantic similarity from SPECTER2"><input type="checkbox" data-rel="semantic" checked><span class="swatch" style="background:#8b5cf6"></span>SPECTER2 paper semantic</label>
    <label class="check" title="Similarity of curated claims; rejected claims are excluded"><input type="checkbox" data-rel="claim" checked><span class="swatch" style="background:#ec4899"></span>Curated claim similarity</label>
    <label class="check" title="Shared studied properties"><input type="checkbox" data-rel="property" checked><span class="swatch" style="background:#14b8a6"></span>Property overlap</label>
    <label class="check" title="Shared experimental or computational methods"><input type="checkbox" data-rel="method" checked><span class="swatch" style="background:#3b82f6"></span>Method overlap</label>
    <label class="check" title="Exact overlap after human-controlled vocabulary normalization"><input type="checkbox" data-rel="keyword" checked><span class="swatch" style="background:#eab308"></span>Canonical keyword overlap</label>
    <label class="check" title="Automatically inferred equivalent/related keywords; does not rewrite curated terms"><input type="checkbox" data-rel="keyword_semantic" checked><span class="swatch" style="background:#22c55e"></span>Automatic keyword semantic relation</label>
    <label class="check" title="Papers citing many of the same references"><input type="checkbox" data-rel="bibliographic_coupling" checked><span class="swatch" style="background:#9ca3af"></span>Bibliographic coupling</label>
  </div>

  <div class="section"><b>View and recluster</b>
    <label>Rendering detail</label>
    <select id="performanceMode"><option value="fast">Fast</option><option value="balanced" selected>Balanced</option><option value="full">Full edges</option></select>
    <div class="help">Fast/Balanced display only a structure-preserving backbone. Full edges may be slow and visually dense.</div>
    <label>Leiden resolution: <b id="resolutionValue">1.00</b></label>
    <div class="rangeRow"><input id="resolution" type="range" min="0.20" max="3.00" step="0.05" value="1.00"><span id="resolutionBox">1.00</span></div>
    <button id="reclusterBtn" class="primary">Recluster using selected layers</button>
    <button id="restoreBtn" class="secondary">Restore saved all-layer clusters</button>
    <div id="reclusterInfo" class="help">Layer checkboxes change the view immediately. Press Recluster to recompute Leiden communities from the full selected-layer graph.</div>
  </div>

  <div class="section"><b>Clusters</b><div id="legend"></div></div>
  <div class="section"><b>Selected paper</b><div id="detail" class="muted">Click a node.</div></div>
</div>
<script>
'use strict';
const baseNodeArray=__NODES__;
const rawEdges=__RAW_EDGES__;
const nodeMeta=__NODE_META__;
const baseNodeMeta=JSON.parse(JSON.stringify(nodeMeta));
const baseClusters=__CLUSTER_META__;
const baseClusterColors=__CLUSTER_COLORS__;
const layerWeights=__LAYER_WEIGHTS__;
const layerColors=__LAYER_COLORS__;
const guiConfig=__GUI_CONFIG__;
const projectSlug=__PROJECT_SLUG__;
const networkSignature=__NETWORK_SIGNATURE__;
const API_BASE=(location.protocol==='http:'||location.protocol==='https:')?location.origin:'http://127.0.0.1:8766';
const palette=['#8b5cf6','#14b8a6','#f59e0b','#ef4444','#3b82f6','#ec4899','#84cc16','#06b6d4','#f97316','#a855f7','#22c55e','#eab308','#6366f1','#10b981','#fb7185','#38bdf8','#c084fc','#facc15'];
const layerOrder=['citation','semantic','claim','property','method','keyword','keyword_semantic','bibliographic_coupling'];
const baseMembership=Object.fromEntries(baseNodeArray.map(n=>[n.id,Number(n.cluster)]));
const basePositions=Object.fromEntries(baseNodeArray.map(n=>[n.id,{x:Number(n.x||0),y:Number(n.y||0)}]));
let currentMembership={...baseMembership};
let currentClusters=JSON.parse(JSON.stringify(baseClusters));
let clusterColors={...baseClusterColors};
let renderTimer=null;

function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function clamp(x,a,b){return Math.max(a,Math.min(b,x));}
function edgeKey(a,b){return a<b?`${a}\u0000${b}`:`${b}\u0000${a}`;}
function activeLayers(){return new Set([...document.querySelectorAll('[data-rel]')].filter(x=>x.checked).map(x=>x.dataset.rel));}
function selectedScore(edge,selected){const c=edge.components||{};let total=0;selected.forEach(name=>{total+=(Number(layerWeights[name]??1)*Number(c[name]||0));});return total;}
function dominantLayer(edge,selected){let best=null,bestScore=-1;const c=edge.components||{};selected.forEach(name=>{const score=Number(layerWeights[name]??1)*Number(c[name]||0);if(score>bestScore){best=name;bestScore=score;}});return best||'semantic';}

class DSU{constructor(ids){this.p=new Map(ids.map(x=>[x,x]));this.r=new Map(ids.map(x=>[x,0]));}find(x){let p=this.p.get(x);if(p!==x){p=this.find(p);this.p.set(x,p);}return p;}union(a,b){a=this.find(a);b=this.find(b);if(a===b)return false;let ra=this.r.get(a)||0,rb=this.r.get(b)||0;if(ra<rb)[a,b]=[b,a];this.p.set(b,a);if(ra===rb)this.r.set(a,ra+1);return true;}}

function performanceProfile(){
  const mode=document.getElementById('performanceMode').value;
  const n=baseNodeArray.length;
  const cfg=(guiConfig.profiles||{})[mode]||{};
  if(mode==='full')return{mode,topK:999999,maxEdges:999999999};
  const topK=Number(cfg.top_k??(mode==='fast'?3:6));
  const factor=Number(cfg.edge_factor??(mode==='fast'?4:8));
  return{mode,topK,maxEdges:Math.max(n-1,Math.ceil(factor*n))};
}

function chooseDisplayEdges(selected){
  const scored=rawEdges.map(e=>({edge:e,score:selectedScore(e,selected)})).filter(x=>x.score>0).sort((a,b)=>b.score-a.score);
  const profile=performanceProfile();
  if(profile.mode==='full')return scored;
  const keep=new Set();
  if(selected.has('citation'))for(const x of scored)if(Number((x.edge.components||{}).citation||0)>0)keep.add(edgeKey(x.edge.source,x.edge.target));
  const dsu=new DSU(baseNodeArray.map(n=>n.id));
  for(const x of scored)if(dsu.union(x.edge.source,x.edge.target))keep.add(edgeKey(x.edge.source,x.edge.target));
  const adj=new Map(baseNodeArray.map(n=>[n.id,[]]));
  for(const x of scored){const k=edgeKey(x.edge.source,x.edge.target);adj.get(x.edge.source)?.push([x.score,k]);adj.get(x.edge.target)?.push([x.score,k]);}
  for(const list of adj.values())for(const [,k] of list.sort((a,b)=>b[0]-a[0]).slice(0,profile.topK))keep.add(k);
  for(const x of scored){if(keep.size>=profile.maxEdges)break;keep.add(edgeKey(x.edge.source,x.edge.target));}
  return scored.filter(x=>keep.has(edgeKey(x.edge.source,x.edge.target)));
}

function buildVisEdge(item,selected,index){
  const e=item.edge,score=item.score,dominant=dominantLayer(e,selected),color=layerColors[dominant]||'#777777';
  const selectedComponents=layerOrder.filter(name=>selected.has(name)&&Number((e.components||{})[name]||0)>0);
  const title=selectedComponents.map(name=>`${name}=${Number(e.components[name]).toFixed(3)}`).join(' | ');
  const citationOn=selected.has('citation')&&Number((e.components||{}).citation||0)>0;
  return{id:`V${index}_${e.source}_${e.target}`,from:e.source,to:e.target,width:clamp(.45+score*1.5,.45,4.2),value:score,relations:selectedComponents,arrows:citationOn?(e.arrows||''):'',color:{color:color+'78',highlight:color},title};
}

const nodes=new vis.DataSet(baseNodeArray);
const edges=new vis.DataSet([]);
const network=new vis.Network(document.getElementById('network'),{nodes,edges},{
  autoResize:true,
  layout:{improvedLayout:false,randomSeed:Number(guiConfig.layout_seed||42)},
  interaction:{hover:false,hoverConnectedEdges:false,hideEdgesOnDrag:true,hideEdgesOnZoom:true,multiselect:true,selectConnectedEdges:false,tooltipDelay:250},
  physics:{enabled:false},
  nodes:{shape:'dot',scaling:{min:7,max:31},shadow:false},
  edges:{smooth:false,shadow:false,selectionWidth:1.1,hoverWidth:0,scaling:{min:.5,max:4.2}}
});

const search=document.getElementById('paperSearch');
search.innerHTML='<option value="">— select —</option>'+Object.values(nodeMeta).sort((a,b)=>(a.title||'').localeCompare(b.title||'')).map(n=>`<option value="${n.paper_id}">${esc(n.display_label||n.paper_id)} · ${esc((n.title||'').slice(0,72))}</option>`).join('');
const cf=document.getElementById('clusterFilter');

function newClusterColors(clusters){const out={};[...clusters].sort((a,b)=>a.cluster_id-b.cluster_id).forEach((c,i)=>out[c.cluster_id]=palette[i%palette.length]);return out;}
function renderClusterUI(){
  cf.innerHTML='<option value="all">All clusters</option>'+currentClusters.sort((a,b)=>a.cluster_id-b.cluster_id).map(c=>`<option value="${c.cluster_id}">C${c.cluster_id+1}: ${esc(c.label)} (${c.size})</option>`).join('');
  document.getElementById('legend').innerHTML=currentClusters.map(c=>`<div class="legend"><span class="dot" style="background:${clusterColors[c.cluster_id]}"></span><span>C${c.cluster_id+1}: ${esc(c.label)} (${c.size})</span></div>`).join('');
}
function applyClusterFilter(){const cluster=cf.value;const updates=baseNodeArray.map(n=>({id:n.id,hidden:cluster!=='all'&&String(currentMembership[n.id])!==cluster}));nodes.update(updates);}

function updateNodeAppearance(selected){
  const degree=Object.fromEntries(baseNodeArray.map(n=>[n.id,0]));
  for(const e of rawEdges){const s=selectedScore(e,selected);if(s>0){degree[e.source]=(degree[e.source]||0)+s;degree[e.target]=(degree[e.target]||0)+s;}}
  const updates=baseNodeArray.map(n=>{
    const cid=Number(currentMembership[n.id]??0);const meta=nodeMeta[n.id];meta.cluster_id=cid;meta.cluster_label=(currentClusters.find(c=>Number(c.cluster_id)===cid)||{}).label||`cluster ${cid+1}`;
    return{id:n.id,cluster:cid,value:8+Math.min(24,6*Math.log1p(degree[n.id]||0)),color:{background:clusterColors[cid]||'#8b5cf6',border:meta.validation_status==='review_required'?'#f59e0b':'#d1d5db',highlight:{background:'#ffffff',border:clusterColors[cid]||'#8b5cf6'}}};
  });
  nodes.update(updates);
}

function applyLayerView(){
  const selected=activeLayers();
  if(!selected.size){edges.clear();updateNodeAppearance(selected);document.getElementById('status').textContent=`${baseNodeArray.length} papers · no layer selected`;return;}
  const chosen=chooseDisplayEdges(selected);
  edges.clear();edges.add(chosen.map((x,i)=>buildVisEdge(x,selected,i)));
  updateNodeAppearance(selected);applyClusterFilter();
  const profile=performanceProfile();
  document.getElementById('status').textContent=`${baseNodeArray.length} papers · ${chosen.length}/${rawEdges.length} edges drawn · ${profile.mode} · ${selected.size} layers`;
  localStorage.setItem(`foliosort.network.view.${projectSlug}`,JSON.stringify({layers:[...selected],mode:profile.mode,resolution:Number(document.getElementById('resolution').value)}));
}
function scheduleView(){clearTimeout(renderTimer);renderTimer=setTimeout(applyLayerView,90);}

async function recluster(){
  const selected=[...activeLayers()];if(!selected.length){alert('Select at least one layer.');return;}
  const resolution=Number(document.getElementById('resolution').value);
  const btn=document.getElementById('reclusterBtn');const info=document.getElementById('reclusterInfo');btn.disabled=true;info.textContent='Running Leiden on the complete selected-layer graph…';
  try{
    const response=await fetch(`${API_BASE}/api/network/recluster?project=${encodeURIComponent(projectSlug)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({layers:selected,resolution})});
    const result=await response.json();if(!response.ok)throw new Error(result.error||response.statusText);
    currentMembership={...result.membership};currentClusters=result.clusters||[];clusterColors=newClusterColors(currentClusters);
    const updates=[];for(const [id,pos] of Object.entries(result.positions||{}))updates.push({id,x:Number(pos.x||0),y:Number(pos.y||0)});if(updates.length)nodes.update(updates);
    renderClusterUI();cf.value='all';applyLayerView();network.fit({animation:{duration:280,easingFunction:'easeInOutQuad'}});
    localStorage.setItem(`foliosort.network.recluster.${projectSlug}`,JSON.stringify(result));
    info.textContent=`Reclustered into ${currentClusters.length} communities using ${selected.length} layer(s), resolution ${resolution.toFixed(2)}.`;
  }catch(error){info.textContent='Reclustering failed. Make sure FolioSort is running.';alert('Could not recluster the selected layers.\n\n'+error);}finally{btn.disabled=false;}
}
function restoreBase(){
  currentMembership={...baseMembership};currentClusters=JSON.parse(JSON.stringify(baseClusters));clusterColors={...baseClusterColors};
  nodes.update(baseNodeArray.map(n=>({id:n.id,x:basePositions[n.id].x,y:basePositions[n.id].y})));
  for(const id of Object.keys(nodeMeta)){nodeMeta[id].cluster_id=baseNodeMeta[id].cluster_id;nodeMeta[id].cluster_label=baseNodeMeta[id].cluster_label;}
  renderClusterUI();cf.value='all';localStorage.removeItem(`foliosort.network.recluster.${projectSlug}`);applyLayerView();network.fit({animation:{duration:240,easingFunction:'easeInOutQuad'}});document.getElementById('reclusterInfo').textContent='Restored the saved all-layer Leiden partition.';
}
function restoreCachedRecluster(){
  try{const raw=localStorage.getItem(`foliosort.network.recluster.${projectSlug}`);if(!raw)return false;const r=JSON.parse(raw);if(r.network_signature!==networkSignature)return false;currentMembership={...r.membership};currentClusters=r.clusters||[];clusterColors=newClusterColors(currentClusters);const updates=[];for(const [id,pos] of Object.entries(r.positions||{}))updates.push({id,x:Number(pos.x||0),y:Number(pos.y||0)});if(updates.length)nodes.update(updates);renderClusterUI();return true;}catch(_){return false;}
}

function showDetail(id){const n=nodeMeta[id];if(!n)return;const props=(n.properties||[]).map(x=>`<span class="badge">${esc(x)}</span>`).join('');const methods=(n.methods||[]).map(x=>`<span class="badge">${esc(x)}</span>`).join('');const keywords=(n.keywords||[]).map(x=>`<span class="badge">${esc(x)}</span>`).join('');const claims=(n.claims||[]).slice(0,6).map(x=>`<div class="claim">• ${esc(x)}</div>`).join('');document.getElementById('detail').innerHTML=`<div><b>${esc(n.display_label||n.paper_id)}</b> <span class="muted">(${esc(n.paper_id)})</span></div><div style="font-size:14px;margin:8px 0"><b>${esc(n.title||'(untitled)')}</b></div><div class="muted">${esc(n.journal||'')}<br>${esc(n.doi||'')}<br>${esc(n.original_filename||n.source_relpath||'')}<br>Cluster C${Number(n.cluster_id)+1}: ${esc(n.cluster_label||'')}<br>Validation: ${esc(n.validation_status||'')}</div><button class="openpdf" id="openPdfBtn">Open original PDF</button><button id="openCurBtn" class="secondary">Open in Curation Editor</button><div style="margin-top:10px"><b>Properties</b><br>${props||'<span class="muted">none</span>'}</div><div style="margin-top:8px"><b>Methods</b><br>${methods||'<span class="muted">none</span>'}</div><div style="margin-top:8px"><b>Keywords</b><br>${keywords||'<span class="muted">none</span>'}</div><div style="margin-top:8px"><b>Representative claims</b>${claims||'<div class="muted">none</div>'}</div>`;document.getElementById('openPdfBtn').onclick=()=>openOriginalPdf(id);document.getElementById('openCurBtn').onclick=()=>openCuration(id);}
async function openOriginalPdf(id){const n=nodeMeta[id];if(!n)return;const status=document.getElementById('status');status.textContent=`Opening ${n.display_label||id}…`;try{const r=await fetch(`${API_BASE}/api/open_pdf?id=${encodeURIComponent(id)}`,{method:'POST'});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);status.textContent=`Opened ${n.display_label||id}`;}catch(e){status.textContent='PDF opener is not running. Start FolioSort.';alert('Could not open the original PDF. Start FolioSort first.\n\n'+e);}}
async function openCuration(id){try{await fetch(`${API_BASE}/api/start_curation`,{method:'POST'});window.open(`http://127.0.0.1:8765/?paper=${encodeURIComponent(id)}`,'_blank');}catch(e){alert('Start FolioSort before opening the curation editor.\n\n'+e);}}

function relaxLayout(){const btn=document.getElementById('relaxBtn');btn.disabled=true;btn.textContent='Relaxing…';network.setOptions({physics:{enabled:true,solver:'barnesHut',barnesHut:{gravitationalConstant:-4200,centralGravity:.18,springLength:115,springConstant:.035,damping:.35,avoidOverlap:.25},stabilization:{enabled:true,iterations:120,fit:false}}});network.stabilize(120);network.once('stabilized',()=>{network.setOptions({physics:{enabled:false}});btn.disabled=false;btn.textContent='Relax layout';});}

renderClusterUI();
const initialMode=String(guiConfig.initial_performance_mode||'balanced');document.getElementById('performanceMode').value=['fast','balanced','full'].includes(initialMode)?initialMode:'balanced';
const initialResolution=Number(guiConfig.recluster_resolution_default||1.0);document.getElementById('resolution').value=String(initialResolution);document.getElementById('resolutionBox').textContent=initialResolution.toFixed(2);document.getElementById('resolutionValue').textContent=initialResolution.toFixed(2);
try{const saved=JSON.parse(localStorage.getItem(`foliosort.network.view.${projectSlug}`)||'{}');if(Array.isArray(saved.layers)){document.querySelectorAll('[data-rel]').forEach(x=>x.checked=saved.layers.includes(x.dataset.rel));}if(saved.mode)document.getElementById('performanceMode').value=saved.mode;if(saved.resolution){document.getElementById('resolution').value=String(saved.resolution);document.getElementById('resolutionBox').textContent=Number(saved.resolution).toFixed(2);document.getElementById('resolutionValue').textContent=Number(saved.resolution).toFixed(2);}}catch(_){}
restoreCachedRecluster();
applyLayerView();setTimeout(()=>network.fit({animation:false}),80);

document.querySelectorAll('[data-rel]').forEach(x=>x.addEventListener('change',scheduleView));
document.getElementById('performanceMode').addEventListener('change',scheduleView);
document.getElementById('resolution').addEventListener('input',e=>{const v=Number(e.target.value).toFixed(2);document.getElementById('resolutionBox').textContent=v;document.getElementById('resolutionValue').textContent=v;});
document.getElementById('reclusterBtn').onclick=recluster;document.getElementById('restoreBtn').onclick=restoreBase;
cf.addEventListener('change',()=>{applyClusterFilter();network.fit({animation:{duration:180}});});
search.addEventListener('change',()=>{if(!search.value)return;nodes.update({id:search.value,hidden:false});network.selectNodes([search.value]);network.focus(search.value,{scale:1.65,animation:true});showDetail(search.value);});
document.getElementById('fitBtn').onclick=()=>network.fit({animation:{duration:180}});document.getElementById('relaxBtn').onclick=relaxLayout;
document.getElementById('resetBtn').onclick=()=>{document.querySelectorAll('[data-rel]').forEach(x=>x.checked=true);document.getElementById('performanceMode').value='balanced';document.getElementById('resolution').value='1.00';document.getElementById('resolutionBox').textContent='1.00';document.getElementById('resolutionValue').textContent='1.00';restoreBase();};
network.on('click',p=>{if(p.nodes.length)showDetail(p.nodes[0]);});network.on('doubleClick',p=>{if(p.nodes.length)openOriginalPdf(p.nodes[0]);});
</script>
</body></html>'''
    replacements = {
        "__VIS_JS__": script_src,
        "__NODES__": json.dumps(vis_nodes, ensure_ascii=False),
        "__RAW_EDGES__": json.dumps(payload["edges"], ensure_ascii=False),
        "__NODE_META__": json.dumps({node["paper_id"]: node for node in payload["nodes"]}, ensure_ascii=False),
        "__CLUSTER_META__": json.dumps(clusters, ensure_ascii=False),
        "__CLUSTER_COLORS__": json.dumps({str(key): value for key, value in color_by_cluster.items()}),
        "__LAYER_WEIGHTS__": json.dumps(payload.get("layer_weights") or {}, ensure_ascii=False),
        "__LAYER_COLORS__": json.dumps(LAYER_COLORS, ensure_ascii=False),
        "__GUI_CONFIG__": json.dumps(payload.get("gui_config") or {}, ensure_ascii=False),
        "__PROJECT_SLUG__": json.dumps(str((payload.get("project") or {}).get("slug") or "default")),
        "__NETWORK_SIGNATURE__": json.dumps(str((payload.get("provenance") or {}).get("network_signature") or "")),
        "__PROJECT_LABEL__": html.escape(str((payload.get("project") or {}).get("name") or "All papers")),
    }
    for key, value in replacements.items():
        html_doc = html_doc.replace(key, value)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")

def main() -> None:
    ap = argparse.ArgumentParser(description="Build a SPECTER2/citation/property/method/keyword multiplex graph and Leiden clusters.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--project", default=None, help="Build only one project. REVIEW_PROJECT is used when omitted.")
    args = ap.parse_args()
    config, root = load_config(args.config)
    paths = get_paths(config, root)
    curated_dir = paths.get("curated", root / "data/curated")
    use_curated = bool(config.get("curation", {}).get("enabled", True) and config.get("curation", {}).get("use_curated_for_graphs", True))
    cfg = config.get("multiplex_graph", {})
    metadata_dir = paths.get("metadata", root / "data/metadata")
    reference_dir = paths.get("reference_matches", root / "data/reference_matches")
    embedding_dir = paths.get("embeddings", root / "data/embeddings")
    project_arg = args.project or os.environ.get("REVIEW_PROJECT")
    project_slug = normalize_project_slug(project_arg) if project_arg else None

    conn = sqlite3.connect(paths["database"])
    conn.row_factory = sqlite3.Row
    ensure_project_schema(conn)
    if project_slug:
        rows = project_rows(conn, project_slug, active_only=True)
        selected_project_name = project_name(conn, project_slug)
        out_dir = project_network_dir(root, project_slug)
    else:
        rows = conn.execute("SELECT * FROM papers WHERE active=1 ORDER BY paper_id").fetchall()
        selected_project_name = "All papers"
        out_dir = paths.get("network_gui", root / "outputs/network_gui")
    out_dir.mkdir(parents=True, exist_ok=True)
    allowed = set(cfg.get("include_validation_status", ["pass", "review_required"]))
    nodes: dict[str, dict[str, Any]] = {}
    reference_payloads: dict[str, dict[str, Any]] = {}
    for row in rows:
        paper_id = row["paper_id"]
        paper_path = paths["paper_json"] / f"{paper_id}.json"
        raw_inventory_path = paths["extracted"] / f"{paper_id}.inventory.json"
        raw_evidence_path = paths["extracted"] / f"{paper_id}.evidence.json"
        curated_inventory_path = curated_dir / f"{paper_id}.inventory.json"
        curated_evidence_path = curated_dir / f"{paper_id}.evidence.json"
        inventory_path = curated_inventory_path if use_curated and curated_inventory_path.exists() else raw_inventory_path
        evidence_path = curated_evidence_path if use_curated and curated_evidence_path.exists() else raw_evidence_path
        validation_path = paths["extracted"] / f"{paper_id}.validation.json"
        raw_metadata_path = metadata_dir / f"{paper_id}.metadata.json"
        curated_metadata_path = curated_dir / f"{paper_id}.metadata.json"
        metadata_path = curated_metadata_path if use_curated and curated_metadata_path.exists() else raw_metadata_path
        memory_path = paths.get("summary_memory", root / "data/summary_memory") / f"{paper_id}.memory.json"
        reference_path = reference_dir / f"{paper_id}.references.json"
        if not all(path.exists() for path in [paper_path, inventory_path, evidence_path, validation_path, memory_path]):
            continue
        paper = read_json(paper_path)
        inventory = read_json(inventory_path)
        evidence = read_json(evidence_path)
        validation = read_json(validation_path)
        metadata = read_json(metadata_path) if metadata_path.exists() else {}
        memory = read_json(memory_path)
        status = validation.get("overall_status", "")
        if allowed and status not in allowed:
            continue
        canonical = merged_canonical_metadata(paper, metadata)
        properties = sorted(normalized_set(inventory.get("studied_properties", []), "property_normalized", "property_raw"))
        methods = sorted(normalized_set(inventory.get("methods", []), "method_normalized", "method_raw"))
        keywords = sorted(normalized_set(inventory.get("keywords", []), "keyword_normalized", "keyword_raw"))
        claim_text, claims = claim_profile(evidence)
        authors = canonical.get("authors") or (paper.get("metadata") or {}).get("authors") or []
        year = canonical.get("year") if canonical.get("year") not in (None, "") else row["year"]
        nodes[paper_id] = {
            "paper_id": paper_id,
            "display_label": paper_display_label(authors, year, paper_id),
            "authors": authors,
            "title": canonical.get("title") or row["title"] or paper_id,
            "year": year,
            "journal": canonical.get("journal") or row["journal"],
            "doi": canonical.get("doi") or row["doi"],
            "openalex_id": canonical.get("openalex_id"),
            "source_relpath": row["source_relpath"],
            "original_filename": row["original_filename"],
            "validation_status": status,
            "properties": properties,
            "methods": methods,
            "keywords": keywords,
            "claims": claims,
            "claim_profile_text": claim_text,
            "central_question": memory.get("central_question"),
        }
        reference_payloads[paper_id] = read_json(reference_path) if reference_path.exists() else {"references": []}

    ids = sorted(nodes)
    if not ids:
        raise SystemExit(f"No fully processed papers found for project {project_slug or 'all'}")

    layers: dict[str, dict[tuple[str, str], float]] = {
        "citation": {},
        "semantic": {},
        "claim": {},
        "property": {},
        "method": {},
        "keyword": {},
        "keyword_semantic": {},
        "bibliographic_coupling": {},
    }
    citation_directions: dict[tuple[str, str], set[str]] = defaultdict(set)
    for citing in ids:
        payload = reference_payloads.get(citing, {})
        targets = [item.get("target_paper_id") for item in payload.get("references", [])]
        targets.extend(item.get("target_paper_id") for item in payload.get("openalex_only_local_edges", []))
        for target in targets:
            if target in nodes and target != citing:
                key = tuple(sorted((citing, target)))
                layers["citation"][key] = 1.0
                citation_directions[key].add(f"{citing}->{target}")

    paper_vectors: dict[str, np.ndarray] = {}
    embedding_matrix_path = embedding_dir / "specter2.npy"
    embedding_index_path = embedding_dir / "specter2.index.json"
    if embedding_matrix_path.exists() and embedding_index_path.exists():
        matrix = np.load(embedding_matrix_path)
        index = read_json(embedding_index_path)
        lookup = {item["paper_id"]: i for i, item in enumerate(index.get("items", []))}
        if all(paper_id in lookup for paper_id in ids):
            selected = np.vstack([matrix[lookup[paper_id]] for paper_id in ids]).astype(np.float32)
            paper_vectors = {paper_id: selected[index] for index, paper_id in enumerate(ids)}
            similarity = selected @ selected.T
            semantic_cfg = cfg.get("semantic", {})
            layers["semantic"] = topk_matrix_edges(
                similarity,
                ids,
                top_k=int(semantic_cfg.get("top_k_per_paper", 8)),
                threshold=float(semantic_cfg.get("min_similarity", 0.55)),
            )
        else:
            print("WARNING: SPECTER2 index does not cover all included papers; semantic layer omitted")
    else:
        print("WARNING: SPECTER2 embeddings missing; semantic layer omitted")

    claim_cfg = cfg.get("claim", {})
    layers["claim"] = tfidf_text_edges(
        {paper_id: str(nodes[paper_id].get("claim_profile_text") or "") for paper_id in ids},
        ids,
        top_k=int(claim_cfg.get("top_k_per_paper", 7)),
        threshold=float(claim_cfg.get("min_similarity", 0.12)),
    )

    property_cfg = cfg.get("property", {})
    method_cfg = cfg.get("method", {})
    keyword_cfg = cfg.get("keyword", {})
    layers["property"] = jaccard_edges(
        {paper_id: set(nodes[paper_id]["properties"]) for paper_id in ids},
        ids,
        top_k=int(property_cfg.get("top_k_per_paper", 7)),
        threshold=float(property_cfg.get("min_jaccard", 0.18)),
    )
    layers["method"] = jaccard_edges(
        {paper_id: set(nodes[paper_id]["methods"]) for paper_id in ids},
        ids,
        top_k=int(method_cfg.get("top_k_per_paper", 7)),
        threshold=float(method_cfg.get("min_jaccard", 0.18)),
    )
    layers["keyword"] = jaccard_edges(
        {paper_id: set(nodes[paper_id].get("keywords", [])) for paper_id in ids},
        ids,
        top_k=int(keyword_cfg.get("top_k_per_paper", 7)),
        threshold=float(keyword_cfg.get("min_jaccard", 0.15)),
    )

    keyword_sem_cfg = cfg.get("keyword_semantic", {})
    keyword_candidates: list[dict[str, Any]] = []
    inferred_keyword_relations: list[dict[str, Any]] = []
    if bool(keyword_sem_cfg.get("enabled", True)):
        keyword_to_papers: dict[str, set[str]] = defaultdict(set)
        for paper_id in ids:
            for keyword in nodes[paper_id].get("keywords", []):
                keyword_to_papers[keyword].add(paper_id)
        unique_keywords = sorted(keyword_to_papers)
        keyword_candidates = keyword_candidate_pairs(unique_keywords, keyword_to_papers, paper_vectors, keyword_sem_cfg)
        inferred_keyword_relations = infer_keyword_relations(keyword_candidates, keyword_sem_cfg, root, config.get("llm", {}))
        kw_relation_map = keyword_relation_map(inferred_keyword_relations, keyword_sem_cfg)
        layers["keyword_semantic"] = soft_keyword_edges(
            {paper_id: set(nodes[paper_id].get("keywords", [])) for paper_id in ids},
            ids,
            kw_relation_map,
            top_k=int(keyword_sem_cfg.get("top_k_per_paper", 7)),
            threshold=float(keyword_sem_cfg.get("min_similarity", 0.10)),
        )
    write_json(out_dir / "keyword_relations.json", {
        "script_version": SCRIPT_VERSION,
        "project": project_slug or "all",
        "enabled": bool(keyword_sem_cfg.get("enabled", True)),
        "note": "Automatic relations do not rewrite curated/raw keywords. 'equivalent' is an inferred network relation, not a human-approved canonical merge.",
        "candidates": keyword_candidates,
        "relations": inferred_keyword_relations,
    })

    coupling_cfg = cfg.get("bibliographic_coupling", {})
    signatures = {
        paper_id: reference_signatures(reference_payloads.get(paper_id, {}).get("references", []))
        for paper_id in ids
    }
    coupling_edges, shared_counts = bibliographic_coupling_edges(
        signatures,
        ids,
        top_k=int(coupling_cfg.get("top_k_per_paper", 6)),
        threshold=float(coupling_cfg.get("min_similarity", 0.08)),
        min_shared=int(coupling_cfg.get("min_shared_references", 2)),
    )
    layers["bibliographic_coupling"] = coupling_edges

    default_layer_weights = {
        "citation": 1.30,
        "semantic": 0.75,
        "claim": 0.30,
        "property": 0.45,
        "method": 0.35,
        "keyword": 0.25,
        "keyword_semantic": 0.20,
        "bibliographic_coupling": 0.45,
    }
    layer_weights = {**default_layer_weights, **(cfg.get("layer_weights") or {})}
    clustering_cfg = cfg.get("clustering", {})
    membership_values = cluster_multiplex(
        ids,
        layers,
        layer_weights,
        resolution=float(clustering_cfg.get("resolution", 1.0)),
        seed=int(clustering_cfg.get("seed", 42)),
    )
    unique = sorted(set(membership_values))
    remap = {old: new for new, old in enumerate(unique)}
    membership = {paper_id: remap[membership_values[index]] for index, paper_id in enumerate(ids)}
    labels = cluster_labels(nodes, membership)

    combined: dict[tuple[str, str], dict[str, Any]] = {}
    for layer_name, edge_map in layers.items():
        for key, value in edge_map.items():
            record = combined.setdefault(
                key,
                {"source": key[0], "target": key[1], "components": {}, "directions": []},
            )
            record["components"][layer_name] = float(value)
    for key, record in combined.items():
        record["combined_weight"] = sum(
            float(layer_weights.get(name, 1.0)) * float(value)
            for name, value in record["components"].items()
        )
        record["directions"] = sorted(citation_directions.get(key, set()))
        source, target = key
        both = {f"{source}->{target}", f"{target}->{source}"}.issubset(set(record["directions"]))
        if both:
            record["arrows"] = "to,from"
        elif f"{source}->{target}" in record["directions"]:
            record["arrows"] = "to"
        elif f"{target}->{source}" in record["directions"]:
            record["arrows"] = "from"
        else:
            record["arrows"] = ""
        if key in shared_counts:
            record["shared_references"] = shared_counts[key]
    edges = sorted(combined.values(), key=lambda item: item["combined_weight"], reverse=True)
    gui_cfg = cfg.get("gui", {})
    positions = compute_layout_positions(
        ids,
        edges,
        set(layers),
        layer_weights,
        seed=int(clustering_cfg.get("seed", 42)),
        top_k=int(gui_cfg.get("layout_top_k", 6)),
        edge_factor=float(gui_cfg.get("layout_edge_factor", 8.0)),
        large_graph_threshold=int(gui_cfg.get("layout_large_graph_threshold", 200)),
        scale=float(gui_cfg.get("layout_scale", 850.0)),
    )

    weighted_degree = defaultdict(float)
    for edge in edges:
        weighted_degree[edge["source"]] += edge["combined_weight"]
        weighted_degree[edge["target"]] += edge["combined_weight"]
    payload_nodes = []
    for paper_id in ids:
        node = dict(nodes[paper_id])
        cluster_id = membership[paper_id]
        node.update(
            {
                "cluster_id": cluster_id,
                "cluster_label": labels[cluster_id],
                "node_size": 8.0 + min(24.0, 6.0 * math.log1p(weighted_degree[paper_id])),
            }
        )
        payload_nodes.append(node)
    clusters = [
        {
            "cluster_id": cluster_id,
            "label": labels[cluster_id],
            "size": sum(membership[paper_id] == cluster_id for paper_id in ids),
        }
        for cluster_id in sorted(labels)
    ]
    layer_records = {
        name: [{"source": key[0], "target": key[1], "weight": value} for key, value in edge_map.items()]
        for name, edge_map in layers.items()
    }
    network_signature = stable_json_hash({
        "script": SCRIPT_VERSION,
        "ids": ids,
        "layers": layer_records,
        "weights": layer_weights,
    })[:20]
    payload = {
        "nodes": payload_nodes,
        "edges": edges,
        "clusters": clusters,
        "layers": layer_records,
        "layer_weights": layer_weights,
        "positions": positions,
        "gui_config": gui_cfg,
        "project": {"slug": project_slug, "name": selected_project_name},
        "provenance": {
            "script_version": SCRIPT_VERSION,
            "algorithm": "Leiden multiplex",
            "curation_enabled": use_curated,
            "project_slug": project_slug,
            "network_signature": network_signature,
            "layout": "precomputed sparse-backbone Fruchterman-Reingold/DrL",
            "rendering": "maximum-spanning-forest + symmetric top-k edge backbone",
        },
    }
    write_json(out_dir / "network.json", payload)

    with (out_dir / "nodes.csv").open("w", encoding="utf-8-sig", newline="") as file:
        fields = ["paper_id", "display_label", "title", "year", "journal", "doi", "source_relpath", "original_filename", "validation_status", "cluster_id", "cluster_label", "node_size"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for node in payload_nodes:
            writer.writerow({key: node.get(key) for key in fields})
    with (out_dir / "edges.csv").open("w", encoding="utf-8-sig", newline="") as file:
        fields = ["source", "target", "combined_weight", "citation", "semantic", "claim", "property", "method", "keyword", "keyword_semantic", "bibliographic_coupling", "shared_references", "directions"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for edge in edges:
            writer.writerow(
                {
                    "source": edge["source"],
                    "target": edge["target"],
                    "combined_weight": edge["combined_weight"],
                    "citation": edge["components"].get("citation", 0),
                    "semantic": edge["components"].get("semantic", 0),
                    "claim": edge["components"].get("claim", 0),
                    "property": edge["components"].get("property", 0),
                    "method": edge["components"].get("method", 0),
                    "keyword": edge["components"].get("keyword", 0),
                    "keyword_semantic": edge["components"].get("keyword_semantic", 0),
                    "bibliographic_coupling": edge["components"].get("bibliographic_coupling", 0),
                    "shared_references": edge.get("shared_references", 0),
                    "directions": ";".join(edge.get("directions", [])),
                }
            )

    graph = nx.Graph()
    for node in payload_nodes:
        attrs = {
            key: graphml_safe(value)
            for key, value in node.items()
            if key != "paper_id"
        }
        graph.add_node(node["paper_id"], **attrs)
    for edge in edges:
        attrs = {
            "weight": graphml_safe(edge.get("combined_weight")),
            "shared_references": graphml_safe(edge.get("shared_references", 0)),
            "directions": graphml_safe(edge.get("directions", [])),
        }
        attrs.update(
            {f"layer_{key}": graphml_safe(value) for key, value in edge.get("components", {}).items()}
        )
        graph.add_edge(edge["source"], edge["target"], **attrs)
    nx.write_graphml(graph, out_dir / "network.graphml")

    local_vis = root / "assets/vis-network.min.js"
    if local_vis.exists():
        asset_dir = out_dir / "assets"
        asset_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_vis, asset_dir / "vis-network.min.js")
        local_asset = asset_dir / "vis-network.min.js"
    else:
        local_asset = None
    make_gui(out_dir / "network.html", payload, local_asset)
    print(f"Project : {selected_project_name} ({project_slug or 'all'})")
    print(f"Papers  : {len(ids)}")
    for name, edge_map in layers.items():
        print(f"Layer {name:24s}: {len(edge_map)} edges")
    print(f"Clusters: {len(clusters)}")
    for cluster in clusters:
        print(f"  C{cluster['cluster_id']+1}: {cluster['label']} ({cluster['size']})")
    print(f"GUI     : {out_dir / 'network.html'}")


if __name__ == "__main__":
    main()
