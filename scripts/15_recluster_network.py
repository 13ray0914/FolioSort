#!/usr/bin/env python3
from __future__ import annotations

# Always use the isolated network environment because igraph/leidenalg live there.
import os as _bootstrap_os
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_BOOT_ROOT = _BootstrapPath(__file__).resolve().parents[1]
_BOOT_VENV = _BOOT_ROOT / ".venv_network"
_BOOT_PY = _BOOT_VENV / "bin" / "python"
if _BOOT_PY.exists() and _BootstrapPath(_bootstrap_sys.prefix).resolve() != _BOOT_VENV.resolve():
    _bootstrap_os.execv(str(_BOOT_PY), [str(_BOOT_PY), str(_BootstrapPath(__file__).resolve()), *_bootstrap_sys.argv[1:]])

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.network_runtime import (
    LAYER_ORDER,
    cluster_labels_from_nodes,
    cluster_multiplex,
    compute_layout_positions,
    remap_membership,
)
from lib.pipeline_common import load_config, read_json, stable_json_hash, write_json
from lib.projects import normalize_project_slug, project_network_dir

SCRIPT_VERSION = "network-recluster-v4.1.5-hierarchical-cluster-naming"


def layer_maps(payload: dict[str, Any], selected: set[str]) -> dict[str, dict[tuple[str, str], float]]:
    out: dict[str, dict[tuple[str, str], float]] = {}
    for name in LAYER_ORDER:
        if name not in selected:
            continue
        records = (payload.get("layers") or {}).get(name) or []
        edges: dict[tuple[str, str], float] = {}
        for item in records:
            source = str(item.get("source") or "")
            target = str(item.get("target") or "")
            if not source or not target or source == target:
                continue
            key = (source, target) if source <= target else (target, source)
            edges[key] = max(edges.get(key, 0.0), float(item.get("weight", 0.0) or 0.0))
        out[name] = edges
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Recluster an existing FolioSort project network using selected layers.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--project", required=True)
    ap.add_argument("--layers", required=True, help="Comma-separated layer names")
    ap.add_argument("--resolution", type=float, default=1.0)
    ap.add_argument("--save", action="store_true", help="Save the last interactive reclustering result")
    args = ap.parse_args()

    config, root = load_config(args.config)
    slug = normalize_project_slug(args.project)
    network_path = project_network_dir(root, slug) / "network.json"
    if not network_path.exists():
        raise SystemExit(f"Network data not found: {network_path}")
    payload = read_json(network_path)

    available = set((payload.get("layers") or {}).keys()) & set(LAYER_ORDER)
    selected = [name.strip() for name in args.layers.split(",") if name.strip()]
    selected = [name for name in LAYER_ORDER if name in selected and name in available]
    if not selected:
        raise SystemExit("Select at least one non-empty network layer")

    resolution = min(3.0, max(0.2, float(args.resolution)))
    ids = [str(node.get("paper_id")) for node in payload.get("nodes", []) if node.get("paper_id")]
    nodes = {str(node["paper_id"]): dict(node) for node in payload.get("nodes", []) if node.get("paper_id")}
    layers = layer_maps(payload, set(selected))
    weights = {name: float((payload.get("layer_weights") or {}).get(name, 1.0)) for name in selected}
    cfg = config.get("multiplex_graph", {})
    clustering_cfg = cfg.get("clustering", {})
    gui_cfg = cfg.get("gui", {})
    seed = int(clustering_cfg.get("seed", 42))

    raw_membership = cluster_multiplex(
        ids,
        layers,
        weights,
        resolution=resolution,
        seed=seed,
    )
    membership = remap_membership(ids, raw_membership)
    labels, clusters = cluster_labels_from_nodes(nodes, membership)

    positions = compute_layout_positions(
        ids,
        list(payload.get("edges", [])),
        set(selected),
        payload.get("layer_weights") or {},
        seed=seed,
        top_k=int(gui_cfg.get("layout_top_k", 6)),
        edge_factor=float(gui_cfg.get("layout_edge_factor", 8.0)),
        large_graph_threshold=int(gui_cfg.get("layout_large_graph_threshold", 200)),
        scale=float(gui_cfg.get("layout_scale", 850.0)),
    )

    network_signature = str((payload.get("provenance") or {}).get("network_signature") or stable_json_hash({
        "nodes": ids,
        "layers": payload.get("layers", {}),
        "weights": payload.get("layer_weights", {}),
    })[:20])
    result = {
        "ok": True,
        "script_version": SCRIPT_VERSION,
        "project": slug,
        "network_signature": network_signature,
        "selected_layers": selected,
        "resolution": resolution,
        "membership": membership,
        "cluster_labels": {str(key): value for key, value in labels.items()},
        "clusters": clusters,
        "positions": positions,
        "algorithm": "Leiden multiplex on full selected layers; sparse backbone used only for layout/rendering",
    }
    if args.save:
        write_json(project_network_dir(root, slug) / "recluster.last.json", result)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
