from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from lib.pipeline_common import read_json, sha256_file, write_json


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def surface_key(value: str | None) -> str:
    """Conservative surface normalization for controlled-vocabulary lookup."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"[_/\\]+", " ", text)
    text = re.sub(r"[^0-9a-zα-ωΑ-Ω+\- ]+", " ", text)
    text = text.replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def canonical_display(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_publication_year(value: Any) -> int | None:
    """Normalize a human-edited publication year while allowing an explicit blank."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"\d{4}", text):
        raise ValueError("publication year must be a 4-digit year or blank")
    year = int(text)
    if year < 1000 or year > 2100:
        raise ValueError("publication year must be between 1000 and 2100")
    return year


def apply_metadata(
    paper_id: str,
    raw: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply append-only human metadata edits without mutating enriched/raw metadata."""
    curated = copy.deepcopy(raw or {})
    raw_canonical = copy.deepcopy(curated.get("canonical") or {})
    canonical = copy.deepcopy(raw_canonical)
    metadata_uid = f"metadata:{paper_id}"
    for event in events:
        if event.get("paper_id") != paper_id or event.get("entity_type") != "metadata":
            continue
        if event.get("entity_uid") not in (None, "", metadata_uid):
            continue
        if event.get("event_type") != "metadata_edit":
            continue
        patch = event.get("new") or {}
        for field in ["title", "year", "journal", "doi", "authors"]:
            if field in patch:
                canonical[field] = copy.deepcopy(patch[field])
    curated["canonical_original"] = raw_canonical
    curated["canonical"] = canonical
    curated["curation"] = {
        "paper_id": paper_id,
        "entity_uid": metadata_uid,
        "materialized_at": now_iso(),
        "event_count": sum(
            1 for x in events
            if x.get("paper_id") == paper_id and x.get("entity_type") == "metadata"
        ),
        "raw_preserved": True,
    }
    return curated


def entity_uid(paper_id: str, entity_type: str, item: dict[str, Any]) -> str:
    evidence = sorted(str(x) for x in item.get("evidence_sids", []) if x)
    if entity_type == "property":
        value = item.get("property_raw") or item.get("property_normalized")
    elif entity_type == "method":
        value = item.get("method_raw") or item.get("method_normalized")
    elif entity_type == "keyword":
        value = item.get("keyword_raw") or item.get("keyword_normalized") or item.get("value")
    elif entity_type == "claim":
        value = item.get("statement")
    elif entity_type == "measurement":
        value = f"{item.get('property_raw') or item.get('property_normalized')}|{item.get('value_raw')}"
    else:
        value = json.dumps(item, ensure_ascii=False, sort_keys=True)
    payload = "|".join([paper_id, entity_type, surface_key(str(value or "")), ",".join(evidence)])
    return f"{entity_type}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def ensure_curation_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS curation_events_v4 (
            event_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            actor TEXT,
            paper_id TEXT,
            event_type TEXT NOT NULL,
            entity_type TEXT,
            entity_uid TEXT,
            old_json TEXT,
            new_json TEXT,
            reason TEXT,
            event_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_curation_events_v4_paper
        ON curation_events_v4(paper_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_curation_events_v4_entity
        ON curation_events_v4(entity_uid, created_at);
        CREATE INDEX IF NOT EXISTS idx_curation_events_v4_type
        ON curation_events_v4(event_type, entity_type, created_at);
        """
    )
    conn.commit()


