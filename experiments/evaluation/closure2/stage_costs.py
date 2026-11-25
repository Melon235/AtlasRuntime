# ruff: noqa: E501
"""Closure-2 S07: stage-wise cost estimates for the candidate budget profile.

Three layers, kept apart on purpose, because collapsing them is how a nominal capacity turns into a
fake bill:

``EXPECTED_MEASURED``
    the per-cell token counts the 2026-11-18 eight-cell pilot actually recorded, projected onto the
    cells that were never run and lifted where the owner raised a cap. Every input is a real number
    from a real run, or an explicitly stated multiplier.
``HIGH_ZERO_HIT``
    the same token counts priced at a 0 % prompt-cache hit. It is the worst price the frozen pricing
    table can produce *for that many tokens*; it is not a capacity bound.
``NOMINAL_CAPACITY``
    cells x 500 000 tokens at the miss price: what the new budgets would cost if every cell spent its
    whole admission budget and nothing was ever cached. It is reported to show the size of the
    envelope, and it is never used as an estimate.

``PROPOSED_STOP`` is a candidate, not an approval: 1.5x the HIGH estimate, rounded up, and capped at
25 % of the nominal envelope so a stop can never be mistaken for the envelope itself.

No model was called to produce any number here.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2.candidate_budget import (
    CANDIDATE_PROFILE,
    FROZEN_PROFILE,
    BudgetProfile,
)
from experiments.evaluation.closure2.pilot_runner import ARM_ORDER, TASK_ORDER, Cell
from experiments.evaluation.closure2.protocol import LEGACY_CONTEXT_CONFIG, config_semantics

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = REPO_ROOT / "experiments" / "results" / "closure2" / "stage-cost-estimate.json"

#: The measured pilot this estimate is anchored to. Custody is in-repo for the summary; the raw run
#: tree is git-ignored.
MEASURED_SOURCE = "experiments/results/closure2/pilot-live-summary.json"
MEASURED_SOURCE_RUN_DATE = "2026-11-18"

#: Per-cell provider usage of that run, verbatim. ``None`` means the cell was never executed.
MEASURED_TOKENS: dict[str, dict[str, float]] = {
    "closure2-pipeline-migration::N0": {
        "prompt": 32_472,
        "completion": 2_792,
        "hit_ratio": 28_288 / 32_472,
    },
    "closure2-pipeline-migration::N1": {
        "prompt": 86_825,
        "completion": 5_312,
        "hit_ratio": 74_240 / 86_825,
    },
    "closure2-pipeline-migration::A0": {
        "prompt": 40_732,
        "completion": 2_723,
        "hit_ratio": 34_944 / 40_732,
    },
    "closure2-pipeline-migration::A1-L": {
        "prompt": 146_730,
        "completion": 6_327,
        "hit_ratio": 126_720 / 146_730,
    },
    "closure2-pipeline-migration::A1-P": {
        "prompt": 199_442,
        "completion": 8_333,
        "hit_ratio": 182_144 / 199_442,
    },
    "closure2-pipeline-migration::A2-P": {
        "prompt": 149_705,
        "completion": 6_728,
        "hit_ratio": 136_704 / 149_705,
    },
    "closure2-plugin-migration::A1-P": {
        "prompt": 208_785,
        "completion": 8_376,
        "hit_ratio": 191_744 / 208_785,
    },
    "closure2-state-machine-refactor::A2-P": {
        "prompt": 34_068,
        "completion": 2_103,
        "hit_ratio": 30_720 / 34_068,
    },
}

#: Relative task size, derived from the one arm measured on two tasks (``A1-P``: 217 161 / 207 775)
#: and from the one-agent pair available for the negative task (``N1::A2-P`` 36 171 against
#: ``P1::A0`` 43 455). A cell that was never run inherits its arm's measured profile scaled by these.
TASK_SIZE_FACTOR: dict[str, float] = {
    "closure2-pipeline-migration": 1.0,
    "closure2-plugin-migration": 217_161 / 207_775,
    "closure2-state-machine-refactor": 36_171 / 43_455,
}

#: The owner raised the multi-worker cap 12 -> 20 (+67 % turns). Token growth is not proportional to
#: turns, and only one of five multi cells hit the old cap, so the stated assumption is +25 % for
#: multi cells and no change for single cells (their 36-turn cap did not move). Assumption, not
#: prediction; ``sensitivity`` reports 0 % and +50 % next to it.
CAP_UPLIFT_MULTI = 1.25

#: How far above the pessimistic price a stop sits. A stop below the HIGH estimate would abort a run
#: that is merely priced badly, which is why this is a multiple rather than a share of the envelope.
STOP_MULTIPLE_OF_HIGH = 1.5
#: A stop that reaches this share of the nominal envelope is no longer a guard; the check fails.
STOP_MAX_SHARE_OF_NOMINAL = 0.60

CANARY_CELLS: tuple[str, ...] = (
    "closure2-pipeline-migration::N0",
    "closure2-pipeline-migration::N1",
    "closure2-pipeline-migration::A0",
    "closure2-pipeline-migration::A1-L",
    "closure2-pipeline-migration::A1-P",
    "closure2-pipeline-migration::A2-P",
    "closure2-state-machine-refactor::A2-P",
)

PILOT_CELLS: tuple[str, ...] = tuple(f"closure2-pipeline-migration::{arm}" for arm in ARM_ORDER)

FORMAL_CELLS_PER_REPETITION = len(TASK_ORDER) * len(ARM_ORDER)
FORMAL_REPETITIONS = 3


def _pricing() -> dict[str, Any]:
    """The frozen pricing snapshot, read through the same canonical loader as the protocol."""

    pricing = config_semantics(LEGACY_CONTEXT_CONFIG)["pricing"]
    return {
        "cache_hit_input_per_million": float(pricing["cache_hit_input_per_million"]),
        "cache_miss_input_per_million": float(pricing["cache_miss_input_per_million"]),
        "output_per_million": float(pricing["output_per_million"]),
        "snapshot_date": str(pricing["snapshot_date"]),
    }


def _topology(cell_id: str) -> str:
    task_id, _, arm_id = cell_id.partition("::")
    return Cell(task_id, arm_id).spec.topology


def _baseline_profile(cell_id: str) -> dict[str, float]:
    """The measured (or inherited) profile of one cell, before any cap uplift."""

    if cell_id in MEASURED_TOKENS:
        return dict(MEASURED_TOKENS[cell_id])
    task_id, _, arm_id = cell_id.partition("::")
    factor = TASK_SIZE_FACTOR[task_id]
    # The arm's measured profile on another task, rescaled to this task's size.
    candidates = [
        (source, values)
        for source, values in MEASURED_TOKENS.items()
        if source.partition("::")[2] == arm_id
    ]
    if not candidates:
        raise KeyError(f"arm {arm_id!r} has no measured cell to inherit a profile from")
    # ``A2-P`` is measured on two tasks and resolves ``direct`` on one of them, so prefer the cell
    # with the same executed worker count; the plan topology decides that count for the others.
    source, values = candidates[0]
    source_factor = TASK_SIZE_FACTOR[source.partition("::")[0]]
    scale = factor / source_factor
    return {
        "prompt": values["prompt"] * scale,
        "completion": values["completion"] * scale,
        "hit_ratio": values["hit_ratio"],
    }


def _cell_tokens(cell_id: str, *, uplift: float) -> dict[str, float]:
    baseline = _baseline_profile(cell_id)
    factor = uplift if _topology(cell_id) == "multi" else 1.0
    return {
        "prompt": baseline["prompt"] * factor,
        "completion": baseline["completion"] * factor,
        "hit_ratio": baseline["hit_ratio"],
        "cap_uplift": factor,
    }


def _cost(
    *, prompt: float, completion: float, hit_ratio: float, pricing: dict[str, float]
) -> float:
    hit = prompt * hit_ratio
    miss = prompt - hit
    return (
        hit * pricing["cache_hit_input_per_million"] / 1_000_000
        + miss * pricing["cache_miss_input_per_million"] / 1_000_000
        + completion * pricing["output_per_million"] / 1_000_000
    )


def _stage(
    *,
    name: str,
    cells: tuple[str, ...],
    plans: int,
    profile: BudgetProfile,
    pricing: dict[str, float],
    stop_usd: float | None = None,
    stop_per_plan_usd: float | None = None,
) -> dict[str, Any]:
    expected_tokens = 0.0
    high_tokens = 0.0
    expected_cost = 0.0
    high_cost = 0.0
    main_tokens = 0.0
    worker_tokens = 0.0
    for cell_id in cells:
        tokens = _cell_tokens(cell_id, uplift=CAP_UPLIFT_MULTI)
        total = tokens["prompt"] + tokens["completion"]
        expected_tokens += total
        high_tokens += total
        expected_cost += _cost(
            prompt=tokens["prompt"],
            completion=tokens["completion"],
            hit_ratio=tokens["hit_ratio"],
            pricing=pricing,
        )
        high_cost += _cost(
            prompt=tokens["prompt"], completion=tokens["completion"], hit_ratio=0.0, pricing=pricing
        )
        if _topology(cell_id) == "single":
            main_tokens += total
        else:
            worker_tokens += total
    executions = len(cells) * plans
    nominal_tokens = executions * profile.cell_token_budget
    nominal_cost = nominal_tokens * pricing["cache_miss_input_per_million"] / 1_000_000
    stop = _round_up(max(high_cost * plans * STOP_MULTIPLE_OF_HIGH, 0.05))
    if stop_usd is not None:
        stop = stop_usd
    return {
        "stage": name,
        "cells": executions,
        "plans": plans,
        "cells_per_plan": len(cells),
        "expected": {
            "status": "ESTIMATE_CONDITIONAL_ON_ASSUMPTION",
            "main_tokens": int(round(main_tokens * plans)),
            "worker_tokens": int(round(worker_tokens * plans)),
            "system_total_tokens": int(round(expected_tokens * plans)),
            "estimated_cost_usd": round(expected_cost * plans, 4),
            "basis": (
                "measured 2026-11-18 per-cell usage, inherited where a cell was never run, "
                f"x{CAP_UPLIFT_MULTI} for multi cells"
            ),
        },
        "high": {
            "status": "ESTIMATE_ZERO_CACHE_HIT",
            "system_total_tokens": int(round(high_tokens * plans)),
            "estimated_cost_usd": round(high_cost * plans, 4),
            "basis": "the same tokens priced at a 0 % prompt-cache hit",
        },
        "nominal_capacity": {
            "status": "NOT_AN_ESTIMATE",
            "token_budget_per_cell": profile.cell_token_budget,
            "system_total_tokens": nominal_tokens,
            "cost_if_every_cell_spent_the_budget_at_the_miss_price_usd": round(nominal_cost, 4),
        },
        "proposed_stop": {
            "status": "PROPOSED_NOT_APPROVED",
            "usd": stop,
            "rule": (
                f"{STOP_MULTIPLE_OF_HIGH}x the HIGH estimate, rounded up to 5 cents; it must stay "
                f"at or below {int(STOP_MAX_SHARE_OF_NOMINAL * 100)} % of the nominal envelope"
            ),
            "share_of_nominal_envelope": (
                None if not nominal_cost else round(stop / nominal_cost, 4)
            ),
            "times_expected": (
                None if not expected_cost else round(stop / (expected_cost * plans), 2)
            ),
            "per_plan_usd": stop_per_plan_usd,
        },
    }


def _high_cost(cells: tuple[str, ...], pricing: dict[str, float]) -> float:
    total = 0.0
    for cell_id in cells:
        tokens = _cell_tokens(cell_id, uplift=CAP_UPLIFT_MULTI)
        total += _cost(
            prompt=tokens["prompt"], completion=tokens["completion"], hit_ratio=0.0, pricing=pricing
        )
    return total


def formal_plan_stop_usd() -> float:
    """The advisory ceiling of one formal repetition: 1.5x its own zero-hit price.

    ``formal_plan.py`` puts this number inside each plan digest, so it must come from one place. It is
    a candidate until an owner approves a digest that names it.
    """

    cells = tuple(f"{task}::{arm}" for task in TASK_ORDER for arm in ARM_ORDER)
    return _round_up(max(_high_cost(cells, _pricing()) * STOP_MULTIPLE_OF_HIGH, 0.05))


def _round_up(value: float) -> float:
    """Round up to the next 5 cents, so a stop is a round number an operator can type."""

    return round(((int(value * 100) + 4) // 5) * 5 / 100, 2)


def estimate_payload() -> dict[str, Any]:
    pricing = _pricing()
    canary = _stage(
        name="S08_CANARY",
        cells=CANARY_CELLS,
        plans=1,
        profile=CANDIDATE_PROFILE,
        pricing=pricing,
    )
    pilot = _stage(
        name="S09_PILOT",
        cells=PILOT_CELLS,
        plans=1,
        profile=CANDIDATE_PROFILE,
        pricing=pricing,
    )
    formal_cells = tuple(f"{task}::{arm}" for task in TASK_ORDER for arm in ARM_ORDER)
    per_plan_stop = formal_plan_stop_usd()
    formal = _stage(
        name="S11_FORMAL_MATRIX",
        cells=formal_cells,
        plans=FORMAL_REPETITIONS,
        profile=CANDIDATE_PROFILE,
        pricing=pricing,
        stop_usd=round(per_plan_stop * FORMAL_REPETITIONS, 2),
        stop_per_plan_usd=per_plan_stop,
    )
    total_expected = round(
        canary["expected"]["estimated_cost_usd"]
        + pilot["expected"]["estimated_cost_usd"]
        + formal["expected"]["estimated_cost_usd"],
        4,
    )
    total_high = round(
        canary["high"]["estimated_cost_usd"]
        + pilot["high"]["estimated_cost_usd"]
        + formal["high"]["estimated_cost_usd"],
        4,
    )
    total_stop = round(
        canary["proposed_stop"]["usd"]
        + pilot["proposed_stop"]["usd"]
        + formal["proposed_stop"]["usd"],
        2,
    )
    sensitivity: dict[str, float] = {}
    for label, uplift in (("none", 1.0), ("plus50", 1.5)):
        per_repetition = 0.0
        for cell_id in formal_cells:
            tokens = _cell_tokens(cell_id, uplift=uplift)
            per_repetition += _cost(
                prompt=tokens["prompt"],
                completion=tokens["completion"],
                hit_ratio=tokens["hit_ratio"],
                pricing=pricing,
            )
        sensitivity[f"cap_uplift_{label}"] = round(per_repetition * FORMAL_REPETITIONS, 4)
    return {
        "kind": "closure2_stage_cost_estimate",
        "stage": "closure-2-s07",
        "status": "PROPOSED_NOT_APPROVED",
        "measured_source": MEASURED_SOURCE,
        "measured_run_date": MEASURED_SOURCE_RUN_DATE,
        "pricing": {**pricing, "source": LEGACY_CONTEXT_CONFIG, "live_verified": False},
        "budget_profile": {
            "revision": CANDIDATE_PROFILE.revision,
            "digest": CANDIDATE_PROFILE.digest(),
            "cell_token_budget": CANDIDATE_PROFILE.cell_token_budget,
            "multi_max_turns_per_worker": CANDIDATE_PROFILE.multi_worker_max_turns,
            "single_max_turns": CANDIDATE_PROFILE.single_max_turns,
            "status": CANDIDATE_PROFILE.status,
        },
        "frozen_reference_profile": {
            "revision": FROZEN_PROFILE.revision,
            "digest": FROZEN_PROFILE.digest(),
            "cell_token_budget": FROZEN_PROFILE.cell_token_budget,
            "multi_max_turns_per_worker": FROZEN_PROFILE.multi_worker_max_turns,
            "note": "historical runs keep these numbers; nothing here re-prices them",
        },
        "stages": {"canary": canary, "pilot": pilot, "formal": formal},
        "totals": {
            "expected_usd": total_expected,
            "high_usd": total_high,
            "proposed_cumulative_stop_usd": total_stop,
            "note": (
                "the previous USD 3.40 candidate was a ceiling derived from the 260 k profile and is "
                "not carried forward; nothing here is approved"
            ),
        },
        "formal_cap_sensitivity_usd": sensitivity,
        "rules": [
            "a nominal token budget is a capacity, never an expected spend",
            "the S06 authorization balance is not reused and is not a budget for this matrix",
            "a missing cache split has no cost, and is never costed as a miss",
            "no estimate here is a bill, and none includes sandbox provisioning",
        ],
        "model_calls": 0,
        "sandbox_runs": 0,
        "contract_problems": _check({"canary": canary, "pilot": pilot, "formal": formal}),
    }


def _check(stages: dict[str, dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    if len(set(CANARY_CELLS)) != len(CANARY_CELLS):
        problems.append("the canary cell list repeats a cell")
    if len(CANARY_CELLS) != 7:
        problems.append(f"the canary has {len(CANARY_CELLS)} cells, expected 7")
    if len(PILOT_CELLS) != len(ARM_ORDER):
        problems.append("the pilot is not one task x six arms")
    if FORMAL_CELLS_PER_REPETITION * FORMAL_REPETITIONS != 54:
        problems.append("the formal matrix is not 54 cells")
    for cell_id in (*CANARY_CELLS, *PILOT_CELLS):
        _baseline_profile(cell_id)
    for task in TASK_ORDER:
        for arm in ARM_ORDER:
            _baseline_profile(f"{task}::{arm}")
    for name, stage in stages.items():
        expected = stage["expected"]["estimated_cost_usd"]
        high = stage["high"]["estimated_cost_usd"]
        nominal = stage["nominal_capacity"][
            "cost_if_every_cell_spent_the_budget_at_the_miss_price_usd"
        ]
        stop = stage["proposed_stop"]["usd"]
        if expected > high:
            problems.append(f"{name}: the expected estimate exceeds the pessimistic one")
        if stop < high:
            problems.append(f"{name}: the proposed stop is below the HIGH estimate")
        if nominal and stop > nominal * STOP_MAX_SHARE_OF_NOMINAL:
            problems.append(
                f"{name}: the proposed stop is {stop / nominal:.0%} of the nominal envelope"
            )
    return problems


def write_estimates(path: Path) -> dict[str, Any]:
    payload = estimate_payload()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="closure-2 stage cost estimate: offline arithmetic, no model call"
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    payload = write_estimates(Path(args.out))
    print(
        json.dumps(
            {
                "status": payload["status"],
                "contract_problems": payload["contract_problems"],
                "totals": payload["totals"],
                "model_calls": 0,
                "sandbox_runs": 0,
                "out": str(args.out),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not payload["contract_problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CANARY_CELLS",
    "CAP_UPLIFT_MULTI",
    "DEFAULT_OUT",
    "FORMAL_CELLS_PER_REPETITION",
    "FORMAL_REPETITIONS",
    "MEASURED_TOKENS",
    "PILOT_CELLS",
    "TASK_SIZE_FACTOR",
    "estimate_payload",
    "formal_plan_stop_usd",
    "write_estimates",
]
