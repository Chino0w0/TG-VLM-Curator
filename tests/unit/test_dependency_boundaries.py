from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_FRAMEWORKS = {"alembic", "celery", "fastapi", "httpx", "sqlalchemy", "telethon"}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class DependencyBoundaryTests(unittest.TestCase):
    def test_domain_has_no_framework_or_infrastructure_dependencies(self) -> None:
        domain_root = ROOT / "tgcurator" / "domain"
        for path in domain_root.rglob("*.py"):
            with self.subTest(path=path.relative_to(ROOT)):
                modules = imported_modules(path)
                roots = {module.split(".", 1)[0] for module in modules}
                self.assertTrue(roots.isdisjoint(FORBIDDEN_FRAMEWORKS))
                self.assertFalse(
                    any(module.startswith("tgcurator.infrastructure") for module in modules)
                )
                self.assertFalse(
                    any(module.startswith("tgcurator.application") for module in modules)
                )

    def test_application_ports_remain_framework_neutral(self) -> None:
        ports_root = ROOT / "tgcurator" / "application" / "ports"
        for path in ports_root.rglob("*.py"):
            with self.subTest(path=path.relative_to(ROOT)):
                modules = imported_modules(path)
                roots = {module.split(".", 1)[0] for module in modules}
                self.assertTrue(roots.isdisjoint(FORBIDDEN_FRAMEWORKS))
                self.assertFalse(
                    any(module.startswith("tgcurator.infrastructure") for module in modules)
                )


if __name__ == "__main__":
    unittest.main()
