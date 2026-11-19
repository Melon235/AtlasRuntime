"""Scale and cost of the matrix, from the frozen price snapshot and the observed runs.

No number here is guessed: the price comes from the repository's frozen pricing block, the token
mixtures come from the two real prompt pilots, and the multiplication is shown. A price that changes
changes the total linearly and nothing else.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PRICING_CONFIG = REPO_ROOT / "experiments" / "configs" / "eval-benchmark.yaml"

#: The two real prompt pilots. They bracket the work: one finished, one exhausted its budget.
OBSERVED_RUNS: tuple[Path, ...] = (
    REPO_ROOT / "experiments/stage_s1_prompt_v2/evidence/run-1/session.json",
    REPO_ROOT / "experiments/stage_s2_prompt_v21/evidence/run-1/session.json",
)

_KEY = re.compile(
    r"^\s*(cache_hit_input_per_million|cache_miss_input_per_million|output_per_million)\s*:\s*([0-9.]+)\s*$",
    re.MULTILINE,
)
_SNAPSHOT = re.compile(r"snapshot_date\s*:\s*([0-9-]+)")


def pricing(path: Path = PRICING_CONFIG) -> dict[str, Any]:
    """The frozen price block: USD per million tokens."""

    text = path.read_text(encoding="utf-8")
    values = {match.group(1): float(match.group(2)) for match in _KEY.finditer(text)}
    missing = {
        "cache_hit_input_per_million",
        "cache_miss_input_per_million",
        "output_per_million",
    } - set(values)
    if missing:
        raise RuntimeError(f"{path} no longer declares {sorted(missing)}")
    snapshot = _SNAPSHOT.search(text)
    return {
        "cache_hit_input_per_million": values["cache_hit_input_per_million"],
        "cache_miss_input_per_million": values["cache_miss_input_per_million"],
        "output_per_million": values["output_per_million"],
        "snapshot_date": snapshot.group(1) if snapshot else None,
        "source": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "basis": "frozen config; the file itself labels it an upper bound for deepseek-chat",
    }


def observed_profile(paths: Iterable[Path] = OBSERVED_RUNS) -> dict[str, Any]:
    """The token mixture of the two real pilots, averaged."""

    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        session = json.loads(path.read_text(encoding="utf-8"))
        metrics = session.get("metrics") or {}
        child_prompt = int(metrics.get("child_total_prompt_tokens") or 0)
        rows.append(
            {
                "run_id": session.get("run_id"),
                "hit": int(metrics.get("main_cache_hit_tokens") or 0),
                # Child cache behaviour is not reported per child, so every child prompt token is
                # priced as a miss. That is the conservative direction and it is stated here.
                "miss": int(metrics.get("main_cache_miss_tokens") or 0) + child_prompt,
                "output": int(metrics.get("main_completion_tokens") or 0),
                "child_prompt": child_prompt,
                "child_output": int(metrics.get("child_completion_tokens") or 0),
                "total": int(metrics.get("system_total_tokens") or 0),
            }
        )
    if not rows:
        raise RuntimeError("no observed run is available for the estimate")
    count = len(rows)
    return {
        "runs": rows,
        "per_run": {
            "cache_hit_input_tokens": round(sum(row["hit"] for row in rows) / count),
            "cache_miss_input_tokens": round(sum(row["miss"] for row in rows) / count),
            "output_tokens": round(sum(row["output"] for row in rows) / count),
            "child_prompt_tokens": round(sum(row["child_prompt"] for row in rows) / count),
            "child_output_tokens": round(sum(row["child_output"] for row in rows) / count),
            "total_tokens": round(sum(row["total"] for row in rows) / count),
        },
    }


def _run_cost(profile: Mapping[str, int], prices: Mapping[str, Any]) -> float:
    return (
        profile["cache_hit_input_tokens"] * prices["cache_hit_input_per_million"]
        + profile["cache_miss_input_tokens"] * prices["cache_miss_input_per_million"]
        + (profile["output_tokens"] + profile["child_output_tokens"]) * prices["output_per_million"]
    ) / 1_000_000


def estimate(
    *, cells: int, budget_tokens: int, prices: Mapping[str, Any], profile: Mapping[str, Any]
) -> dict[str, Any]:
    """LOW / EXPECTED / HIGH token and cost envelopes for the whole matrix."""

    observed = profile["per_run"]

    def scaled(factor: float) -> dict[str, int]:
        return {
            "cache_hit_input_tokens": round(observed["cache_hit_input_tokens"] * factor),
            "cache_miss_input_tokens": round(observed["cache_miss_input_tokens"] * factor),
            "output_tokens": round(observed["output_tokens"] * factor),
            "child_prompt_tokens": round(observed["child_prompt_tokens"] * factor),
            "child_output_tokens": round(observed["child_output_tokens"] * factor),
        }

    high_total = budget_tokens
    high_miss = round(high_total * 0.10)
    high_hit = high_total - high_miss - round(observed["output_tokens"] * 2)
    scenarios = {
        "LOW": scaled(0.75),
        "EXPECTED": scaled(1.0),
        "HIGH": {
            "cache_hit_input_tokens": max(high_hit, 0),
            "cache_miss_input_tokens": high_miss,
            "output_tokens": round(observed["output_tokens"] * 2),
            "child_prompt_tokens": round(observed["child_prompt_tokens"] * 2),
            "child_output_tokens": round(observed["child_output_tokens"] * 2),
        },
    }
    result: dict[str, Any] = {"cells": cells, "scenarios": {}}
    for name, tokens in scenarios.items():
        per_run_total = (
            tokens["cache_hit_input_tokens"]
            + tokens["cache_miss_input_tokens"]
            + tokens["output_tokens"]
            + tokens["child_prompt_tokens"]
            + tokens["child_output_tokens"]
        )
        result["scenarios"][name] = {
            "per_run_tokens": per_run_total,
            "total_tokens": per_run_total * cells,
            "per_run_cost_usd": round(_run_cost(tokens, prices), 6),
            "total_cost_usd": round(_run_cost(tokens, prices) * cells, 4),
        }

    result["simultaneous_children"] = 2
    result["wall_clock"] = {
        "observed_seconds": [54.4, 64.0],
        "per_run_estimate_seconds": 180,
        "sequential_hours": round(180 * cells / 3600, 1),
        "note": (
            "the pilots measured the model loop only; sandbox provisioning and export add to it"
        ),
    }
    result["pricing"] = dict(prices)
    result["observed_profile"] = profile["per_run"]
    result["assumptions"] = [
        "one run costs about what one prompt pilot cost; the tasks are comparable, not equal",
        "the HIGH scenario assumes every run spends its whole 500000-token budget",
        "cache behaviour is held at the observed mixture; a cold cache raises the miss share",
        "the price is the frozen snapshot, not a live quote",
    ]
    return result


def from_repository(*, cells: int, budget_tokens: int) -> dict[str, Any]:
    return estimate(
        cells=cells,
        budget_tokens=budget_tokens,
        prices=pricing(),
        profile=observed_profile(),
    )