def append_event(
    conn: sqlite3.Connection,
    events_path: Path,
    *,
    event_type: str,
    actor: str | None = None,
    paper_id: str | None = None,
    entity_type: str | None = None,
    entity_uid_value: str | None = None,
    old: Any = None,
    new: Any = None,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_curation_schema(conn)
    event = {
        "event_id": str(uuid.uuid4()),
        "created_at": now_iso(),
        "actor": actor or os.environ.get("USER") or "local_user",
        "paper_id": paper_id,
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_uid": entity_uid_value,
        "old": old,
        "new": new,
        "reason": reason or "",
    }
    if extra:
        event.update(extra)
    payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(payload + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    conn.execute(
        """
        INSERT INTO curation_events_v4(
            event_id,created_at,actor,paper_id,event_type,entity_type,entity_uid,
            old_json,new_json,reason,event_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event["event_id"], event["created_at"], event["actor"], paper_id,
            event_type, entity_type, entity_uid_value,
            json.dumps(old, ensure_ascii=False) if old is not None else None,
            json.dumps(new, ensure_ascii=False) if new is not None else None,
            event["reason"], payload,
        ),
    )
    conn.commit()
    return event


def read_event_log(events_path: Path) -> list[dict[str, Any]]:
    if not events_path.exists():
        return []
    out: list[dict[str, Any]] = []
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    out.sort(key=lambda x: (str(x.get("created_at") or ""), str(x.get("event_id") or "")))
    return out


class Ontology:
    def __init__(self, ontology_path: Path, events: Iterable[dict[str, Any]] = ()):
        self.ontology_path = ontology_path
        self.alias_maps: dict[str, dict[str, str]] = {"property": {}, "method": {}, "keyword": {}}
        self.canonical_terms: dict[str, set[str]] = {"property": set(), "method": set(), "keyword": set()}
        if ontology_path.exists():
            payload = read_json(ontology_path)
            for term_type, rows in (payload.get("terms") or {}).items():
                self.alias_maps.setdefault(term_type, {})
                self.canonical_terms.setdefault(term_type, set())
                for row in rows or []:
                    canonical = canonical_display(row.get("canonical"))
                    if not canonical:
                        continue
                    self.canonical_terms[term_type].add(canonical)
                    aliases = [canonical] + list(row.get("aliases") or [])
                    for alias in aliases:
                        key = surface_key(alias)
                        if key:
                            self.alias_maps[term_type][key] = canonical
        # Human global alias decisions override shipped ontology, latest event wins.
        for event in events:
            if event.get("event_type") != "term_alias":
                continue
            term_type = str(event.get("entity_type") or "property")
            new = event.get("new") or {}
            alias = canonical_display(new.get("alias"))
            canonical = canonical_display(new.get("canonical"))
            if alias and canonical:
                self.alias_maps.setdefault(term_type, {})[surface_key(alias)] = canonical
                self.alias_maps.setdefault(term_type, {})[surface_key(canonical)] = canonical
                self.canonical_terms.setdefault(term_type, set()).add(canonical)

    def resolve(self, term_type: str, value: str | None) -> tuple[str, str]:
        display = canonical_display(value)
        key = surface_key(display)
        if not key:
            return display, "empty"
        hit = self.alias_maps.get(term_type, {}).get(key)
        if hit:
            return hit, "ontology_or_user_alias"
        # Keep unseen terms rather than fuzzy-merging them automatically.
        return display, "passthrough"


def _latest_entity_events(events: Iterable[dict[str, Any]], paper_id: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        if event.get("paper_id") != paper_id:
            continue
        entity_type = str(event.get("entity_type") or "")
        uid = str(event.get("entity_uid") or "")
        if entity_type and uid:
            grouped.setdefault((entity_type, uid), []).append(event)
    return grouped


def _term_items_with_uids(paper_id: str, entity_type: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in items:
        clone = copy.deepcopy(item)
        clone["curation_uid"] = entity_uid(paper_id, entity_type, clone)
        out.append(clone)
    return out


def apply_inventory(
    paper_id: str,
    raw: dict[str, Any],
    ontology: Ontology,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    curated = copy.deepcopy(raw)
    grouped = _latest_entity_events(events, paper_id)

    term_specs = [
        ("property", "studied_properties", "property_normalized", "property_raw"),
        ("method", "methods", "method_normalized", "method_raw"),
        ("keyword", "keywords", "keyword_normalized", "keyword_raw"),
    ]
    # Keywords may be absent from the raw schema or represented as strings.
    raw_keywords = curated.get("keywords") or curated.get("key_terms") or curated.get("topic_keywords") or []
    if isinstance(raw_keywords, str):
        raw_keywords = [x.strip() for x in re.split(r"[,;]", raw_keywords) if x.strip()]
    keyword_items: list[dict[str, Any]] = []
    for value in raw_keywords if isinstance(raw_keywords, list) else []:
        if isinstance(value, dict):
            item = copy.deepcopy(value)
            raw_value = item.get("keyword_raw") or item.get("keyword_normalized") or item.get("value") or item.get("name")
            item.setdefault("keyword_raw", raw_value)
            item.setdefault("keyword_normalized", raw_value)
            keyword_items.append(item)
        elif value not in (None, ""):
            keyword_items.append({"keyword_raw": str(value), "keyword_normalized": str(value), "evidence_sids": []})
    curated["keywords"] = keyword_items

    for entity_type, list_key, norm_key, raw_key in term_specs:
        new_items = []
        for item in _term_items_with_uids(paper_id, entity_type, curated.get(list_key, [])):
            uid = item["curation_uid"]
            original_norm = item.get(norm_key)
            source_value = original_norm or item.get(raw_key)
            canonical, source = ontology.resolve(entity_type, source_value)
            item[norm_key + "_original"] = original_norm
            item[norm_key] = canonical
            item["canonical_source"] = source
            hidden = False
            for event in grouped.get((entity_type, uid), []):
                if event.get("event_type") == "term_override":
                    canonical = canonical_display((event.get("new") or {}).get("canonical"))
                    if canonical:
                        item[norm_key] = canonical
                        item["canonical_source"] = "paper_override"
                elif event.get("event_type") == "term_delete":
                    hidden = True
            if not hidden:
                if entity_type == "method" and item.get("target_property"):
                    target, target_source = ontology.resolve("property", item.get("target_property"))
                    item["target_property_original"] = item.get("target_property")
                    item["target_property"] = target
                    item["target_property_canonical_source"] = target_source
                new_items.append(item)
        # User-added terms are appended in event order.
        for event in events:
            if event.get("paper_id") != paper_id or event.get("entity_type") != entity_type or event.get("event_type") != "term_add":
                continue
            new = event.get("new") or {}
            value = canonical_display(new.get("value"))
            if not value:
                continue
            canonical, source = ontology.resolve(entity_type, new.get("canonical") or value)
            user_uid = f"{entity_type}:user:{event['event_id']}"
            if entity_type == "property":
                item = {
                    "property_raw": value,
                    "property_normalized_original": None,
                    "property_normalized": canonical,
                    "evidence_sids": list(new.get("evidence_sids") or []),
                }
                norm_field = "property_normalized"
            elif entity_type == "method":
                item = {
                    "method_raw": value,
                    "method_normalized_original": None,
                    "method_normalized": canonical,
                    "target_property": new.get("target_property"),
                    "evidence_sids": list(new.get("evidence_sids") or []),
                }
                norm_field = "method_normalized"
            else:
                item = {
                    "keyword_raw": value,
                    "keyword_normalized_original": None,
                    "keyword_normalized": canonical,
                    "evidence_sids": list(new.get("evidence_sids") or []),
                }
                norm_field = "keyword_normalized"
            item["curation_uid"] = user_uid
            item["canonical_source"] = "user_added"
            item["curation_origin"] = "user_added"
            hidden = False
            for later in grouped.get((entity_type, user_uid), []):
                if later.get("event_type") == "term_delete":
                    hidden = True
                elif later.get("event_type") == "term_override":
                    override = canonical_display((later.get("new") or {}).get("canonical"))
                    if override:
                        item[norm_field] = override
                        item["canonical_source"] = "paper_override"
            if not hidden:
                new_items.append(item)
        curated[list_key] = new_items

    curated["curation"] = {
        "paper_id": paper_id,
        "materialized_at": now_iso(),
        "event_count": sum(1 for x in events if x.get("paper_id") in (None, paper_id)),
        "raw_preserved": True,
    }
    return curated


def apply_evidence(
    paper_id: str,
    raw: dict[str, Any],
    ontology: Ontology,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    curated = copy.deepcopy(raw)
    grouped = _latest_entity_events(events, paper_id)

    measurements = []
    for item in _term_items_with_uids(paper_id, "measurement", curated.get("measurements", [])):
        original = item.get("property_normalized")
        canonical, source = ontology.resolve("property", original or item.get("property_raw"))
        item["property_normalized_original"] = original
        item["property_normalized"] = canonical
        item["canonical_source"] = source
        measurements.append(item)
    curated["measurements"] = measurements

    claims = []
    for item in _term_items_with_uids(paper_id, "claim", curated.get("claims", [])):
        uid = item["curation_uid"]
        for original_field in ["statement", "claim_type", "subject", "relation", "object", "conditions_text"]:
            item[original_field + "_original"] = item.get(original_field)
        item["curated_tags_original"] = list(item.get("curated_tags") or [])
        hidden = False
        for event in grouped.get(("claim", uid), []):
            if event.get("event_type") == "claim_delete":
                hidden = True
            elif event.get("event_type") == "claim_edit":
                patch = event.get("new") or {}
                for field in ["statement", "claim_type", "subject", "relation", "object", "conditions_text", "review_status", "review_notes"]:
                    if field in patch:
                        item[field] = patch[field]
                if "tags" in patch:
                    item["curated_tags"] = list(patch.get("tags") or [])
                item["curation_origin"] = "user_edited"
        if not hidden:
            claims.append(item)
    for event in events:
        if event.get("paper_id") != paper_id or event.get("entity_type") != "claim" or event.get("event_type") != "claim_add":
            continue
        new = copy.deepcopy(event.get("new") or {})
        statement = canonical_display(new.get("statement"))
        if not statement:
            continue
        user_uid = f"claim:user:{event['event_id']}"
        claim = {
            "claim_id": f"USER-{event['event_id'][:8]}",
            "curation_uid": user_uid,
            "claim_type": new.get("claim_type") or "user_curated",
            "statement": statement,
            "subject": new.get("subject"),
            "relation": new.get("relation"),
            "object": new.get("object"),
            "conditions_text": new.get("conditions_text"),
            "system_refs": list(new.get("system_refs") or []),
            "claim_origin": "user_curated",
            "evidence_sids": list(new.get("evidence_sids") or []),
            "curated_tags": list(new.get("tags") or []),
            "curation_origin": "user_added",
            "review_status": new.get("review_status") or "edited",
            "review_notes": new.get("review_notes"),
        }
        hidden = False
        for later in grouped.get(("claim", user_uid), []):
            if later.get("event_type") == "claim_delete":
                hidden = True
            elif later.get("event_type") == "claim_edit":
                patch = later.get("new") or {}
                for field in ["statement", "claim_type", "subject", "relation", "object", "conditions_text", "review_status", "review_notes"]:
                    if field in patch:
                        claim[field] = patch[field]
                if "tags" in patch:
                    claim["curated_tags"] = list(patch.get("tags") or [])
                claim["curation_origin"] = "user_edited"
        if not hidden:
            claims.append(claim)
    curated["claims"] = claims
    curated["curation"] = {
        "paper_id": paper_id,
        "materialized_at": now_iso(),
        "event_count": sum(1 for x in events if x.get("paper_id") in (None, paper_id)),
        "raw_preserved": True,
    }
    return curated


def materialize_paper(
    paper_id: str,
    *,
    extracted_dir: Path,
    curated_dir: Path,
    ontology_path: Path,
    events_path: Path,
    metadata_dir: Path | None = None,
) -> dict[str, Any]:
    events = read_event_log(events_path)
    ontology = Ontology(ontology_path, events)
    curated_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"paper_id": paper_id, "written": []}
    inventory_path = extracted_dir / f"{paper_id}.inventory.json"
    evidence_path = extracted_dir / f"{paper_id}.evidence.json"
    if inventory_path.exists():
        raw = read_json(inventory_path)
        curated = apply_inventory(paper_id, raw, ontology, events)
        curated["curation"]["raw_sha256"] = sha256_file(inventory_path)
        out = curated_dir / inventory_path.name
        write_json(out, curated)
        result["written"].append(str(out))
    if evidence_path.exists():
        raw = read_json(evidence_path)
        curated = apply_evidence(paper_id, raw, ontology, events)
        curated["curation"]["raw_sha256"] = sha256_file(evidence_path)
        out = curated_dir / evidence_path.name
        write_json(out, curated)
        result["written"].append(str(out))
    if metadata_dir is not None:
        metadata_path = metadata_dir / f"{paper_id}.metadata.json"
        raw_metadata = read_json(metadata_path) if metadata_path.exists() else {}
        curated_metadata = apply_metadata(paper_id, raw_metadata, events)
        if metadata_path.exists():
            curated_metadata["curation"]["raw_sha256"] = sha256_file(metadata_path)
        else:
            curated_metadata["curation"]["raw_sha256"] = None
        out = curated_dir / f"{paper_id}.metadata.json"
        write_json(out, curated_metadata)
        result["written"].append(str(out))
    return result


def materialize_all(
    paper_ids: Iterable[str],
    *,
    extracted_dir: Path,
    curated_dir: Path,
    ontology_path: Path,
    events_path: Path,
    metadata_dir: Path | None = None,
) -> list[dict[str, Any]]:
    return [
        materialize_paper(
            paper_id,
            extracted_dir=extracted_dir,
            curated_dir=curated_dir,
            ontology_path=ontology_path,
            events_path=events_path,
            metadata_dir=metadata_dir,
        )
        for paper_id in paper_ids
    ]
