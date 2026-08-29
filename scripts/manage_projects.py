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
_BOOT_VENV = _BOOT_ROOT / ".venv"
_BOOT_PY = _BOOT_VENV / "bin" / "python"
if _BOOT_PY.exists() and _BootstrapPath(_bootstrap_sys.prefix).resolve() != _BOOT_VENV.resolve():
    _bootstrap_os.execv(str(_BOOT_PY), [str(_BOOT_PY), str(_BootstrapPath(__file__).resolve()), *_bootstrap_sys.argv[1:]])

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import get_paths, load_config
from lib.projects import (
    assign_paper_to_project,
    create_project,
    ensure_project_schema,
    list_projects,
    normalize_project_slug,
    rename_project,
    unassign_paper_from_project,
)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage project scopes for literature graphs.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("list")
    c = sub.add_parser("create"); c.add_argument("name"); c.add_argument("--slug")
    r = sub.add_parser("rename"); r.add_argument("project"); r.add_argument("name")
    a = sub.add_parser("assign"); a.add_argument("project"); a.add_argument("paper_ids", nargs="+")
    u = sub.add_parser("unassign"); u.add_argument("project"); u.add_argument("paper_ids", nargs="+")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    conn = connect(paths["database"])
    ensure_project_schema(conn)

    if args.command == "init":
        print("Project schema ready.")
    elif args.command == "list":
        for item in list_projects(conn):
            print(f"{item['project_slug']:20s} {item['active_papers']:4d} active  {item['name']}")
    elif args.command == "create":
        slug = create_project(conn, args.name, args.slug)
        print(f"Created project: {slug} ({args.name})")
    elif args.command == "rename":
        rename_project(conn, args.project, args.name)
        print(f"Renamed project: {normalize_project_slug(args.project)} -> {args.name}")
    elif args.command in {"assign", "unassign"}:
        slug = normalize_project_slug(args.project)
        for paper_id in args.paper_ids:
            row = conn.execute("SELECT 1 FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
            if not row:
                raise SystemExit(f"Unknown paper ID: {paper_id}")
            if args.command == "assign":
                assign_paper_to_project(conn, paper_id, slug)
            else:
                unassign_paper_from_project(conn, paper_id, slug)
        conn.commit()
        print(f"{args.command.title()}ed {len(args.paper_ids)} paper(s) {'to' if args.command == 'assign' else 'from'} {slug}.")

    conn.close()


if __name__ == "__main__":
    main()
