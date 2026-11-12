"""M8 production mapper contracts; no InMemory substitutes."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from tgcurator.infrastructure.database import models as m

ROOT = Path(__file__).resolve().parents[2]


class MapperContractTests(unittest.TestCase):
    def test_admin_orm_constructor_keywords_are_mapped(self):
        tree = ast.parse(
            (ROOT / "tgcurator/infrastructure/database/admin_api_repository.py").read_text(
                encoding="utf-8"
            )
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            cls = getattr(m, node.func.id, None)
            if cls is None or not hasattr(cls, "__mapper__"):
                continue
            for kw in node.keywords:
                if kw.arg is not None:
                    self.assertIn(kw.arg, cls.__mapper__.attrs, f"{cls.__name__}:{node.lineno}")
