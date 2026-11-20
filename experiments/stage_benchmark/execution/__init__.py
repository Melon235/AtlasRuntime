"""Independent per-cell execution layer for the frozen Stage B benchmark.

This package composes the existing ``run_goal`` pipeline. It has its own source digest and freeze;
it never modifies the Stage B protocol v1.1, its analyser or any historical evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from .models import Attempt, CellIdentity, CellRequest, CellResult  # noqa: E402

__all__ = ["Attempt", "CellIdentity", "CellRequest", "CellResult"]
