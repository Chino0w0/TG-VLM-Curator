from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


class RuntimeDependencyMetadataTests(unittest.TestCase):
    def test_async_sqlalchemy_installs_required_greenlet_on_clean_environments(self):
        project = Path(__file__).resolve().parents[2] / "pyproject.toml"
        with project.open("rb") as handle:
            metadata = tomllib.load(handle)
        runtime = metadata["project"]["optional-dependencies"]["runtime"]
        sqlalchemy = next(item for item in runtime if item.lower().startswith("sqlalchemy"))
        self.assertIn("asyncio", sqlalchemy.partition("]")[0])
