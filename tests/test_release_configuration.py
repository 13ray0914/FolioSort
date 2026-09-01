from __future__ import annotations

import re
import unittest
from pathlib import Path

from foliosort import __version__


ROOT = Path(__file__).resolve().parents[1]


class ReleaseConfigurationTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_dashboard_launcher_expects_the_server_version(self) -> None:
        server = self.read("scripts/review_app_server.py")
        launcher = self.read("scripts/start_review_app.sh")
        # APP_VERSION is now an f-string: APP_VERSION = f"{__version__}-<suffix>"
        app_version = re.search(r'^APP_VERSION = f"\{__version__\}-([^"]+)"$', server, re.MULTILINE)
        # EXPECTED_VERSION is now: EXPECTED_VERSION="${_BASE_VERSION}-<suffix>"
        expected = re.search(r'^EXPECTED_VERSION="\$\{_BASE_VERSION\}-([^"]+)"$', launcher, re.MULTILINE)

        self.assertIsNotNone(app_version, "APP_VERSION not found as a dynamic f-string in review_app_server.py")
        self.assertIsNotNone(expected, "EXPECTED_VERSION not found as a dynamic pattern in start_review_app.sh")
        # Both files must use the same version suffix after the base version
        self.assertEqual(app_version.group(1), expected.group(1))

    def test_curation_clients_expect_the_health_version(self) -> None:
        server = self.read("scripts/curation_server.py")
        dashboard = self.read("scripts/review_app_server.py")
        launcher = self.read("scripts/open_curation_gui.sh")
        server_version = re.search(
            r'^CURATION_APP_VERSION = f"\{__version__\}-([^"]+)"$',
            server,
            re.MULTILINE,
        )
        dashboard_version = re.search(
            r'^\s*expected_version = f"\{__version__\}-([^"]+)"$',
            dashboard,
            re.MULTILINE,
        )
        launcher_version = re.search(
            r'^EXPECTED_VERSION="\$\{_BASE_VERSION\}-([^"]+)"$',
            launcher,
            re.MULTILINE,
        )
        self.assertIsNotNone(server_version)
        self.assertIsNotNone(dashboard_version)
        self.assertIsNotNone(launcher_version)
        self.assertEqual(server_version.group(1), dashboard_version.group(1))
        self.assertEqual(server_version.group(1), launcher_version.group(1))

    def test_grobid_ports_are_loopback_only(self) -> None:
        compose = self.read("docker-compose.grobid.yml")

        self.assertIn('"127.0.0.1:8070:8070"', compose)
        self.assertIn('"127.0.0.1:8071:8071"', compose)
        self.assertNotIn('- "8070:8070"', compose)
        self.assertNotIn('- "8071:8071"', compose)

    def test_graph_generators_use_script_safe_json(self) -> None:
        multiplex = self.read("scripts/13_build_multiplex_network.py")
        knowledge = self.read("scripts/14_build_knowledge_graph.py")

        for source in (multiplex, knowledge):
            self.assertIn("from lib.web_security import html_script_json", source)
        self.assertIn('"__NODES__": html_script_json(', multiplex)
        self.assertNotIn('"__NODES__": json.dumps(', multiplex)
        self.assertIn('"__META__": html_script_json(', knowledge)
        self.assertIn('"__EDGES__": html_script_json(', knowledge)
        self.assertNotIn('"__META__": json.dumps(', knowledge)

    def test_network_sections_are_independently_expandable(self) -> None:
        source = self.read("scripts/13_build_multiplex_network.py")

        self.assertIn("foliosort.network.openSections", source)
        self.assertNotIn("if(other!==details)other.open=false", source)

    def test_knowledge_graph_uses_progressive_local_rendering(self) -> None:
        source = self.read("assets/knowledge_graph_template.html")

        self.assertIn('id="panelToggle"', source)
        self.assertIn("function candidateEdgeIndexes()", source)
        self.assertIn("function prioritizedHiddenNeighbors(id)", source)
        self.assertIn("hideEdgesOnDrag:true", source)
        self.assertIn("hideEdgesOnZoom:true", source)
        self.assertIn("smooth:{enabled:false}", source)
        self.assertNotIn("for(const edge of compactEdges)", source)

    def test_knowledge_graph_panels_and_paper_spacing_are_available(self) -> None:
        source = self.read("assets/knowledge_graph_template.html")

        self.assertGreaterEqual(source.count("data-panel-section="), 7)
        self.assertIn('id="closeSections"', source)
        self.assertIn("function buildPaperGrid()", source)
        self.assertIn("function layoutPapers(", source)
        self.assertIn("avoidOverlap:.85", source)

    def test_home_dashboard_exposes_version_theme_and_requested_layout(self) -> None:
        source = self.read("scripts/review_app_server.py")

        self.assertIn('class="versionBadge">Version __APP_VERSION__', source)
        self.assertIn('id="themeToggle"', source)
        self.assertIn("foliosort-theme", source)
        self.assertIn("data-theme=\"light\"", source)
        self.assertIn('class="brandMark"', source)
        self.assertIn('id="projectTab"', source)
        self.assertIn('aria-selected="true">Home</button>', source)
        self.assertIn(".appTab{position:relative;width:96px", source)
        self.assertIn('id="settingsTab"', source)
        self.assertIn('id="projectView"', source)
        self.assertIn('id="settingsView"', source)
        self.assertIn("foliosort-tab", source)
        self.assertLess(source.index('class="card projectCard"'), source.index('class="card results"'))
        self.assertLess(source.index('class="card results"'), source.index('class="card pipelineCard"'))
        self.assertLess(source.index('class="card pipelineCard"'), source.index('id="settingsView"'))
        settings_start = source.index('id="settingsView"')
        settings_end = source.index("<script>", settings_start)
        settings = source[settings_start:settings_end]
        self.assertIn('class="card libraryCard"', settings)
        self.assertIn('class="card referenceCard"', settings)
        self.assertIn('class="card advancedSettingsCard"', settings)
        self.assertIn('id="curation"', settings)
        self.assertLess(settings.index('class="card libraryCard"'), settings.index('class="card referenceCard"'))
        self.assertLess(settings.index('class="card referenceCard"'), settings.index('class="card advancedSettingsCard"'))
        self.assertLess(settings.index('class="card advancedSettingsCard"'), settings.index('class="card curationSettingsCard"'))
        self.assertNotIn("Manage the shared PDF library, reference corrections, and curation tools.", settings)
        self.assertNotIn("<h1>Settings</h1>", settings)
        results_start = source.index('class="card results"')
        results_end = source.index('class="card pipelineCard"', results_start)
        self.assertNotIn('id="curation"', source[results_start:results_end])
        self.assertIn("grid-template-rows:minmax(390px,1fr) clamp(145px,20vh,185px)", source)
        self.assertIn(".pipelineCard .log{min-height:0;max-height:none;flex:1", source)
        self.assertIn("html{overflow-y:scroll;scrollbar-gutter:stable}", source)
        self.assertIn("Analyze / Update<br>Selected Project", source)
        self.assertIn(">Stop Process</button>", source)
        self.assertIn("<h2>Process log</h2>", source)
        self.assertIn("Process: checking", source)
        self.assertIn(".pipelineActions button{flex:1 1 0;min-width:0}", source)
        self.assertNotIn("Pipeline log", source)
        self.assertNotIn("Pipeline: checking", source)
        self.assertIn('id="clusterCount"', source)
        self.assertIn('id="clusterList"', source)
        self.assertIn("renderClusters(j.network_clusters)", source)
        for key in ("citation", "semantic", "claim", "property", "method", "keyword", "keyword_semantic", "bibliographic_coupling"):
            self.assertIn(f'data-network-weight="{key}"', source)
        self.assertIn('id="saveNetworkSettings"', source)
        self.assertIn('id="rebuildNetwork"', source)
        self.assertIn('parsed.path == "/api/network_settings"', source)
        self.assertIn('parsed.path == "/api/rebuild_network"', source)

    def test_network_only_rebuild_skips_full_analysis_and_uses_a_process_snapshot(self) -> None:
        server = self.read("scripts/review_app_server.py")
        rebuild = self.read("scripts/rebuild_network.sh")
        graph = self.read("scripts/13_build_multiplex_network.py")

        self.assertIn('script_name="rebuild_network.sh", kind="network"', server)
        self.assertIn("snapshot.write_bytes(source.read_bytes())", server)
        self.assertIn("process-snapshots", server)
        self.assertIn('"$NETWORK_PYTHON" -u "$ROOT/scripts/13_build_multiplex_network.py"', rebuild)
        self.assertIn("--skip-ai-cluster-naming", rebuild)
        self.assertNotIn("run_pipeline.py", rebuild)
        self.assertNotIn("04_enrich_metadata.py", rebuild)
        self.assertNotIn("14_build_knowledge_graph", rebuild)
        self.assertIn('"resolution": float(clustering_cfg.get("resolution", 1.0))', graph)
        self.assertIn('"clustering_resolution": float(clustering_cfg.get("resolution", 1.0))', graph)
        self.assertIn('"--skip-ai-cluster-naming"', graph)
        self.assertIn("not args.skip_ai_cluster_naming", graph)

    def test_light_theme_action_buttons_use_readable_low_saturation_colors(self) -> None:
        source = self.read("scripts/review_app_server.py")

        self.assertIn("--primary-bg:#e6ebf2", source)
        self.assertIn("--primary-border:#c7d0dd", source)
        self.assertIn("--primary-text:#334155", source)
        self.assertIn("--danger-bg:#f6dddd", source)
        self.assertIn("--danger-border:#d8a1a1", source)
        self.assertIn("--danger-text:#7f1d1d", source)
        self.assertIn(':root[data-theme="light"] .pill.ok{color:#166534', source)
        self.assertIn(':root[data-theme="light"] .pill.busy{color:#7c4a03', source)
        self.assertIn(
            ".primary{background:var(--primary-bg);border-color:var(--primary-border);color:var(--primary-text)}",
            source,
        )
        self.assertIn(
            ".danger{background:var(--danger-bg);border-color:var(--danger-border);color:var(--danger-text)}",
            source,
        )

    def test_windows_shortcut_uses_the_foliosort_icon(self) -> None:
        source = self.read("scripts/install_windows_app.sh")

        self.assertIn('ICON="$ROOT/assets/foliosort.ico"', source)
        self.assertIn("$s.IconLocation = $Icon + ',0'", source)
        self.assertTrue((ROOT / "assets" / "foliosort.ico").exists())
        self.assertTrue((ROOT / "assets" / "foliosort-icon.png").exists())


if __name__ == "__main__":
    unittest.main()
