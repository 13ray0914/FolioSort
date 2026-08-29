#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import networkx as nx
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
from lib.v4_common import ensure_v4_schema, now_iso

SCRIPT_VERSION = "knowledge-graph-v4.1.0-curated-author-year-labels-progressive-gui-graphml-safe"


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


def paper_display_label(canonical: dict[str, Any], paper_id: str) -> str:
    family = first_author_family(canonical.get("authors") or [])
    year = canonical.get("year")
    year_text = str(year) if year not in (None, "") else "?"
    if family:
        return f"{family}, {year_text}"
    return f"{paper_id}, {year_text}"


def slug(value: str) -> str:
    normalized = normalize_key(value)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def canonical_metadata(paper: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    return metadata.get("canonical") or paper.get("metadata") or {}


def add_node(nodes: dict[str, dict[str, Any]], node_id: str, node_type: str, label: str, **attrs: Any) -> None:
    if node_id in nodes:
        current = nodes[node_id]
        for key, value in attrs.items():
            if value not in (None, "", [], {}) and current.get(key) in (None, "", [], {}):
                current[key] = value
        return
    nodes[node_id] = {"id": node_id, "type": node_type, "label": label, **attrs}


def add_edge(
    edges: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    source: str,
    target: str,
    relation: str,
    *,
    weight: float = 1.0,
    directed: bool = True,
    **attrs: Any,
) -> None:
    key = (source, target, relation) if directed else tuple(sorted((source, target))) + (relation,)
    if key in seen:
        return
    seen.add(key)
    edges.append(
        {
            "source": source,
            "target": target,
            "relation": relation,
            "weight": round(float(weight), 6),
            "directed": bool(directed),
            **attrs,
        }
    )


def claim_text(claim: dict[str, Any]) -> str:
    parts = [
        claim.get("statement"),
        claim.get("subject"),
        claim.get("relation"),
        claim.get("object"),
        claim.get("conditions_text"),
    ]
    return " ".join(str(x) for x in parts if x)



def build_evidence_text_map(paper: dict[str, Any], visual: dict[str, Any]) -> dict[str, str]:
    """Map stable text/visual evidence IDs to compact source excerpts."""
    evidence_map: dict[str, str] = {}
    for paragraph in paper.get("abstract", []):
        for sentence in paragraph.get("sentences", []):
            sid = sentence.get("sid")
            text = str(sentence.get("text") or "").strip()
            if sid and text:
                evidence_map[str(sid)] = text
    for section in paper.get("sections", []):
        for paragraph in section.get("paragraphs", []):
            for sentence in paragraph.get("sentences", []):
                sid = sentence.get("sid")
                text = str(sentence.get("text") or "").strip()
                if sid and text:
                    evidence_map[str(sid)] = text
    for item in paper.get("auxiliary_text", []):
        sid = item.get("sid")
        text = str(item.get("text") or "").strip()
        if sid and text:
            evidence_map[str(sid)] = text
    for item in visual.get("assets", []):
        eid = item.get("evidence_id")
        if not eid:
            continue
        parts = [
            item.get("caption"),
            item.get("structured_text"),
            item.get("summary_text"),
            json.dumps(item.get("table_rows") or [], ensure_ascii=False),
            json.dumps(item.get("analysis") or {}, ensure_ascii=False),
        ]
        text = " ".join(str(value) for value in parts if value not in (None, "", [], {})).strip()
        if text:
            evidence_map[str(eid)] = text
    return evidence_map


def evidence_excerpts(ids: list[str], evidence_map: dict[str, str], *, max_items: int = 4, max_chars: int = 1800) -> list[str]:
    excerpts: list[str] = []
    used = 0
    for evidence_id in ids or []:
        text = evidence_map.get(str(evidence_id), "").strip()
        if not text:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        excerpt = text[:remaining]
        excerpts.append(f"[{evidence_id}] {excerpt}")
        used += len(excerpt)
        if len(excerpts) >= max_items:
            break
    return excerpts

def candidate_claim_pairs(
    claims: list[dict[str, Any]],
    *,
    top_k: int,
    threshold: float,
    max_pairs: int,
) -> list[dict[str, Any]]:
    if len(claims) < 2:
        return []
    texts = [claim_text(item) for item in claims]
    try:
        matrix = TfidfVectorizer(
            stop_words="english",
            ngram_range=(1, 2),
            min_df=1,
            max_features=30000,
        ).fit_transform(texts)
    except ValueError:
        return []
    sims = cosine_similarity(matrix)
    by_pair: dict[tuple[int, int], float] = {}
    for i, source in enumerate(claims):
        order = sims[i].argsort()[::-1]
        accepted = 0
        for raw_j in order:
            j = int(raw_j)
            if j == i:
                continue
            target = claims[j]
            if source["paper_id"] == target["paper_id"]:
                continue
            score = float(sims[i, j])
            shared_properties = set(source.get("paper_properties", [])) & set(target.get("paper_properties", []))
            effective = max(score, 0.38 if shared_properties else 0.0)
            if effective < threshold:
                continue
            key = (min(i, j), max(i, j))
            by_pair[key] = max(by_pair.get(key, 0.0), effective)
            accepted += 1
            if accepted >= top_k:
                break
    ranked = sorted(by_pair.items(), key=lambda item: item[1], reverse=True)[:max_pairs]
    result = []
    for pair_index, ((i, j), score) in enumerate(ranked, start=1):
        source, target = claims[i], claims[j]
        result.append(
            {
                "pair_id": f"PAIR{pair_index:05d}",
                "source_uid": source["uid"],
                "target_uid": target["uid"],
                "similarity": round(score, 6),
                "source": source,
                "target": target,
            }
        )
    return result


def infer_relations(
    pairs: list[dict[str, Any]],
    cfg: dict[str, Any],
    root: Path,
    conn: sqlite3.Connection,
    llm_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    if not pairs or not cfg.get("infer_claim_relations", False):
        return []
    client = LlamaCppClient(llm_cfg)
    model = client.healthcheck()
    schema_path = root / "schemas/v4/claim_relations.schema.json"
    prompt_path = root / "prompts/v4/claim_relations_system.txt"
    schema = load_schema(schema_path)
    system_prompt = prompt_path.read_text(encoding="utf-8")
    signature = stable_json_hash(
        {
            "script": SCRIPT_VERSION,
            "model": model,
            "schema": sha256_file(schema_path),
            "prompt": sha256_file(prompt_path),
        }
    )
    cache_root = root / "data/llm_raw/knowledge_relations"
    cache_root.mkdir(parents=True, exist_ok=True)
    batch_size = max(1, int(cfg.get("relation_batch_size", 16)))
    output: list[dict[str, Any]] = []
    pair_lookup = {item["pair_id"]: item for item in pairs}
    for start in range(0, len(pairs), batch_size):
        batch = pairs[start : start + batch_size]
        compact = []
        for item in batch:
            compact.append(
                {
                    "pair_id": item["pair_id"],
                    "source": {
                        "uid": item["source_uid"],
                        "statement": item["source"].get("statement"),
                        "conditions": item["source"].get("conditions_text"),
                        "systems": item["source"].get("system_names", []),
                        "properties": item["source"].get("paper_properties", []),
                        "methods": item["source"].get("paper_methods", []),
                        "paper_title": item["source"].get("paper_title"),
                        "evidence_excerpts": item["source"].get("evidence_excerpts", []),
                    },
                    "target": {
                        "uid": item["target_uid"],
                        "statement": item["target"].get("statement"),
                        "conditions": item["target"].get("conditions_text"),
                        "systems": item["target"].get("system_names", []),
                        "properties": item["target"].get("paper_properties", []),
                        "methods": item["target"].get("paper_methods", []),
                        "paper_title": item["target"].get("paper_title"),
                        "evidence_excerpts": item["target"].get("evidence_excerpts", []),
                    },
                }
            )
        user_prompt = "CLASSIFY THESE CLAIM PAIRS:\n" + json.dumps(compact, ensure_ascii=False, indent=2)
        input_hash = sha256_text(user_prompt + signature)
        cache_path = cache_root / f"batch_{start // batch_size + 1:04d}_{input_hash[:12]}.json"
        if cache_path.exists():
            result_data = read_json(cache_path)
            print(f"  REL-CACHE {cache_path.name}")
        else:
            print(f"  REL-LLM   pairs {start + 1}-{min(start + batch_size, len(pairs))}/{len(pairs)}")
            result_data = client.chat_json(system_prompt, user_prompt, schema).data
            write_json(cache_path, result_data)
        for relation in result_data.get("relations", []):
            pair = pair_lookup.get(relation.get("pair_id"))
            if not pair:
                continue
            record = {
                **relation,
                "source_claim_uid": pair["source_uid"],
                "target_claim_uid": pair["target_uid"],
                "candidate_similarity": pair["similarity"],
            }
            output.append(record)
    minimum = float(cfg.get("relation_min_confidence", 0.70))
    selected = [
        item for item in output
        if item.get("relation") != "unrelated" and float(item.get("confidence", 0.0)) >= minimum
    ]
    conn.execute("DELETE FROM knowledge_relations_v4")
    for item in selected:
        conn.execute(
            """
            INSERT OR REPLACE INTO knowledge_relations_v4(
                source_claim_uid,target_claim_uid,relation,confidence,rationale,
                condition_difference,record_json,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                item["source_claim_uid"],
                item["target_claim_uid"],
                item["relation"],
                item.get("confidence"),
                item.get("rationale"),
                item.get("condition_difference"),
                json.dumps(item, ensure_ascii=False),
                now_iso(),
            ),
        )
    conn.commit()
    return selected


def graphml_safe(value: Any) -> str | int | float | bool:
    """Convert metadata to scalar values accepted by NetworkX GraphML."""
    if value is None:
        return ""
    if type(value).__module__.startswith("numpy") and hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def write_tables(out_dir: Path, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    node_fields = ["id", "type", "label", "display_label", "paper_id", "year", "journal", "doi", "source_relpath", "original_filename", "value", "unit", "status"]
    with (out_dir / "knowledge_nodes.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=node_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(nodes)
    edge_fields = ["source", "target", "relation", "weight", "directed", "confidence", "rationale", "condition_difference"]
    with (out_dir / "knowledge_edges.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=edge_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(edges)
    graph = nx.MultiDiGraph()
    for node in nodes:
        graph.add_node(node["id"], **{key: graphml_safe(value) for key, value in node.items() if key != "id"})
    for index, edge in enumerate(edges):
        graph.add_edge(
            edge["source"],
            edge["target"],
            key=f"{edge['relation']}:{index}",
            **{key: graphml_safe(value) for key, value in edge.items() if key not in {"source", "target"}},
        )
    nx.write_graphml(graph, out_dir / "knowledge.graphml")


def make_gui(out_path: Path, payload: dict[str, Any], local_vis_js: Path | None) -> None:
    """Write a progressive/lazy knowledge-graph GUI.

    The complete graph remains embedded for offline use, but vis-network only
    receives the small currently-visible subset.  Initial view contains paper
    nodes (plus paper-to-paper citations); neighbors are added on demand.
    """
    type_colors = {
        "paper": "#8b5cf6",
        "claim": "#f97316",
        "property": "#14b8a6",
        "method": "#3b82f6",
        "system": "#ec4899",
        "measurement": "#eab308",
        "visual": "#84cc16",
    }
    shapes = {
        "paper": "dot", "claim": "diamond", "property": "hexagon", "method": "triangle",
        "system": "box", "measurement": "star", "visual": "square",
    }
    vis_nodes = []
    for node in payload["nodes"]:
        node_type = node["type"]
        vis_nodes.append(
            {
                "id": node["id"],
                "label": node.get("display_label") or node.get("label") or node["id"],
                "title": html.escape(str(node.get("label") or node["id"])[:900]),
                "type": node_type,
                "shape": shapes.get(node_type, "dot"),
                "color": {"background": type_colors.get(node_type, "#9ca3af"), "border": "#d1d5db"},
                "font": {"color": "#e5e7eb", "size": 12},
                "value": node.get("node_size", 8),
            }
        )
    edge_colors = {
        "CITES": "#ef4444", "HAS_CLAIM": "#6b7280", "STUDIES_PROPERTY": "#14b8a6",
        "USES_METHOD": "#3b82f6", "STUDIES_SYSTEM": "#ec4899", "REPORTS_MEASUREMENT": "#eab308",
        "MEASURES_PROPERTY": "#facc15", "MEASURED_ON": "#f59e0b", "HAS_VISUAL": "#84cc16",
        "SUPPORTED_BY_VISUAL": "#84cc16", "CLAIM_ABOUT_SYSTEM": "#fb7185", "CLAIM_ABOUT_PROPERTY": "#2dd4bf",
        "supports": "#22c55e", "contradicts": "#ef4444", "qualifies": "#f59e0b",
        "extends": "#06b6d4", "same_observation_different_interpretation": "#a855f7",
        "not_directly_comparable": "#9ca3af",
    }
    vis_edges = []
    for index, edge in enumerate(payload["edges"]):
        relation = edge["relation"]
        vis_edges.append(
            {
                "id": f"KE{index:07d}",
                "from": edge["source"],
                "to": edge["target"],
                "relation": relation,
                "arrows": "to" if edge.get("directed", True) else "",
                "width": max(0.5, min(4.5, 0.5 + float(edge.get("weight", 1.0)) * 1.2)),
                "color": {"color": edge_colors.get(relation, "#6b728088")},
                "title": html.escape(
                    relation
                    + (f" · confidence={edge.get('confidence'):.2f}" if edge.get("confidence") is not None else "")
                    + (f" · {edge.get('rationale')}" if edge.get("rationale") else "")
                ),
            }
        )
    script_src = "assets/vis-network.min.js" if local_vis_js else "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"
    type_list = sorted(type_colors)
    relation_list = sorted({edge["relation"] for edge in payload["edges"]})
    gui_cfg = payload.get("gui", {})
    expand_limit = max(50, int(gui_cfg.get("expand_limit", 400)))
    physics_threshold = max(100, int(gui_cfg.get("physics_disable_threshold", 450)))
    html_doc = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Scientific Knowledge Graph</title><script src="__VIS_JS__"></script><style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#151515;color:#e5e7eb;font-family:Inter,Segoe UI,Arial,sans-serif}#network{position:absolute;inset:0 400px 0 0}#side{position:absolute;right:0;top:0;bottom:0;width:400px;padding:17px;box-sizing:border-box;background:#19191c;border-left:1px solid #333;overflow:auto}h2{margin:0 0 8px;font-size:18px}.muted{color:#9ca3af;font-size:12px}.section{border-top:1px solid #333;margin-top:14px;padding-top:13px}.check{display:flex;align-items:center;gap:7px;margin:5px 0;font-size:12px}.check input{width:auto}button,select,input{width:100%;box-sizing:border-box;background:#26262a;color:#eee;border:1px solid #424248;border-radius:7px;padding:8px;margin:4px 0}.row{display:flex;gap:7px}.row button{width:50%}.badge{display:inline-block;border-radius:12px;padding:3px 7px;margin:2px;background:#2b2b31;font-size:11px}.detail{font-size:12px;line-height:1.5;word-break:break-word}.status{padding:8px;border-radius:7px;background:#222228;margin-top:8px}.accent{color:#c4b5fd}</style></head><body><div id="network"></div><div id="side"><h2>Scientific Knowledge Graph</h2><div class="muted">Progressive view: starts with papers only. Expand one node at a time instead of rendering the entire graph at once.</div><div id="status" class="status muted">Loading…</div><div class="section"><label>Find any node</label><input id="findQuery" placeholder="P0005, water solubility, claim…"><button id="findBtn">Find / focus</button><div class="row"><button id="fit">Fit</button><button id="physics">Physics: on</button></div><button id="collapse">Collapse to papers</button></div><div class="section"><b>Node types</b><div id="types"></div></div><div class="section"><b>Relations</b><div id="relations"></div></div><div class="section"><b>Selected</b><div id="detail" class="muted">Click a node. Use “Expand 1-hop” to reveal its local neighborhood.</div></div></div><script>
const allNodeArray=__NODES__,allEdgeArray=__EDGES__,meta=__META__,types=__TYPES__,relations=__RELATIONS__,EXPAND_LIMIT=__EXPAND_LIMIT__,PHYSICS_THRESHOLD=__PHYSICS_THRESHOLD__;
const allNodesById=new Map(allNodeArray.map(n=>[n.id,n]));const allEdgesById=new Map(allEdgeArray.map(e=>[e.id,e]));const adjacency=new Map();for(const e of allEdgeArray){if(!adjacency.has(e.from))adjacency.set(e.from,[]);if(!adjacency.has(e.to))adjacency.set(e.to,[]);adjacency.get(e.from).push(e.id);adjacency.get(e.to).push(e.id);}const initialNodes=allNodeArray.filter(n=>n.type==='paper');let visibleNodeIds=new Set(initialNodes.map(n=>n.id));const nodes=new vis.DataSet(initialNodes),edges=new vis.DataSet();let physics=true;const network=new vis.Network(document.getElementById('network'),{nodes,edges},{interaction:{hover:true,tooltipDelay:180,multiselect:true},physics:{enabled:true,solver:'forceAtlas2Based',forceAtlas2Based:{gravitationalConstant:-72,centralGravity:.006,springLength:120,springConstant:.035,damping:.48,avoidOverlap:.4},stabilization:{iterations:350}},nodes:{scaling:{min:6,max:30}},edges:{smooth:{enabled:true,type:'continuous'}}});
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));document.getElementById('types').innerHTML=types.map(t=>`<label class="check"><input type="checkbox" data-type="${t}" checked>${t}</label>`).join('');document.getElementById('relations').innerHTML=relations.map(r=>`<label class="check"><input type="checkbox" data-rel="${r}" checked>${r}</label>`).join('');
function activeTypes(){return new Set([...document.querySelectorAll('[data-type]')].filter(x=>x.checked).map(x=>x.dataset.type));}function activeRelations(){return new Set([...document.querySelectorAll('[data-rel]')].filter(x=>x.checked).map(x=>x.dataset.rel));}function setStatus(message=''){const suffix=`${visibleNodeIds.size}/${allNodeArray.length} nodes · ${edges.length}/${allEdgeArray.length} visible edges`;document.getElementById('status').innerHTML=(message?esc(message)+'<br>':'')+`<span class="accent">${suffix}</span>`;}function setPhysics(on){physics=!!on;network.setOptions({physics:{enabled:physics}});document.getElementById('physics').textContent='Physics: '+(physics?'on':'off');}
function refreshNodeVisibility(){const ts=activeTypes();nodes.forEach(n=>nodes.update({id:n.id,hidden:!ts.has(n.type)}));}function rebuildEdges(){const ts=activeTypes(),rs=activeRelations();const batch=[];for(const e of allEdgeArray){if(!visibleNodeIds.has(e.from)||!visibleNodeIds.has(e.to)||!rs.has(e.relation))continue;const a=allNodesById.get(e.from),b=allNodesById.get(e.to);if(!a||!b||!ts.has(a.type)||!ts.has(b.type))continue;batch.push(e);}edges.clear();if(batch.length)edges.add(batch);setStatus();}
function addVisibleNode(id){if(visibleNodeIds.has(id))return false;const n=allNodesById.get(id);if(!n)return false;visibleNodeIds.add(id);nodes.add(n);return true;}function enableType(type){const box=document.querySelector(`[data-type="${CSS.escape(type)}"]`);if(box)box.checked=true;}function expandNode(id){const source=allNodesById.get(id);if(!source)return;const rs=activeRelations(),ts=activeTypes();let added=0,skipped=0;for(const eid of (adjacency.get(id)||[])){const e=allEdgesById.get(eid);if(!e||!rs.has(e.relation))continue;const other=e.from===id?e.to:e.from;const n=allNodesById.get(other);if(!n||!ts.has(n.type)||visibleNodeIds.has(other))continue;if(added>=EXPAND_LIMIT){skipped++;continue;}if(addVisibleNode(other))added++;}refreshNodeVisibility();rebuildEdges();if(visibleNodeIds.size<=PHYSICS_THRESHOLD&&added){setPhysics(true);network.stabilize(120);network.once('stabilized',()=>setPhysics(false));}else if(visibleNodeIds.size>PHYSICS_THRESHOLD){setPhysics(false);}network.selectNodes([id]);network.focus(id,{scale:Math.min(1.45,Math.max(.75,1.15)),animation:true});setStatus(`Expanded ${added} neighbors${skipped?`; ${skipped} more not added in this click`:''}.`);}
function collapseToPapers(){const paperNodes=allNodeArray.filter(n=>n.type==='paper');visibleNodeIds=new Set(paperNodes.map(n=>n.id));nodes.clear();nodes.add(paperNodes);refreshNodeVisibility();rebuildEdges();setPhysics(true);network.stabilize(220);network.once('stabilized',()=>setPhysics(false));network.fit({animation:true});setStatus('Collapsed to paper nodes.');}
function bestMatch(query){const q=query.trim().toLowerCase();if(!q)return null;for(const n of allNodeArray){if(String(n.id).toLowerCase()===q||String(meta[n.id]?.paper_id||'').toLowerCase()===q)return n;}let best=null,bestScore=-1;for(const n of allNodeArray){const m=meta[n.id]||{};const text=`${m.display_label||''} ${m.label||''} ${m.paper_id||''} ${n.id}`.toLowerCase();let score=-1;if(text.startsWith(q))score=4;else if(text.includes(q))score=2;if(n.type==='paper')score+=.25;if(score>bestScore){best=n;bestScore=score;}}return bestScore>=0?best:null;}
function ensureContext(id){const n=allNodesById.get(id);if(!n)return;enableType(n.type);addVisibleNode(id);if(n.type!=='paper'){for(const eid of (adjacency.get(id)||[])){const e=allEdgesById.get(eid);const other=e.from===id?e.to:e.from;const p=allNodesById.get(other);if(p&&p.type==='paper'){enableType('paper');addVisibleNode(other);break;}}}refreshNodeVisibility();rebuildEdges();}
function focusQuery(){const input=document.getElementById('findQuery');const found=bestMatch(input.value);if(!found){setStatus(`No node matched “${input.value}”.`);return;}ensureContext(found.id);network.selectNodes([found.id]);network.focus(found.id,{scale:1.55,animation:true});show(found.id);setStatus(`Focused ${meta[found.id]?.display_label||meta[found.id]?.label||found.id}.`);}
async function openPaper(n){if(!n||n.type!=='paper'||!n.paper_id)return;try{const r=await fetch(`http://127.0.0.1:8766/api/open_pdf?id=${encodeURIComponent(n.paper_id)}`,{method:'POST'});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);}catch(e){alert('Could not open the original PDF. Start the Review Literature App first.\n\n'+e);}}
function show(id){const n=meta[id];if(!n)return;const open=n.type==='paper'?'<button id="openPdfBtn">Open original PDF</button><div class="muted">Double-click this paper node to open immediately.</div>':'';document.getElementById('detail').innerHTML=`<div class="detail"><b>${esc(n.display_label||n.label||n.id)}</b><br><span class="badge">${esc(n.type)}</span><br>${Object.entries(n).filter(([k,v])=>!['id','label','type','display_label','node_size'].includes(k)&&v!==null&&v!==''&&JSON.stringify(v)!=='[]').map(([k,v])=>`<b>${esc(k)}</b>: ${esc(typeof v==='object'?JSON.stringify(v):v)}<br>`).join('')}<button id="expandSelectedBtn">Expand 1-hop</button>${open}</div>`;const x=document.getElementById('expandSelectedBtn');if(x)x.onclick=()=>expandNode(id);const b=document.getElementById('openPdfBtn');if(b)b.onclick=()=>openPaper(n);}
[...document.querySelectorAll('[data-type],[data-rel]')].forEach(x=>x.onchange=()=>{refreshNodeVisibility();rebuildEdges();});document.getElementById('fit').onclick=()=>network.fit({animation:true});document.getElementById('physics').onclick=()=>setPhysics(!physics);document.getElementById('collapse').onclick=collapseToPapers;document.getElementById('findBtn').onclick=focusQuery;document.getElementById('findQuery').addEventListener('keydown',e=>{if(e.key==='Enter')focusQuery();});network.on('click',p=>{if(p.nodes.length)show(p.nodes[0]);});network.on('doubleClick',p=>{if(!p.nodes.length)return;const n=meta[p.nodes[0]];if(n&&n.type==='paper')openPaper(n);else expandNode(p.nodes[0]);});network.once('stabilizationIterationsDone',()=>{setPhysics(false);setStatus('Paper overview ready. Click a node, then expand its neighborhood.');});refreshNodeVisibility();rebuildEdges();
</script></body></html>'''
    replacements = {
        "__VIS_JS__": script_src,
        "__NODES__": json.dumps(vis_nodes, ensure_ascii=False),
        "__EDGES__": json.dumps(vis_edges, ensure_ascii=False),
        "__META__": json.dumps({node["id"]: node for node in payload["nodes"]}, ensure_ascii=False),
        "__TYPES__": json.dumps(type_list),
        "__RELATIONS__": json.dumps(relation_list, ensure_ascii=False),
        "__EXPAND_LIMIT__": str(expand_limit),
        "__PHYSICS_THRESHOLD__": str(physics_threshold),
    }
    for key, value in replacements.items():
        html_doc = html_doc.replace(key, value)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")

def main() -> None:
    ap = argparse.ArgumentParser(description="Build a scientific knowledge graph from structured paper evidence.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--infer-claim-relations", action="store_true", help="Override config and run Qwen claim-relation inference.")
    ap.add_argument("--no-claim-relations", action="store_true", help="Disable Qwen claim-relation inference for this run.")
    args = ap.parse_args()
    config, root = load_config(args.config)
    paths = get_paths(config, root)
    curated_dir = paths.get("curated", root / "data/curated")
    use_curated = bool(config.get("curation", {}).get("enabled", True) and config.get("curation", {}).get("use_curated_for_graphs", True))
    cfg = dict(config.get("knowledge_graph", {}))
    if args.infer_claim_relations:
        cfg["infer_claim_relations"] = True
    if args.no_claim_relations:
        cfg["infer_claim_relations"] = False
    out_dir = paths.get("knowledge_graph", root / "outputs/knowledge_graph")
    out_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir = paths.get("metadata", root / "data/metadata")
    reference_dir = paths.get("reference_matches", root / "data/reference_matches")
    visual_dir = paths.get("visual_analysis", root / "data/visual_analysis")

    conn = sqlite3.connect(paths["database"])
    conn.row_factory = sqlite3.Row
    ensure_v4_schema(conn)
    rows = conn.execute("SELECT * FROM papers WHERE active=1 ORDER BY paper_id").fetchall()
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    all_claims: list[dict[str, Any]] = []

    for row in rows:
        paper_id = row["paper_id"]
        paper_path = paths["paper_json"] / f"{paper_id}.json"
        raw_inventory_path = paths["extracted"] / f"{paper_id}.inventory.json"
        raw_evidence_path = paths["extracted"] / f"{paper_id}.evidence.json"
        curated_inventory_path = curated_dir / f"{paper_id}.inventory.json"
        curated_evidence_path = curated_dir / f"{paper_id}.evidence.json"
        inventory_path = curated_inventory_path if use_curated and curated_inventory_path.exists() else raw_inventory_path
        evidence_path = curated_evidence_path if use_curated and curated_evidence_path.exists() else raw_evidence_path
        metadata_path = metadata_dir / f"{paper_id}.metadata.json"
        visual_path = visual_dir / f"{paper_id}.visual.json"
        reference_path = reference_dir / f"{paper_id}.references.json"
        if not paper_path.exists() or not inventory_path.exists() or not evidence_path.exists():
            continue
        paper = read_json(paper_path)
        inventory = read_json(inventory_path)
        evidence = read_json(evidence_path)
        metadata = read_json(metadata_path) if metadata_path.exists() else {}
        visual = read_json(visual_path) if visual_path.exists() else {}
        references = read_json(reference_path) if reference_path.exists() else {}
        canonical = canonical_metadata(paper, metadata)
        source_evidence_map = build_evidence_text_map(paper, visual)
        paper_node = f"paper:{paper_id}"
        add_node(
            nodes,
            paper_node,
            "paper",
            canonical.get("title") or paper_id,
            display_label=paper_display_label(canonical, paper_id),
            paper_id=paper_id,
            year=canonical.get("year"),
            journal=canonical.get("journal"),
            doi=canonical.get("doi"),
            source_relpath=row["source_relpath"],
            original_filename=row["original_filename"],
            article_type=inventory.get("article_type"),
            node_size=18,
        )

        property_nodes: dict[str, str] = {}
        property_terms: list[str] = []
        for item in inventory.get("studied_properties", []):
            value = item.get("property_normalized") or item.get("property_raw")
            if not value:
                continue
            normalized = normalize_key(value)
            node_id = f"property:{slug(normalized)}"
            property_nodes[normalized] = node_id
            property_terms.append(normalized)
            add_node(nodes, node_id, "property", value, normalized_name=normalized, node_size=10)
            add_edge(edges, seen_edges, paper_node, node_id, "STUDIES_PROPERTY")

        method_nodes: dict[str, str] = {}
        method_terms: list[str] = []
        for item in inventory.get("methods", []):
            value = item.get("method_normalized") or item.get("method_raw")
            if not value:
                continue
            normalized = normalize_key(value)
            node_id = f"method:{slug(normalized)}"
            method_nodes[normalized] = node_id
            method_terms.append(normalized)
            add_node(nodes, node_id, "method", value, normalized_name=normalized, target_property=item.get("target_property"), node_size=10)
            add_edge(edges, seen_edges, paper_node, node_id, "USES_METHOD")

        system_nodes: dict[str, str] = {}
        system_names: dict[str, str] = {}
        for item in inventory.get("systems", []):
            system_id = item.get("system_id")
            value = item.get("normalized_name") or item.get("system_name_raw")
            if not system_id or not value:
                continue
            normalized = normalize_key(value)
            node_id = f"system:{slug(normalized)}"
            system_nodes[system_id] = node_id
            system_names[system_id] = str(value)
            add_node(
                nodes,
                node_id,
                "system",
                value,
                normalized_name=normalized,
                system_type=item.get("system_type"),
                attributes=item.get("attributes", {}),
                node_size=10,
            )
            add_edge(edges, seen_edges, paper_node, node_id, "STUDIES_SYSTEM")

        for item in evidence.get("measurements", []):
            measurement_id = item.get("measurement_id")
            if not measurement_id:
                continue
            uid = f"measurement:{paper_id}:{measurement_id}"
            label = f"{item.get('property_normalized') or item.get('property_raw')}: {item.get('value_raw')}"
            add_node(
                nodes,
                uid,
                "measurement",
                label,
                paper_id=paper_id,
                value=item.get("value_raw"),
                parsed_value=item.get("parsed_value"),
                unit=item.get("unit_raw"),
                conditions=item.get("conditions_text"),
                status=item.get("status"),
                evidence_ids=item.get("evidence_sids", []),
                node_size=8,
            )
            add_edge(edges, seen_edges, paper_node, uid, "REPORTS_MEASUREMENT")
            prop_value = normalize_key(item.get("property_normalized") or item.get("property_raw") or "")
            if prop_value:
                prop_node = property_nodes.get(prop_value) or f"property:{slug(prop_value)}"
                add_node(nodes, prop_node, "property", item.get("property_normalized") or item.get("property_raw"), normalized_name=prop_value, node_size=10)
                add_edge(edges, seen_edges, uid, prop_node, "MEASURES_PROPERTY")
            for system_ref in item.get("system_refs", []):
                target = system_nodes.get(system_ref)
                if target:
                    add_edge(edges, seen_edges, uid, target, "MEASURED_ON")
            for evidence_id in item.get("evidence_sids", []):
                if str(evidence_id).startswith("vis:"):
                    visual_uid = f"visual:{paper_id}:{str(evidence_id).replace(':', '_')}"
                    add_edge(edges, seen_edges, uid, visual_uid, "SUPPORTED_BY_VISUAL")

        for item in evidence.get("claims", []):
            claim_id = item.get("claim_id")
            if not claim_id:
                continue
            uid = f"claim:{paper_id}:{claim_id}"
            add_node(
                nodes,
                uid,
                "claim",
                item.get("statement") or uid,
                display_label=claim_id,
                paper_id=paper_id,
                claim_type=item.get("claim_type"),
                origin=item.get("claim_origin"),
                conditions=item.get("conditions_text"),
                evidence_ids=item.get("evidence_sids", []),
                node_size=8,
            )
            add_edge(edges, seen_edges, paper_node, uid, "HAS_CLAIM")
            for system_ref in item.get("system_refs", []):
                target = system_nodes.get(system_ref)
                if target:
                    add_edge(edges, seen_edges, uid, target, "CLAIM_ABOUT_SYSTEM")
            statement_normalized = normalize_key(claim_text(item))
            for property_term, property_node in property_nodes.items():
                tokens = [token for token in property_term.split() if len(token) >= 4]
                if property_term and (property_term in statement_normalized or (tokens and all(token in statement_normalized for token in tokens[:3]))):
                    add_edge(edges, seen_edges, uid, property_node, "CLAIM_ABOUT_PROPERTY")
            for evidence_id in item.get("evidence_sids", []):
                if str(evidence_id).startswith("vis:"):
                    visual_uid = f"visual:{paper_id}:{str(evidence_id).replace(':', '_')}"
                    add_edge(edges, seen_edges, uid, visual_uid, "SUPPORTED_BY_VISUAL")
            if item.get("claim_origin") != "cited_literature_summary":
                all_claims.append(
                    {
                        **item,
                        "uid": uid,
                        "paper_id": paper_id,
                        "paper_title": canonical.get("title") or paper_id,
                        "paper_properties": property_terms,
                        "paper_methods": method_terms,
                        "system_names": [system_names[x] for x in item.get("system_refs", []) if x in system_names],
                        "evidence_excerpts": evidence_excerpts(item.get("evidence_sids", []), source_evidence_map),
                    }
                )

        for item in visual.get("assets", []):
            evidence_id = item.get("evidence_id") or item.get("asset_id")
            if not evidence_id:
                continue
            uid = f"visual:{paper_id}:{evidence_id.replace(':', '_')}"
            add_node(
                nodes,
                uid,
                "visual",
                item.get("caption") or item.get("summary_text") or evidence_id,
                display_label=evidence_id,
                paper_id=paper_id,
                kind=item.get("kind"),
                page=item.get("page"),
                crop_path=item.get("crop_path"),
                analysis_status=item.get("analysis_status"),
                node_size=7,
            )
            add_edge(edges, seen_edges, paper_node, uid, "HAS_VISUAL")

        citation_records = list(references.get("references", [])) + list(references.get("openalex_only_local_edges", []))
        for item in citation_records:
            target = item.get("target_paper_id")
            if target:
                target_node = f"paper:{target}"
                add_edge(
                    edges,
                    seen_edges,
                    paper_node,
                    target_node,
                    "CITES",
                    weight=1.0,
                    match_method=item.get("match_method"),
                    ref_id=item.get("ref_id"),
                )

    pair_cfg = cfg.get("claim_relations", {})
    pairs = candidate_claim_pairs(
        all_claims,
        top_k=int(pair_cfg.get("top_k_per_claim", 5)),
        threshold=float(pair_cfg.get("candidate_similarity_threshold", 0.30)),
        max_pairs=int(pair_cfg.get("max_pairs", 250)),
    )
    if cfg.get("infer_claim_relations", False):
        relation_cfg = {**cfg, **pair_cfg, "infer_claim_relations": True}
        inferred = infer_relations(pairs, relation_cfg, root, conn, config["llm"])
    else:
        inferred = []
    for item in inferred:
        add_edge(
            edges,
            seen_edges,
            item["source_claim_uid"],
            item["target_claim_uid"],
            item["relation"],
            weight=max(0.1, float(item.get("confidence", 0.0))),
            confidence=item.get("confidence"),
            rationale=item.get("rationale"),
            condition_difference=item.get("condition_difference"),
        )

    node_list = list(nodes.values())
    type_counts = Counter(node["type"] for node in node_list)
    relation_counts = Counter(edge["relation"] for edge in edges)
    payload = {
        "version": SCRIPT_VERSION,
        "nodes": node_list,
        "edges": edges,
        "summary": {
            "node_count": len(node_list),
            "edge_count": len(edges),
            "node_types": dict(type_counts),
            "relations": dict(relation_counts),
            "claim_relation_candidates": len(pairs),
            "claim_relations_inferred": len(inferred),
        },
        "provenance": {
            "claim_relation_inference_enabled": bool(cfg.get("infer_claim_relations", False)),
        },
        "gui": {
            "initial_mode": "papers",
            "expand_limit": int((cfg.get("gui") or {}).get("expand_limit", 400)),
            "physics_disable_threshold": int((cfg.get("gui") or {}).get("physics_disable_threshold", 450)),
        },
    }
    payload.setdefault("provenance", {})["curation_enabled"] = use_curated
    write_json(out_dir / "knowledge.json", payload)
    write_tables(out_dir, node_list, edges)
    local_js = root / "assets/vis-network.min.js"
    target_asset = out_dir / "assets/vis-network.min.js"
    local_arg = None
    if local_js.exists():
        target_asset.parent.mkdir(parents=True, exist_ok=True)
        target_asset.write_bytes(local_js.read_bytes())
        local_arg = target_asset
    make_gui(out_dir / "knowledge.html", payload, local_arg)
    print(
        f"Knowledge graph: {len(node_list)} nodes, {len(edges)} edges, "
        f"{len(inferred)} inferred claim relations"
    )
    print(f"GUI: {out_dir / 'knowledge.html'}")


if __name__ == "__main__":
    main()
