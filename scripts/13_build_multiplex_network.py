#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import math
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import get_paths, load_config, normalize_key, read_json, write_json
from lib.v4_common import normalize_doi, normalize_openalex_id, normalize_title

SCRIPT_VERSION = "multiplex-network-v4.1.0-curated-author-year-labels-pdf-open-graphml-safe"


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
        terms = [term.split(":", 1)[1] for term, _ in counter.most_common(4)]
        labels[cluster_id] = " / ".join(terms) if terms else f"cluster {cluster_id + 1}"
    return labels


def make_gui(out_path: Path, payload: dict[str, Any], local_vis_js: Path | None) -> None:
    clusters = payload["clusters"]
    colors = palette(len(clusters))
    color_by_cluster = {int(item["cluster_id"]): colors[index] for index, item in enumerate(clusters)}
    vis_nodes = []
    for node in payload["nodes"]:
        color = color_by_cluster.get(int(node["cluster_id"]), "#8b5cf6")
        border = "#f59e0b" if node.get("validation_status") == "review_required" else "#d1d5db"
        vis_nodes.append(
            {
                "id": node["paper_id"],
                "label": node.get("display_label") or node["paper_id"],
                "title": html.escape((node.get("title") or node["paper_id"])[:500]),
                "value": node["node_size"],
                "cluster": node["cluster_id"],
                "color": {"background": color, "border": border, "highlight": {"background": "#ffffff", "border": color}},
                "font": {"color": "#e5e7eb", "size": 13},
                "borderWidth": 1.4,
            }
        )
    edge_colors = {
        "citation": "#f97316",
        "semantic": "#8b5cf6",
        "property": "#14b8a6",
        "method": "#3b82f6",
        "bibliographic_coupling": "#9ca3af",
    }
    vis_edges = []
    for index, edge in enumerate(payload["edges"]):
        components = edge["components"]
        relations = [name for name, value in components.items() if value > 0]
        dominant = max(relations, key=lambda name: payload["layer_weights"].get(name, 1.0) * components[name]) if relations else "semantic"
        vis_edges.append(
            {
                "id": f"E{index:06d}",
                "from": edge["source"],
                "to": edge["target"],
                "value": max(0.4, edge["combined_weight"] * 2.5),
                "width": max(0.45, min(5.0, 0.45 + edge["combined_weight"] * 1.6)),
                "relations": relations,
                "arrows": edge.get("arrows", ""),
                "color": {"color": edge_colors.get(dominant, "#777777") + "88", "highlight": edge_colors.get(dominant, "#a78bfa")},
                "title": html.escape(" | ".join(f"{name}={value:.3f}" for name, value in components.items() if value > 0)),
            }
        )
    script_src = "assets/vis-network.min.js" if local_vis_js else "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"
    html_doc = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Multiplex Literature Network</title><script src="__VIS_JS__"></script><style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#151515;color:#e5e7eb;font-family:Inter,Segoe UI,Arial,sans-serif}#network{position:absolute;inset:0 380px 0 0;background:#151515}#side{position:absolute;right:0;top:0;bottom:0;width:380px;background:rgba(22,22,24,.98);border-left:1px solid #333;padding:18px;box-sizing:border-box;overflow:auto;box-shadow:-12px 0 28px rgba(0,0,0,.22)}h2{font-size:18px;margin:0 0 12px}.muted{color:#9ca3af;font-size:12px}.section{border-top:1px solid #333;margin-top:16px;padding-top:14px}select,input,button{width:100%;box-sizing:border-box;background:#232326;color:#eee;border:1px solid #3c3c42;border-radius:7px;padding:9px;margin:5px 0}button{cursor:pointer}.row{display:flex;gap:7px}.row button{width:50%}.check{display:flex;align-items:center;gap:7px;font-size:13px;margin:7px 0}.check input{width:auto;margin:0}.badge{display:inline-block;padding:3px 7px;border-radius:12px;background:#2c2c32;margin:2px;font-size:11px}.claim{font-size:12px;line-height:1.45;margin:8px 0;color:#d1d5db}.legend{display:flex;align-items:center;gap:7px;font-size:11px;margin:5px 0}.dot{width:10px;height:10px;border-radius:50%}.openpdf{margin-top:10px;background:#303037;border-color:#5b5b66;font-weight:600}.openpdf:hover{border-color:#a3a3b0}#status{position:absolute;left:12px;bottom:10px;background:rgba(20,20,20,.7);padding:7px 10px;border-radius:7px;font-size:11px;color:#aaa;pointer-events:none}</style></head><body>
