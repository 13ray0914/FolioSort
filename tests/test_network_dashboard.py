from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from lib.projects import project_network_dir
from scripts.review_app_server import FolioSortApp, NETWORK_WEIGHT_DEFAULTS


class NetworkDashboardTests(unittest.TestCase):
    def test_network_settings_are_validated_and_saved_without_losing_other_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.json"
            document = {
                "openalex": {"api_key": "preserve-this-value"},
                "multiplex_graph": {"clustering": {"resolution": 1.0, "seed": 42}},
            }
            config_path.write_text(json.dumps(document), encoding="utf-8")
            app = object.__new__(FolioSortApp)
            app.config_path = config_path
            app.config = document
            app.network_settings_lock = threading.Lock()

            weights = {key: round(value + 0.05, 4) for key, value in NETWORK_WEIGHT_DEFAULTS.items()}
            saved = app.save_network_settings({"weights": weights, "resolution": 1.25})
            persisted = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual(persisted["openalex"]["api_key"], "preserve-this-value")
            self.assertEqual(persisted["multiplex_graph"]["layer_weights"], weights)
            self.assertEqual(persisted["multiplex_graph"]["clustering"]["seed"], 42)
            self.assertEqual(saved["resolution"], 1.25)

    def test_network_settings_reject_disabling_every_layer(self) -> None:
        app = object.__new__(FolioSortApp)
        app.network_settings_lock = threading.Lock()
        with self.assertRaisesRegex(ValueError, "At least one"):
            app.save_network_settings(
                {"weights": {key: 0 for key in NETWORK_WEIGHT_DEFAULTS}, "resolution": 1.0}
            )

    def test_default_cluster_summary_uses_generated_names_and_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            network_dir = project_network_dir(root, "default")
            network_dir.mkdir(parents=True, exist_ok=True)
            (network_dir / "network.json").write_text(
                json.dumps(
                    {
                        "clusters": [
                            {"cluster_id": 1, "label": "fallback two", "size": 3},
                            {"cluster_id": 0, "label": "fallback one", "size": 5},
                        ],
                        "cluster_names": {"0": {"short_name": "Named cluster"}},
                    }
                ),
                encoding="utf-8",
            )
            app = object.__new__(FolioSortApp)
            app.root = root
            app.network_summary_cache = {}

            summary = app.network_cluster_summary("default")

            self.assertTrue(summary["ready"])
            self.assertEqual(
                summary["clusters"],
                [
                    {"cluster_id": 0, "name": "Named cluster", "size": 5},
                    {"cluster_id": 1, "name": "fallback two", "size": 3},
                ],
            )


if __name__ == "__main__":
    unittest.main()
