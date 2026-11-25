"""Stage B+ cost envelope, per scope, with the sensitivities that actually move it.

Four base scenarios per scope, each carrying the assumption that produced it:

``S0_reference``          one B+ cell costs what one S06 cell cost. S06 never delegated, so this
                          *reference scenario*, not a mathematical lower bound: a B+ cell has a
                          different task, a different worker turn cap and a different prompt.
``S1b_delegated_6_turns`` the same Main mixture plus child usage at S06's 6-turn cap.
``S1_delegated``          the same shape at the candidate 20-turn cap.
``S2_budget_fill``        every cell consumes its whole admission budget, priced at S06's cache
                          mixture. This is *not* an absolute HIGH: with a cold cache the same tokens
                          cost more, and the sensitivity rows say how much.

The price comes from the repository's frozen snapshot. The only *assumed* numbers are the child
shape and the delegation share, and both are varied rather than asserted.

What this module will not do: approve a budget. Every number is a candidate for an Owner decision,
and neither the S06 nor the S08 remainder is carried forward.
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
#: S06's measured cache-hit share. Used as the optimistic end of the sensitivity range.
S06_HIT_RATIO = round(
    S06_USAGE["main_prompt_hit_tokens"]
    / (S06_USAGE["main_prompt_hit_tokens"] + S06_USAGE["main_prompt_miss_tokens"]),
    4,
)

#: Assumed child shape. No B+ child has ever run, so these are assumptions, not observations.
CHILD_TURNS = int(contract.BUDGET["child_max_turns"])
CHILD_FIRST_PROMPT_TOKENS = 1_200.0
CHILD_GROWTH_PER_TURN = 900.0
CHILD_COMPLETION_PER_TURN = 260.0

#: Assumed share of cells that delegate at all. S06 delegated in 0 of 36, so nothing is measured.
DELEGATED_SHARE = 0.6
CHILDREN_PER_CELL = 2

SENSITIVITY: tuple[dict[str, Any], ...] = (
    {
        "id": "low_cache_hit",
        "assumption": "the Main's cache-hit share falls from S06's 93.7 % to 50 %",
        "main_hit_ratio": 0.50,
    },
    {
        "id": "child_prompt_all_miss",
        "assumption": "every child prompt token is a miss (the base assumption, stated alone)",
        "main_hit_ratio": S06_HIT_RATIO,
    },
    {
        "id": "high_output_share",
        "assumption": "completion tokens are 1.6x the base estimate",
        "output_multiplier": 1.6,
    },
    {
        "id": "higher_delegation_share",
        "assumption": "90 % of cells delegate two children instead of 60 %",
        "delegated_share": 0.9,
    },
    {
        "id": "worker_near_cap",
        "assumption": "children run to the 20-turn cap in every delegating cell",
        "child_turns": CHILD_TURNS,
        "force_full_turns": True,
    },
)


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


def _with_usage(
    *,
    cells: int,
    prices: dict[str, Any],
    main_hit_ratio: float | None = None,
    output_multiplier: float = 1.0,
    child_turns: int | None = None,
    delegated_share: float = DELEGATED_SHARE,
    force_full_turns: bool = False,
    child_scale: float = 1.0,
) -> dict[str, Any]:
    """Price one usage mixture. ``child_scale`` multiplies child tokens for a sensitivity row."""

    base = _per_cell(S06_USAGE)
    main_input = base["main_prompt_hit_tokens"] + base["main_prompt_miss_tokens"]
    ratio = S06_HIT_RATIO if main_hit_ratio is None else float(main_hit_ratio)
    usage = {
        "main_prompt_hit_tokens": round(main_input * ratio, 1),
        "main_prompt_miss_tokens": round(main_input * (1.0 - ratio), 1),
        "main_completion_tokens": round(base["main_completion_tokens"] * output_multiplier, 1),
        "child_prompt_tokens": 0.0,
        "child_completion_tokens": 0.0,
    }

    turns = child_turns if child_turns is not None else CHILD_TURNS
    share = 1.0 if force_full_turns else float(delegated_share)
    prompt_per_child = sum(
        CHILD_FIRST_PROMPT_TOKENS + CHILD_GROWTH_PER_TURN * turn for turn in range(turns)
    )
    usage["child_prompt_tokens"] = round(
        prompt_per_child * share * CHILDREN_PER_CELL * child_scale, 1
    )
    usage["child_completion_tokens"] = round(
        CHILD_COMPLETION_PER_TURN * turns * share * CHILDREN_PER_CELL * child_scale, 1
    )

    scenario = stage_b_estimate.estimate(
        cells=cells,
        budget_tokens=int(contract.BUDGET["token_budget"]),
        prices=prices,
        profile=_profile(usage),
    )
    expected = scenario["scenarios"]["EXPECTED"]
    high = scenario["scenarios"]["HIGH"]
    return {
        "per_cell_tokens": expected["per_run_tokens"],
        "total_tokens": expected["total_tokens"],
        "per_cell_cost_usd": expected["per_run_cost_usd"],
        "total_cost_usd": expected["total_cost_usd"],
        "budget_filled_cost_usd": high["total_cost_usd"],
        "main_hit_ratio": round(ratio, 4),
        "child_turns": turns,
        "delegated_share": share,
        "usage_per_cell": usage,
    }


def scope_estimate(
    plan: dict[str, Any], *, prices: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The envelope for one scope: six cells for the canary, twenty-four for the formal matrix."""

    price = dict(prices or stage_b_estimate.pricing())
    cells = int(plan["cells_count"])
    reference = _with_usage(cells=cells, prices=price, force_full_turns=True, child_scale=0.0,
                            output_multiplier=1.0)
    short = _with_usage(cells=cells, prices=price, child_turns=6)
    long_shape = _with_usage(cells=cells, prices=price)
    sensitivity = []
    for row in SENSITIVITY:
        result = _with_usage(
            cells=cells,
            prices=price,
            main_hit_ratio=row.get("main_hit_ratio"),
            output_multiplier=float(row.get("output_multiplier", 1.0)),
            child_turns=row.get("child_turns"),
            delegated_share=float(row.get("delegated_share", DELEGATED_SHARE)),
            force_full_turns=bool(row.get("force_full_turns", False)),
        )
        sensitivity.append(
            {
                "id": row["id"],
                "assumption": row["assumption"],
                "total_tokens": result["total_tokens"],
                "total_cost_usd": result["total_cost_usd"],
            }
        )
    turn_ratio = long_shape["total_cost_usd"] / max(short["total_cost_usd"], 1e-6)
    return {
        "plan_id": plan["plan_id"],
        "scope": plan["scope"],
        "cells": cells,
        "nominal_capacity": {
            "tokens": int(plan["nominal_token_capacity"]),
            "cost_usd_at_s06_cache_mixture": long_shape["budget_filled_cost_usd"],
            "note": "nominal capacity is the admission ceiling, not a forecast",
        },
        "scenarios": {
            "S0_reference": {
                "assumption": "one B+ cell costs exactly one S06 cell; a reference scenario, not a "
                "lower bound (different task, different turn cap, different prompt)",
                "total_tokens": reference["total_tokens"],
                "total_cost_usd": reference["total_cost_usd"],
            },
            "S1b_delegated_6_turns": {
                "assumption": (
                    f"{DELEGATED_SHARE:.0%} of cells delegate {CHILDREN_PER_CELL} children at 6 "
                ),
                **short,
            },
            "S1_delegated": {
                "assumption": (
                    f"{DELEGATED_SHARE:.0%} of cells delegate {CHILDREN_PER_CELL} children at "
                    f"{CHILD_TURNS} turns; a child turn costs "
                    f"{CHILD_FIRST_PROMPT_TOKENS:g}+{CHILD_GROWTH_PER_TURN:g}x turn prompt tokens"
                ),
                **long_shape,
            },
            "S2_budget_fill": {
                "assumption": "every cell consumes its whole 500 000-token budget, priced at S06's "
                "measured cache mixture; not an absolute HIGH",
                "total_tokens": int(plan["nominal_token_capacity"]),
                "total_cost_usd": reference["budget_filled_cost_usd"],
            },
        },
        "sensitivity": sensitivity,
        "range": {
            "low_usd": min(
                reference["total_cost_usd"],
                short["total_cost_usd"],
                long_shape["total_cost_usd"],
                reference["budget_filled_cost_usd"],
                *(row["total_cost_usd"] for row in sensitivity),
            ),
            "high_usd": max(
                reference["total_cost_usd"],
                short["total_cost_usd"],
                long_shape["total_cost_usd"],
                reference["budget_filled_cost_usd"],
                *(row["total_cost_usd"] for row in sensitivity),
            ),
        },
        "dominant_variable": {
            "field": "delegation rate x worker turn cap",
            "evidence": (
                f"20 turns costs {turn_ratio:.1f}x the 6-turn shape; the child prompt is priced "
                "as a cache miss because the provider reports no child cache split"
            ),
        },
    }