<div id="network"></div><div id="status"></div><div id="side"><h2>Multiplex Literature Network</h2><div class="muted">Leiden clustering over separate citation, semantic, property, method, and coupling layers.</div><div class="section"><label>Find paper</label><select id="paperSearch"></select><label>Cluster</label><select id="clusterFilter"></select><div class="row"><button id="fitBtn">Fit</button><button id="physicsBtn">Physics: on</button></div><button id="resetBtn">Reset filters</button></div>
<div class="section"><b>Layers</b><label class="check"><input type="checkbox" data-rel="citation" checked>Citation</label><label class="check"><input type="checkbox" data-rel="semantic" checked>SPECTER2 semantic</label><label class="check"><input type="checkbox" data-rel="property" checked>Property overlap</label><label class="check"><input type="checkbox" data-rel="method" checked>Method overlap</label><label class="check"><input type="checkbox" data-rel="bibliographic_coupling" checked>Bibliographic coupling</label></div><div class="section"><b>Clusters</b><div id="legend"></div></div><div class="section"><b>Selected paper</b><div id="detail" class="muted">Click a node.</div></div></div>
<script>const nodeArray=__NODES__;const edgeArray=__EDGES__;const nodeMeta=__NODE_META__;const clusterMeta=__CLUSTER_META__;const clusterColors=__CLUSTER_COLORS__;const nodes=new vis.DataSet(nodeArray),edges=new vis.DataSet(edgeArray);const options={interaction:{hover:true,multiselect:true,tooltipDelay:180},physics:{enabled:true,solver:'forceAtlas2Based',forceAtlas2Based:{gravitationalConstant:-58,centralGravity:.008,springLength:125,springConstant:.04,damping:.42,avoidOverlap:.45},stabilization:{iterations:600}},nodes:{shape:'dot',scaling:{min:7,max:31}},edges:{smooth:{enabled:true,type:'continuous',roundness:.25},scaling:{min:.5,max:5}}};const network=new vis.Network(document.getElementById('network'),{nodes,edges},options);let physics=true;const search=document.getElementById('paperSearch');search.innerHTML='<option value="">— select —</option>'+Object.values(nodeMeta).sort((a,b)=>(a.title||'').localeCompare(b.title||'')).map(n=>`<option value="${n.paper_id}">${esc(n.display_label||n.paper_id)} · ${(n.title||'').slice(0,72)}</option>`).join('');const cf=document.getElementById('clusterFilter');cf.innerHTML='<option value="all">All clusters</option>'+Object.values(clusterMeta).sort((a,b)=>a.cluster_id-b.cluster_id).map(c=>`<option value="${c.cluster_id}">C${c.cluster_id+1}: ${c.label} (${c.size})</option>`).join('');document.getElementById('legend').innerHTML=Object.values(clusterMeta).sort((a,b)=>a.cluster_id-b.cluster_id).map(c=>`<div class="legend"><span class="dot" style="background:${clusterColors[c.cluster_id]}"></span><span>C${c.cluster_id+1}: ${c.label} (${c.size})</span></div>`).join('');function activeRels(){return new Set([...document.querySelectorAll('[data-rel]')].filter(x=>x.checked).map(x=>x.dataset.rel));}function applyFilters(){const rel=activeRels(),cluster=cf.value;nodes.forEach(n=>nodes.update({id:n.id,hidden:cluster!=='all'&&String(n.cluster)!==cluster}));edges.forEach(e=>edges.update({id:e.id,hidden:!e.relations.some(r=>rel.has(r))}));}document.querySelectorAll('[data-rel]').forEach(x=>x.addEventListener('change',applyFilters));cf.addEventListener('change',()=>{applyFilters();network.fit({animation:true});});search.addEventListener('change',()=>{if(!search.value)return;nodes.update({id:search.value,hidden:false});network.selectNodes([search.value]);network.focus(search.value,{scale:1.65,animation:true});showDetail(search.value);});document.getElementById('fitBtn').onclick=()=>network.fit({animation:true});document.getElementById('physicsBtn').onclick=()=>{physics=!physics;network.setOptions({physics:{enabled:physics}});document.getElementById('physicsBtn').textContent='Physics: '+(physics?'on':'off');};document.getElementById('resetBtn').onclick=()=>{cf.value='all';document.querySelectorAll('[data-rel]').forEach(x=>x.checked=true);applyFilters();network.fit({animation:true});};function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}async function openOriginalPdf(id){const n=nodeMeta[id];if(!n)return;const status=document.getElementById('status');status.textContent=`Opening ${n.display_label||id}…`;try{const r=await fetch(`http://127.0.0.1:8766/api/open_pdf?id=${encodeURIComponent(id)}`,{method:'POST'});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);status.textContent=`Opened ${n.display_label||id}`;}catch(e){status.textContent='PDF opener is not running. Start Review Literature App.';alert('Could not open the original PDF. Start the Review Literature App first.\n\n'+e);}}function showDetail(id){const n=nodeMeta[id];if(!n)return;const props=(n.properties||[]).map(x=>`<span class="badge">${esc(x)}</span>`).join('');const methods=(n.methods||[]).map(x=>`<span class="badge">${esc(x)}</span>`).join('');const claims=(n.claims||[]).slice(0,6).map(x=>`<div class="claim">• ${esc(x)}</div>`).join('');document.getElementById('detail').innerHTML=`<div><b>${esc(n.display_label||n.paper_id)}</b> <span class="muted">(${esc(n.paper_id)})</span></div><div style="font-size:14px;margin:8px 0"><b>${esc(n.title||'(untitled)')}</b></div><div class="muted">${esc(n.journal||'')}<br>${esc(n.doi||'')}<br>${esc(n.original_filename||n.source_relpath||'')}<br>Cluster C${n.cluster_id+1}: ${esc(n.cluster_label)}<br>Validation: ${esc(n.validation_status||'')}</div><button class="openpdf" id="openPdfBtn">Open original PDF</button><div class="muted" style="margin-top:5px">Double-click the node to open immediately.</div><div style="margin-top:8px"><b>Properties</b><br>${props||'—'}<br><b>Methods</b><br>${methods||'—'}</div><div class="section"><b>Representative claims</b>${claims||'<div class="muted">No claims extracted.</div>'}</div>`;document.getElementById('openPdfBtn').onclick=()=>openOriginalPdf(id);}network.on('click',p=>{if(p.nodes.length)showDetail(p.nodes[0]);});network.on('doubleClick',p=>{if(p.nodes.length)openOriginalPdf(p.nodes[0]);});network.on('stabilizationProgress',p=>document.getElementById('status').textContent=`Stabilizing ${Math.round(100*p.iterations/p.total)}%`);network.once('stabilizationIterationsDone',()=>{document.getElementById('status').textContent=`${nodes.length} papers · ${edges.length} edges`;});</script></body></html>'''
    replacements = {
        "__VIS_JS__": script_src,
        "__NODES__": json.dumps(vis_nodes, ensure_ascii=False),
        "__EDGES__": json.dumps(vis_edges, ensure_ascii=False),
        "__NODE_META__": json.dumps({node["paper_id"]: node for node in payload["nodes"]}, ensure_ascii=False),
        "__CLUSTER_META__": json.dumps({str(item["cluster_id"]): item for item in clusters}, ensure_ascii=False),
        "__CLUSTER_COLORS__": json.dumps({str(key): value for key, value in color_by_cluster.items()}),
    }
    for key, value in replacements.items():
        html_doc = html_doc.replace(key, value)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a SPECTER2/citation/property/method multiplex graph and Leiden clusters.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    args = ap.parse_args()
    config, root = load_config(args.config)
    paths = get_paths(config, root)
    curated_dir = paths.get("curated", root / "data/curated")
    use_curated = bool(config.get("curation", {}).get("enabled", True) and config.get("curation", {}).get("use_curated_for_graphs", True))
    cfg = config.get("multiplex_graph", {})
    metadata_dir = paths.get("metadata", root / "data/metadata")
    reference_dir = paths.get("reference_matches", root / "data/reference_matches")
    embedding_dir = paths.get("embeddings", root / "data/embeddings")
    out_dir = paths.get("network_gui", root / "outputs/network_gui")
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(paths["database"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM papers WHERE active=1 ORDER BY paper_id").fetchall()
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
        metadata_path = metadata_dir / f"{paper_id}.metadata.json"
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
        canonical = metadata.get("canonical") or paper.get("metadata") or {}
        properties = sorted(normalized_set(inventory.get("studied_properties", []), "property_normalized", "property_raw"))
        methods = sorted(normalized_set(inventory.get("methods", []), "method_normalized", "method_raw"))
        claims = [item.get("statement") for item in evidence.get("claims", []) if item.get("statement")]
        authors = canonical.get("authors") or (paper.get("metadata") or {}).get("authors") or []
        year = canonical.get("year") or row["year"]
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
            "claims": claims,
            "central_question": memory.get("central_question"),
        }
        reference_payloads[paper_id] = read_json(reference_path) if reference_path.exists() else {"references": []}

    ids = sorted(nodes)
    if len(ids) < 2:
        raise SystemExit(f"Need at least two fully processed papers; found {len(ids)}")

    layers: dict[str, dict[tuple[str, str], float]] = {
        "citation": {},
        "semantic": {},
        "property": {},
        "method": {},
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

    embedding_matrix_path = embedding_dir / "specter2.npy"
    embedding_index_path = embedding_dir / "specter2.index.json"
    if embedding_matrix_path.exists() and embedding_index_path.exists():
        matrix = np.load(embedding_matrix_path)
        index = read_json(embedding_index_path)
        lookup = {item["paper_id"]: i for i, item in enumerate(index.get("items", []))}
        if all(paper_id in lookup for paper_id in ids):
            selected = np.vstack([matrix[lookup[paper_id]] for paper_id in ids]).astype(np.float32)
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

    property_cfg = cfg.get("property", {})
    method_cfg = cfg.get("method", {})
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

    layer_weights = cfg.get(
        "layer_weights",
        {"citation": 1.3, "semantic": 0.75, "property": 0.45, "method": 0.35, "bibliographic_coupling": 0.45},
    )
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
    payload = {
        "nodes": payload_nodes,
        "edges": edges,
        "clusters": clusters,
        "layers": {name: [{"source": key[0], "target": key[1], "weight": value} for key, value in edge_map.items()] for name, edge_map in layers.items()},
        "layer_weights": layer_weights,
        "provenance": {"script_version": SCRIPT_VERSION, "algorithm": "Leiden multiplex", "curation_enabled": use_curated},
    }
    write_json(out_dir / "network.json", payload)

    with (out_dir / "nodes.csv").open("w", encoding="utf-8-sig", newline="") as file:
        fields = ["paper_id", "display_label", "title", "year", "journal", "doi", "source_relpath", "original_filename", "validation_status", "cluster_id", "cluster_label", "node_size"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for node in payload_nodes:
            writer.writerow({key: node.get(key) for key in fields})
    with (out_dir / "edges.csv").open("w", encoding="utf-8-sig", newline="") as file:
        fields = ["source", "target", "combined_weight", "citation", "semantic", "property", "method", "bibliographic_coupling", "shared_references", "directions"]
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
                    "property": edge["components"].get("property", 0),
                    "method": edge["components"].get("method", 0),
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
    print(f"Papers  : {len(ids)}")
    for name, edge_map in layers.items():
        print(f"Layer {name:24s}: {len(edge_map)} edges")
    print(f"Clusters: {len(clusters)}")
    for cluster in clusters:
        print(f"  C{cluster['cluster_id']+1}: {cluster['label']} ({cluster['size']})")
    print(f"GUI     : {out_dir / 'network.html'}")


if __name__ == "__main__":
    main()
