"""Small, synthetic festive offer ranking example."""

from __future__ import annotations

from .io import load_dataset
from .pipeline import run_batch
from .ranking import rank_checkout

__all__ = ["load_dataset", "rank_checkout", "run_batch"]
