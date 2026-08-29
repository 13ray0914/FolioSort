from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

DEFAULT_PROJECT_SLUG = "default"
DEFAULT_PROJECT_NAME = "Default project"
PROJECTS_PREFIX = "projects"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify_project_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-+", "-", text)
    if text:
        return text[:64]
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_PROJECT_SLUG
    return "project-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def normalize_project_slug(value: str | None) -> str:
    if value in (None, ""):
        return DEFAULT_PROJECT_SLUG
    slug = slugify_project_name(str(value))
    if slug in {"all", "global", "none"}:
        raise ValueError(f"Reserved project slug: {slug}")
    return slug


def infer_project_slug(source_relpath: str | None) -> str:
    rel = PurePosixPath(str(source_relpath or ""))
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == PROJECTS_PREFIX:
        return normalize_project_slug(parts[1])
    return DEFAULT_PROJECT_SLUG


def ensure_project_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects(
            project_slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_projects(
            paper_id TEXT NOT NULL,
            project_slug TEXT NOT NULL,
            assigned_at TEXT NOT NULL,
            PRIMARY KEY(paper_id, project_slug)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_projects_project ON paper_projects(project_slug, paper_id)")
    ts = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO projects(project_slug,name,created_at,updated_at) VALUES (?,?,?,?)",
        (DEFAULT_PROJECT_SLUG, DEFAULT_PROJECT_NAME, ts, ts),
    )
    # Backward-compatible migration: every paper that predates project support gets
    # a membership inferred from its current source path. Existing memberships are
    # never removed because one paper may intentionally belong to multiple projects.
    try:
        rows = conn.execute("SELECT paper_id,source_relpath FROM papers").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        paper_id = row["paper_id"] if isinstance(row, sqlite3.Row) else row[0]
        source_relpath = row["source_relpath"] if isinstance(row, sqlite3.Row) else row[1]
        has_any = conn.execute("SELECT 1 FROM paper_projects WHERE paper_id=? LIMIT 1", (paper_id,)).fetchone()
        if has_any:
            continue
        slug = infer_project_slug(source_relpath)
        ensure_project(conn, slug, DEFAULT_PROJECT_NAME if slug == DEFAULT_PROJECT_SLUG else slug.replace("-", " ").title())
        assign_paper_to_project(conn, paper_id, slug)
    conn.commit()


def ensure_project(conn: sqlite3.Connection, project_slug: str, name: str | None = None) -> str:
    slug = normalize_project_slug(project_slug)
    ts = now_iso()
    display = (name or (DEFAULT_PROJECT_NAME if slug == DEFAULT_PROJECT_SLUG else slug.replace("-", " ").title())).strip()
    conn.execute(
        "INSERT OR IGNORE INTO projects(project_slug,name,created_at,updated_at) VALUES (?,?,?,?)",
        (slug, display, ts, ts),
    )
    return slug


def create_project(conn: sqlite3.Connection, name: str, project_slug: str | None = None) -> str:
    display = str(name or "").strip()
    if not display:
        raise ValueError("Project name is required")
    slug = normalize_project_slug(project_slug or display)
    existing = conn.execute("SELECT name FROM projects WHERE project_slug=?", (slug,)).fetchone()
    if existing:
        raise ValueError(f"Project already exists: {slug}")
    ts = now_iso()
    conn.execute(
        "INSERT INTO projects(project_slug,name,created_at,updated_at) VALUES (?,?,?,?)",
        (slug, display, ts, ts),
    )
    conn.commit()
    return slug


def rename_project(conn: sqlite3.Connection, project_slug: str, name: str) -> None:
    slug = normalize_project_slug(project_slug)
    display = str(name or "").strip()
    if not display:
        raise ValueError("Project name is required")
    cur = conn.execute("UPDATE projects SET name=?,updated_at=? WHERE project_slug=?", (display, now_iso(), slug))
    if cur.rowcount == 0:
        raise ValueError(f"Unknown project: {slug}")
    conn.commit()


def assign_paper_to_project(conn: sqlite3.Connection, paper_id: str, project_slug: str) -> None:
    slug = ensure_project(conn, project_slug)
    conn.execute(
        "INSERT OR IGNORE INTO paper_projects(paper_id,project_slug,assigned_at) VALUES (?,?,?)",
        (paper_id, slug, now_iso()),
    )


def unassign_paper_from_project(conn: sqlite3.Connection, paper_id: str, project_slug: str) -> None:
    slug = normalize_project_slug(project_slug)
    conn.execute("DELETE FROM paper_projects WHERE paper_id=? AND project_slug=?", (paper_id, slug))
    conn.commit()


def project_paper_ids(conn: sqlite3.Connection, project_slug: str, *, active_only: bool = True) -> list[str]:
    slug = normalize_project_slug(project_slug)
    if active_only:
        rows = conn.execute(
            """
            SELECT p.paper_id
            FROM papers p
            JOIN paper_projects pp ON pp.paper_id=p.paper_id
            WHERE pp.project_slug=? AND p.active=1
            ORDER BY p.paper_id
            """,
            (slug,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT paper_id FROM paper_projects WHERE project_slug=? ORDER BY paper_id",
            (slug,),
        ).fetchall()
    return [row[0] for row in rows]


def project_rows(conn: sqlite3.Connection, project_slug: str, *, active_only: bool = True) -> list[sqlite3.Row]:
    slug = normalize_project_slug(project_slug)
    where_active = "AND p.active=1" if active_only else ""
    return conn.execute(
        f"""
        SELECT p.*
        FROM papers p
        JOIN paper_projects pp ON pp.paper_id=p.paper_id
        WHERE pp.project_slug=? {where_active}
        ORDER BY p.paper_id
        """,
        (slug,),
    ).fetchall()


def list_projects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_project_schema(conn)
    rows = conn.execute(
        """
        SELECT pr.project_slug,pr.name,pr.created_at,pr.updated_at,
               COUNT(DISTINCT CASE WHEN p.active=1 THEN pp.paper_id END) AS active_papers,
               COUNT(DISTINCT pp.paper_id) AS total_papers
        FROM projects pr
        LEFT JOIN paper_projects pp ON pp.project_slug=pr.project_slug
        LEFT JOIN papers p ON p.paper_id=pp.paper_id
        GROUP BY pr.project_slug,pr.name,pr.created_at,pr.updated_at
        ORDER BY CASE WHEN pr.project_slug=? THEN 0 ELSE 1 END, lower(pr.name), pr.project_slug
        """,
        (DEFAULT_PROJECT_SLUG,),
    ).fetchall()
    return [dict(row) for row in rows]


def project_name(conn: sqlite3.Connection, project_slug: str) -> str:
    slug = normalize_project_slug(project_slug)
    row = conn.execute("SELECT name FROM projects WHERE project_slug=?", (slug,)).fetchone()
    return str(row[0]) if row else slug


def project_upload_dir(raw_dir: Path, project_slug: str) -> Path:
    slug = normalize_project_slug(project_slug)
    path = raw_dir / PROJECTS_PREFIX / slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_output_root(root: Path, project_slug: str) -> Path:
    return root / "outputs" / "projects" / normalize_project_slug(project_slug)


def project_network_dir(root: Path, project_slug: str) -> Path:
    return project_output_root(root, project_slug) / "network_gui"


def project_knowledge_dir(root: Path, project_slug: str) -> Path:
    return project_output_root(root, project_slug) / "knowledge_graph"
