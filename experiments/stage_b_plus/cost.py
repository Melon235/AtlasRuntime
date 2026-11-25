"""Stage B+ cost envelope, derived from S06's measured matrix and the frozen price block.

Four scenarios, each with the assumption that produced it:

``S0_s06_basis``          one B+ cell costs what one S06 cell cost. S06 never delegated, so this is
                          a *lower* bound, not a forecast.
``S1_delegated``          the same Main mixture plus child usage for the cells that delegate, at the
                          candidate 20-turn worker cap.
``S1b_delegated_6_turns`` the same shape at S06's never-exercised 6-turn cap, as a sensitivity row.
``S2_budget_fill``        every cell consumes its whole 500 000-token admission budget.

The Main mixture comes from S06's published totals; the price comes from the repository's frozen
snapshot. The only *assumed* number is the child shape, and it is stated as a range.

What this module will not do: approve a budget. Every number is a candidate for an Owner decision.
"""

from __future__ import annotations

from typing import Any

from experiments.stage_b_plus import contract
from experiments.stage_benchmark import estimate as stage_b_estimate

#: S06 published totals (execution out of repository). Each is counted once.
S06_USAGE: dict[str, float] = {
    "main_prompt_hit_tokens": 2_754_304.0,
    "main_prompt_miss_tokens": 186_553.0,
    "child_prompt_tokens": 0.0,
    "main_completion_tokens": 91_315.0,
    "child_completion_tokens": 0.0,
}
S06_CELLS = int(contract.S06_REFERENCE["cells"])
S06_REQUESTS = int(contract.S06_REFERENCE["provider_requests"])

#: Mean Main request size. The only measured anchor available for scaling a child conversation.
S06_TOKENS_PER_REQUEST = round(
    (S06_USAGE["main_prompt_hit_tokens"] + S06_USAGE["main_prompt_miss_tokens"]) / S06_REQUESTS, 1
)

#: Assumed child shape. No B+ child has ever run, so these are assumptions, not observations.
CHILD_TURNS = int(contract.BUDGET["child_max_turns"])
CHILD_FIRST_PROMPT_TOKENS = 1_200.0
CHILD_GROWTH_PER_TURN = 900.0
CHILD_COMPLETION_PER_TURN = 260.0

#: Assumed share of cells that delegate at all. S06 delegated in 0 of 36, so nothing is measured.
DELEGATED_SHARE = 0.6
CHILDREN_PER_CELL = 2


def _per_cell(usage: dict[str, float]) -> dict[str, float]:
    return {key: round(value / S06_CELLS, 1) for key, value in usage.items()}


def _profile(per_cell: dict[str, float]) -> dict[str, Any]:
    total = round(stage_b_estimate.total_tokens(per_cell), 1)
    return {
        "runs": [
            {
                "run_id": "s06-published-matrix",
                "usage": per_cell,
                "total_from_usage": total,
                "system_total_tokens": total,
                "usage_sums_agree": True,
                "main_input_tokens": round(
                    per_cell["main_prompt_hit_tokens"] + per_cell["main_prompt_miss_tokens"], 1
                ),
                "cache_split_agrees": True,
            }
        ],
        "per_run": per_cell,
        "per_run_total": total,
        "child_cache_visibility": stage_b_estimate.CHILD_CACHE_VISIBILITY,
        "child_miss_share_assumption": stage_b_estimate.CHILD_MISS_SHARE,
        "all_usage_sums_agree": True,
        "main_input_identity_holds": True,
        "runs_with_complete_split": 1,
    }


def _delegated(
    *, per_cell: dict[str, float], total_cells: int, turns: int, prices: dict[str, Any]
) -> dict[str, Any]:
    delegated_cells = round(total_cells * DELEGATED_SHARE, 2)
    prompt_per_child = sum(
        CHILD_FIRST_PROMPT_TOKENS + CHILD_GROWTH_PER_TURN * turn for turn in range(turns)
    )
    child_prompt = prompt_per_child * delegated_cells * CHILDREN_PER_CELL
    child_completion = CHILD_COMPLETION_PER_TURN * turns * delegated_cells * CHILDREN_PER_CELL
    usage = {
        **per_cell,
        "child_prompt_tokens": round(child_prompt / total_cells, 1),
        "child_completion_tokens": round(child_completion / total_cells, 1),
    }
    scenario = stage_b_estimate.estimate(
        cells=total_cells,
        budget_tokens=int(contract.BUDGET["token_budget"]),
        prices=prices,
        profile=_profile(usage),
    )["scenarios"]["EXPECTED"]
    return {
        "child_turns": turns,
        "delegated_cells": delegated_cells,
        "child_prompt_tokens_total": round(child_prompt, 1),
        "child_completion_tokens_total": round(child_completion, 1),
        "per_cell_tokens": scenario["per_run_tokens"],
        "total_tokens": scenario["total_tokens"],
        "per_cell_cost_usd": scenario["per_run_cost_usd"],
        "total_cost_usd": scenario["total_cost_usd"],
    }


