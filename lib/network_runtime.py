from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from typing import Any, Iterable

import igraph as ig
import leidenalg as la
import networkx as nx
import numpy as np

LAYER_ORDER = [
    "citation",
    "semantic",
    "claim",
    "property",
    "method",
    "keyword",
    "keyword_semantic",
    "bibliographic_coupling",
]

LAYER_LABELS = {
    "citation": "Citation",
    "semantic": "SPECTER2 paper semantic",
    "claim": "Curated claim similarity",
    "property": "Property overlap",
    "method": "Method overlap",
    "keyword": "Canonical keyword overlap",
    "keyword_semantic": "Automatic keyword semantic relation",
    "bibliographic_coupling": "Bibliographic coupling",
}

LAYER_COLORS = {
    "citation": "#f97316",
    "semantic": "#8b5cf6",
    "claim": "#ec4899",
    "property": "#14b8a6",
    "method": "#3b82f6",
    "keyword": "#eab308",
    "keyword_semantic": "#22c55e",
    "bibliographic_coupling": "#9ca3af",
}


def _edge_key(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left <= right else (right, left)


def layer_graph(ids: list[str], edges: dict[tuple[str, str], float]) -> ig.Graph:
    index = {paper_id: i for i, paper_id in enumerate(ids)}
    graph = ig.Graph(
        n=len(ids),
        edges=[(index[a], index[b]) for a, b in edges if a in index and b in index and a != b],
        directed=False,
    )
    graph.vs["name"] = ids
    if graph.ecount():
        graph.es["weight"] = [float(edges[_edge_key(ids[e.source], ids[e.target])]) for e in graph.es]
    return graph


def cluster_multiplex(
    ids: list[str],
    layers: dict[str, dict[tuple[str, str], float]],
    weights: dict[str, float],
    *,
    resolution: float,
    seed: int,
) -> list[int]:
    """Run Leiden over the full selected multiplex layers.

    The visualization may later sparsify edges, but community detection always
    receives the complete selected-layer graph so display settings do not alter
    the scientific clustering input.
    """
    active_names = [
        name
        for name in LAYER_ORDER
        if layers.get(name) and float(weights.get(name, 0.0)) != 0.0
    ]
    if not active_names:
        return list(range(len(ids)))

    graphs = [layer_graph(ids, layers[name]) for name in active_names]
    layer_weights = [float(weights.get(name, 1.0)) for name in active_names]
    kwargs = {"weights": "weight", "resolution_parameter": float(resolution)}
    try:
        if len(graphs) == 1:
            partition = la.find_partition(
                graphs[0],
                la.RBConfigurationVertexPartition,
                n_iterations=-1,
                seed=int(seed),
                **kwargs,
            )
            return list(partition.membership)
        membership, _ = la.find_partition_multiplex(
            graphs,
            la.RBConfigurationVertexPartition,
            layer_weights=layer_weights,
            n_iterations=-1,
            seed=int(seed),
            **kwargs,
        )
        return list(membership)
    except Exception:
        # A weighted aggregate fallback keeps the endpoint useful even if a
        # particular leidenalg build lacks multiplex support.
        combined: dict[tuple[str, str], float] = defaultdict(float)
        for name in active_names:
            weight = float(weights.get(name, 1.0))
            for key, value in layers[name].items():
                combined[_edge_key(*key)] += weight * float(value)
        graph = layer_graph(ids, dict(combined))
        partition = la.find_partition(
            graph,
            la.RBConfigurationVertexPartition,
            weights="weight",
            resolution_parameter=float(resolution),
            n_iterations=-1,
            seed=int(seed),
        )
        return list(partition.membership)


def remap_membership(ids: list[str], raw_membership: Iterable[int]) -> dict[str, int]:
    values = [int(x) for x in raw_membership]
    unique = sorted(set(values))
    remap = {old: new for new, old in enumerate(unique)}
    return {paper_id: remap[values[index]] for index, paper_id in enumerate(ids)}


def cluster_labels_from_nodes(
    nodes: dict[str, dict[str, Any]], membership: dict[str, int], *, max_terms: int = 4
) -> tuple[dict[int, str], list[dict[str, Any]]]:
    grouped: dict[int, list[str]] = defaultdict(list)
    for paper_id, cluster_id in membership.items():
        grouped[int(cluster_id)].append(paper_id)

    labels: dict[int, str] = {}
    clusters: list[dict[str, Any]] = []
    for cluster_id in sorted(grouped):
        paper_ids = grouped[cluster_id]
        counter: Counter[str] = Counter()
        for paper_id in paper_ids:
            node = nodes.get(paper_id, {})
            counter.update("property:" + str(x) for x in node.get("properties", []) if x)
            counter.update("method:" + str(x) for x in node.get("methods", []) if x)
            counter.update("keyword:" + str(x) for x in node.get("keywords", []) if x)
        terms = [term.split(":", 1)[1] for term, _ in counter.most_common(max_terms)]
        label = " / ".join(terms) if terms else f"cluster {cluster_id + 1}"
        labels[cluster_id] = label
        clusters.append({
            "cluster_id": cluster_id,
            "label": label,
            "size": len(paper_ids),
            "paper_ids": sorted(paper_ids),
        })
    return labels, clusters


def edge_selected_score(
    edge: dict[str, Any], selected_layers: set[str], layer_weights: dict[str, float]
) -> float:
    components = edge.get("components") or {}
    return sum(
        float(layer_weights.get(name, 1.0)) * float(components.get(name, 0.0) or 0.0)
        for name in selected_layers
    )


def sparsify_edge_records(
    ids: list[str],
    edge_records: list[dict[str, Any]],
    selected_layers: set[str],
    layer_weights: dict[str, float],
    *,
    top_k: int,
    max_edges: int,
    preserve_citation: bool = True,
) -> list[dict[str, Any]]:
    """Create a display backbone using MSF + symmetric weighted top-k.

    The maximum spanning forest preserves the strongest available connection
    structure in every connected component. The symmetric top-k union retains
    each paper's strongest local neighbors. Strongest remaining edges fill the
    configured edge budget. This is strictly a rendering sparsifier; Leiden is
    run on the unsparsified selected layers.
    """
    scored: list[tuple[float, dict[str, Any]]] = []
    for edge in edge_records:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if not source or not target or source == target:
            continue
        score = edge_selected_score(edge, selected_layers, layer_weights)
        if score > 0:
            scored.append((float(score), edge))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return []

    keep: set[tuple[str, str]] = set()

    # Preserve direct citations because they encode direction and research
    # genealogy rather than just visual similarity.
    if preserve_citation and "citation" in selected_layers:
        for _, edge in scored:
            if float((edge.get("components") or {}).get("citation", 0.0) or 0.0) > 0:
                keep.add(_edge_key(str(edge["source"]), str(edge["target"])))

    graph = nx.Graph()
    graph.add_nodes_from(ids)
    for score, edge in scored:
        graph.add_edge(str(edge["source"]), str(edge["target"]), weight=float(score))
    if graph.number_of_edges():
        forest = nx.maximum_spanning_tree(graph, weight="weight")
        keep.update(_edge_key(str(a), str(b)) for a, b in forest.edges())

    adjacency: dict[str, list[tuple[float, tuple[str, str]]]] = defaultdict(list)
    for score, edge in scored:
        key = _edge_key(str(edge["source"]), str(edge["target"]))
        adjacency[key[0]].append((score, key))
        adjacency[key[1]].append((score, key))
    for paper_id in ids:
        for _, key in sorted(adjacency.get(paper_id, []), reverse=True)[: max(0, int(top_k))]:
            keep.add(key)

    target_budget = max(len(keep), int(max_edges))
    for _, edge in scored:
        if len(keep) >= target_budget:
            break
        keep.add(_edge_key(str(edge["source"]), str(edge["target"])))

    result = []
    for score, edge in scored:
        if _edge_key(str(edge["source"]), str(edge["target"])) in keep:
            item = dict(edge)
            item["selected_weight"] = float(score)
            result.append(item)
    return result


def _circle_positions(ids: list[str], scale: float) -> dict[str, dict[str, float]]:
    n = max(1, len(ids))
    return {
        paper_id: {
            "x": float(scale * math.cos(2.0 * math.pi * i / n)),
            "y": float(scale * math.sin(2.0 * math.pi * i / n)),
        }
        for i, paper_id in enumerate(ids)
    }


def compute_layout_positions(
    ids: list[str],
    edge_records: list[dict[str, Any]],
    selected_layers: set[str],
    layer_weights: dict[str, float],
    *,
    seed: int = 42,
    top_k: int = 6,
    edge_factor: float = 8.0,
    large_graph_threshold: int = 200,
    scale: float = 850.0,
) -> dict[str, dict[str, float]]:
    """Precompute a deterministic, sparse-backbone layout in Python.

    Browser physics can then remain disabled, avoiding a costly stabilization
    pass on every page load. Fruchterman-Reingold is used for ordinary project
    sizes; igraph DrL is used for larger projects.
    """
    if len(ids) <= 1:
        return {paper_id: {"x": 0.0, "y": 0.0} for paper_id in ids}

    sparse = sparsify_edge_records(
        ids,
        edge_records,
        selected_layers,
        layer_weights,
        top_k=max(1, int(top_k)),
        max_edges=max(len(ids) - 1, int(math.ceil(float(edge_factor) * len(ids)))),
        preserve_citation=True,
    )
    if not sparse:
        return _circle_positions(ids, scale)

    index = {paper_id: i for i, paper_id in enumerate(ids)}
    graph_edges: list[tuple[int, int]] = []
    graph_weights: list[float] = []
    for edge in sparse:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in index and target in index and source != target:
            graph_edges.append((index[source], index[target]))
            graph_weights.append(max(1e-6, float(edge.get("selected_weight") or 0.0)))
    if not graph_edges:
        return _circle_positions(ids, scale)

    graph = ig.Graph(n=len(ids), edges=graph_edges, directed=False)
    graph.es["weight"] = graph_weights
    random.seed(int(seed))
    np.random.seed(int(seed) & 0xFFFFFFFF)
    try:
        if len(ids) >= int(large_graph_threshold):
            layout = graph.layout_drl(weights="weight")
        else:
            niter = max(350, min(1200, 8 * len(ids)))
            layout = graph.layout_fruchterman_reingold(weights="weight", niter=niter)
        coords = np.asarray(layout.coords, dtype=float)
    except Exception:
        return _circle_positions(ids, scale)

    if coords.shape != (len(ids), 2) or not np.isfinite(coords).all():
        return _circle_positions(ids, scale)
    coords -= coords.mean(axis=0, keepdims=True)
    span = float(np.max(np.abs(coords)))
    if span <= 1e-12:
        return _circle_positions(ids, scale)
    coords *= float(scale) / span
    return {
        paper_id: {"x": round(float(coords[i, 0]), 3), "y": round(float(coords[i, 1]), 3)}
        for i, paper_id in enumerate(ids)
    }
