from __future__ import annotations

import unittest

from lib.claim_ranking import RANKING_VERSION, rank_representative_claims


class RepresentativeClaimRankingTests(unittest.TestCase):
    def test_abstract_and_conclusion_claim_ranks_first_and_rejected_is_ignored(self) -> None:
        paper = {
            "abstract": [
                {"sentences": [{"sid": "s1", "text": "The treatment strongly improves target stability."}]}
            ],
            "sections": [
                {
                    "heading": "Conclusion",
                    "paragraphs": [
                        {"sentences": [{"sid": "s9", "text": "The treatment strongly improves target stability."}]}
                    ],
                },
                {
                    "heading": "Methods",
                    "paragraphs": [
                        {"sentences": [{"sid": "s4", "text": "Samples were prepared with a centrifuge."}]}
                    ],
                },
            ],
        }
        evidence = {
            "claims": [
                {
                    "statement": "Samples were prepared with a centrifuge.",
                    "claim_origin": "this_paper_result",
                    "evidence_sids": ["s4"],
                },
                {
                    "statement": "The treatment strongly improves target stability.",
                    "claim_origin": "this_paper_result",
                    "evidence_sids": ["s1", "s9"],
                },
                {
                    "statement": "The treatment strongly improves target stability under every condition.",
                    "claim_origin": "this_paper_result",
                    "evidence_sids": ["s9"],
                    "review_status": "rejected",
                },
            ]
        }

        ranked = rank_representative_claims(
            evidence,
            paper,
            title="Improving target stability",
            memory={"major_findings": [{"statement": "Treatment improves stability."}]},
        )

        self.assertEqual(ranked[0]["statement"], "The treatment strongly improves target stability.")
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0]["ranking_version"], RANKING_VERSION)
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])


if __name__ == "__main__":
    unittest.main()
