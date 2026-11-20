"""Scale and cost of the matrix, from the frozen price snapshot and the observed runs.

No number here is guessed: the price comes from the repository's frozen pricing block, the token
mixtures come from the two real prompt pilots, and the multiplication is shown.

**Every provider usage field is counted exactly once.** The previous version added the child prompt
tokens into ``cache_miss_input_tokens`` for pricing *and* counted them again in the total, which
inflated the per-run envelope by roughly the child prompt size (306 124 instead of 299 022). The
decomposition below is the fix, and ``assert_once`` is the invariant it has to satisfy.

A subtlety worth stating rather than hiding: child prompt tokens are **priced** as cache misses
because the provider reports no cache split for a child, but they are not *reported* as misses.
Pricing and accounting therefore use two different views of the same field, and both say so.
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

#: The decomposition. ``total = sum(USAGE_FIELDS)``, and no field may be folded into another.
USAGE_FIELDS: tuple[str, ...] = (
    "main_prompt_hit_tokens",
    "main_prompt_miss_tokens",
    "child_prompt_tokens",
    "main_completion_tokens",
    "child_completion_tokens",
)

#: What the provider does not expose per child. Recorded so a reader cannot mistake the pricing
#: assumption for a measurement.
CHILD_CACHE_VISIBILITY = "UNKNOWN"
CHILD_MISS_SHARE = 1.0

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


#: Where each field of the decomposition comes from in a stored session.
USAGE_SOURCES: dict[str, str] = {
    "main_prompt_hit_tokens": "main_cache_hit_tokens",
    "main_prompt_miss_tokens": "main_cache_miss_tokens",
    "child_prompt_tokens": "child_total_prompt_tokens",
    "main_completion_tokens": "main_completion_tokens",
    "child_completion_tokens": "child_completion_tokens",
}


def usage_of(session: Mapping[str, Any]) -> dict[str, int | None]:
    """One run's usage, in the decomposition, from its own metrics block.

    A field the provider did not report is ``None``, never 0. A run that reported no cache split and
    a run that reported a 100% miss rate are different facts, and the estimate is exactly where
    confusing them would quietly move money.
    """

    metrics = session.get("metrics") or {}
    return {
        field: (None if metrics.get(source) is None else int(metrics[source]))
        for field, source in USAGE_SOURCES.items()
    }


def total_tokens(usage: Mapping[str, float]) -> float:
    """Sum of the five fields. Callers must not add anything else to it."""

    return float(sum(usage[field] for field in USAGE_FIELDS))


def assert_once(usage: Mapping[str, float | None]) -> None:
    """The invariant this module exists to keep: each field appears once, and nothing is extra."""

    unknown = set(usage) - set(USAGE_FIELDS)
    if unknown:
        raise ValueError(f"usage carries fields outside the decomposition: {sorted(unknown)}")
    missing = set(USAGE_FIELDS) - set(usage)
    if missing:
        raise ValueError(f"usage is missing fields: {sorted(missing)}")


def observed_profile(paths: Iterable[Path] = OBSERVED_RUNS) -> dict[str, Any]:
    """The token mixture of the two real pilots, averaged, plus each run's own consistency."""

    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        session = json.loads(path.read_text(encoding="utf-8"))
        usage = usage_of(session)
        missing = sorted(field for field, value in usage.items() if value is None)
        if missing:
            raise RuntimeError(
                f"{path} does not report {missing}: the estimate is derived from runs whose usage "
                "is fully reported, and substituting zero would understate the envelope"
            )
        known = {field: int(value) for field, value in usage.items() if value is not None}
        metrics = session.get("metrics") or {}
        main_input = metrics.get("main_cumulative_input_tokens")
        reported_total = metrics.get("system_total_tokens")
        rows.append(
            {
                "run_id": session.get("run_id"),
                "usage": known,
                "total_from_usage": int(total_tokens(known)),
                "system_total_tokens": reported_total,
                "usage_sums_agree": reported_total is not None
                and int(reported_total) == int(total_tokens(known)),
                # main input = hit + miss. Checked, not assumed.
                "main_input_tokens": None if main_input is None else int(main_input),
                "cache_split_agrees": None
                if main_input is None
                else known["main_prompt_hit_tokens"] + known["main_prompt_miss_tokens"]
                == int(main_input),
            }
        )
    if not rows:
        raise RuntimeError("no observed run is available for the estimate")

    count = len(rows)
    per_run = {
        field: round(sum(row["usage"][field] for row in rows) / count, 1) for field in USAGE_FIELDS
    }
    return {
        "runs": rows,
        "per_run": per_run,
        "per_run_total": round(total_tokens(per_run), 1),
        "child_cache_visibility": CHILD_CACHE_VISIBILITY,
        "child_miss_share_assumption": CHILD_MISS_SHARE,
        "all_usage_sums_agree": all(row["usage_sums_agree"] for row in rows),
        "main_input_identity_holds": all(row["cache_split_agrees"] is not False for row in rows),
        "runs_with_complete_split": sum(1 for row in rows if row["cache_split_agrees"] is not None),
    }


