from __future__ import annotations

import html
import re
from typing import Any


CITATION_STYLES = ("acs", "rsc", "wiley", "nature", "science")
_ISO4_ABBREVIATOR: Any = None


def plain_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _initials(given: str) -> str:
    tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", given or "")
    return " ".join(f"{token[0].upper()}." for token in tokens if token)


def _author_parts(author: Any) -> tuple[str, str, str]:
    if isinstance(author, dict):
        family = plain_text(author.get("family") or author.get("surname"))
        given = plain_text(author.get("given") or author.get("forename"))
        full = plain_text(author.get("full_name") or author.get("display_name") or author.get("name") or author.get("raw"))
        if not family and full:
            pieces = full.rsplit(" ", 1)
            family = pieces[-1]
            given = pieces[0] if len(pieces) > 1 else ""
        elif not given and family and full:
            suffix = re.compile(rf"(?:^|\s){re.escape(family)}$", re.IGNORECASE)
            given = suffix.sub("", full).strip(" ,")
        return family, _initials(given), full or " ".join(x for x in (given, family) if x)
    full = plain_text(author)
    pieces = full.rsplit(" ", 1)
    family = pieces[-1] if pieces else ""
    given = pieces[0] if len(pieces) > 1 else ""
    return family, _initials(given), full


def _authors(value: Any) -> list[Any]:
    if isinstance(value, list):
        return [item for item in value if item]
    if isinstance(value, dict):
        return [value]
    text = plain_text(value)
    return [part.strip() for part in text.split(";") if part.strip()] if text else []


def _join_authors(items: list[str], *, final: str = ", ") -> str:
    if not items:
        return "Unknown author"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + final + items[-1]


def _journal_text(record: dict[str, Any]) -> str:
    supplied = plain_text(
        record.get("journal_abbreviation")
        or record.get("journal_short")
        or record.get("short_container_title")
    )
    if supplied:
        return supplied
    journal = plain_text(record.get("journal"))
    if not journal:
        return "Unknown journal"
    try:
        global _ISO4_ABBREVIATOR
        if _ISO4_ABBREVIATOR is None:
            from pyiso4.ltwa import Abbreviate

            _ISO4_ABBREVIATOR = Abbreviate.create()
        return plain_text(_ISO4_ABBREVIATOR(journal, remove_part=True)) or journal
    except Exception:
        # Citation export remains available in minimal/source-only environments;
        # normal FolioSort installations include pyiso4.
        return journal


def _author_text(value: Any, style: str) -> str:
    authors = _authors(value)
    if style == "acs":
        rendered = [", ".join(x for x in (_author_parts(a)[0], _author_parts(a)[1]) if x) for a in authors]
        if len(rendered) > 10:
            rendered = rendered[:10] + ["et al."]
        return "; ".join(rendered) or "Unknown author"
    if style == "nature":
        rendered = [", ".join(x for x in (_author_parts(a)[0], _author_parts(a)[1]) if x) for a in authors]
        if len(rendered) > 5:
            return (rendered[0] if rendered else "Unknown author") + " et al."
        return _join_authors(rendered, final=" & ")
    rendered = [" ".join(x for x in (_author_parts(a)[1], _author_parts(a)[0]) if x) for a in authors]
    if style == "science" and len(rendered) > 10:
        return ", ".join(rendered[:10]) + ", et al."
    return _join_authors(rendered)


def format_citation(record: dict[str, Any], style: str) -> str:
    style = str(style or "acs").strip().lower()
    if style not in CITATION_STYLES:
        raise ValueError(f"Unsupported citation style: {style}")
    authors = _author_text(record.get("authors"), style)
    authors_sentence = authors if authors.endswith(".") else authors + "."
    title = plain_text(record.get("title")).rstrip(".") or "Untitled"
    journal = _journal_text(record)
    year = plain_text(record.get("year")).rstrip(".") or "n.d."
    volume = plain_text(record.get("volume")).rstrip(".")
    issue = plain_text(record.get("issue")).rstrip(".")
    pages = plain_text(record.get("pages") or record.get("page") or record.get("article_number")).rstrip(".")
    doi = plain_text(record.get("doi")).rstrip(".")
    doi_url = f"https://doi.org/{doi}" if doi else ""
    volume_issue = volume + (f"({issue})" if issue else "") if volume else ""

    if style == "acs":
        publication = " ".join(x for x in (journal, year) if x)
        details = ", ".join(x for x in (volume_issue, pages) if x)
        citation = f"{authors_sentence} {title}. {publication}"
        if details:
            citation += f", {details}"
        if doi:
            citation += f". DOI: {doi}"
    elif style == "rsc":
        details = ", ".join(x for x in (year, volume_issue, pages) if x)
        citation = f"{authors}, {journal}, {details}"
        if doi:
            citation += f", DOI: {doi}"
    elif style == "wiley":
        details = ", ".join(x for x in (volume_issue, pages) if x)
        citation = f"{authors}, {journal} {year}"
        if details:
            citation += f", {details}"
        if doi_url:
            citation += f", {doi_url}"
    elif style == "nature":
        details = ", ".join(x for x in (volume_issue, pages) if x)
        citation = f"{authors_sentence} {title}. {journal}"
        if details:
            citation += f" {details}"
        citation += f" ({year})"
        if doi_url:
            citation += f". {doi_url}"
    else:
        details = " ".join(x for x in (volume_issue, pages) if x)
        citation = f"{authors}, {title}. {journal}"
        if details:
            citation += f" {details}"
        citation += f" ({year})"
        if doi:
            citation += f". DOI: {doi}"
    return re.sub(r"\s+", " ", citation).strip().rstrip(".") + "."


def format_citations(record: dict[str, Any]) -> dict[str, str]:
    return {style: format_citation(record, style) for style in CITATION_STYLES}
