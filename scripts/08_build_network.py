#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import networkx as nx
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def norm_doi(value: str | None) -> str:
    if not value:
        return ""
    s = value.strip().lower()
    for p in ("https://doi.org/", "http://doi.org/", "doi:"):
        if s.startswith(p):
            s = s[len(p):]
    return s.strip().rstrip(".,; ")


def norm_text(value: str | None) -> str:
    s = (value or "").lower()
    s = re.sub(r"&[a-z]+;", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def abstract_text(paper: dict[str, Any]) -> str:
    out: list[str] = []
    for para in paper.get("abstract", []):
        for sent in para.get("sentences", []):
            if sent.get("text"):
                out.append(sent["text"])
    return " ".join(out)


def collect_inventory_tags(inv: dict[str, Any]) -> set[str]:
    tags: set[str] = set()
    for item in inv.get("studied_properties", []):
        x = item.get("property_normalized") or item.get("property_raw")
        if x:
            tags.add("property:" + norm_text(str(x)))
    for item in inv.get("methods", []):
        x = item.get("method_normalized") or item.get("method_raw")
        if x:
            tags.add("method:" + norm_text(str(x)))
    for item in inv.get("systems", []):
        attrs = item.get("attributes", {}) or {}
        for key in ("topology", "architecture", "end_groups"):
            val = attrs.get(key)
            if isinstance(val, list):
                for v in val:
                    if v:
                        tags.add(f"{key}:" + norm_text(str(v)))
            elif val:
                tags.add(f"{key}:" + norm_text(str(val)))
    return {x for x in tags if not x.endswith(":")}


def collect_search_text(paper: dict[str, Any], inv: dict[str, Any], ev: dict[str, Any]) -> str:
    meta = paper.get("metadata", {}) or {}
    parts = [meta.get("title") or "", abstract_text(paper)]
    parts += [x.get("text", "") for x in inv.get("objectives", [])]
    parts += [x.get("property_normalized") or x.get("property_raw") or "" for x in inv.get("studied_properties", [])]
    parts += [x.get("method_normalized") or x.get("method_raw") or "" for x in inv.get("methods", [])]
    parts += [x.get("statement", "") for x in ev.get("claims", [])]
    return "\n".join(str(x) for x in parts if x)


def ref_signature(ref: dict[str, Any]) -> str:
    doi = norm_doi(ref.get("doi"))
    if doi:
        return "doi:" + doi
    title = norm_text(ref.get("title"))
    if len(title) >= 18:
        return "title:" + title
    return ""


def add_component(edges: dict[tuple[str, str], dict[str, Any]], a: str, b: str, component: str, value: float, detail: str | None = None) -> None:
    if a == b:
        return
    key = tuple(sorted((a, b)))
    d = edges.setdefault(key, {"source": key[0], "target": key[1], "components": {}, "citations": []})
    d["components"][component] = max(float(value), float(d["components"].get(component, 0.0)))
    if component == "direct_citation" and detail and detail not in d["citations"]:
        d["citations"].append(detail)


def topk_pairs(matrix: np.ndarray, ids: list[str], top_k: int, threshold: float):
    for i, pid in enumerate(ids):
        row = matrix[i].copy()
        row[i] = -1.0
        candidates = np.argsort(row)[::-1][:top_k]
        for j in candidates:
            val = float(row[j])
            if val >= threshold:
                yield pid, ids[j], val


def build_cluster_labels(nodes: dict[str, dict[str, Any]], cluster_by_pid: dict[str, int], vectorizer, X) -> dict[int, str]:
    labels: dict[int, str] = {}
    features = np.array(vectorizer.get_feature_names_out())
    generic = {"polyethylene", "glycol", "peg", "polymer", "polymers", "water", "aqueous", "solution", "solutions"}
    for cid in sorted(set(cluster_by_pid.values())):
        pids = [p for p, c in cluster_by_pid.items() if c == cid]
        props = Counter()
        for pid in pids:
            for tag in nodes[pid]["tags"]:
                if tag.startswith("property:"):
                    props[tag.split(":", 1)[1]] += 1
        prop_terms = [p for p, _ in props.most_common(3) if p]
        if prop_terms:
            labels[cid] = " / ".join(prop_terms)
            continue
        idxs = [nodes[pid]["row_index"] for pid in pids]
        centroid = np.asarray(X[idxs].mean(axis=0)).ravel()
        ranked = centroid.argsort()[::-1]
        terms = []
        for idx in ranked:
            term = features[idx]
            if term in generic or any(tok in generic for tok in term.split()):
                continue
            terms.append(term)
            if len(terms) == 3:
                break
        labels[cid] = " / ".join(terms) if terms else f"Cluster {cid + 1}"
    return labels


def palette(n: int) -> list[str]:
    base = [
        "#8b5cf6", "#22c55e", "#38bdf8", "#f97316", "#ec4899", "#eab308",
        "#14b8a6", "#f43f5e", "#a3e635", "#06b6d4", "#c084fc", "#fb7185",
        "#60a5fa", "#34d399", "#f59e0b", "#a78bfa"
    ]
    return [base[i % len(base)] for i in range(max(1, n))]


def make_gui(out_path: Path, payload: dict[str, Any], cfg: dict[str, Any]) -> None:
    nodes = payload["nodes"]
    edges = payload["edges"]
    clusters = payload["clusters"]
    vis_nodes = []
    cluster_colors = palette(len(clusters))
    color_by_cluster = {int(c["cluster_id"]): cluster_colors[i] for i, c in enumerate(clusters)}
    for n in nodes:
        color = color_by_cluster.get(int(n["cluster_id"]), "#8b5cf6")
        border = "#f59e0b" if n.get("validation_status") == "review_required" else "#d1d5db"
        vis_nodes.append({
            "id": n["paper_id"],
            "label": n["paper_id"],
            "title": html.escape((n.get("title") or n["paper_id"])[:400]),
            "value": n["node_size"],
            "cluster": n["cluster_id"],
            "color": {"background": color, "border": border, "highlight": {"background": "#ffffff", "border": color}},
            "font": {"color": "#e5e7eb", "size": 13},
            "borderWidth": 1.4
        })
    vis_edges = []
    for i, e in enumerate(edges):
        comps = e["components"]
        rels = [k for k, v in comps.items() if v > 0]
        direct = comps.get("direct_citation", 0) > 0
        vis_edges.append({
            "id": f"E{i:06d}",
            "from": e["source"], "to": e["target"],
            "value": max(0.5, e["weight"] * 3.0),
            "width": max(0.5, min(4.5, 0.45 + e["weight"] * 1.8)),
            "relations": rels,
            "arrows": (
                "to,from" if (f"{e['source']}->{e['target']}" in e.get("citations", []) and f"{e['target']}->{e['source']}" in e.get("citations", []))
                else "to" if f"{e['source']}->{e['target']}" in e.get("citations", [])
                else "from" if f"{e['target']}->{e['source']}" in e.get("citations", [])
                else ""
            ),
            "color": {"color": "rgba(110,110,125,0.28)", "highlight": "#8b5cf6", "hover": "#a78bfa"},
            "title": html.escape(" | ".join(f"{k}={v:.3f}" for k, v in comps.items() if v > 0))
        })

    node_meta = {n["paper_id"]: n for n in nodes}
    cluster_meta = {str(c["cluster_id"]): c for c in clusters}
    html_doc = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Literature Network</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#151515;color:#e5e7eb;font-family:Inter,Segoe UI,Arial,sans-serif}
#network{position:absolute;inset:0 360px 0 0;background:#151515}
#side{position:absolute;right:0;top:0;bottom:0;width:360px;background:rgba(22,22,24,.97);border-left:1px solid #333;padding:18px;box-sizing:border-box;overflow:auto;box-shadow:-12px 0 28px rgba(0,0,0,.22)}
h2{font-size:18px;margin:0 0 12px}.muted{color:#9ca3af;font-size:12px}.section{border-top:1px solid #333;margin-top:16px;padding-top:14px}
select,input,button{width:100%;box-sizing:border-box;background:#232326;color:#eee;border:1px solid #3c3c42;border-radius:7px;padding:9px;margin:5px 0}
button{cursor:pointer}button:hover{background:#303036}.row{display:flex;gap:7px}.row button{width:50%}.check{display:flex;align-items:center;gap:7px;font-size:13px;margin:6px 0}.check input{width:auto;margin:0}
.badge{display:inline-block;padding:3px 7px;border-radius:12px;background:#2c2c32;margin:2px;font-size:11px}.claim{font-size:12px;line-height:1.45;margin:8px 0;color:#d1d5db}.legend{display:flex;align-items:center;gap:7px;font-size:11px;margin:5px 0}.dot{width:10px;height:10px;border-radius:50%}
#status{position:absolute;left:12px;bottom:10px;background:rgba(20,20,20,.7);padding:7px 10px;border-radius:7px;font-size:11px;color:#aaa;pointer-events:none}
</style></head><body>
<div id="network"></div><div id="status"></div>
<div id="side"><h2>Literature Network</h2><div class="muted">Drag · zoom · click a paper · filter relations</div>
<div class="section"><label>Find paper</label><select id="paperSearch"></select><label>Cluster</label><select id="clusterFilter"></select>
<div class="row"><button id="fitBtn">Fit</button><button id="physicsBtn">Physics: on</button></div><button id="resetBtn">Reset filters</button></div>
<div class="section"><b>Edges</b>
<label class="check"><input type="checkbox" data-rel="direct_citation" checked>Direct citation</label>
<label class="check"><input type="checkbox" data-rel="semantic" checked>Semantic similarity</label>
<label class="check"><input type="checkbox" data-rel="bibliographic_coupling" checked>Bibliographic coupling</label>
<label class="check"><input type="checkbox" data-rel="tag_similarity" checked>Property / method similarity</label></div>
<div class="section"><b>Clusters</b><div id="legend"></div></div>
<div class="section"><b>Selected paper</b><div id="detail" class="muted">Click a node.</div></div></div>
<script>
const nodeArray=__NODES__; const edgeArray=__EDGES__; const nodeMeta=__NODE_META__; const clusterMeta=__CLUSTER_META__; const clusterColors=__CLUSTER_COLORS__;
const nodes=new vis.DataSet(nodeArray), edges=new vis.DataSet(edgeArray);
const options={autoResize:true,interaction:{hover:true,multiselect:true,tooltipDelay:180,hideEdgesOnDrag:false},physics:{enabled:__PHYSICS__,solver:'forceAtlas2Based',forceAtlas2Based:{gravitationalConstant:-55,centralGravity:0.008,springLength:115,springConstant:0.045,damping:0.42,avoidOverlap:0.45},stabilization:{iterations:500,updateInterval:25}},nodes:{shape:'dot',scaling:{min:7,max:29}},edges:{smooth:{enabled:true,type:'continuous',roundness:.25},scaling:{min:.5,max:5}}};
const network=new vis.Network(document.getElementById('network'),{nodes,edges},options);
let physics=__PHYSICS__;
const search=document.getElementById('paperSearch'); search.innerHTML='<option value="">— select —</option>'+Object.values(nodeMeta).sort((a,b)=>(a.title||'').localeCompare(b.title||'')).map(n=>`<option value="${n.paper_id}">${n.paper_id} · ${(n.year||'?')} · ${(n.title||'').slice(0,72)}</option>`).join('');
const cf=document.getElementById('clusterFilter'); cf.innerHTML='<option value="all">All clusters</option>'+Object.values(clusterMeta).sort((a,b)=>a.cluster_id-b.cluster_id).map(c=>`<option value="${c.cluster_id}">C${c.cluster_id+1}: ${c.label} (${c.size})</option>`).join('');
const legend=document.getElementById('legend'); legend.innerHTML=Object.values(clusterMeta).sort((a,b)=>a.cluster_id-b.cluster_id).map(c=>`<div class="legend"><span class="dot" style="background:${clusterColors[c.cluster_id]}"></span><span>C${c.cluster_id+1}: ${c.label} (${c.size})</span></div>`).join('');
function activeRels(){return new Set([...document.querySelectorAll('[data-rel]')].filter(x=>x.checked).map(x=>x.dataset.rel));}
function applyFilters(){const rel=activeRels(), cluster=cf.value; nodes.forEach(n=>nodes.update({id:n.id,hidden:cluster!=='all' && String(n.cluster)!==cluster})); edges.forEach(e=>{const ok=e.relations.some(r=>rel.has(r)); edges.update({id:e.id,hidden:!ok});});}
document.querySelectorAll('[data-rel]').forEach(x=>x.addEventListener('change',applyFilters)); cf.addEventListener('change',()=>{applyFilters(); network.fit({animation:true});});
search.addEventListener('change',()=>{if(!search.value)return; nodes.update({id:search.value,hidden:false}); network.selectNodes([search.value]); network.focus(search.value,{scale:1.65,animation:{duration:650,easingFunction:'easeInOutQuad'}}); showDetail(search.value);});
document.getElementById('fitBtn').onclick=()=>network.fit({animation:true});
document.getElementById('physicsBtn').onclick=()=>{physics=!physics; network.setOptions({physics:{enabled:physics}}); document.getElementById('physicsBtn').textContent='Physics: '+(physics?'on':'off');};
document.getElementById('resetBtn').onclick=()=>{cf.value='all'; document.querySelectorAll('[data-rel]').forEach(x=>x.checked=true); applyFilters(); network.fit({animation:true});};
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function showDetail(id){const n=nodeMeta[id]; if(!n)return; const props=(n.properties||[]).map(x=>`<span class="badge">${esc(x)}</span>`).join(''); const claims=(n.claims||[]).slice(0,__MAX_CLAIMS__).map(x=>`<div class="claim">• ${esc(x)}</div>`).join(''); document.getElementById('detail').innerHTML=`<div><b>${esc(n.paper_id)}</b> · ${esc(n.year||'?')}</div><div style="font-size:14px;margin:8px 0"><b>${esc(n.title||'(untitled)')}</b></div><div class="muted">${esc(n.journal||'')}<br>Cluster C${n.cluster_id+1}: ${esc(n.cluster_label)}<br>Validation: ${esc(n.validation_status||'')}</div><div style="margin-top:8px">${props}</div><div class="section"><b>Representative claims</b>${claims||'<div class="muted">No claims extracted.</div>'}</div>`;}
network.on('click',p=>{if(p.nodes.length)showDetail(p.nodes[0]);}); network.on('stabilizationProgress',p=>document.getElementById('status').textContent=`Stabilizing ${Math.round(100*p.iterations/p.total)}%`); network.once('stabilizationIterationsDone',()=>{document.getElementById('status').textContent=`${nodes.length} papers · ${edges.length} relations`; setTimeout(()=>network.setOptions({physics:{stabilization:false}}),500);});
</script></body></html>'''
    replacements = {
        "__NODES__": json.dumps(vis_nodes, ensure_ascii=False),
        "__EDGES__": json.dumps(vis_edges, ensure_ascii=False),
        "__NODE_META__": json.dumps(node_meta, ensure_ascii=False),
        "__CLUSTER_META__": json.dumps(cluster_meta, ensure_ascii=False),
        "__CLUSTER_COLORS__": json.dumps({str(k): v for k, v in color_by_cluster.items()}),
        "__PHYSICS__": "true" if cfg.get("gui", {}).get("physics", True) else "false",
        "__MAX_CLAIMS__": str(int(cfg.get("gui", {}).get("max_claims_in_panel", 5)))
    }
    for k, v in replacements.items():
        html_doc = html_doc.replace(k, v)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build content/citation network, cluster it, and generate an Obsidian-like HTML GUI.")
    ap.add_argument("--project-root", default=str(ROOT))
    ap.add_argument("--network-config", default="network_config.json")
    args = ap.parse_args()
    root = Path(args.project_root).resolve()
    cfg_path = Path(args.network_config)
    if not cfg_path.is_absolute():
        cfg_path = root / cfg_path
    cfg = read_json(cfg_path)

    db = root / "db/literature.sqlite"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    papers_db = {r["paper_id"]: dict(r) for r in conn.execute("SELECT * FROM papers WHERE active=1 ORDER BY paper_id")}

    allowed = set(cfg.get("include_validation_status", ["pass", "review_required"]))
    nodes: dict[str, dict[str, Any]] = {}
    for pid, dbrow in papers_db.items():
        pp = root / "data/paper_json" / f"{pid}.json"
        ip = root / "data/extracted" / f"{pid}.inventory.json"
        ep = root / "data/extracted" / f"{pid}.evidence.json"
        vp = root / "data/extracted" / f"{pid}.validation.json"
        if not all(p.exists() for p in (pp, ip, ep, vp)):
            continue
        paper, inv, ev, val = map(read_json, (pp, ip, ep, vp))
        status = val.get("overall_status", "")
        if allowed and status not in allowed:
            continue
        meta = paper.get("metadata", {}) or {}
        title = meta.get("title") or dbrow.get("title") or pid
        props = [x.get("property_normalized") or x.get("property_raw") for x in inv.get("studied_properties", [])]
        props = [x for x in props if x]
        claims = [x.get("statement") for x in ev.get("claims", []) if x.get("statement")]
        refs = paper.get("references", []) or []
        nodes[pid] = {
            "paper_id": pid,
            "title": title,
            "year": meta.get("year") or dbrow.get("year"),
            "journal": meta.get("journal") or dbrow.get("journal"),
            "doi": meta.get("doi") or dbrow.get("doi"),
            "validation_status": status,
            "properties": props,
            "claims": claims,
            "tags": collect_inventory_tags(inv),
            "text": collect_search_text(paper, inv, ev),
            "references": refs,
            "ref_sigs": {s for s in (ref_signature(r) for r in refs) if s},
        }
    ids = sorted(nodes)
    if len(ids) < 2:
        raise SystemExit(f"Need at least two fully processed papers; found {len(ids)}")
    for i, pid in enumerate(ids):
        nodes[pid]["row_index"] = i

    # Semantic similarity: title + abstract + structured objectives/properties/methods/claims.
    sem = cfg["semantic"]
    vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, int(sem.get("ngram_max", 2))), max_features=int(sem.get("max_features", 30000)), sublinear_tf=True)
    X = vectorizer.fit_transform([nodes[p]["text"] for p in ids])
    sim = cosine_similarity(X)
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for a, b, v in topk_pairs(sim, ids, int(sem.get("top_k_per_paper", 6)), float(sem.get("min_similarity", 0.10))):
        add_component(edges, a, b, "semantic", v)

    # Direct citation: exact DOI first; otherwise high-confidence local title match.
    doi_to_pid = {norm_doi(nodes[p].get("doi")): p for p in ids if norm_doi(nodes[p].get("doi"))}
    title_norm = {p: norm_text(nodes[p]["title"]) for p in ids}
    for citing in ids:
        for ref in nodes[citing]["references"]:
            target = None
            d = norm_doi(ref.get("doi"))
            if d and d in doi_to_pid:
                target = doi_to_pid[d]
            else:
                rt = norm_text(ref.get("title"))
                if len(rt) >= 22:
                    best_pid, best_score = None, 0.0
                    for pid in ids:
                        if pid == citing or not title_norm[pid]:
                            continue
                        score = SequenceMatcher(None, rt, title_norm[pid]).ratio()
                        if score > best_score:
                            best_pid, best_score = pid, score
                    if best_score >= 0.92:
                        target = best_pid
            if target and target != citing:
                add_component(edges, citing, target, "direct_citation", 1.0, f"{citing}->{target}")

    # Bibliographic coupling.
    bc = cfg["bibliographic_coupling"]
    bc_matrix = np.zeros((len(ids), len(ids)), dtype=float)
    shared_matrix = np.zeros((len(ids), len(ids)), dtype=int)
    for i in range(len(ids)):
        A = nodes[ids[i]]["ref_sigs"]
        for j in range(i + 1, len(ids)):
            B = nodes[ids[j]]["ref_sigs"]
            if not A or not B:
                continue
            shared = len(A & B)
            if shared:
                v = shared / math.sqrt(len(A) * len(B))
                bc_matrix[i, j] = bc_matrix[j, i] = v
                shared_matrix[i, j] = shared_matrix[j, i] = shared
    for a, b, v in topk_pairs(bc_matrix, ids, int(bc.get("top_k_per_paper", 5)), float(bc.get("min_similarity", 0.08))):
        i, j = ids.index(a), ids.index(b)
        if shared_matrix[i, j] >= int(bc.get("min_shared_references", 2)):
            add_component(edges, a, b, "bibliographic_coupling", v)

    # PEG-domain/profile tags (or future profile-specific normalized properties/methods).
    tag_cfg = cfg["tag_similarity"]
    tag_matrix = np.zeros((len(ids), len(ids)), dtype=float)
    for i in range(len(ids)):
        A = nodes[ids[i]]["tags"]
        for j in range(i + 1, len(ids)):
            B = nodes[ids[j]]["tags"]
            if A or B:
                v = len(A & B) / len(A | B) if (A | B) else 0.0
                tag_matrix[i, j] = tag_matrix[j, i] = v
    for a, b, v in topk_pairs(tag_matrix, ids, int(tag_cfg.get("top_k_per_paper", 5)), float(tag_cfg.get("min_jaccard", 0.15))):
        add_component(edges, a, b, "tag_similarity", v)

    weights = cfg["weights"]
    edge_list = []
    for d in edges.values():
        c = d["components"]
        d["weight"] = sum(float(weights.get(k, 1.0)) * float(v) for k, v in c.items())
        edge_list.append(d)
    edge_list.sort(key=lambda e: e["weight"], reverse=True)

    G = nx.Graph()
    G.add_nodes_from(ids)
    for e in edge_list:
        G.add_edge(e["source"], e["target"], weight=e["weight"])
    cl_cfg = cfg.get("clustering", {})
    resolution = float(cl_cfg.get("resolution", 1.0))
    seed = int(cl_cfg.get("seed", 42))
    if G.number_of_edges() == 0:
        communities = [{x} for x in ids]
    else:
        communities = nx.community.louvain_communities(G, weight="weight", resolution=resolution, seed=seed)
        communities = sorted(communities, key=lambda c: (-len(c), sorted(c)[0]))
    cluster_by_pid = {}
    for cid, comm in enumerate(communities):
        for pid in comm:
            cluster_by_pid[pid] = cid
    cluster_labels = build_cluster_labels(nodes, cluster_by_pid, vectorizer, X)

    degree = dict(G.degree(weight="weight"))
    payload_nodes = []
    for pid in ids:
        n = nodes[pid]
        cid = cluster_by_pid[pid]
        payload_nodes.append({
            "paper_id": pid,
            "title": n["title"], "year": n["year"], "journal": n["journal"], "doi": n["doi"],
            "validation_status": n["validation_status"],
            "cluster_id": cid, "cluster_label": cluster_labels[cid],
            "properties": n["properties"], "claims": n["claims"],
            "node_size": 8.0 + min(22.0, 6.0 * math.log1p(max(0.0, degree.get(pid, 0.0))))
        })
    clusters = [{"cluster_id": cid, "label": cluster_labels[cid], "size": sum(1 for p in ids if cluster_by_pid[p] == cid)} for cid in sorted(cluster_labels)]
    payload = {"nodes": payload_nodes, "edges": edge_list, "clusters": clusters, "settings": cfg}

    outdir = root / "outputs/network_gui"
    outdir.mkdir(parents=True, exist_ok=True)
    write_json(outdir / "network.json", payload)
    with (outdir / "nodes.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["paper_id", "title", "year", "journal", "doi", "validation_status", "cluster_id", "cluster_label", "node_size"]
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader();
        for n in payload_nodes: w.writerow({k: n.get(k) for k in fields})
    with (outdir / "edges.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["source", "target", "weight", "semantic", "direct_citation", "bibliographic_coupling", "tag_similarity", "citations"]
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for e in edge_list:
            w.writerow({"source":e["source"],"target":e["target"],"weight":e["weight"],
                        "semantic":e["components"].get("semantic",0),"direct_citation":e["components"].get("direct_citation",0),
                        "bibliographic_coupling":e["components"].get("bibliographic_coupling",0),"tag_similarity":e["components"].get("tag_similarity",0),
                        "citations":";".join(e.get("citations",[]))})
    make_gui(outdir / "network.html", payload, cfg)
    print(f"Papers: {len(payload_nodes)}")
    print(f"Edges : {len(edge_list)}")
    print(f"Clusters: {len(clusters)}")
    for c in clusters:
        print(f"  C{c['cluster_id']+1}: {c['label']} ({c['size']})")
    print(f"GUI: {outdir / 'network.html'}")

if __name__ == "__main__":
    main()
