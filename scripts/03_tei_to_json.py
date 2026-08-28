#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import (
    connect_db,
    get_paths,
    load_config,
    normalize_ws,
    parse_ids,
    read_json,
    select_papers,
    set_stage,
    sha256_file,
    sha256_text,
    stage_is_current,
    write_json,
)

STAGE = "tei_to_json"
PARSER_VERSION = "tei-json-v1.2"
NS = {"tei": "http://www.tei-c.org/ns/1.0"}
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"


def text_of(el) -> str:
    if el is None:
        return ""
    return normalize_ws(" ".join(el.itertext()))


def first_text(root, xpath: str) -> str:
    els = root.xpath(xpath, namespaces=NS)
    for el in els:
        value = text_of(el) if hasattr(el, "itertext") else normalize_ws(str(el))
        if value:
            return value
    return ""


def get_page_from_coords(coords: str | None) -> int | None:
    if not coords:
        return None
    first_box = coords.split(";")[0]
    m = re.match(r"\s*(\d+)\s*,", first_box)
    return int(m.group(1)) if m else None


def extract_citation_ref_ids(sentence_el) -> list[str]:
    refs = []
    for ref in sentence_el.xpath('.//tei:ref[@type="bibr"]', namespaces=NS):
        target = ref.get("target", "")
        for part in target.split():
            part = part.lstrip("#")
            if part and part not in refs:
                refs.append(part)
    return refs


def make_sentence(sentence_el, sid: str) -> dict:
    return {
        "sid": sid,
        "grobid_id": sentence_el.get(XML_ID),
        "page": get_page_from_coords(sentence_el.get("coords")),
        "coords": sentence_el.get("coords"),
        "citation_ref_ids": extract_citation_ref_ids(sentence_el),
        "text": text_of(sentence_el),
    }


def paragraph_to_sentences(p_el, sid_counter: list[int]) -> list[dict]:
    s_els = p_el.xpath('./tei:s', namespaces=NS)
    out = []
    if s_els:
        for s_el in s_els:
            txt = text_of(s_el)
            if not txt:
                continue
            sid_counter[0] += 1
            out.append(make_sentence(s_el, f"s{sid_counter[0]:06d}"))
    else:
        txt = text_of(p_el)
        if txt:
            sid_counter[0] += 1
            refs = []
            for ref in p_el.xpath('.//tei:ref[@type="bibr"]', namespaces=NS):
                target = ref.get("target", "")
                refs.extend(x.lstrip("#") for x in target.split() if x)
            out.append(
                {
                    "sid": f"s{sid_counter[0]:06d}",
                    "grobid_id": p_el.get(XML_ID),
                    "page": get_page_from_coords(p_el.get("coords")),
                    "coords": p_el.get("coords"),
                    "citation_ref_ids": list(dict.fromkeys(refs)),
                    "text": txt,
                }
            )
    return out


def parse_abstract(root, sid_counter: list[int]) -> list[dict]:
    abstract = root.xpath('//tei:teiHeader//tei:profileDesc//tei:abstract', namespaces=NS)
    if not abstract:
        abstract = root.xpath('//tei:teiHeader//tei:abstract', namespaces=NS)
    if not abstract:
        return []
    out = []
    p_counter = 0
    for p in abstract[0].xpath('.//tei:p', namespaces=NS):
        sents = paragraph_to_sentences(p, sid_counter)
        if sents:
            p_counter += 1
            out.append({"paragraph_id": f"abs_p{p_counter:04d}", "sentences": sents})
    return out


def walk_div(div_el, section_counter: list[int], paragraph_counter: list[int], sid_counter: list[int], inherited_heading: str = "") -> list[dict]:
    sections = []
    head_el = div_el.find("{http://www.tei-c.org/ns/1.0}head")
    heading = text_of(head_el) or inherited_heading or "Untitled section"

    direct_paragraphs = []
    for child in div_el:
        local = etree.QName(child).localname
        if local == "p":
            sents = paragraph_to_sentences(child, sid_counter)
            if sents:
                paragraph_counter[0] += 1
                direct_paragraphs.append(
                    {"paragraph_id": f"p{paragraph_counter[0]:05d}", "sentences": sents}
                )

    if direct_paragraphs:
        section_counter[0] += 1
        sections.append(
            {
                "section_id": f"sec{section_counter[0]:04d}",
                "grobid_id": div_el.get(XML_ID),
                "heading": heading,
                "paragraphs": direct_paragraphs,
            }
        )

    for child_div in div_el.findall("{http://www.tei-c.org/ns/1.0}div"):
        sections.extend(walk_div(child_div, section_counter, paragraph_counter, sid_counter, heading))
    return sections


