#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import LlamaCppClient, get_paths, load_config, load_schema


def main() -> None:
    ap = argparse.ArgumentParser(description="Check local directories, GROBID, llama.cpp, and profile schemas.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    print("[OK] Project paths")
    for key, path in paths.items():
        print(f"  {key}: {path}")

    profile_dir = root / "profiles" / config["profile"]
    load_schema(profile_dir / "inventory.schema.json")
    load_schema(profile_dir / "evidence.schema.json")
    print(f"[OK] Profile schemas: {config['profile']}")

    grobid = config["grobid"]["base_url"].rstrip("/")
    r = requests.get(f"{grobid}/api/isalive", timeout=15)
    r.raise_for_status()
    if r.text.strip().lower() != "true":
        raise RuntimeError(f"GROBID returned: {r.text}")
    version = requests.get(f"{grobid}/api/version", timeout=15)
    print(f"[OK] GROBID: {version.text.strip() if version.ok else 'alive'}")

    client = LlamaCppClient(config["llm"])
    model = client.healthcheck()
    print(f"[OK] llama.cpp model: {model}")
    print("\nEnvironment check passed.")


if __name__ == "__main__":
    main()
