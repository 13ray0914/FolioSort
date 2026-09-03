from __future__ import annotations

import unittest
import importlib.util

from lib.citation_styles import CITATION_STYLES, format_citation, format_citations, plain_text


class CitationStyleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paper = {
            "authors": [
                {"forename": "Ada M", "surname": "Lovelace"},
                {"forename": "Grace B", "surname": "Hopper"},
            ],
            "title": "A useful scientific result",
            "journal": "Journal of Examples",
            "year": 2026,
            "volume": "12",
            "issue": "3",
            "pages": "101-109",
            "doi": "10.1000/example",
        }

    def test_all_requested_styles_are_available(self) -> None:
        rendered = format_citations(self.paper)
        self.assertEqual(set(rendered), set(CITATION_STYLES))
        self.assertTrue(all("10.1000/example" in value for value in rendered.values()))

    def test_styles_use_distinct_author_and_date_conventions(self) -> None:
        self.assertIn("Lovelace, A. M.; Hopper, G. B.", format_citation(self.paper, "acs"))
        self.assertIn("A. M. Lovelace, G. B. Hopper", format_citation(self.paper, "rsc"))
        self.assertIn("Lovelace, A. M. & Hopper, G. B.", format_citation(self.paper, "nature"))
        self.assertIn("(2026)", format_citation(self.paper, "science"))

    def test_old_crossref_authors_recover_given_name_initials(self) -> None:
        old = dict(self.paper, authors=[{"full_name": "Ada M Lovelace", "surname": "Lovelace"}])
        self.assertIn("Lovelace, A. M.", format_citation(old, "acs"))

    def test_abbreviated_journal_volume_issue_and_pages_are_used(self) -> None:
        paper = dict(self.paper, journal_abbreviation="J. Ex.")
        rendered = format_citation(paper, "acs")
        self.assertIn("J. Ex. 2026", rendered)
        self.assertIn("12(3), 101-109", rendered)

    @unittest.skipUnless(importlib.util.find_spec("pyiso4"), "pyiso4 not installed")
    def test_missing_provider_abbreviation_uses_iso4(self) -> None:
        paper = dict(self.paper, journal="Bulletin of the Chemical Society of Japan")
        paper.pop("journal_abbreviation", None)
        self.assertIn("Bull. Chem. Soc. Jpn.", format_citation(paper, "acs"))

    def test_markup_is_removed_from_titles(self) -> None:
        self.assertEqual(plain_text("An <i>in situ</i> H<sub>2</sub> study"), "An in situ H2 study")
        self.assertNotIn("<sub>", format_citation(dict(self.paper, title="H<sub>2</sub>"), "acs"))

    def test_missing_volume_and_pages_are_not_invented(self) -> None:
        sparse = dict(self.paper, volume="", issue="", pages="")
        result = format_citation(sparse, "wiley")
        self.assertIn("https://doi.org/10.1000/example", result)
        self.assertNotIn("None", result)

    def test_unknown_style_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            format_citation(self.paper, "unknown")

    def test_et_al_has_only_one_period(self) -> None:
        many_authors = [
            {"forename": f"Author {index}", "surname": f"Family{index}"}
            for index in range(7)
        ]
        result = format_citation(dict(self.paper, authors=many_authors), "nature")
        self.assertIn("et al. A useful", result)
        self.assertNotIn("et al..", result)


if __name__ == "__main__":
    unittest.main()