def parse_body(root, sid_counter: list[int]) -> list[dict]:
    body = root.xpath('//tei:text/tei:body', namespaces=NS)
    if not body:
        return []
    section_counter = [0]
    paragraph_counter = [0]
    sections = []
    body_el = body[0]

    # Paragraphs directly under <body> become a synthetic section.
    direct = []
    for p in body_el.findall("{http://www.tei-c.org/ns/1.0}p"):
        sents = paragraph_to_sentences(p, sid_counter)
        if sents:
            paragraph_counter[0] += 1
            direct.append({"paragraph_id": f"p{paragraph_counter[0]:05d}", "sentences": sents})
    if direct:
        section_counter[0] += 1
        sections.append(
            {"section_id": f"sec{section_counter[0]:04d}", "grobid_id": None, "heading": "Body", "paragraphs": direct}
        )

    for div in body_el.findall("{http://www.tei-c.org/ns/1.0}div"):
        sections.extend(walk_div(div, section_counter, paragraph_counter, sid_counter))
    return sections


def parse_authors(root) -> list[dict]:
    out = []
    author_els = root.xpath('//tei:teiHeader//tei:sourceDesc//tei:biblStruct/tei:analytic/tei:author', namespaces=NS)
    if not author_els:
        author_els = root.xpath('//tei:teiHeader//tei:fileDesc//tei:titleStmt/tei:author', namespaces=NS)
    for a in author_els:
        forename = " ".join(
            normalize_ws(text_of(x)) for x in a.xpath('.//tei:forename', namespaces=NS) if text_of(x)
        )
        surname = first_text(a, './/tei:surname')
        full = normalize_ws(f"{forename} {surname}") or text_of(a)
        if full:
            out.append({"full_name": full, "forename": forename or None, "surname": surname or None})
    return out


def parse_metadata(root) -> dict:
    title = first_text(root, '//tei:teiHeader//tei:sourceDesc//tei:biblStruct/tei:analytic/tei:title[@level="a"]')
    if not title:
        title = first_text(root, '//tei:teiHeader//tei:titleStmt/tei:title')
    doi = first_text(root, '//tei:teiHeader//tei:sourceDesc//tei:biblStruct//tei:idno[translate(@type,"doi","DOI")="DOI"]')
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "").strip() or None
    journal = first_text(root, '//tei:teiHeader//tei:sourceDesc//tei:biblStruct/tei:monogr/tei:title[@level="j"]') or None
    date_when = root.xpath('//tei:teiHeader//tei:sourceDesc//tei:biblStruct//tei:imprint/tei:date/@when', namespaces=NS)
    date_text = date_when[0] if date_when else first_text(root, '//tei:teiHeader//tei:sourceDesc//tei:biblStruct//tei:imprint/tei:date')
    year = None
    m = re.search(r"\b(18|19|20|21)\d{2}\b", date_text or "")
    if m:
        year = int(m.group(0))
    return {
        "title": title or None,
        "authors": parse_authors(root),
        "doi": doi,
        "journal": journal,
        "year": year,
    }


def parse_references(root) -> list[dict]:
    refs = []
    for i, b in enumerate(root.xpath('//tei:text/tei:back//tei:listBibl/tei:biblStruct', namespaces=NS), start=1):
        ref_id = b.get(XML_ID) or f"B{i}"
        title = first_text(b, './tei:analytic/tei:title[@level="a"]') or first_text(b, './tei:monogr/tei:title')
        doi = first_text(b, './/tei:idno[translate(@type,"doi","DOI")="DOI"]') or None
        if doi:
            doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "").strip()
        raw = first_text(b, './tei:note[@type="raw_reference"]') or text_of(b)
        journal = first_text(b, './tei:monogr/tei:title[@level="j"]') or None
        date_when = b.xpath('.//tei:imprint/tei:date/@when', namespaces=NS)
        date_text = date_when[0] if date_when else first_text(b, './/tei:imprint/tei:date')
        year = None
        m = re.search(r"\b(18|19|20|21)\d{2}\b", date_text or "")
        if m:
            year = int(m.group(0))
        authors = []
        for a in b.xpath('./tei:analytic/tei:author', namespaces=NS):
            name = normalize_ws(
                " ".join(text_of(x) for x in a.xpath('.//tei:forename|.//tei:surname', namespaces=NS))
            )
            if name:
                authors.append(name)
        refs.append(
            {
                "ref_id": ref_id,
                "title": title or None,
                "authors": authors,
                "journal": journal,
                "year": year,
                "doi": doi,
                "raw_reference": raw,
            }
        )
    return refs


