from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.check_source_layout import inspect_source_layout
from tools.package_review import package_review


class ReviewPackageTests(unittest.TestCase):
    def test_default_cli_packages_its_repository_without_neighboring_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            repository = workspace / "engine"
            tools = repository / "tools"
            tools.mkdir(parents=True)
            shutil.copyfile(
                Path(__file__).resolve().parents[1] / "package_review.py",
                tools / "package_review.py",
            )
            (repository / "readme.md").write_text("# Engine\n")
            (workspace / "unrelated-private-source.txt").write_text("neighbor marker")
            subprocess.run(
                [sys.executable, str(tools / "package_review.py")],
                cwd=workspace,
                check=True,
                capture_output=True,
            )
            with zipfile.ZipFile(repository / "source-review-package.zip") as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"engine/readme.md", "engine/tools/package_review.py"},
                )

    def test_sources_and_images_round_trip_while_private_runtime_files_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary) / "article"
            files = {
                "code/src/example.py": "print('example')\n",
                "assets/diagram.svg": "<svg/>\n",
                "custom/runtime.env": "public-test-private-config-marker",
                "code/.env.local": "public-test-env-marker",
                "code/output/run/data.json": "{}",
                "code/.venv/ignored.py": "# dependency",
                "code/output/run.sqlite": "database",
            }
            for name, value in files.items():
                path = article / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            result = package_review(article)
            self.assertEqual(result.file_count, 2)
            with zipfile.ZipFile(result.path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"article/code/src/example.py", "article/assets/diagram.svg"},
                )
                self.assertEqual(archive.read("article/assets/diagram.svg"), b"<svg/>\n")

    def test_external_source_link_does_not_replace_a_previous_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            article = root / "article"
            article.mkdir()
            destination = article / "article-review-package.zip"
            destination.write_bytes(b"previous review")
            outside = root / "outside.py"
            outside.write_text("# outside source")
            (article / "linked.py").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "escapes"):
                package_review(article)
            self.assertEqual(destination.read_bytes(), b"previous review")

    def test_dependency_guard_detects_adapter_import_in_a_use_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src/offer_service.py"
            source.parent.mkdir()
            source.write_text("import sqlite3\n")
            self.assertIn("use cases must depend on ports", inspect_source_layout(root)[0])
            source.write_text("from .ports import Provider\n")
            self.assertEqual(inspect_source_layout(root), [])
