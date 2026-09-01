from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class OcrWorkflowTests(unittest.TestCase):
    def test_ocr_is_non_destructive_and_only_reprocesses_successful_ids(self) -> None:
        worker = read("scripts/ocr_blocked_papers.py")
        wrapper = read("scripts/run_ocr_blocked.sh")
        parser = read("scripts/02_grobid_parse.py")

        self.assertIn('output_dir = root / "data" / "ocr_pdfs"', worker)
        self.assertIn("original_pdf_preserved", worker)
        self.assertIn("os.replace(temporary, target)", worker)
        self.assertNotIn("os.replace(temporary, source)", worker)
        self.assertIn('REVIEW_IDS="$(tr -d', wrapper)
        self.assertIn('PIPELINE_ARGS+=(--ids "$REVIEW_IDS")', read("scripts/run_review_pipeline.sh"))
        self.assertIn("def preferred_pdf", parser)
        self.assertIn('get_stage(conn, str(row["paper_id"]), "ocr")', parser)

    def test_dashboard_exposes_ocr_status_and_action(self) -> None:
        server = read("scripts/review_app_server.py")

        self.assertIn('id="runOcr"', server)
        self.assertIn('parsed.path == "/api/run_ocr"', server)
        self.assertIn('script_name="run_ocr_blocked.sh", kind="ocr"', server)
        self.assertIn('"ocr_status": APP.ocr_status(slug)', server)


class ValidationReviewTests(unittest.TestCase):
    def test_curation_explains_validation_and_records_human_decision_separately(self) -> None:
        curation = read("scripts/curation_server.py")
        graph = read("scripts/13_build_multiplex_network.py")

        self.assertIn("Automatic validation review", curation)
        self.assertIn('path == "/api/validation/review"', curation)
        self.assertIn("INSERT INTO human_reviews", curation)
        self.assertIn('"validation": read_json(validation_path)', curation)
        self.assertIn('"human_review": human_reviews.get', graph)
        self.assertIn("Automatic validation:", graph)


class NetworkInteractionTests(unittest.TestCase):
    def test_layout_uses_the_original_whole_graph_force_layout(self) -> None:
        runtime = read("lib/network_runtime.py")
        builder = read("scripts/13_build_multiplex_network.py")
        recluster = read("scripts/15_recluster_network.py")

        self.assertNotIn("def _clustered_layout_positions", runtime)
        self.assertIn("graph.layout_fruchterman_reingold", runtime)
        self.assertNotIn("membership=membership", builder)
        self.assertNotIn("membership=membership", recluster)
        self.assertIn("solver:'repulsion'", builder)
        self.assertIn("nodeDistance:165", builder)

    def test_search_can_highlight_all_matching_papers(self) -> None:
        graph = read("scripts/13_build_multiplex_network.py")

        self.assertIn('id="highlightMatches"', graph)
        self.assertIn("function highlightPapers", graph)
        self.assertIn("highlightPapers(searchMatches.map", graph)
        self.assertIn("borderWidthSelected:5", graph)
        self.assertIn("Ctrl/Cmd-click", graph)

    def test_curated_metadata_rebuild_is_fast_and_live_network_refreshes(self) -> None:
        curation = read("scripts/curation_server.py")
        dashboard = read("scripts/review_app_server.py")
        graph = read("scripts/13_build_multiplex_network.py")

        self.assertIn('"--skip-ai-cluster-naming"', curation)
        self.assertIn('"network_revision": network_path.stat().st_mtime_ns', dashboard)
        self.assertIn("function watchPublishedNetwork", graph)
        self.assertIn("location.reload()", graph)
        self.assertIn("curated_metadata_path if use_curated", graph)
        self.assertIn('tmp_path.replace(out_path)', graph)

    def test_network_is_embedded_themed_rotatable_and_renamed(self) -> None:
        dashboard = read("scripts/review_app_server.py")
        graph = read("scripts/13_build_multiplex_network.py")
        curation = read("scripts/curation_server.py")

        self.assertIn('id="networkView"', dashboard)
        self.assertIn('id="networkFrame"', dashboard)
        self.assertIn("showAppTab('network')", dashboard)
        self.assertIn('parsed.path == "/network-content"', dashboard)
        self.assertIn("self._allow_same_origin_frame = True", dashboard)
        self.assertIn("frame-ancestors 'self'", dashboard)
        self.assertIn('"SAMEORIGIN" if allow_same_origin_frame', dashboard)
        self.assertIn("syncNetworkTheme", dashboard)
        self.assertIn(':root[data-theme="light"]', graph)
        self.assertIn("function applyNetworkTheme", graph)
        self.assertIn("function setupRightDragTransform", graph)
        self.assertIn("event.button!==2", graph)
        self.assertIn("network.moveTo({scale:", graph)
        self.assertIn("<title>Multiplex Network</title>", graph)
        self.assertNotIn("Multiplex Literature Network", graph)
        self.assertIn('id="themeToggle"', curation)
        self.assertIn("foliosort-curation-theme", curation)
        self.assertIn(':root[data-theme="light"]', curation)


if __name__ == "__main__":
    unittest.main()
