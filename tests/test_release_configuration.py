from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseConfigurationTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_dashboard_launcher_expects_the_server_version(self) -> None:
        server = self.read("scripts/review_app_server.py")
        launcher = self.read("scripts/start_review_app.sh")
        app_version = re.search(r'^APP_VERSION = "([^"]+)"$', server, re.MULTILINE)
        expected = re.search(r'^EXPECTED_VERSION="([^"]+)"$', launcher, re.MULTILINE)

        self.assertIsNotNone(app_version)
        self.assertIsNotNone(expected)
        self.assertEqual(app_version.group(1), expected.group(1))
        self.assertTrue(app_version.group(1).startswith("4.2.2-"))

    def test_curation_clients_expect_the_health_version(self) -> None:
        server = self.read("scripts/curation_server.py")
        dashboard = self.read("scripts/review_app_server.py")
        launcher = self.read("scripts/open_curation_gui.sh")
        expected_version = "4.2.2-security-hardening"

        self.assertIn(f'"version": "{expected_version}"', server)
        self.assertIn(f'expected_version = "{expected_version}"', dashboard)
        self.assertIn(f'EXPECTED_VERSION="{expected_version}"', launcher)

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
        self.assertLess(source.index('class="card projectCard"'), source.index('class="card results"'))
        self.assertLess(source.index('class="card results"'), source.index('class="card pipelineCard"'))
        self.assertLess(source.index('class="card libraryCard"'), source.index('class="card pipelineCard"'))

    def test_light_theme_action_buttons_use_readable_low_saturation_colors(self) -> None:
        source = self.read("scripts/review_app_server.py")

        self.assertIn("--primary-bg:#e6ebf2", source)
        self.assertIn("--primary-border:#c7d0dd", source)
        self.assertIn("--primary-text:#334155", source)
        self.assertIn("--danger-bg:#c81e1e", source)
        self.assertIn("--danger-text:#fff", source)
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