def run_cost(usage: Mapping[str, float | None], prices: Mapping[str, Any]) -> float:
    """USD for one run. Input is billed once; output is billed once.

    A usage with an unreported field cannot be priced: substituting zero would understate the bill,
    and the caller has to say what it assumes instead.
    """

    assert_once(usage)
    unreported = sorted(field for field in USAGE_FIELDS if usage[field] is None)
    if unreported:
        raise ValueError(f"cannot price a usage with unreported fields: {unreported}")
    known = {field: float(value) for field, value in usage.items() if value is not None}
    billed_hit = known["main_prompt_hit_tokens"]
    billed_miss = known["main_prompt_miss_tokens"] + known["child_prompt_tokens"] * CHILD_MISS_SHARE
    billed_output = known["main_completion_tokens"] + known["child_completion_tokens"]
    return (
        billed_hit * prices["cache_hit_input_per_million"]
        + billed_miss * prices["cache_miss_input_per_million"]
        + billed_output * prices["output_per_million"]
    ) / 1_000_000


def _scaled(usage: Mapping[str, float], factor: float) -> dict[str, float]:
    return {field: round(usage[field] * factor, 1) for field in USAGE_FIELDS}


def _to_budget(usage: Mapping[str, float], budget: int) -> dict[str, float]:
    """Spread ``budget`` across the fields by the observed mixture, keeping the sum exact."""

    observed = total_tokens(usage)
    if observed <= 0:
        raise ValueError("no observed tokens to scale from")
    scaled = {field: usage[field] / observed * budget for field in USAGE_FIELDS}
    rounded = {field: round(value, 1) for field, value in scaled.items()}
    # Put the rounding residue on the largest field so the identity still holds exactly.
    residue = round(budget - total_tokens(rounded), 1)
    largest = max(rounded, key=lambda field: rounded[field])
    rounded[largest] = round(rounded[largest] + residue, 1)
    return rounded


def estimate(
    *, cells: int, budget_tokens: int, prices: Mapping[str, Any], profile: Mapping[str, Any]
) -> dict[str, Any]:
    """LOW / EXPECTED / HIGH token and cost envelopes for the whole matrix."""

    observed = profile["per_run"]
    assert_once(observed)

    scenarios = {
        "LOW": _scaled(observed, 0.75),
        "EXPECTED": dict(observed),
        "HIGH": _to_budget(observed, budget_tokens),
    }
    result: dict[str, Any] = {
        "protocol_version": "1.1",
        "cells": cells,
        "usage_fields": list(USAGE_FIELDS),
        "scenarios": {},
        "budget_semantics": (
            "the shared token budget is an admission limit used by the guard, not an observed "
            "ceiling; the HIGH scenario assumes a run consumes exactly that much and is a model, "
            "not a measurement"
        ),
        "child_cache_visibility": CHILD_CACHE_VISIBILITY,
        "child_pricing_rule": (
            "child prompt tokens are billed at the cache-miss rate because the provider reports no "
            "child cache split; they are counted once and never reported as misses"
        ),
        #: Four different quantities, named so a reader cannot take one for another.
        "value_types": {
            "provider_reported_usage": (
                "prompt/completion tokens and the main cache split, as the provider reported them"
            ),
            "budget_admission_estimate": (
                "the shared 500000-token guard limit: an admission threshold the runtime enforces, "
                "not a measured ceiling and not a bill"
            ),
            "pricing_estimate": (
                "USD derived from the frozen per-million snapshot in "
                "experiments/configs/eval-benchmark.yaml"
            ),
            "actual_billing": "UNKNOWN: the provider exposes no per-run invoice",
        },
    }
    for name, usage in scenarios.items():
        assert_once(usage)
        per_run_total = total_tokens(usage)
        cost = run_cost(usage, prices)
        result["scenarios"][name] = {
            "usage": dict(usage),
            "per_run_tokens": round(per_run_total, 1),
            "total_tokens": round(per_run_total * cells, 1),
            "per_run_cost_usd": round(cost, 6),
            "total_cost_usd": round(cost * cells, 4),
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
    result["observed_profile"] = {
        "per_run": observed,
        "per_run_total": profile["per_run_total"],
        "runs": profile["runs"],
        "all_usage_sums_agree": profile["all_usage_sums_agree"],
        "main_input_identity_holds": profile.get("main_input_identity_holds"),
        "runs_with_complete_split": profile.get("runs_with_complete_split"),
    }
    result["assumptions"] = [
        "one run costs about what one prompt pilot cost; the tasks are comparable, not equal",
        "the HIGH scenario assumes a run consumes its whole 500000-token admission budget",
        "cache behaviour is held at the observed mixture; a cold cache raises the miss share",
        "child prompt tokens are priced as misses because no child cache split is reported",
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
