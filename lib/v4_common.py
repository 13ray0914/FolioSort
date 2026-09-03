from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests

from lib.pipeline_common import (
    LLMOutputTruncatedError,
    normalize_ws,
    parse_json_content,
    validate_schema,
)

try:
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover
    fuzz = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_v4_schema(conn: sqlite3.Connection) -> None:
    """Create v4 tables without modifying existing v1-v3 records."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS api_cache_v4 (
            provider TEXT NOT NULL,
            cache_key TEXT NOT NULL,
            request_url TEXT NOT NULL,
            request_params_json TEXT,
            status_code INTEGER,
            response_json TEXT,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (provider, cache_key)
        );

        CREATE TABLE IF NOT EXISTS paper_metadata_v4 (
            paper_id TEXT PRIMARY KEY,
            title TEXT,
            doi TEXT,
            year INTEGER,
            journal TEXT,
            openalex_id TEXT,
            crossref_score REAL,
            openalex_score REAL,
            confidence REAL,
            match_status TEXT,
            canonical_source TEXT,
            metadata_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_paper_metadata_v4_doi
        ON paper_metadata_v4(doi);
        CREATE INDEX IF NOT EXISTS idx_paper_metadata_v4_openalex
        ON paper_metadata_v4(openalex_id);

        CREATE TABLE IF NOT EXISTS reference_matches_v4 (
            citing_paper_id TEXT NOT NULL,
            ref_id TEXT NOT NULL,
            target_paper_id TEXT,
            match_method TEXT,
            match_score REAL,
            resolved_title TEXT,
            resolved_doi TEXT,
            resolved_year INTEGER,
            resolved_journal TEXT,
            resolved_openalex_id TEXT,
            status TEXT NOT NULL,
            record_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (citing_paper_id, ref_id),
            FOREIGN KEY (citing_paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE,
            FOREIGN KEY (target_paper_id) REFERENCES papers(paper_id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_reference_matches_v4_target
        ON reference_matches_v4(target_paper_id);

        CREATE TABLE IF NOT EXISTS reference_doi_overrides_v4 (
            citing_paper_id TEXT NOT NULL,
            ref_id TEXT NOT NULL,
            doi TEXT NOT NULL,
            note TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (citing_paper_id, ref_id),
            FOREIGN KEY (citing_paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS knowledge_relations_v4 (
            source_claim_uid TEXT NOT NULL,
            target_claim_uid TEXT NOT NULL,
            relation TEXT NOT NULL,
            confidence REAL,
            rationale TEXT,
            condition_difference TEXT,
            record_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_claim_uid, target_claim_uid, relation)
        );
        """
    )
    conn.commit()


def env_or(config_value: str | None, env_name: str) -> str:
    value = os.environ.get(env_name)
    if value is not None and value.strip():
        return value.strip()
    return (config_value or "").strip()


def normalize_doi(value: str | None) -> str:
    if not value:
        return ""
    s = value.strip().lower()
    s = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", s)
    s = re.sub(r"^doi\s*:\s*", "", s)
    return s.strip().strip(".,;()[]{}<>")


def valid_doi(value: str | None) -> str:
    """Return a normalized DOI or raise for input that cannot be a DOI."""
    doi = normalize_doi(value)
    if not re.fullmatch(r"10\.\d{4,9}/\S+", doi) or any(ch.isspace() for ch in doi):
        raise ValueError("DOI must look like 10.xxxx/suffix")
    return doi


def normalize_openalex_id(value: str | None) -> str:
    if not value:
        return ""
    s = value.strip()
    if "/" in s:
        s = s.rstrip("/").split("/")[-1]
    return s.upper()


def normalize_title(value: str | None) -> str:
    s = normalize_ws(value or "").lower()
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&amp;", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return normalize_ws(s)


def text_similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    if fuzz is not None:
        token = fuzz.token_set_ratio(na, nb) / 100.0
        ratio = fuzz.ratio(na, nb) / 100.0
        return max(seq, 0.65 * token + 0.35 * ratio)
    return seq


def surname(value: str | None) -> str:
    if not value:
        return ""
    tokens = re.findall(r"[A-Za-z][A-Za-z'\-]+", value)
    return tokens[-1].lower() if tokens else ""


def extract_first_author_surname(authors: Iterable[Any] | None) -> str:
    authors = list(authors or [])
    if not authors:
        return ""
    first = authors[0]
    if isinstance(first, dict):
        return surname(first.get("surname") or first.get("family") or first.get("full_name") or first.get("display_name"))
    return surname(str(first))


