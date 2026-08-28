#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import torch
from adapters import AutoAdapterModel
from transformers import AutoTokenizer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import connect_db, get_paths, load_config, read_json, sha256_text, write_json

SCRIPT_VERSION = "specter2-v4.0"


def abstract_text(paper: dict[str, Any]) -> str:
    return " ".join(
        sentence.get("text", "")
        for paragraph in paper.get("abstract", [])
        for sentence in paragraph.get("sentences", [])
        if sentence.get("text")
    )


def paper_text(paper: dict[str, Any], metadata: dict[str, Any], memory: dict[str, Any]) -> tuple[str, str, str]:
    canonical = metadata.get("canonical") or paper.get("metadata") or {}
    title = str(canonical.get("title") or paper.get("metadata", {}).get("title") or "").strip()
    abstract = abstract_text(paper).strip()
    if not abstract:
        parts = [
            memory.get("central_question") or "",
            memory.get("study_design") or "",
            memory.get("mechanistic_model") or "",
        ]
        parts.extend(item.get("statement", "") for item in memory.get("major_findings", []))
        abstract = " ".join(x for x in parts if x).strip()
    return title, abstract, sha256_text(title + "\n" + abstract)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build incremental SPECTER2 paper embeddings from title + abstract.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    embedding_dir = paths.get("embeddings", root / "data/embeddings")
    metadata_dir = paths.get("metadata", root / "data/metadata")
    memory_dir = paths.get("summary_memory", root / "data/summary_memory")
    embedding_dir.mkdir(parents=True, exist_ok=True)
    matrix_path = embedding_dir / "specter2.npy"
    index_path = embedding_dir / "specter2.index.json"

    cfg = config.get("embedding", {})
    base_model = cfg.get("base_model", "allenai/specter2_base")
    adapter_model = cfg.get("adapter_model", "allenai/specter2")
    adapter_name = cfg.get("adapter_name", "proximity")
    device_name = cfg.get("device", "cpu")
    if device_name == "auto":
        device_name = "cuda" if torch.cuda.is_available() else "cpu"
    if device_name.startswith("cuda") and not torch.cuda.is_available():
        print("WARNING: CUDA requested for SPECTER2 but unavailable; using CPU")
        device_name = "cpu"
    device = torch.device(device_name)
    batch_size = int(cfg.get("batch_size", 8 if device.type == "cpu" else 16))
    max_length = int(cfg.get("max_length", 512))

    conn = connect_db(paths["database"])
    rows = conn.execute("SELECT * FROM papers WHERE active=1 ORDER BY paper_id").fetchall()
    papers: list[dict[str, Any]] = []
    for row in rows:
        paper_id = row["paper_id"]
        paper_path = paths["paper_json"] / f"{paper_id}.json"
        memory_path = memory_dir / f"{paper_id}.memory.json"
        metadata_path = metadata_dir / f"{paper_id}.metadata.json"
        if not paper_path.exists() or not memory_path.exists():
            continue
        paper = read_json(paper_path)
        memory = read_json(memory_path)
        metadata = read_json(metadata_path) if metadata_path.exists() else {}
        title, abstract, text_hash = paper_text(paper, metadata, memory)
        if not title and not abstract:
            continue
        papers.append({"paper_id": paper_id, "title": title, "abstract": abstract, "input_hash": text_hash})

    old_index = read_json(index_path) if index_path.exists() else {}
    old_matrix = np.load(matrix_path) if matrix_path.exists() else None
    old_lookup: dict[str, tuple[int, str]] = {}
    if old_matrix is not None and old_index.get("model_signature"):
        for index, item in enumerate(old_index.get("items", [])):
            old_lookup[item["paper_id"]] = (index, item.get("input_hash", ""))

    model_signature = sha256_text(
        json.dumps(
            {
                "script": SCRIPT_VERSION,
                "base_model": base_model,
                "adapter_model": adapter_model,
                "adapter_name": adapter_name,
                "max_length": max_length,
            },
            sort_keys=True,
        )
    )
    can_reuse = old_index.get("model_signature") == model_signature and old_matrix is not None
    pending = [
        item for item in papers
        if args.force or not can_reuse or item["paper_id"] not in old_lookup or old_lookup[item["paper_id"]][1] != item["input_hash"]
    ]
    print(f"SPECTER2 papers={len(papers)} pending={len(pending)} reused={len(papers)-len(pending)} device={device}")

    new_vectors: dict[str, np.ndarray] = {}
    if pending:
        tokenizer = AutoTokenizer.from_pretrained(base_model)
        model = AutoAdapterModel.from_pretrained(base_model)
        model.load_adapter(adapter_model, source="hf", load_as=adapter_name, set_active=True)
        model.to(device)
        model.eval()
        separator = tokenizer.sep_token or " [SEP] "
        with torch.inference_mode():
            for start in range(0, len(pending), batch_size):
                batch = pending[start : start + batch_size]
                texts = [item["title"] + separator + (item["abstract"] or "") for item in batch]
                inputs = tokenizer(
                    texts,
                    padding=True,
                    truncation=True,
                    return_tensors="pt",
                    return_token_type_ids=False,
                    max_length=max_length,
                )
                inputs = {key: value.to(device) for key, value in inputs.items()}
                output = model(**inputs)
                vectors = output.last_hidden_state[:, 0, :].detach().float().cpu().numpy()
                norms = np.linalg.norm(vectors, axis=1, keepdims=True)
                vectors = vectors / np.maximum(norms, 1e-12)
                for item, vector in zip(batch, vectors):
                    new_vectors[item["paper_id"]] = vector.astype(np.float32)
                print(f"  encoded {min(start+len(batch), len(pending))}/{len(pending)}")

    vectors_out: list[np.ndarray] = []
    for item in papers:
        paper_id = item["paper_id"]
        if paper_id in new_vectors:
            vectors_out.append(new_vectors[paper_id])
        elif can_reuse and paper_id in old_lookup:
            vectors_out.append(np.asarray(old_matrix[old_lookup[paper_id][0]], dtype=np.float32))
        else:
            raise RuntimeError(f"missing vector for {paper_id}")
    matrix = np.vstack(vectors_out) if vectors_out else np.zeros((0, 768), dtype=np.float32)
    np.save(matrix_path, matrix)
    index = {
        "model_signature": model_signature,
        "base_model": base_model,
        "adapter_model": adapter_model,
        "adapter_name": adapter_name,
        "dimension": int(matrix.shape[1]) if matrix.ndim == 2 and matrix.shape[0] else None,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": [
            {
                "paper_id": item["paper_id"],
                "input_hash": item["input_hash"],
                "title": item["title"],
                "abstract_chars": len(item["abstract"]),
            }
            for item in papers
        ],
    }
    write_json(index_path, index)
    print(f"Embeddings: {matrix_path} shape={matrix.shape}")
    print(f"Index     : {index_path}")


if __name__ == "__main__":
    main()
