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
        self.assertTrue(app_version.group(1).startswith("4.2.0-"))

    def test_curation_clients_expect_the_health_version(self) -> None:
        server = self.read("scripts/curation_server.py")
        dashboard = self.read("scripts/review_app_server.py")
        launcher = self.read("scripts/open_curation_gui.sh")
        expected_version = "4.2.0-security-hardening"

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


if __name__ == "__main__":
    unittest.main()