def parse_figures_tables(root) -> tuple[list[dict], list[dict], list[dict]]:
    figures = []
    tables = []
    auxiliary = []
    fig_n = 0
    table_n = 0
    for idx, fig in enumerate(root.xpath('//tei:text/tei:body//tei:figure', namespaces=NS), start=1):
        item = {
            "id": fig.get(XML_ID) or f"fig_{idx}",
            "label": first_text(fig, './tei:label') or None,
            "head": first_text(fig, './tei:head') or None,
            "caption": first_text(fig, './tei:figDesc') or first_text(fig, './tei:head') or None,
            "page": get_page_from_coords(fig.get("coords")),
            "coords": fig.get("coords"),
        }
        is_table = fig.get("type") == "table" or bool(fig.xpath('./tei:table', namespaces=NS))
        if is_table:
            table_n += 1
            item["table_text"] = first_text(fig, './tei:table') or None
            tables.append(item)
            combined = normalize_ws(" ".join(x for x in [item.get("label"), item.get("caption"), item.get("table_text")] if x))
            if combined:
                auxiliary.append({
                    "sid": f"table{table_n:04d}",
                    "kind": "table",
                    "heading": normalize_ws(" ".join(x for x in [item.get("label"), item.get("head")] if x)) or f"Table {table_n}",
                    "page": item.get("page"),
                    "coords": item.get("coords"),
                    "citation_ref_ids": [],
                    "text": combined,
                })
        else:
            fig_n += 1
            figures.append(item)
            combined = normalize_ws(" ".join(x for x in [item.get("label"), item.get("caption")] if x))
            if combined:
                auxiliary.append({
                    "sid": f"figcap{fig_n:04d}",
                    "kind": "figure_caption",
                    "heading": normalize_ws(" ".join(x for x in [item.get("label"), item.get("head")] if x)) or f"Figure {fig_n}",
                    "page": item.get("page"),
                    "coords": item.get("coords"),
                    "citation_ref_ids": [],
                    "text": combined,
                })
    return figures, tables, auxiliary


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert GROBID TEI XML to stable, sentence-ID JSON.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    config, root = load_config(args.config)
    paths = get_paths(config, root)
    conn = connect_db(paths["database"])
    rows = select_papers(conn, parse_ids(args.ids), args.limit)

    for row in rows:
        paper_id = row["paper_id"]
        tei_path = paths["tei"] / f"{paper_id}.tei.xml"
        out_path = paths["paper_json"] / f"{paper_id}.json"
        if not tei_path.exists():
            print(f"WAIT    {paper_id}: TEI missing")
            continue
        input_hash = sha256_text(sha256_file(tei_path) + PARSER_VERSION)
        if not args.force and stage_is_current(conn, paper_id, STAGE, input_hash, out_path):
            print(f"SKIP    {paper_id} already current")
            continue

        print(f"JSON    {paper_id}")
        set_stage(conn, paper_id, STAGE, "running", input_hash=input_hash)
        try:
            parser = etree.XMLParser(recover=True, huge_tree=True)
            tree = etree.parse(str(tei_path), parser)
            root_el = tree.getroot()
            sid_counter = [0]
            metadata = parse_metadata(root_el)
            abstract = parse_abstract(root_el, sid_counter)
            sections = parse_body(root_el, sid_counter)
            refs = parse_references(root_el)
            figures, tables, auxiliary_text = parse_figures_tables(root_el)
            payload = {
                "paper_id": paper_id,
                "source": {
                    "original_filename": row["original_filename"],
                    "source_relpath": row["source_relpath"],
                    "source_sha256": row["source_sha256"],
                },
                "metadata": metadata,
                "abstract": abstract,
                "sections": sections,
                "references": refs,
                "figures": figures,
                "tables": tables,
                "auxiliary_text": auxiliary_text,
                "stats": {
                    "sentences": sid_counter[0],
                    "sections": len(sections),
                    "references": len(refs),
                    "figures": len(figures),
                    "tables": len(tables),
                    "auxiliary_text_units": len(auxiliary_text),
                },
            }
            write_json(out_path, payload)
            conn.execute(
                """
                UPDATE papers SET title=?, doi=?, year=?, journal=? WHERE paper_id=?
                """,
                (metadata["title"], metadata["doi"], metadata["year"], metadata["journal"], paper_id),
            )
            conn.commit()
            set_stage(conn, paper_id, STAGE, "success", input_hash, out_path, meta=payload["stats"])
        except Exception as e:
            set_stage(conn, paper_id, STAGE, "error", input_hash=input_hash, error=str(e))
            print(f"ERROR   {paper_id}: {e}")


if __name__ == "__main__":
    main()