def year_score(query_year: int | None, candidate_year: int | None) -> float:
    if not query_year or not candidate_year:
        return 0.45
    diff = abs(int(query_year) - int(candidate_year))
    if diff == 0:
        return 1.0
    if diff == 1:
        return 0.72
    if diff == 2:
        return 0.35
    return 0.0


def author_score(query_authors: Iterable[Any] | None, candidate_authors: Iterable[Any] | None) -> float:
    q = extract_first_author_surname(query_authors)
    c = extract_first_author_surname(candidate_authors)
    if not q or not c:
        return 0.45
    if q == c:
        return 1.0
    return text_similarity(q, c)


def metadata_match_score(query: dict[str, Any], candidate: dict[str, Any]) -> dict[str, float]:
    title = text_similarity(query.get("title"), candidate.get("title"))
    author = author_score(query.get("authors"), candidate.get("authors"))
    year = year_score(query.get("year"), candidate.get("year"))
    journal = text_similarity(query.get("journal"), candidate.get("journal"))
    if not query.get("journal") or not candidate.get("journal"):
        journal = 0.45
    overall = 0.72 * title + 0.12 * author + 0.10 * year + 0.06 * journal
    return {
        "overall": round(overall, 6),
        "title": round(title, 6),
        "author": round(author, 6),
        "year": round(year, 6),
        "journal": round(journal, 6),
    }


def crossref_year(item: dict[str, Any]) -> int | None:
    for key in ["published-print", "published-online", "published", "issued", "created"]:
        value = item.get(key) or {}
        parts = value.get("date-parts") or []
        if parts and parts[0] and isinstance(parts[0][0], int):
            return int(parts[0][0])
        date_time = value.get("date-time")
        if date_time:
            match = re.search(r"\b(18|19|20|21)\d{2}\b", str(date_time))
            if match:
                return int(match.group(0))
    return None


def crossref_candidate(item: dict[str, Any]) -> dict[str, Any]:
    authors = []
    for author in item.get("author") or []:
        full = normalize_ws(" ".join(x for x in [author.get("given"), author.get("family")] if x))
        authors.append({
            "full_name": full or None,
            "given": author.get("given"),
            "forename": author.get("given"),
            "family": author.get("family"),
            "surname": author.get("family"),
            "orcid": author.get("ORCID"),
        })
    return {
        "provider": "crossref",
        "title": (item.get("title") or [None])[0],
        "doi": normalize_doi(item.get("DOI")),
        "year": crossref_year(item),
        "journal": (item.get("container-title") or [None])[0],
        "journal_abbreviation": (item.get("short-container-title") or [None])[0],
        "volume": item.get("volume"),
        "issue": item.get("issue"),
        "pages": item.get("page"),
        "article_number": item.get("article-number"),
        "issn": item.get("ISSN") or [],
        "authors": authors,
        "type": item.get("type"),
        "publisher": item.get("publisher"),
        "url": item.get("URL"),
        "raw": item,
    }


def openalex_candidate(item: dict[str, Any]) -> dict[str, Any]:
    authors = []
    for authorship in item.get("authorships") or []:
        author = authorship.get("author") or {}
        full = normalize_ws(author.get("display_name") or authorship.get("raw_author_name") or "")
        name_parts = full.rsplit(" ", 1)
        family = name_parts[-1] if name_parts else ""
        given = name_parts[0] if len(name_parts) > 1 else ""
        authors.append({
            "full_name": full or None,
            "given": given or None,
            "forename": given or None,
            "family": family or None,
            "surname": family or None,
            "orcid": author.get("orcid"),
        })
    location = item.get("primary_location") or {}
    source = location.get("source") or {}
    biblio = item.get("biblio") or {}
    first_page = biblio.get("first_page")
    last_page = biblio.get("last_page")
    pages = first_page
    if first_page and last_page and str(last_page) != str(first_page):
        pages = f"{first_page}-{last_page}"
    return {
        "provider": "openalex",
        "title": item.get("title") or item.get("display_name"),
        "doi": normalize_doi(item.get("doi")),
        "year": item.get("publication_year"),
        "journal": source.get("display_name"),
        "journal_abbreviation": source.get("abbreviated_title"),
        "volume": biblio.get("volume"),
        "issue": biblio.get("issue"),
        "pages": pages,
        "article_number": biblio.get("article_number"),
        "issn": source.get("issn") or [],
        "authors": authors,
        "openalex_id": normalize_openalex_id(item.get("id")),
        "referenced_works": [normalize_openalex_id(x) for x in item.get("referenced_works") or []],
        "cited_by_count": item.get("cited_by_count"),
        "type": item.get("type"),
        "raw": item,
    }


