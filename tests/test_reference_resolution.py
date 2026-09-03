from __future__ import annotations

import importlib.util
import sqlite3
import sys
import types
import unittest
from pathlib import Path

if importlib.util.find_spec("requests") is None:
    sys.modules["requests"] = types.SimpleNamespace(Response=object)

from lib.v4_common import crossref_candidate, ensure_v4_schema, openalex_candidate, valid_doi


ROOT = Path(__file__).resolve().parents[1]


def load_resolver_module():
    path = ROOT / "scripts" / "10_resolve_references.py"
    spec = importlib.util.spec_from_file_location("resolve_references_v4", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReferenceResolutionTests(unittest.TestCase):
    def test_manual_doi_validation_normalizes_urls(self) -> None:
        self.assertEqual(valid_doi("https://doi.org/10.1000/Example.1"), "10.1000/example.1")
        with self.assertRaises(ValueError):
            valid_doi("not-a-doi")

    def test_manual_override_table_is_created(self) -> None:
        conn = sqlite3.connect(":memory:")
        ensure_v4_schema(conn)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("reference_doi_overrides_v4", tables)

    def test_crossref_candidate_retains_complete_bibliographic_fields(self) -> None:
        candidate = crossref_candidate({
            "DOI": "10.1000/example",
            "title": ["H<sub>2</sub> study"],
            "container-title": ["Journal of Examples"],
            "short-container-title": ["J. Ex."],
            "author": [{"given": "Ada M.", "family": "Lovelace"}],
            "volume": "12",
            "issue": "3",
            "page": "101-109",
        })
        self.assertEqual(candidate["authors"][0]["given"], "Ada M.")
        self.assertEqual(candidate["journal_abbreviation"], "J. Ex.")
        self.assertEqual(candidate["volume"], "12")
        self.assertEqual(candidate["issue"], "3")
        self.assertEqual(candidate["pages"], "101-109")

    def test_openalex_candidate_retains_biblio_fields(self) -> None:
        candidate = openalex_candidate({
            "id": "https://openalex.org/W1",
            "title": "Example",
            "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
            "primary_location": {"source": {"display_name": "Journal of Examples", "abbreviated_title": "J. Ex."}},
            "biblio": {"volume": "12", "issue": "3", "first_page": "101", "last_page": "109"},
        })
        self.assertEqual(candidate["authors"][0]["given"], "Ada")
        self.assertEqual(candidate["journal_abbreviation"], "J. Ex.")
        self.assertEqual(candidate["pages"], "101-109")

    def test_oversized_ocr_text_is_not_sent_as_a_provider_query(self) -> None:
        resolver = load_resolver_module()
        issue = resolver.reference_search_issue(
            {"title": "OCR body " * 100, "raw_reference": "", "doi": ""},
            {"external_query_max_chars": 350},
        )
        self.assertIn("Enter the DOI manually", issue)
        self.assertIsNone(
            resolver.reference_search_issue(
                {"title": "A normal reference title", "raw_reference": "", "doi": ""},
                {"external_query_max_chars": 350},
            )
        )

    def test_incremental_and_manual_resolution_paths_are_present(self) -> None:
        source = (ROOT / "scripts" / "10_resolve_references.py").read_text(encoding="utf-8")
        self.assertIn("previous_reference_records", source)
        self.assertIn('method = "manual_doi_override"', source)
        self.assertIn('"reused_external_resolution": reused', source)
        self.assertIn('provider_scores["openalex"] = {"error": str(error)', source)

    def test_grobid_reports_image_only_pdfs_as_ocr_required(self) -> None:
        source = (ROOT / "scripts" / "02_grobid_parse.py").read_text(encoding="utf-8")
        self.assertIn("image_only_pdf_diagnostic", source)
        self.assertIn("OCR_REQUIRED", source)
        self.assertIn("every page is an image", source)

    def test_dashboard_exposes_reference_override_api_and_ui(self) -> None:
        source = (ROOT / "scripts" / "review_app_server.py").read_text(encoding="utf-8")
        self.assertIn('id="referenceDoi"', source)
        self.assertIn('id="saveReferenceDoi"', source)
        self.assertIn('parsed.path == "/api/reference_issues"', source)
        self.assertIn('parsed.path == "/api/reference_override"', source)


if __name__ == "__main__":
    unittest.main()
