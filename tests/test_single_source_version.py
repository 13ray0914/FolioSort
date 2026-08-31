"""Tests that every version string in the project derives from the single
source of truth: ``__version__`` in ``foliosort/__init__.py``.

Running these tests after a version bump immediately catches any file
that was left with an old hardcoded string.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from foliosort import __version__

ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class SingleSourceVersionTests(unittest.TestCase):
    # ------------------------------------------------------------------ #
    # Package metadata                                                     #
    # ------------------------------------------------------------------ #

    def test_init_exposes_version(self) -> None:
        """foliosort/__init__.py is the canonical version store."""
        source = read("foliosort/__init__.py")
        self.assertIn(f'__version__ = "{__version__}"', source)

    def test_pyproject_uses_dynamic_version_not_literal(self) -> None:
        """pyproject.toml must not hardcode the version — it must be dynamic."""
        pyproject = read("pyproject.toml")
        self.assertIn('dynamic = ["version"]', pyproject)
        self.assertIn('version = {attr = "foliosort.__version__"}', pyproject)
        # Must NOT contain a frozen literal like version = "4.3.1"
        self.assertNotRegex(pyproject, r'\bversion\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+"')

    # ------------------------------------------------------------------ #
    # Python scripts — must derive from __version__, not hardcode         #
    # ------------------------------------------------------------------ #

    def test_review_app_server_app_version_is_dynamic(self) -> None:
        source = read("scripts/review_app_server.py")
        self.assertIn("from foliosort import __version__", source)
        self.assertIn('APP_VERSION = f"{__version__}-', source)
        self.assertNotRegex(source, r'APP_VERSION\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+-')

    def test_review_app_server_expected_version_is_dynamic(self) -> None:
        source = read("scripts/review_app_server.py")
        self.assertIn('expected_version = f"{__version__}-security-hardening"', source)
        self.assertNotRegex(source, r'expected_version\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+-')

    def test_curation_server_server_version_is_dynamic(self) -> None:
        source = read("scripts/curation_server.py")
        self.assertIn("from foliosort import __version__", source)
        self.assertIn('server_version = f"LiteratureCuration/{__version__}"', source)
        self.assertNotRegex(source, r'server_version\s*=\s*"LiteratureCuration/[0-9]+')

    def test_curation_server_health_version_is_dynamic(self) -> None:
        source = read("scripts/curation_server.py")
        self.assertIn('"version": f"{__version__}-security-hardening"', source)
        self.assertNotRegex(source, r'"version"\s*:\s*"[0-9]+\.[0-9]+\.[0-9]+-')

    def test_multiplex_network_script_version_is_dynamic(self) -> None:
        source = read("scripts/13_build_multiplex_network.py")
        self.assertIn("from foliosort import __version__", source)
        self.assertIn('SCRIPT_VERSION = f"multiplex-network-v{__version__}-', source)
        self.assertNotRegex(source, r'SCRIPT_VERSION\s*=\s*"multiplex-network-v[0-9]+')

    def test_knowledge_graph_script_version_is_dynamic(self) -> None:
        source = read("scripts/14_build_knowledge_graph.py")
        self.assertIn("from foliosort import __version__", source)
        self.assertIn('SCRIPT_VERSION = f"knowledge-graph-v{__version__}-', source)
        self.assertNotRegex(source, r'SCRIPT_VERSION\s*=\s*"knowledge-graph-v[0-9]+')

    def test_recluster_script_version_is_dynamic(self) -> None:
        source = read("scripts/15_recluster_network.py")
        self.assertIn("from foliosort import __version__", source)
        self.assertIn('SCRIPT_VERSION = f"network-recluster-v{__version__}-', source)
        self.assertNotRegex(source, r'SCRIPT_VERSION\s*=\s*"network-recluster-v[0-9]+')

    # ------------------------------------------------------------------ #
    # Shell scripts — must derive version from foliosort at runtime       #
    # ------------------------------------------------------------------ #

    def test_start_review_app_expected_version_is_dynamic(self) -> None:
        source = read("scripts/start_review_app.sh")
        self.assertIn("from foliosort import __version__", source)
        self.assertIn("${_BASE_VERSION}-security-hardened-network-workspace", source)
        self.assertNotRegex(source, r'EXPECTED_VERSION="[0-9]+\.[0-9]+\.[0-9]+-')

    def test_open_curation_gui_expected_version_is_dynamic(self) -> None:
        source = read("scripts/open_curation_gui.sh")
        self.assertIn("from foliosort import __version__", source)
        self.assertIn("${_BASE_VERSION}-security-hardening", source)
        self.assertNotRegex(source, r'EXPECTED_VERSION="[0-9]+\.[0-9]+\.[0-9]+-')

    def test_pipeline_script_banners_are_dynamic(self) -> None:
        source = read("scripts/run_review_pipeline.sh")
        self.assertIn("from foliosort import __version__", source)
        self.assertIn("_FS_VERSION=", source)
        self.assertIn("${_FS_VERSION}", source)
        self.assertNotRegex(source, r'echo.*v[0-9]+\.[0-9]+\.[0-9]+')


    # ------------------------------------------------------------------ #
    # Regression: no stray hardcoded version literals                     #
    # ------------------------------------------------------------------ #

    def test_no_stale_version_literals_in_python_scripts(self) -> None:
        """Tracked scripts must not contain a frozen x.y.z version literal
        that would become stale after a version bump."""
        checked = [
            "scripts/review_app_server.py",
            "scripts/curation_server.py",
            "scripts/13_build_multiplex_network.py",
            "scripts/14_build_knowledge_graph.py",
            "scripts/15_recluster_network.py",
        ]
        pattern = re.compile(r'"' + re.escape(__version__) + r'"')
        for path in checked:
            source = read(path)
            bare = [
                m for m in pattern.finditer(source)
                if "__version__" not in source[max(0, m.start() - 25):m.start()]
            ]
            self.assertEqual(
                bare, [],
                msg=f"{path} still contains a frozen '{__version__}' literal",
            )

    def test_no_stale_version_literals_in_shell_scripts(self) -> None:
        checked = [
            "scripts/start_review_app.sh",
            "scripts/open_curation_gui.sh",
            "scripts/run_review_pipeline.sh",
        ]
        for path in checked:
            source = read(path)
            self.assertNotRegex(
                source,
                r'"' + re.escape(__version__) + r'"',
                msg=f"{path} still contains a frozen '{__version__}' literal",
            )


if __name__ == "__main__":
    unittest.main()
