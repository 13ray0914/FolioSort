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
        for relative in (
            "scripts/13_build_multiplex_network.py",
            "scripts/14_build_knowledge_graph.py",
        ):
            with self.subTest(relative=relative):
                source = self.read(relative)
                self.assertIn("from lib.web_security import html_script_json", source)
                self.assertIn('"__NODES__": html_script_json(', source)
                self.assertNotIn('"__NODES__": json.dumps(', source)


if __name__ == "__main__":
    unittest.main()
