"""Build hook that embeds a clean FolioSort workspace in the wheel."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


ROOT = Path(__file__).resolve().parent
WORKSPACE_DIRECTORIES = (
    "assets",
    "lib",
    "profiles",
    "prompts",
    "schemas",
    "scripts",
    "windows",
)
WORKSPACE_FILES = (
    "check_environment.py",
    "config.example.json",
    "config.v4.defaults.json",
    "config.v4_1.defaults.json",
    "docker-compose.grobid.yml",
    "network_config.json",
    "requirements.txt",
    "requirements_v4.txt",
    "requirements_network.txt",
    "requirements_network_v4.txt",
    "requirements_specter2.txt",
    "run_pipeline.py",
)


class build_py(_build_py):
    def run(self) -> None:
        super().run()
        target = Path(self.build_lib) / "foliosort" / "_workspace"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for name in WORKSPACE_DIRECTORIES:
            shutil.copytree(
                ROOT / name,
                target / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
        for name in WORKSPACE_FILES:
            shutil.copy2(ROOT / name, target / name)


setup(cmdclass={"build_py": build_py})

