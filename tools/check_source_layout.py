"""Check module naming and prevent database/transport dependencies in domain use cases."""

from __future__ import annotations

import ast
import re
from pathlib import Path

MODULE_NAME = re.compile(r"[a-z_][a-z0-9_]*\.py")
FORBIDDEN_IMPORTS = {
    "sqlite3",
    "psycopg",
    "urllib",
    "requests",
    "http_provider",
    "store",
    "provider",
    "sqlite_store",
    "postgres_store",
    "sqlite_repository",
    "postgres_repository",
    "fake_provider",
    "runtime_factory",
    "runtime_cli",
    "security",
    "authentication",
    "api",
    "api_access",
    "api_routes",
}


def inspect_source_layout(root: Path) -> list[str]:
    """Keep actual adapter selection in composition, and identifiers consistent."""
    findings: list[str] = []
    for directory in ("src", "tests", "tools", "notebooks"):
        for source in sorted((root / directory).rglob("*.py")):
            if "__pycache__" in source.parts:
                continue
            relative = source.relative_to(root)
            if not MODULE_NAME.fullmatch(source.name):
                findings.append(f"{relative}: Python modules must use snake_case names")
            if directory != "src":
                continue
            protected = source.name.endswith("_service.py") or source.name in {
                "ranking.py",
                "pipeline.py",
                "policy.py",
                "validation.py",
            }
            if not protected:
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                for module in modules:
                    if FORBIDDEN_IMPORTS.intersection(module.split(".")):
                        findings.append(
                            f"{relative}:{node.lineno}: use cases must depend on ports: {module}"
                        )
    return findings


def main() -> None:
    findings = inspect_source_layout(Path(__file__).resolve().parents[1])
    if findings:
        raise SystemExit("\n".join(findings))
    print("Source names and domain dependency boundaries: passed")


if __name__ == "__main__":
    main()
