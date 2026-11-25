"""Closure-2 S07: the stage-wise cost estimate.

The assertions are about the *shape* of the estimate — which layer feeds which number — because the
numbers themselves are arithmetic over a frozen price list and a measured run.
"""

from __future__ import annotations

import pytest
from experiments.evaluation.closure2.candidate_budget import CANDIDATE_PROFILE
from experiments.evaluation.closure2.stage_costs import (
    CANARY_CELLS,
    FORMAL_REPETITIONS,
    MEASURED_TOKENS,
    PILOT_CELLS,
    estimate_payload,
    formal_plan_stop_usd,
)


@pytest.fixture(scope="module")
def payload() -> dict:
    return estimate_payload()


def test_the_three_stages_are_the_planned_ones(payload: dict) -> None:
    stages = payload["stages"]
    assert stages["canary"]["cells"] == len(CANARY_CELLS) == 7
    assert stages["pilot"]["cells"] == len(PILOT_CELLS) == 6
    assert stages["formal"]["cells"] == 54
    assert stages["formal"]["plans"] == FORMAL_REPETITIONS == 3
    assert stages["formal"]["cells_per_plan"] == 18


def test_the_estimate_is_anchored_to_measured_cells(payload: dict) -> None:
    assert payload["measured_source"] == "experiments/results/closure2/pilot-live-summary.json"
    assert payload["measured_run_date"] == "2026-11-18"
    assert len(MEASURED_TOKENS) == 8
    # Every canary cell either was measured or inherits a measured profile; none is invented.
    for cell_id in CANARY_CELLS:
        assert cell_id.partition("::")[2] in {key.partition("::")[2] for key in MEASURED_TOKENS}


def test_the_nominal_envelope_is_labelled_and_never_used_as_an_estimate(payload: dict) -> None:
    for stage in payload["stages"].values():
        nominal = stage["nominal_capacity"]
        expected = stage["expected"]
        assert nominal["status"] == "NOT_AN_ESTIMATE"
        assert nominal["token_budget_per_cell"] == 500_000
        assert nominal["system_total_tokens"] == stage["cells"] * 500_000
        assert expected["system_total_tokens"] < nominal["system_total_tokens"]
        assert nominal["system_total_tokens"] > 0


def test_the_pessimistic_layer_costs_more_than_the_expected_one(payload: dict) -> None:
    for stage in payload["stages"].values():
        assert stage["expected"]["estimated_cost_usd"] < stage["high"]["estimated_cost_usd"]
        assert stage["expected"]["system_total_tokens"] == stage["high"]["system_total_tokens"]


def test_the_stop_sits_between_the_pessimistic_price_and_the_envelope(payload: dict) -> None:
    for name, stage in payload["stages"].items():
        stop = stage["proposed_stop"]["usd"]
        high = stage["high"]["estimated_cost_usd"]
        nominal = stage["nominal_capacity"][
            "cost_if_every_cell_spent_the_budget_at_the_miss_price_usd"
        ]
        assert stop >= high, name
        assert stop <= nominal * 0.6, name
        assert stage["proposed_stop"]["status"] == "PROPOSED_NOT_APPROVED"


def test_the_formal_plans_share_one_per_plan_ceiling(payload: dict) -> None:
    per_plan = payload["stages"]["formal"]["proposed_stop"]["per_plan_usd"]
    assert per_plan == formal_plan_stop_usd() == 1.2
    assert payload["stages"]["formal"]["proposed_stop"]["usd"] == round(per_plan * 3, 2)


def test_the_previous_ceiling_is_not_carried_forward(payload: dict) -> None:
    totals = payload["totals"]
    assert "not carried forward" in totals["note"]
    assert totals["expected_usd"] < totals["high_usd"] <= totals["proposed_cumulative_stop_usd"]
    assert totals["proposed_cumulative_stop_usd"] != 3.40


def test_the_estimate_uses_the_candidate_profile_and_the_frozen_prices(payload: dict) -> None:
    assert payload["budget_profile"]["digest"] == CANDIDATE_PROFILE.digest()
    assert payload["budget_profile"]["cell_token_budget"] == 500_000
    assert payload["frozen_reference_profile"]["cell_token_budget"] == 260_000
    assert payload["pricing"]["cache_miss_input_per_million"] == 0.3
    assert payload["contract_problems"] == []
    assert payload["model_calls"] == 0