class CachedAPIClient:
    def __init__(self, conn: sqlite3.Connection, provider: str, *, min_interval: float = 0.12):
        ensure_v4_schema(conn)
        self.conn = conn
        self.provider = provider
        self.min_interval = max(0.0, float(min_interval))
        self._last_request = 0.0

    def _cache_key(self, url: str, params: dict[str, Any] | None) -> str:
        payload = json.dumps({"url": url, "params": params or {}}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 40,
        attempts: int = 4,
        force: bool = False,
    ) -> tuple[int, Any]:
        key = self._cache_key(url, params)
        if not force:
            row = self.conn.execute(
                "SELECT status_code, response_json FROM api_cache_v4 WHERE provider=? AND cache_key=?",
                (self.provider, key),
            ).fetchone()
            if row and row["response_json"]:
                status = int(row["status_code"] or 200)
                # Keep successful and stable not-found responses. Older builds also
                # cached transient 5xx/429 and malformed 4xx responses, which made a
                # temporary provider failure look permanent on subsequent runs.
                if 200 <= status < 300 or status == 404:
                    return status, json.loads(row["response_json"])
                self.conn.execute(
                    "DELETE FROM api_cache_v4 WHERE provider=? AND cache_key=?",
                    (self.provider, key),
                )
                self.conn.commit()

        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        last_error: Exception | None = None
        response: requests.Response | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = requests.get(url, params=params, headers=headers, timeout=timeout, allow_redirects=True)
                self._last_request = time.monotonic()
                if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts:
                    retry_after = response.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * attempt
                    time.sleep(wait)
                    continue
                if response.status_code == 404:
                    data: Any = {"_not_found": True}
                    self._store_cache(key, url, params, response.status_code, data)
                    return response.status_code, data
                response.raise_for_status()
                data = response.json()
                self._store_cache(key, url, params, response.status_code, data)
                return response.status_code, data
            except Exception as error:
                last_error = error
                if attempt >= attempts:
                    break
                time.sleep(1.5 * attempt)
        # Do not cache failures. Successful/404 responses are stored above; errors
        # must remain retryable, while per-reference reuse prevents repeated calls
        # during ordinary incremental project updates.
        raise RuntimeError(f"{self.provider} API request failed: {last_error}")

    def _store_cache(self, key: str, url: str, params: dict[str, Any] | None, status: int, data: Any) -> None:
        self.conn.execute(
            """
            INSERT INTO api_cache_v4(provider, cache_key, request_url, request_params_json,
                                     status_code, response_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, cache_key) DO UPDATE SET
                request_url=excluded.request_url,
                request_params_json=excluded.request_params_json,
                status_code=excluded.status_code,
                response_json=excluded.response_json,
                fetched_at=excluded.fetched_at
            """,
            (
                self.provider,
                key,
                url,
                json.dumps(params or {}, ensure_ascii=False, sort_keys=True),
                int(status),
                json.dumps(data, ensure_ascii=False),
                now_iso(),
            ),
        )
        self.conn.commit()


