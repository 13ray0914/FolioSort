from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from foliosort import __version__
from foliosort.cli import build_parser, command_init


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_version_is_single_sourced_for_package_metadata(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertEqual(__version__, "4.2.2")
        self.assertIn(f'version = "{__version__}"', pyproject)

    def test_pipeline_cli_accepts_core_stage_options(self) -> None:
        args = build_parser().parse_args(
            ["pipeline", "--from-step", "6", "--to-step", "10", "--ids", "P0001,P0002"]
        )
        self.assertEqual(args.from_step, 6)
        self.assertEqual(args.to_step, 10)
        self.assertEqual(args.ids, "P0001,P0002")

    def test_init_creates_workspace_and_preserves_config_on_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "review"
            self.assertEqual(
                command_init(Namespace(directory=str(workspace), force=False, no_openalex_prompt=True)), 0
            )
            self.assertTrue((workspace / "scripts" / "review_app_server.py").is_file())
            self.assertTrue((workspace / "run_pipeline.py").is_file())
            config_path = workspace / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["test_marker"] = "preserve-me"
            config["metadata_enrichment"]["openalex"]["api_key"] = "existing-key"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(
                command_init(Namespace(directory=str(workspace), force=True, no_openalex_prompt=True)), 0
            )
            refreshed = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(refreshed["test_marker"], "preserve-me")
            self.assertEqual(
                refreshed["metadata_enrichment"]["openalex"]["api_key"], "existing-key"
            )
            self.assertFalse((workspace / ".git").exists())

    def test_init_prompts_for_and_saves_openalex_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "review"
            args = Namespace(directory=str(workspace), force=False, no_openalex_prompt=False)
            with patch("foliosort.cli.sys.stdin.isatty", return_value=True), patch(
                "foliosort.cli.getpass.getpass", return_value="test-openalex-key"
            ):
                self.assertEqual(command_init(args), 0)
            config = json.loads((workspace / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(
                config["metadata_enrichment"]["openalex"]["api_key"], "test-openalex-key"
            )


if __name__ == "__main__":
    unittest.main()