def estimate(cells: int | None = None) -> dict[str, Any]:
    """The whole envelope. ``None`` uses the 24-cell candidate matrix."""

    total_cells = int(cells if cells is not None else len(contract.cells()))
    prices = stage_b_estimate.pricing()
    per_cell = _per_cell(S06_USAGE)

    base = stage_b_estimate.estimate(
        cells=total_cells,
        budget_tokens=int(contract.BUDGET["token_budget"]),
        prices=prices,
        profile=_profile(per_cell),
    )
    long_shape = _delegated(per_cell=per_cell, total_cells=total_cells, turns=CHILD_TURNS,
                            prices=prices)
    short_shape = _delegated(per_cell=per_cell, total_cells=total_cells, turns=6, prices=prices)
    turn_ratio = long_shape["total_cost_usd"] / max(short_shape["total_cost_usd"], 1e-6)

    return {
        "stage": contract.STAGE_ID,
        "cells": total_cells,
        "pricing": dict(prices),
        "anchors": {
            "s06_usage": dict(S06_USAGE),
            "s06_cells": S06_CELLS,
            "s06_provider_requests": S06_REQUESTS,
            "s06_tokens_per_request": S06_TOKENS_PER_REQUEST,
            "per_cell_from_s06": dict(per_cell),
        },
        "scenarios": {
            "S0_s06_basis": {
                "assumption": (
                    "one B+ cell costs exactly one S06 cell; S06 delegated in 0 cells, so this is "
                    "a lower bound"
                ),
                "per_cell_tokens": base["scenarios"]["EXPECTED"]["per_run_tokens"],
                "total_tokens": base["scenarios"]["EXPECTED"]["total_tokens"],
                "per_cell_cost_usd": base["scenarios"]["EXPECTED"]["per_run_cost_usd"],
                "total_cost_usd": base["scenarios"]["EXPECTED"]["total_cost_usd"],
            },
            "S1_delegated": {
                "assumption": (
                    f"{DELEGATED_SHARE:.0%} of cells delegate {CHILDREN_PER_CELL} children at "
                    f"{CHILD_TURNS} turns each; a child turn costs "
                    f"{CHILD_FIRST_PROMPT_TOKENS:g}+{CHILD_GROWTH_PER_TURN:g}x turn prompt tokens"
                ),
                **long_shape,
            },
            "S1b_delegated_6_turns": {
                "assumption": (
                    "the same delegation shape at S06's 6-turn child cap, which no S06 child ever "
                    "reached because no worker was ever created"
                ),
                **short_shape,
            },
            "S2_budget_fill": {
                "assumption": "every cell consumes its whole 500 000-token admission budget",
                "total_tokens": base["scenarios"]["HIGH"]["total_tokens"],
                "total_cost_usd": base["scenarios"]["HIGH"]["total_cost_usd"],
            },
        },
        "nominal_capacity": {
            "tokens": total_cells * int(contract.BUDGET["token_budget"]),
            "cost_usd_at_s06_cache_mixture": base["scenarios"]["HIGH"]["total_cost_usd"],
            "note": (
                "nominal capacity is the admission ceiling, not a forecast. At S06's 93.7 % "
                "cache-hit mixture it is cheap; the same tokens with child prompts priced as "
                "misses cost what S1 says, which is why S1 and S2 do not order the way their "
                "token totals do"
            ),
        },
        "dominant_variable": {
            "field": "worker turn cap",
            "evidence": (
                f"20 turns costs {turn_ratio:.1f}x the 6-turn shape: the child prompt is re-sent "
                "every turn and is priced as a cache miss"
            ),
            "decision": (
                "S06 requested 6 turns for a profile that caps at 6, so requested and effective "
                "agreed. B+ requests 20. The Owner should decide the cap on cost grounds rather "
                "than inherit it from a budget sheet"
            ),
        },
        "proposed": {
            "cumulative_cost_stop_usd": 3.0,
            "rationale": (
                "covers S1 with margin; S2 is cheaper because it assumes the S06 cache mixture "
                "over the whole budget"
            ),
            "canary_cells": 4,
            "canary_stop_usd": 0.4,
            "canary_shape": "one cell per arm on P1, repetition 1, in matrix order",
            "approved": False,
            "reuses_s06_or_s08_balance": False,
            "note": (
                "a candidate for the Owner to accept or lower; this module authorizes nothing and "
                "no S06 or S08 remainder is carried forward"
            ),
        },
        "limitations": [
            "S06 measured a no-delegation regime on different tasks; its mixture is an anchor, not "
            "a forecast",
            "the child shape is an assumption: no B+ child has ever run, and a 20-turn cap is "
            "three times S06's never-exercised 6",
            "child prompt tokens are priced as cache misses because the provider reports no child "
            "cache split",
            "the price is the repository's frozen snapshot, not a live quote",
            "whether these cells delegate at all is the largest single cost variable",
        ],
    }


__all__ = ["CHILD_TURNS", "DELEGATED_SHARE", "S06_USAGE", "estimate"]