class CrossrefClient:
    def __init__(self, conn: sqlite3.Connection, cfg: dict[str, Any]):
        self.cfg = cfg
        self.base_url = (cfg.get("base_url") or "https://api.crossref.org").rstrip("/")
        self.mailto = env_or(cfg.get("mailto"), "CROSSREF_MAILTO")
        user_agent = cfg.get("user_agent") or "LocalLiteratureReviewPipeline/4.0"
        if self.mailto and "mailto:" not in user_agent:
            user_agent += f" (mailto:{self.mailto})"
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self.api = CachedAPIClient(conn, "crossref", min_interval=float(cfg.get("request_interval_seconds", 0.12)))

    def by_doi(self, doi: str, *, force: bool = False) -> dict[str, Any] | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        status, data = self.api.get_json(
            f"{self.base_url}/works/{quote(normalized, safe='')}",
            params={"mailto": self.mailto} if self.mailto else None,
            headers=self.headers,
            timeout=int(self.cfg.get("timeout_seconds", 40)),
            force=force,
        )
        if status == 404 or data.get("_not_found"):
            return None
        item = (data.get("message") or {}) if isinstance(data, dict) else {}
        return crossref_candidate(item) if item else None

    def search(self, query: dict[str, Any], *, rows: int | None = None, force: bool = False) -> list[dict[str, Any]]:
        title = normalize_ws(query.get("title") or "")
        raw = normalize_ws(query.get("raw_reference") or "")
        author = extract_first_author_surname(query.get("authors"))
        year = query.get("year")
        bibliographic = " ".join(x for x in [title or raw, author, str(year or "")] if x)
        if not bibliographic:
            return []
        params: dict[str, Any] = {
            "query.bibliographic": bibliographic,
            "rows": int(rows or self.cfg.get("rows", 5)),
        }
        if self.mailto:
            params["mailto"] = self.mailto
        _, data = self.api.get_json(
            f"{self.base_url}/works",
            params=params,
            headers=self.headers,
            timeout=int(self.cfg.get("timeout_seconds", 40)),
            force=force,
        )
        items = (((data or {}).get("message") or {}).get("items") or [])
        return [crossref_candidate(item) for item in items]


class OpenAlexClient:
    def __init__(self, conn: sqlite3.Connection, cfg: dict[str, Any]):
        self.cfg = cfg
        self.base_url = (cfg.get("base_url") or "https://api.openalex.org").rstrip("/")
        self.api_key = env_or(cfg.get("api_key"), "OPENALEX_API_KEY")
        self.mailto = env_or(cfg.get("mailto"), "OPENALEX_MAILTO")
        self.headers = {
            "Accept": "application/json",
            "User-Agent": cfg.get("user_agent") or "LocalLiteratureReviewPipeline/4.0",
        }
        self.api = CachedAPIClient(conn, "openalex", min_interval=float(cfg.get("request_interval_seconds", 0.12)))

    def _auth_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.api_key:
            params["api_key"] = self.api_key
        if self.mailto:
            params["mailto"] = self.mailto
        return params

    def by_doi(self, doi: str, *, force: bool = False) -> dict[str, Any] | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        status, data = self.api.get_json(
            f"{self.base_url}/works/doi:{quote(normalized, safe='')}",
            params=self._auth_params() or None,
            headers=self.headers,
            timeout=int(self.cfg.get("timeout_seconds", 40)),
            force=force,
        )
        if status == 404 or data.get("_not_found"):
            return None
        return openalex_candidate(data) if isinstance(data, dict) and data.get("id") else None

    def by_id(self, openalex_id: str, *, force: bool = False) -> dict[str, Any] | None:
        normalized = normalize_openalex_id(openalex_id)
        if not normalized:
            return None
        status, data = self.api.get_json(
            f"{self.base_url}/works/{quote(normalized, safe='')}",
            params=self._auth_params() or None,
            headers=self.headers,
            timeout=int(self.cfg.get("timeout_seconds", 40)),
            force=force,
        )
        if status == 404 or data.get("_not_found"):
            return None
        return openalex_candidate(data) if isinstance(data, dict) and data.get("id") else None

    def search(self, query: dict[str, Any], *, per_page: int | None = None, force: bool = False) -> list[dict[str, Any]]:
        search_text = normalize_ws(query.get("title") or query.get("raw_reference") or "")
        if not search_text:
            return []
        params: dict[str, Any] = {
            "search": search_text,
            "per_page": int(per_page or self.cfg.get("per_page", 10)),
            "select": "id,doi,title,display_name,publication_year,primary_location,authorships,referenced_works,cited_by_count,type,relevance_score",
        }
        params.update(self._auth_params())
        _, data = self.api.get_json(
            f"{self.base_url}/works",
            params=params,
            headers=self.headers,
            timeout=int(self.cfg.get("timeout_seconds", 40)),
            force=force,
        )
        return [openalex_candidate(item) for item in (data.get("results") or [])]


def choose_best_candidate(
    query: dict[str, Any], candidates: Iterable[dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, float] | None, float]:
    scored: list[tuple[dict[str, Any], dict[str, float]]] = []
    for candidate in candidates:
        scored.append((candidate, metadata_match_score(query, candidate)))
    scored.sort(key=lambda item: item[1]["overall"], reverse=True)
    if not scored:
        return None, None, 0.0
    best, score = scored[0]
    margin = score["overall"] - (scored[1][1]["overall"] if len(scored) > 1 else 0.0)
    return best, score, round(margin, 6)


