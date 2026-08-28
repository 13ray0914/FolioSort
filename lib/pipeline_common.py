from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from jsonschema import Draft202012Validator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_json_hash(obj: Any) -> str:
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(text)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def normalize_ws(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def normalize_key(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def load_config(config_path: str | Path) -> tuple[dict[str, Any], Path]:
    path = Path(config_path).expanduser().resolve()
    config = read_json(path)
    root = path.parent
    return config, root


def rpath(root: Path, path_value: str) -> Path:
    p = Path(path_value).expanduser()
    return p.resolve() if p.is_absolute() else (root / p).resolve()


def get_paths(config: dict[str, Any], root: Path) -> dict[str, Path]:
    paths = {k: rpath(root, v) for k, v in config["paths"].items()}
    for p in paths.values():
        if p.suffix:
            p.parent.mkdir(parents=True, exist_ok=True)
        else:
            p.mkdir(parents=True, exist_ok=True)
    return paths


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
            paper_id TEXT PRIMARY KEY,
            source_relpath TEXT UNIQUE NOT NULL,
            original_filename TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            title TEXT,
            doi TEXT,
            year INTEGER,
            journal TEXT,
            article_type TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_papers_sha ON papers(source_sha256);
        CREATE INDEX IF NOT EXISTS idx_papers_active ON papers(active);

        CREATE TABLE IF NOT EXISTS stages (
            paper_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            input_hash TEXT,
            output_path TEXT,
            updated_at TEXT NOT NULL,
            error TEXT,
            meta_json TEXT,
            PRIMARY KEY (paper_id, stage),
            FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS llm_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            chunk_id TEXT,
            prompt_version TEXT,
            schema_version TEXT,
            model TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            input_hash TEXT,
            output_path TEXT,
            error TEXT,
            FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS human_reviews (
            paper_id TEXT PRIMARY KEY,
            decision TEXT NOT NULL,
            notes TEXT,
            reviewed_at TEXT NOT NULL,
            FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()


def next_paper_id(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT paper_id FROM papers ORDER BY paper_id").fetchall()
    max_n = 0
    for row in rows:
        m = re.fullmatch(r"P(\d+)", row["paper_id"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"P{max_n + 1:04d}"


def reset_stages(conn: sqlite3.Connection, paper_id: str) -> None:
    conn.execute("DELETE FROM stages WHERE paper_id = ?", (paper_id,))
    conn.execute("DELETE FROM human_reviews WHERE paper_id = ?", (paper_id,))
    conn.commit()


def get_stage(conn: sqlite3.Connection, paper_id: str, stage: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM stages WHERE paper_id = ? AND stage = ?", (paper_id, stage)
    ).fetchone()


def stage_is_current(
    conn: sqlite3.Connection,
    paper_id: str,
    stage: str,
    input_hash: str,
    output_path: Path | None = None,
) -> bool:
    row = get_stage(conn, paper_id, stage)
    if not row or row["status"] != "success" or row["input_hash"] != input_hash:
        return False
    if output_path is not None and not output_path.exists():
        return False
    return True


def set_stage(
    conn: sqlite3.Connection,
    paper_id: str,
    stage: str,
    status: str,
    input_hash: str | None = None,
    output_path: Path | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO stages(paper_id, stage, status, input_hash, output_path, updated_at, error, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paper_id, stage) DO UPDATE SET
            status=excluded.status,
            input_hash=excluded.input_hash,
            output_path=excluded.output_path,
            updated_at=excluded.updated_at,
            error=excluded.error,
            meta_json=excluded.meta_json
        """,
        (
            paper_id,
            stage,
            status,
            input_hash,
            str(output_path) if output_path else None,
            now_iso(),
            error,
            json.dumps(meta, ensure_ascii=False) if meta else None,
        ),
    )
    conn.commit()


def select_papers(
    conn: sqlite3.Connection,
    ids: list[str] | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM papers WHERE active = 1"
    params: list[Any] = []
    if ids:
        placeholders = ",".join("?" for _ in ids)
        sql += f" AND paper_id IN ({placeholders})"
        params.extend(ids)
    sql += " ORDER BY paper_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def parse_ids(value: str | None) -> list[str] | None:
    if not value:
        return None
    ids = [x.strip().upper() for x in value.split(",") if x.strip()]
    return ids or None


def export_manifest(conn: sqlite3.Connection, out_path: Path) -> None:
    rows = conn.execute(
        """
        SELECT p.paper_id, p.original_filename, p.source_relpath, p.source_sha256,
               p.file_size, p.active, p.title, p.doi, p.year, p.journal, p.article_type,
               COALESCE(hr.decision, '') AS human_review
        FROM papers p
        LEFT JOIN human_reviews hr ON p.paper_id = hr.paper_id
        ORDER BY p.paper_id
        """
    ).fetchall()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "paper_id",
        "original_filename",
        "source_relpath",
        "source_sha256",
        "file_size",
        "active",
        "title",
        "doi",
        "year",
        "journal",
        "article_type",
        "human_review",
    ]
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in fields})


def load_schema(path: Path) -> dict[str, Any]:
    schema = read_json(path)
    Draft202012Validator.check_schema(schema)
    return schema


def validate_schema(instance: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    messages = []
    for e in errors:
        loc = "/".join(str(x) for x in e.absolute_path)
        messages.append(f"{loc or '<root>'}: {e.message}")
    return messages


def flatten_sentences(paper_json: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in paper_json.get("abstract", []):
        for s in p.get("sentences", []):
            out[s["sid"]] = s
    for section in paper_json.get("sections", []):
        for p in section.get("paragraphs", []):
            for s in p.get("sentences", []):
                out[s["sid"]] = s
    for s in paper_json.get("auxiliary_text", []):
        out[s["sid"]] = s
    return out


def section_role(heading: str) -> str:
    h = normalize_key(heading)
    if any(k in h for k in ["introduction", "background", "literature review"]):
        return "background"
    if any(k in h for k in ["method", "experimental", "materials and methods", "computational detail"]):
        return "methods"
    if any(k in h for k in ["result", "discussion", "finding"]):
        return "results_discussion"
    if any(k in h for k in ["conclusion", "summary", "perspective", "outlook"]):
        return "conclusion"
    return "other"


def sentence_line(sentence: dict[str, Any]) -> str:
    cites = sentence.get("citation_ref_ids") or []
    cite_text = f" [CITES:{','.join(cites)}]" if cites else ""
    page = sentence.get("page")
    page_text = f" page={page}" if page is not None else ""
    return f"[{sentence['sid']}{page_text}] {sentence.get('text', '')}{cite_text}"


def make_text_chunks(
    paper_json: dict[str, Any],
    max_chars: int,
    include_abstract: bool = True,
) -> list[dict[str, Any]]:
    """Chunk on paragraph boundaries. Each chunk contains exact sentence IDs."""
    units: list[dict[str, Any]] = []
    if include_abstract:
        for p in paper_json.get("abstract", []):
            lines = [sentence_line(s) for s in p.get("sentences", [])]
            if lines:
                units.append(
                    {
                        "section_id": "abstract",
                        "heading": "Abstract",
                        "role": "abstract",
                        "paragraph_id": p.get("paragraph_id"),
                        "text": "\n".join(lines),
                    }
                )
    for sec in paper_json.get("sections", []):
        role = section_role(sec.get("heading", ""))
        for p in sec.get("paragraphs", []):
            lines = [sentence_line(s) for s in p.get("sentences", [])]
            if lines:
                units.append(
                    {
                        "section_id": sec.get("section_id"),
                        "heading": sec.get("heading") or "Untitled section",
                        "role": role,
                        "paragraph_id": p.get("paragraph_id"),
                        "text": "\n".join(lines),
                    }
                )
    for aux in paper_json.get("auxiliary_text", []):
        units.append(
            {
                "section_id": aux.get("kind", "auxiliary"),
                "heading": aux.get("heading") or aux.get("kind", "Figure/Table"),
                "role": "figure_table",
                "paragraph_id": aux.get("sid"),
                "text": sentence_line(aux),
            }
        )

    chunks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for unit in units:
        unit_len = len(unit["text"]) + len(unit["heading"]) + 64
        role_changed = bool(current and current[-1]["role"] != unit["role"])
        if current and (role_changed or current_chars + unit_len > max_chars):
            chunks.append(_finalize_chunk(current, len(chunks) + 1))
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += unit_len
    if current:
        chunks.append(_finalize_chunk(current, len(chunks) + 1))
    return chunks


def _finalize_chunk(units: list[dict[str, Any]], index: int) -> dict[str, Any]:
    sections = []
    text_parts = []
    for u in units:
        sections.append({"heading": u["heading"], "role": u["role"], "paragraph_id": u["paragraph_id"]})
        text_parts.append(
            f"\n--- SECTION: {u['heading']} | role={u['role']} | paragraph={u['paragraph_id']} ---\n{u['text']}"
        )
    return {
        "chunk_id": f"chunk_{index:04d}",
        "sections": sections,
        "text": "".join(text_parts).strip(),
        # Internal structure retained so an oversized LLM response can be
        # retried on deterministic child chunks without losing section roles.
        "_units": [dict(u) for u in units],
    }


def _finalize_chunk_with_id(units: list[dict[str, Any]], chunk_id: str) -> dict[str, Any]:
    out = _finalize_chunk(units, 1)
    out["chunk_id"] = chunk_id
    return out


def _split_unit_near_half(unit: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Split one paragraph/auxiliary unit on sentence-line boundaries."""
    text = unit.get("text", "")
    lines = text.splitlines()
    if len(lines) >= 2:
        target = len(text) / 2
        running = 0
        best_i = 1
        best_dist = float("inf")
        for i in range(1, len(lines)):
            running += len(lines[i - 1]) + 1
            dist = abs(running - target)
            if dist < best_dist:
                best_i = i
                best_dist = dist
        left_text = "\n".join(lines[:best_i]).strip()
        right_text = "\n".join(lines[best_i:]).strip()
    else:
        # Rare fallback for a single giant line. Prefer a whitespace boundary.
        if len(text) < 2:
            return None
        mid = len(text) // 2
        candidates = [text.rfind(" ", 0, mid), text.find(" ", mid)]
        candidates = [x for x in candidates if x > 0]
        cut = min(candidates, key=lambda x: abs(x - mid)) if candidates else mid
        left_text = text[:cut].strip()
        right_text = text[cut:].strip()
    if not left_text or not right_text:
        return None
    left = dict(unit); left["text"] = left_text
    right = dict(unit); right["text"] = right_text
    return left, right


def split_chunk_adaptive(chunk: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Deterministically split a chunk into two roughly equal children.

    It first splits on paragraph/figure/table units. If a chunk contains only
    one unit, it splits that unit on stable sentence-line boundaries. Child
    IDs are deterministic (parent + 'a'/'b'), so successful child results can
    be cached and resumed after interruption.
    """
    units = [dict(u) for u in chunk.get("_units", [])]
    parent_id = str(chunk.get("chunk_id", "chunk"))
    if len(units) >= 2:
        lengths = [len(u.get("text", "")) + len(u.get("heading", "")) + 64 for u in units]
        total = sum(lengths)
        running = 0
        best_i = 1
        best_dist = float("inf")
        for i in range(1, len(units)):
            running += lengths[i - 1]
            dist = abs(running - total / 2)
            if dist < best_dist:
                best_i = i
                best_dist = dist
        left_units, right_units = units[:best_i], units[best_i:]
    elif len(units) == 1:
        split = _split_unit_near_half(units[0])
        if split is None:
            return None
        left_units, right_units = [split[0]], [split[1]]
    else:
        # Backward-compatible fallback for chunks created before _units existed.
        text = chunk.get("text", "")
        lines = text.splitlines()
        if len(lines) < 2:
            return None
        target = len(text) / 2
        running = 0
        best_i = 1
        best_dist = float("inf")
        for i in range(1, len(lines)):
            running += len(lines[i - 1]) + 1
            dist = abs(running - target)
            if dist < best_dist:
                best_i = i
                best_dist = dist
        sections = chunk.get("sections", [])
        roles = {s.get("role") for s in sections if s.get("role")}
        role = next(iter(roles)) if len(roles) == 1 else "unknown"
        heading = sections[0].get("heading", "Adaptive continuation") if sections else "Adaptive continuation"
        base = {"section_id": "adaptive", "heading": heading, "role": role, "paragraph_id": parent_id}
        left_units = [{**base, "text": "\n".join(lines[:best_i]).strip()}]
        right_units = [{**base, "text": "\n".join(lines[best_i:]).strip()}]
    if not left_units or not right_units:
        return None
    return (
        _finalize_chunk_with_id(left_units, parent_id + "a"),
        _finalize_chunk_with_id(right_units, parent_id + "b"),
    )


def dedupe_dicts(items: Iterable[dict[str, Any]], key_func) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = key_func(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def parse_json_content(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


@dataclass
class LLMResult:
    data: dict[str, Any]
    model: str
    raw_response: dict[str, Any]


class LLMOutputTruncatedError(RuntimeError):
    """Raised when llama.cpp explicitly stops because max_tokens was reached."""

    def __init__(self, message: str, *, variant: str = "", content_chars: int = 0):
        super().__init__(message)
        self.variant = variant
        self.content_chars = content_chars


class LlamaCppClient:
    def __init__(self, cfg: dict[str, Any]):
        self.base_url = cfg["base_url"].rstrip("/")
        self.api_key = cfg.get("api_key", "no-key")
        self.model_cfg = cfg.get("model", "auto")
        self.timeout = int(cfg.get("timeout_seconds", 1200))
        self.temperature = float(cfg.get("temperature", 0.0))
        self.max_tokens = int(cfg.get("max_tokens", 8192))
        self.enable_thinking = bool(cfg.get("enable_thinking", False))
        self.response_format_mode = cfg.get("response_format", "json_schema")
        self.attempts = int(cfg.get("attempts", 3))
        self.retry_wait_seconds = float(cfg.get("retry_wait_seconds", 5))
        self._model_id: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def model_id(self) -> str:
        if self._model_id:
            return self._model_id
        if self.model_cfg and self.model_cfg != "auto":
            self._model_id = self.model_cfg
            return self._model_id
        r = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=30)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            raise RuntimeError("llama.cpp /v1/models returned no loaded/available model")
        self._model_id = data[0]["id"]
        return self._model_id

    def healthcheck(self) -> str:
        return self.model_id()

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        """Return schema-valid JSON from llama.cpp.

        llama.cpp has supported more than one structured-output request shape
        across server versions/builds.  We therefore try several *constrained*
        variants and accept a response only after local JSON-Schema validation.
        We never silently fall back to unconstrained JSON.
        """
        model = self.model_id()

        # Supplying the schema in the instruction as well as at the sampler level
        # makes the semantic intent clear to the model.  The sampler-level schema
        # remains the hard constraint.
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        schema_prompt = (
            system_prompt.rstrip()
            + "\n\nYou MUST return exactly one JSON value matching this schema. "
              "Do not add keys that are absent from the schema.\nJSON_SCHEMA:\n"
            + schema_text
        )

        base_payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": schema_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
        }

        # Keep all variants schema-constrained.  Some llama.cpp builds understand
        # one form but not another.  A 2xx response is not considered sufficient:
        # the returned JSON must also validate locally.
        if self.response_format_mode == "json_object":
            variants = [
                ("response_format_json_object_schema", {
                    "response_format": {"type": "json_object", "schema": schema}
                }),
                ("top_level_json_schema", {
                    "response_format": {"type": "json_object"},
                    "json_schema": schema,
                }),
                ("response_format_json_schema", {
                    "response_format": {"type": "json_schema", "schema": schema}
                }),
            ]
        else:
            variants = [
                ("response_format_json_schema", {
                    "response_format": {"type": "json_schema", "schema": schema}
                }),
                ("top_level_json_schema", {
                    "response_format": {"type": "json_object"},
                    "json_schema": schema,
                }),
                ("response_format_json_object_schema", {
                    "response_format": {"type": "json_object", "schema": schema}
                }),
            ]

        url = f"{self.base_url}/chat/completions"
        failures: list[str] = []

        for variant_name, structured_fields in variants:
            payload = dict(base_payload)
            payload.update(structured_fields)
            response = None
            last_exc: Exception | None = None

            for attempt in range(1, self.attempts + 1):
                try:
                    response = requests.post(
                        url, headers=self.headers, json=payload, timeout=self.timeout
                    )
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < self.attempts:
                        time.sleep(self.retry_wait_seconds * attempt)
                        continue
                    # A 400 often means this particular structured-output syntax is
                    # unsupported. Try the next constrained syntax instead.
                    if response.status_code == 400:
                        body = response.text.replace("\n", " ")[:500]
                        failures.append(f"{variant_name}: HTTP 400: {body}")
                        response = None
                        break
                    response.raise_for_status()
                    break
                except requests.RequestException as e:
                    last_exc = e
                    if attempt >= self.attempts:
                        failures.append(f"{variant_name}: request error: {e}")
                        response = None
                        break
                    time.sleep(self.retry_wait_seconds * attempt)

            if response is None:
                if last_exc and not failures:
                    failures.append(f"{variant_name}: {last_exc}")
                continue

            try:
                raw = response.json()
                choice = raw["choices"][0]
                content = choice["message"].get("content", "")
                finish_reason = choice.get("finish_reason")
                if finish_reason == "length":
                    # Request syntax cannot fix a token-limit truncation, so do
                    # not waste time trying the other schema variants. The
                    # caller can split only this chunk and retry recursively.
                    raise LLMOutputTruncatedError(
                        f"llama.cpp output truncated at max_tokens using {variant_name} ",
                        variant=variant_name,
                        content_chars=len(content),
                    )
                data = parse_json_content(content)
            except LLMOutputTruncatedError:
                raise
            except Exception as e:
                excerpt = response.text.replace("\n", " ")[:500]
                failures.append(f"{variant_name}: response parse error: {e}; {excerpt}")
                continue

            schema_errors = validate_schema(data, schema)
            if not schema_errors:
                return LLMResult(data=data, model=model, raw_response=raw)

            excerpt = content.replace("\n", " ")[:700]
            failures.append(
                f"{variant_name}: schema mismatch: "
                + " | ".join(schema_errors[:6])
                + f"; output={excerpt}"
            )

        raise RuntimeError(
            "llama.cpp did not produce schema-valid JSON with any supported "
            "structured-output request form. " + " || ".join(failures[:6])
        )


def log_llm_run_start(
    conn: sqlite3.Connection,
    paper_id: str,
    stage: str,
    chunk_id: str,
    prompt_version: str,
    schema_version: str,
    model: str,
    input_hash: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO llm_runs(paper_id, stage, chunk_id, prompt_version, schema_version,
                             model, started_at, status, input_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
        """,
        (paper_id, stage, chunk_id, prompt_version, schema_version, model, now_iso(), input_hash),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_llm_run_finish(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    output_path: Path | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE llm_runs
        SET finished_at = ?, status = ?, output_path = ?, error = ?
        WHERE id = ?
        """,
        (now_iso(), status, str(output_path) if output_path else None, error, run_id),
    )
    conn.commit()


def retry_request(method: str, url: str, *, attempts: int = 4, wait_seconds: float = 3.0, **kwargs):
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.request(method, url, **kwargs)
            if r.status_code in {429, 503} and attempt < attempts:
                time.sleep(wait_seconds * attempt)
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            if attempt >= attempts:
                raise
            time.sleep(wait_seconds * attempt)
    if last_exc:
        raise last_exc
    raise RuntimeError("request failed")
