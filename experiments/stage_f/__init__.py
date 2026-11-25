"""Stage F — the unified 96-cell evaluation: tasks, arms, adapters, plan and gates.

Nothing in this package calls a model, opens a sandbox or consumes a claim unless a caller passes a
live `mode` to the shared dispatch entry.
"""

from __future__ import annotations

__all__ = ["ADAPTER_VERSION"]

from experiments.stage_f.arms import ADAPTER_VERSION