def compact_memory(memory: dict[str, Any], max_chars: int = 9000) -> str:
    lines: list[str] = []
    for key, label in [
        ("central_question", "CENTRAL QUESTION"),
        ("study_design", "STUDY DESIGN"),
        ("mechanistic_model", "MECHANISTIC MODEL"),
    ]:
        value = memory.get(key)
        if isinstance(value, str) and value:
            lines.append(f"{label}: {value}")
    for key, label in [
        ("systems_overview", "SYSTEMS"),
        ("method_map", "METHODS"),
        ("definitions", "DEFINITIONS"),
        ("major_findings", "MAJOR FINDINGS"),
        ("limitations", "LIMITATIONS"),
        ("global_constraints", "GLOBAL CONSTRAINTS"),
        ("cross_chunk_dependencies", "CROSS-CHUNK DEPENDENCIES"),
    ]:
        items = memory.get(key) or []
        if not items:
            continue
        lines.append(label + ":")
        for item in items:
            if isinstance(item, str):
                text = item
                evidence_ids: list[str] = []
            else:
                text = (
                    item.get("statement")
                    or item.get("summary")
                    or item.get("text")
                    or item.get("name")
                    or item.get("term")
                    or ""
                )
                evidence_ids = item.get("evidence_sids") or item.get("evidence_ids") or []
            if text:
                suffix = f" [MEMORY_EVIDENCE:{','.join(evidence_ids)}]" if evidence_ids else ""
                lines.append(f"- {text}{suffix}")
            if sum(len(line) + 1 for line in lines) >= max_chars:
                lines.append("- [memory truncated for context budget]")
                return "\n".join(lines)[:max_chars]
    return "\n".join(lines)[:max_chars]


