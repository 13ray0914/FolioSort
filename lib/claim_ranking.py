from __future__ import annotations

import math
import re
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


RANKING_VERSION = "abstract-conclusion-mmr-v1"


def _sentence_text(paragraphs: Any) -> str:
    if isinstance(paragraphs, dict):
        paragraphs = [paragraphs]
    parts: list[str] = []
    for paragraph in paragraphs or []:
        if not isinstance(paragraph, dict):
            continue
        for sentence in paragraph.get("sentences", []) or []:
            text = str((sentence or {}).get("text") or "").strip()
            if text:
                parts.append(text)
    return " ".join(parts)


def _section_text_and_sids(paper: dict[str, Any]) -> tuple[str, set[str]]:
    parts: list[str] = []
    sids: set[str] = set()
    for section in paper.get("sections", []) or []:
        heading = re.sub(r"[^a-z]+", " ", str(section.get("heading") or "").lower()).strip()
        if not any(token in heading for token in ("conclusion", "concluding", "summary", "outlook")):
            continue
        parts.append(_sentence_text(section.get("paragraphs", [])))
        for paragraph in section.get("paragraphs", []) or []:
            for sentence in paragraph.get("sentences", []) or []:
                sid = str((sentence or {}).get("sid") or "").strip()
                if sid:
                    sids.add(sid)
    return " ".join(x for x in parts if x), sids


def _abstract_text_and_sids(paper: dict[str, Any]) -> tuple[str, set[str]]:
    paragraphs = paper.get("abstract", []) or []
    if isinstance(paragraphs, dict):
        paragraphs = [paragraphs]
    sids = {
        str(sentence.get("sid"))
        for paragraph in paragraphs
        if isinstance(paragraph, dict)
        for sentence in paragraph.get("sentences", []) or []
        if sentence.get("sid")
    }
    return _sentence_text(paragraphs), sids


def _flatten_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        preferred = [value.get(key) for key in ("statement", "summary", "description", "model")]
        texts = [text for item in preferred for text in _flatten_text(item)]
        return texts or [text for item in value.values() for text in _flatten_text(item)]
    if isinstance(value, list):
        return [text for item in value for text in _flatten_text(item)]
    return []


def _memory_priority_text(memory: dict[str, Any]) -> str:
    values = [
        memory.get("central_question"),
        memory.get("major_findings"),
        memory.get("mechanistic_model"),
        memory.get("global_constraints"),
    ]
    return " ".join(text for value in values for text in _flatten_text(value))


def _claim_document(item: dict[str, Any]) -> str:
    parts: list[Any] = [
        item.get("statement"),
        item.get("subject"),
        item.get("relation"),
        item.get("object"),
        item.get("conditions_text"),
        " ".join(str(x) for x in (item.get("curated_tags") or []) if x),
    ]
    return " ".join(str(part).strip() for part in parts if str(part or "").strip())


def _eligible_claims(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in evidence.get("claims", []) or []
        if str(item.get("statement") or "").strip()
        and str(item.get("review_status") or "").strip().lower() != "rejected"
    ]


def rank_representative_claims(
    evidence: dict[str, Any],
    paper: dict[str, Any],
    *,
    title: str = "",
    memory: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rank display claims without changing the full claim-similarity corpus.

    Relevance to author-emphasized text is combined with study-origin and
    evidence-support priors. MMR then reduces near-duplicate claims near the top.
    The score is an explainable representativeness proxy, not a scientific truth
    or a field-wide novelty measurement.
    """
    candidates = _eligible_claims(evidence)
    if not candidates:
        return []

    abstract, abstract_sids = _abstract_text_and_sids(paper)
    conclusion, conclusion_sids = _section_text_and_sids(paper)
    references = {
        "abstract": (abstract, 0.34),
        "conclusion": (conclusion, 0.38),
        "title": (str(title or "").strip(), 0.10),
        "summary": (_memory_priority_text(memory or {}), 0.18),
    }
    active_references = [(name, text, weight) for name, (text, weight) in references.items() if text]
    claim_docs = [_claim_document(item) for item in candidates]
    similarities = {name: np.zeros(len(candidates), dtype=np.float32) for name in references}
    claim_similarity = np.eye(len(candidates), dtype=np.float32)

    if active_references:
        try:
            corpus = claim_docs + [text for _, text, _ in active_references]
            matrix = TfidfVectorizer(
                stop_words="english",
                ngram_range=(1, 2),
                min_df=1,
                sublinear_tf=True,
            ).fit_transform(corpus)
            claim_matrix = matrix[: len(candidates)]
            reference_matrix = matrix[len(candidates) :]
            reference_similarity = cosine_similarity(claim_matrix, reference_matrix).astype(np.float32)
            claim_similarity = cosine_similarity(claim_matrix).astype(np.float32)
            for column, (name, _, _) in enumerate(active_references):
                similarities[name] = reference_similarity[:, column]
        except ValueError:
            pass

    active_weight = sum(weight for _, _, weight in active_references) or 1.0
    origin_priority = {
        "this_paper_result": 1.0,
        "user_curated": 0.95,
        "author_interpretation": 0.82,
        "review_synthesis": 0.58,
        "cited_literature_summary": 0.12,
    }
    ranked_rows: list[dict[str, Any]] = []
    base_scores: list[float] = []
    for index, item in enumerate(candidates):
        relevance = sum(
            weight * float(similarities[name][index])
            for name, _, weight in active_references
        ) / active_weight
        evidence_sids = {str(x) for x in (item.get("evidence_sids") or []) if x}
        section_evidence = 1.0 if evidence_sids & conclusion_sids else (0.8 if evidence_sids & abstract_sids else 0.0)
        evidence_support = min(1.0, math.log1p(len(evidence_sids)) / math.log(5.0))
        study_origin = origin_priority.get(str(item.get("claim_origin") or ""), 0.65)
        score = 0.68 * relevance + 0.14 * study_origin + 0.10 * section_evidence + 0.08 * evidence_support
        base_scores.append(score)
        ranked_rows.append(
            {
                "statement": str(item.get("statement") or "").strip(),
                "score": round(score, 4),
                "abstract_similarity": round(float(similarities["abstract"][index]), 4),
                "conclusion_similarity": round(float(similarities["conclusion"][index]), 4),
                "title_similarity": round(float(similarities["title"][index]), 4),
                "summary_similarity": round(float(similarities["summary"][index]), 4),
                "study_origin": str(item.get("claim_origin") or "unspecified"),
                "evidence_count": len(evidence_sids),
                "ranking_version": RANKING_VERSION,
            }
        )

    remaining = set(range(len(candidates)))
    selected: list[int] = []
    while remaining:
        def selection_key(index: int) -> tuple[float, float, int]:
            redundancy = max((float(claim_similarity[index, prior]) for prior in selected), default=0.0)
            mmr_score = base_scores[index] if not selected else 0.85 * base_scores[index] - 0.15 * redundancy
            return mmr_score, base_scores[index], -index

        choice = max(remaining, key=selection_key)
        selected.append(choice)
        remaining.remove(choice)

    return [ranked_rows[index] for index in selected]
