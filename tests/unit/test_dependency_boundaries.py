from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_FRAMEWORKS = {
    "PIL",
    "alembic",
    "argon2",
    "asyncpg",
    "celery",
    "cryptography",
    "fastapi",
    "httpx",
    "kombu",
    "pydantic",
    "pydantic_settings",
    "redis",
    "sqlalchemy",
    "starlette",
    "telethon",
    "uvicorn",
}
SETTINGS_ALLOWED_FRAMEWORKS = {"pydantic", "pydantic_settings"}


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
    def test_shared_has_no_framework_or_higher_layer_dependencies(self) -> None:
        shared_root = ROOT / "tgcurator" / "shared"
        for path in shared_root.rglob("*.py"):
            with self.subTest(path=path.relative_to(ROOT)):
                modules = imported_modules(path)
                roots = {module.split(".", 1)[0] for module in modules}
                self.assertTrue(roots.isdisjoint(FORBIDDEN_FRAMEWORKS))
                self.assertFalse(
                    any(
                        module.startswith(
                            (
                                "tgcurator.domain",
                                "tgcurator.application",
                                "tgcurator.infrastructure",
                                "apps",
                            )
                        )
                        for module in modules
                    )
                )

    def test_domain_has_no_framework_or_infrastructure_dependencies(self) -> None:
        domain_root = ROOT / "tgcurator" / "domain"
        for path in domain_root.rglob("*.py"):
            with self.subTest(path=path.relative_to(ROOT)):
                modules = imported_modules(path)
                roots = {module.split(".", 1)[0] for module in modules}
                self.assertTrue(roots.isdisjoint(FORBIDDEN_FRAMEWORKS))
                self.assertFalse(
                    any(
                        module.startswith(
                            ("tgcurator.infrastructure", "tgcurator.application", "apps")
                        )
                        for module in modules
                    )
                )

    def test_application_ports_remain_framework_neutral(self) -> None:
        ports_root = ROOT / "tgcurator" / "application" / "ports"
        for path in ports_root.rglob("*.py"):
            with self.subTest(path=path.relative_to(ROOT)):
                modules = imported_modules(path)
                roots = {module.split(".", 1)[0] for module in modules}
                self.assertTrue(roots.isdisjoint(FORBIDDEN_FRAMEWORKS))
                self.assertFalse(
                    any(
                        module.startswith(("tgcurator.infrastructure", "apps"))
                        for module in modules
                    )
                )

    def test_application_layer_has_no_infrastructure_or_forbidden_framework_imports(self) -> None:
        app_root = ROOT / "tgcurator" / "application"
        settings_path = app_root / "settings.py"
        for path in app_root.rglob("*.py"):
            with self.subTest(path=path.relative_to(ROOT)):
                modules = imported_modules(path)
                roots = {module.split(".", 1)[0] for module in modules}
                forbidden = (
                    FORBIDDEN_FRAMEWORKS - SETTINGS_ALLOWED_FRAMEWORKS
                    if path == settings_path
                    else FORBIDDEN_FRAMEWORKS
                )
                self.assertTrue(
                    roots.isdisjoint(forbidden), f"forbidden imports in {path}: {roots & forbidden}"
                )
                self.assertFalse(
                    any(
                        module.startswith(("tgcurator.infrastructure", "apps"))
                        for module in modules
                    )
                )


if __name__ == "__main__":
    unittest.main()