def data_url_from_file(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = "image/png"
    if suffix in {".jpg", ".jpeg"}:
        mime = "image/jpeg"
    elif suffix == ".webp":
        mime = "image/webp"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


@dataclass
class VisionResult:
    data: dict[str, Any]
    model: str
    raw_response: dict[str, Any]


class OpenAICompatibleVisionClient:
    """OpenAI-compatible image client for llama.cpp or another local server."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.base_url = (cfg.get("base_url") or "http://127.0.0.1:8081/v1").rstrip("/")
        self.api_key = cfg.get("api_key") or "no-key"
        self.model_cfg = cfg.get("model") or "auto"
        self.timeout = int(cfg.get("timeout_seconds", 1200))
        self.max_tokens = int(cfg.get("max_tokens", 4096))
        self.temperature = float(cfg.get("temperature", 0.0))
        self.enable_thinking = bool(cfg.get("enable_thinking", False))
        self._model_id: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

    def model_id(self) -> str:
        if self._model_id:
            return self._model_id
        if self.model_cfg != "auto":
            self._model_id = self.model_cfg
            return self._model_id
        response = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=30)
        response.raise_for_status()
        data = response.json().get("data") or []
        if not data:
            raise RuntimeError("vision server returned no model")
        self._model_id = data[0]["id"]
        return self._model_id

    def healthcheck(self) -> str:
        return self.model_id()

    def chat_image_json(
        self,
        system_prompt: str,
        user_prompt: str,
        image_path: Path,
        schema: dict[str, Any],
    ) -> VisionResult:
        model = self.model_id()
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        system = (
            system_prompt.rstrip()
            + "\n\nReturn exactly one JSON value matching the supplied JSON Schema. "
            "Do not add keys. Do not infer details that are not visible.\nJSON_SCHEMA:\n"
            + schema_text
        )
        content = [
            {"type": "text", "text": user_prompt},
            {"type": "image_url", "image_url": {"url": data_url_from_file(image_path)}},
        ]
        base_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
        }
        variants = [
            {"response_format": {"type": "json_schema", "schema": schema}},
            {"response_format": {"type": "json_object"}, "json_schema": schema},
            {"response_format": {"type": "json_object", "schema": schema}},
        ]
        failures: list[str] = []
        for extra in variants:
            payload = dict(base_payload)
            payload.update(extra)
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=payload,
                timeout=self.timeout,
            )
            if response.status_code == 400:
                failures.append("HTTP 400: " + response.text[:300])
                continue
            response.raise_for_status()
            raw = response.json()
            choice = (raw.get("choices") or [{}])[0]
            finish_reason = choice.get("finish_reason")
            content_text = (choice.get("message") or {}).get("content", "")
            if isinstance(content_text, list):
                content_text = "".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                    for item in content_text
                )
            if finish_reason == "length":
                raise LLMOutputTruncatedError(
                    "vision output reached max_tokens", content_chars=len(content_text)
                )
            try:
                data = parse_json_content(content_text)
            except Exception as error:
                failures.append(f"parse error: {error}: {content_text[:300]}")
                continue
            errors = validate_schema(data, schema)
            if not errors:
                return VisionResult(data=data, model=model, raw_response=raw)
            failures.append("schema mismatch: " + " | ".join(errors[:5]))
        raise RuntimeError(
            "vision server did not return schema-valid JSON: " + " || ".join(failures[:4])
        )


def visual_context_text(visual: dict[str, Any], max_chars: int = 12000) -> str:
    lines: list[str] = []
    for item in visual.get("visual_evidence") or []:
        eid = item.get("evidence_id")
        if not eid:
            continue
        parts = [
            f"[{eid} page={item.get('page') or ''} kind={item.get('kind') or ''}]",
            str(item.get("caption") or "").strip(),
            str(item.get("text") or "").strip(),
        ]
        line = " ".join(x for x in parts if x)
        if line:
            lines.append(line)
        if sum(len(x) + 1 for x in lines) >= max_chars:
            lines.append("[visual context truncated]")
            break
    return "\n".join(lines)[:max_chars]


def make_visual_chunks(visual: dict[str, Any], max_chars: int = 9000) -> list[dict[str, Any]]:
    """Turn analyzed visual assets into extraction chunks with stable vis:* IDs."""
    units: list[dict[str, Any]] = []
    for item in visual.get("assets") or []:
        evidence_id = item.get("evidence_id")
        if not evidence_id:
            continue
        analysis = item.get("analysis") or {}
        parts = [
            f"[{evidence_id} page={item.get('page') or ''}]",
            f"TYPE: {item.get('kind') or analysis.get('asset_type') or 'visual'}",
            f"CAPTION: {item.get('caption') or ''}",
            f"STRUCTURED_TEXT: {item.get('structured_text') or ''}",
            f"TABLE_ROWS: {json.dumps(item.get('table_rows') or [], ensure_ascii=False)}",
            f"VISION_ANALYSIS: {json.dumps(analysis, ensure_ascii=False)}" if analysis else "",
            f"NEARBY_TEXT: {' '.join(item.get('nearby_text') or [])}",
        ]
        text = "\n".join(x for x in parts if x and not x.endswith(": ")).strip()
        if not text:
            continue
        units.append(
            {
                "section_id": "visual",
                "heading": f"Visual asset {item.get('asset_id') or evidence_id}",
                "role": "figure_table",
                "paragraph_id": evidence_id,
                "text": text,
            }
        )
    chunks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for unit in units:
        unit_len = len(unit["text"]) + 128
        if current and current_chars + unit_len > max_chars:
            chunks.append(_visual_finalize(current, len(chunks) + 1))
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += unit_len
    if current:
        chunks.append(_visual_finalize(current, len(chunks) + 1))
    return chunks


def _visual_finalize(units: list[dict[str, Any]], index: int) -> dict[str, Any]:
    return {
        "chunk_id": f"visual_{index:04d}",
        "sections": [
            {"heading": u["heading"], "role": u["role"], "paragraph_id": u["paragraph_id"]}
            for u in units
        ],
        "text": "\n\n".join(
            f"--- {u['heading']} | role={u['role']} | evidence={u['paragraph_id']} ---\n{u['text']}"
            for u in units
        ),
        "_units": [dict(u) for u in units],
    }


def visual_evidence_map(visual: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in visual.get("assets") or []:
        eid = item.get("evidence_id")
        if not eid:
            continue
        analysis = item.get("analysis") or {}
        text_parts = [
            item.get("caption"),
            item.get("structured_text"),
            item.get("summary_text"),
            json.dumps(item.get("table_rows") or [], ensure_ascii=False),
            json.dumps(analysis, ensure_ascii=False),
        ]
        out[eid] = {
            "text": normalize_ws(" ".join(str(x) for x in text_parts if x)),
            "page": item.get("page"),
            "kind": item.get("kind"),
        }
    return out