def estimate() -> dict[str, Any]:
    prices = stage_b_estimate.pricing()
    canary = scope_estimate(contract.scope_plan(contract.SCOPE_CANARY), prices=prices)
    formal = scope_estimate(contract.scope_plan(contract.SCOPE_FORMAL), prices=prices)
    return {
        "stage": contract.STAGE_ID,
        "protocol_version": contract.PROTOCOL_VERSION,
        "pricing": dict(prices),
        "anchors": {
            "s06_usage": dict(S06_USAGE),
            "s06_cells": S06_CELLS,
            "s06_provider_requests": S06_REQUESTS,
            "s06_tokens_per_request": S06_TOKENS_PER_REQUEST,
            "s06_hit_ratio": S06_HIT_RATIO,
            "child_shape": {
                "first_prompt_tokens": CHILD_FIRST_PROMPT_TOKENS,
                "growth_per_turn": CHILD_GROWTH_PER_TURN,
                "completion_per_turn": CHILD_COMPLETION_PER_TURN,
                "children_per_delegating_cell": CHILDREN_PER_CELL,
                "delegated_share": DELEGATED_SHARE,
                "measured": False,
            },
        },
        "scopes": {canary["plan_id"]: canary, formal["plan_id"]: formal},
        "proposed": {
            "canary": {
                "plan_id": canary["plan_id"],
                "scope": canary["scope"],
                "cells": canary["cells"],
                "cost_stop_usd": 0.60,
                "per_cell_cost_stop_usd": 0.15,
                "token_stop": canary["nominal_capacity"]["tokens"],
                "basis": "covers S1 (0.50) and the low-cache-hit row (0.56) with a little room; "
                "replaces the withdrawn 0.40 figure for the four-cell shape",
                "approved": False,
            },
            "formal": {
                "plan_id": formal["plan_id"],
                "scope": formal["scope"],
                "cells": formal["cells"],
                "cost_stop_usd": 3.0,
                "per_cell_cost_stop_usd": 0.25,
                "token_stop": formal["nominal_capacity"]["tokens"],
                "basis": "still a candidate; it covers S1 with margin and does not cover the "
                "low_cache_hit sensitivity with margin, which is why the stop is a stop",
                "approved": False,
            },
            "reuses_s06_or_s08_balance": False,
            "note": "no value here is an approval; the canary and the matrix are separate scopes",
        },
        "limitations": [
            "S06 measured a no-delegation regime on different tasks; its mixture is an anchor",
            "the child shape is an assumption: no B+ child has ever run",
            "child prompt tokens are priced as misses because the provider reports no child split",
            "the price is the repository's frozen snapshot, not a live quote",
            "whether these cells delegate at all is the largest single cost variable",
        ],
    }


__all__ = [
    "CHILD_TURNS",
    "DELEGATED_SHARE",
    "S06_USAGE",
    "SENSITIVITY",
    "estimate",
    "scope_estimate",
]
