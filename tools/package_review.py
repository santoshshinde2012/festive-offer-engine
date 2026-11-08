"""Package repository sources without generated databases or private configuration."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "output",
}


@dataclass(frozen=True)
class ReviewPackage:
    """Record an archive only after its CRC and every selected file have verified."""

    path: str
    file_count: int
    size_bytes: int
    sha256: str


def collect_review_files(article: Path, destination: Path) -> tuple[Path, ...]:
    """Select a reproducible source set, rejecting links that escape the article."""
    article = article.resolve()
    destination = destination.resolve()
    selected: list[Path] = []
    for path in article.rglob("*"):
        relative = path.relative_to(article)
        if EXCLUDED_DIRECTORIES.intersection(relative.parts) or not path.is_file():
            continue
        if (
            path.name.startswith(("article-review-package.zip", "source-review-package.zip"))
            or path.resolve() == destination
            or path.name == ".DS_Store"
            or path.name == ".env"
            or path.name.startswith(".env.")
            or path.name.endswith(".env")
            or path.suffix in {".pyc", ".sqlite", ".db", ".tmp"}
            or ".sqlite-" in path.name
        ):
            continue
        if not path.resolve().is_relative_to(article):
            raise ValueError(f"Review source escapes the article directory: {relative}")
        selected.append(path)
    return tuple(sorted(selected))


def package_review(article: Path, destination: Path | None = None) -> ReviewPackage:
    """Atomically replace the requested ZIP only after validating its full contents."""
    article = article.resolve()
    destination = (destination or article / "source-review-package.zip").resolve()
    if not article.is_dir() or destination.suffix != ".zip":
        raise ValueError(
            "A review package needs an existing article directory and a .zip destination"
        )
    sources = collect_review_files(article, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, suffix=".zip.tmp", delete=False
    ) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sources:
                archive.write(path, Path(article.name) / path.relative_to(article))
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("The generated review archive failed its CRC check")
            for path in sources:
                name = str(Path(article.name) / path.relative_to(article))
                if archive.read(name) != path.read_bytes():
                    raise ValueError(f"Source changed while building the review package: {name}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return ReviewPackage(
        str(destination),
        len(sources),
        destination.stat().st_size,
        hashlib.sha256(destination.read_bytes()).hexdigest(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        "--article",
        dest="source",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(asdict(package_review(args.source, args.output)), indent=2))


if __name__ == "__main__":
    main()
