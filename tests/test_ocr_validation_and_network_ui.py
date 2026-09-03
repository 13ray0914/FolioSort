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

    def test_cluster_controls_are_grouped_under_clusters(self) -> None:
        graph = read("scripts/13_build_multiplex_network.py")
        find_section = graph[graph.index('data-section="find"'):graph.index('data-section="layers"')]
        clusters_section = graph[graph.index('data-section="clusters"'):graph.index('data-section="clusterPapers"')]
        cluster_papers_section = graph[graph.index('data-section="clusterPapers"'):graph.index('data-section="selectedPaper"')]

        self.assertIn("<span>Find paper</span>", find_section)
        self.assertNotIn("Find paper and cluster", graph)
        self.assertNotIn('id="clusterFilter"', find_section)
        self.assertIn('id="clusterFilter" hidden', clusters_section)
        self.assertIn('id="allClustersBtn"', clusters_section)
        self.assertIn("document.getElementById('allClustersBtn').onclick", graph)
        self.assertNotIn('id="downloadAllClustersMd"', clusters_section)
        self.assertIn('id="downloadAllClustersMd"', cluster_papers_section)
        self.assertIn("function allClustersMarkdown", graph)
        self.assertIn("ranked representative claims", graph)
        self.assertNotIn('id="citationStyle"', clusters_section)
        self.assertIn('id="citationStyle"', cluster_papers_section)
        for style in ["acs", "rsc", "wiley", "nature", "science"]:
            self.assertIn(f'<option value="{style}">', graph)
        self.assertIn('id="downloadClusterBibliography"', graph)
        self.assertNotIn('id="downloadClusterTxt"', graph)
        self.assertNotIn("function downloadClusterTxt", graph)
        self.assertNotIn('id="clusterPaperList"', cluster_papers_section)
        self.assertNotIn("document.getElementById('clusterPaperList')", graph)
        self.assertNotIn("openAccordion('clusterPapers')", graph)
        self.assertIn("side.scrollTop=scrollTop", graph)

    def test_pdf_zip_manifest_uses_the_selected_citation_style(self) -> None:
        graph = read("scripts/13_build_multiplex_network.py")
        dashboard = read("scripts/review_app_server.py")

        self.assertIn("citation_style:style", graph)
        self.assertIn("citation_style=str(body.get(\"citation_style\") or \"acs\")", dashboard)
        self.assertIn("formatted_reference = format_citation(row, citation_style)", dashboard)
        self.assertIn('"citation_style", "formatted_reference"', dashboard)

    def test_representative_claims_are_ranked_and_explained(self) -> None:
        graph = read("scripts/13_build_multiplex_network.py")

        self.assertIn("rank_representative_claims", graph)
        self.assertIn('"representative_claims": representative_claims', graph)
        self.assertIn("Ranked by Abstract, Conclusion", graph)
        self.assertIn("<b>${i+1}.</b>", graph)

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
        self.assertIn("frame.contentWindow.applyNetworkTheme(theme,false)", dashboard)
        self.assertIn(':root[data-theme="light"]', graph)
        self.assertIn("function applyNetworkTheme", graph)
        self.assertIn("function setupRightDragTransform", graph)
        self.assertIn("event.button!==2", graph)
        self.assertIn("network.moveTo({scale:", graph)
        self.assertIn("<title>Multiplex Network</title>", graph)
        self.assertNotIn("Multiplex Literature Network", graph)
        self.assertNotIn("Leiden clustering uses the complete selected layers", graph)
        self.assertNotIn("Drag the vertical ⋮ handle to resize the canvas and controls", graph)
        self.assertIn('id="themeToggle"', curation)
        self.assertIn("foliosort-curation-theme", curation)
        self.assertIn(':root[data-theme="light"]', curation)


if __name__ == "__main__":
    unittest.main()
