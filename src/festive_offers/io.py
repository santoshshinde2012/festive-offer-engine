from __future__ import annotations

import json
from pathlib import Path

from .models import BatchResult, Dataset
from .serialization import dataset_from_dict, result_artifacts

# Keep the original import path available to notebooks and existing readers.
__all__ = ["dataset_from_dict", "load_dataset", "write_result"]


def load_dataset(path: Path | str) -> Dataset:
    """Adapt a UTF-8 JSON file to the pure dataset decoder."""
    return dataset_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def write_result(result: BatchResult, output: Path) -> None:
    """Persist encoded artifacts in a new directory without overwriting a prior run."""
    artifacts = result_artifacts(result)
    output.mkdir(parents=True, exist_ok=False)
    for filename, content in artifacts.items():
        (output / filename).write_text(content, encoding="utf-8", newline="")
