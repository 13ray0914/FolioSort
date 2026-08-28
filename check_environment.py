#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent


def check_url(label: str, url: str, *, expect_true: bool = False) -> bool:
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        if expect_true and response.text.strip().lower() != "true":
            raise RuntimeError(response.text[:160])
        print(f"[OK] {label}: {url}")
        return True
    except Exception as error:
        print(f"[FAIL] {label}: {error}")
        return False


def main() -> None:
    config_path = ROOT / "config.json"
    if not config_path.exists():
        raise SystemExit("config.json is missing")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    failures = 0
    for relative in [
        "scripts/01_make_manifest.py", "scripts/04_enrich_metadata.py",
        "scripts/06_build_summary_memory.py", "scripts/10_resolve_references.py",
        "scripts/13_build_multiplex_network.py", "scripts/14_build_knowledge_graph.py",
        "schemas/v4/summary_memory.schema.json", "lib/v4_common.py",
    ]:
        path = ROOT / relative
        if path.exists():
            print(f"[OK] file: {relative}")
        else:
            print(f"[FAIL] missing: {relative}")
            failures += 1
    for module in ["requests", "lxml", "jsonschema", "fitz", "rapidfuzz"]:
        if importlib.util.find_spec(module):
            print(f"[OK] Python module: {module}")
        else:
            print(f"[FAIL] Python module: {module}")
            failures += 1
    grobid = config["grobid"]["base_url"].rstrip("/") + "/api/isalive"
    llm = config["llm"]["base_url"].rstrip("/") + "/models"
    failures += 0 if check_url("GROBID", grobid, expect_true=True) else 1
    failures += 0 if check_url("text Qwen", llm) else 1
    vision_cfg = config.get("visual", {}).get("vision_llm", {})
    if vision_cfg.get("enabled"):
        failures += 0 if check_url("vision LLM", vision_cfg["base_url"].rstrip("/") + "/models") else 1
    for name, path in [
        ("network env", ROOT / ".venv_network/bin/python"),
        ("SPECTER2 env", ROOT / ".venv_specter2/bin/python"),
        ("MinerU env", ROOT / ".venv_mineru/bin/mineru"),
    ]:
        print(f"[{'OK' if path.exists() else 'OPTIONAL'}] {name}: {path}")
    if failures:
        raise SystemExit(f"Environment check failed: {failures} required item(s)")
    print("Environment check passed.")


if __name__ == "__main__":
    main()
