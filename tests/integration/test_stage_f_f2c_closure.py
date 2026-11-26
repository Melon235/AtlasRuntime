"""Stage F F2-C: production measurement, common integrity and the cross-batch guard.

Each test names the defect it would have caught. Nothing here calls a model, opens a sandbox or
consumes a production claim: the cell runner is the offline synthetic one and every claim lives in a
throwaway override root.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from experiments.stage_b_plus import contract, executor
from experiments.stage_f import cumulative, freeze, normalize, plan

REPO = Path(__file__).resolve().parents[2]


def _cell(arm_id: str) -> dict:
    return next(cell for cell in plan.batch_cells(1) if cell["arm_id"] == arm_id)


def _session(*, main_peak_series: list[int], children: list[str]) -> dict:
    return {
        "run_id": "run-x",
        "status": "succeeded",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "main": {"task_id": "run-x-main", "success": True, "stop_reason": None},
        "metrics": {
            "main_requests": len(main_peak_series),
            "main_peak_context_tokens": max(main_peak_series),
            "main_cumulative_input_tokens": sum(main_peak_series),
            "main_context_tokens_by_request": main_peak_series,
            "main_cache_hit_tokens": 0,
            "main_cache_miss_tokens": sum(main_peak_series),
            "main_completion_tokens": 10,
            "child_total_prompt_tokens": 900 if children else 0,
            "child_completion_tokens": 90 if children else 0,
            "system_total_tokens": sum(main_peak_series) + 10 + (990 if children else 0),
        },
        "delegation": {
            "totals": {"cache_hit_tokens": 0, "cache_miss_tokens": 900},
            "batches": [
                {
                    "round": 1,
                    "route": "delegate",
                    "decision": {"reason_codes": [], "blocked_gates": []},
                    "children": [
                        {"key": key, "status": "completed", "success": True} for key in children
                    ],
                }
            ]
            if children
            else [],
        },
        "budget": {"limit_tokens": 1_000_000, "used_tokens": 1000},
        "public_tests": {"runs": []},
        "verification": {"verdict": "PASS"},
        "execution_integrity": {"mode": "record", "records": 3, "write_failures": 0},
        "sandbox": {"sandbox_id": "sbx", "destroyed": True},
        "handoff_reads": {},
    }


def test_the_main_per_request_series_is_the_production_measurement() -> None:
    """The arm without children has a complete census; the arm with children does not."""

    direct = normalize.from_main_child_session(
        _cell("A1"),
        _session(main_peak_series=[1000, 2500, 4000], children=[]),
        oracle={"status": "PASS", "passed": True},
        export={"workspace_path": None},
    )
    assert direct["context"]["per_request_input_tokens"] == [1000, 2500, 4000]
    assert direct["context"]["system_max_agent_context"] == 4000
    assert direct["usage"]["requests"] == {"main": 3, "children": "NOT_APPLICABLE", "total": 3}

    delegated = normalize.from_main_child_session(
        _cell("A3"),
        _session(main_peak_series=[1000, 2500, 4000], children=["csv-unit"]),
        oracle={"status": "PASS", "passed": True},
        export={"workspace_path": None},
    )
    # A child peak is not recorded by the delegation evidence: the Main peak must not stand in.
    assert delegated["context"]["system_max_agent_context"] == normalize.UNKNOWN
    assert delegated["context"]["agent_context_census"]["missing_agents"] == ["csv-unit"]
    assert delegated["usage"]["requests"]["children"] == normalize.UNKNOWN
    assert delegated["usage"]["requests"]["total"] is None


def test_delivered_success_is_the_integrity_conjunction() -> None:
    """A0 used to report delivered success with integrity NOT_APPLICABLE."""

    def probe(verdict: str) -> bool:
        evidence = {
            "correctness": {
                "agent_success": True,
                "independent_oracle": {"status": "PASS", "passed": True},
                "public_test": "PASS",
            }
        }
        normalize.apply_integrity(evidence, {"verdict": verdict, "checks": [{"check": "p"}]})
        return evidence["correctness"]["delivered_success"]

    assert probe(normalize.PASS) is True
    assert probe(normalize.UNKNOWN) is False
    assert probe(normalize.FAIL) is False


async def _dispatch(batch: int, root: Path, claim_root: Path, scratch: Path) -> dict:
    from experiments.stage_b_plus.offline import synthetic_cell_run
    from experiments.stage_f.settlement_check import _authority_file

    plan_id = plan.BATCH_PLAN_IDS[batch]
    batch_plan = contract.scope_plan(plan_id)
    path = _authority_file(batch_plan, scratch)

    async def runner(cell: dict, context) -> executor.CellRun:
        return synthetic_cell_run(cell, Path(context["evidence_root"]) / "cells")

    with executor.claim_root_override(claim_root):
        return await executor.dispatch(
            plan_id,
            runs_root=root / plan_id / "runs",
            evidence_root=root,
            authority_path=path,
            cell_runner=runner,
            repo_root=REPO,
            mode=executor.MODE_OFFLINE_PROOF,
            clock=lambda: "2026-11-26T00:00:00Z",
            stage_f_root=root,
        )


def test_a_batch_without_a_prior_report_is_refused_before_the_claim(tmp_path: Path) -> None:
    """Three independent claims would each see the full ceiling."""

    root = tmp_path / "evidence"
    claims = tmp_path / "claims"
    report = asyncio.run(_dispatch(2, root, claims, tmp_path / "scratch"))
    assert report["stop_reason"] == executor.STOP_CUMULATIVE
    assert report["refused"] is True
    assert report["cells_started"] == 0
    settlement = json.loads(
        (root / plan.BATCH_PLAN_IDS[2] / "cumulative-settlement.json").read_text(encoding="utf-8")
    )
    assert settlement["may_start"] is False
    assert settlement["unknown_consumption"] is True
    with executor.claim_root_override(claims):
        digest = contract.scope_plan(plan.BATCH_PLAN_IDS[2])["digest"]
        assert not executor.claim_path(digest).exists()


def test_the_global_remainder_reaches_the_dispatcher(tmp_path: Path) -> None:
    """A valid report that leaves 6M must cap the next batch at 6M, not 32M."""

    root = tmp_path / "evidence"
    claims = tmp_path / "claims"
    first = plan.batch_plan(1)
    directory = root / first["plan_id"]
    directory.mkdir(parents=True)
    (directory / "matrix-report.json").write_text(
        json.dumps(
            {
                "plan_id": first["plan_id"],
                "plan_digest": first["digest"],
                "executor_digest": executor.executor_identity(REPO)["executor_digest"],
                "total_tokens": 90_000_000,
                "total_cost_usd": 90.0,
                "usage_completeness": cumulative.COMPLETE,
                "stop_reason": executor.STOP_NONE,
                "complete": True,
            }
        ),
        encoding="utf-8",
    )
    claims.mkdir(parents=True, exist_ok=True)
    with executor.claim_root_override(claims):
        executor.claim_path(first["digest"]).write_text(
            json.dumps({"plan_id": first["plan_id"], "offline_proof": True}), encoding="utf-8"
        )
    report = asyncio.run(_dispatch(2, root, claims, tmp_path / "scratch"))
    assert report["complete"] is True
    assert report["token_stop"] == 6_000_000
    assert report["effective_stops"]["applied"]["token_stop"] == 6_000_000
    assert report["cumulative_settlement"]["allowed_cost_usd"] == 6.0


def test_the_freeze_carries_every_execution_identity() -> None:
    """A frozen matrix without the executor, adapter, pricing and settlement identities is stale."""

    built = freeze.build()
    assert built["problems"] == []
    manifest = built["manifest"]
    assert manifest["executor_digest"] == executor.executor_identity(REPO)["executor_digest"]
    assert manifest["adapter"]["schema_version"] == normalize.SCHEMA_VERSION
    assert manifest["adapter"]["normaliser_digest"].startswith("sha256:")
    assert manifest["pricing"]["currency"] == "USD"
    assert manifest["pricing"]["is_an_estimate"] is True
    assert manifest["settlement_contract"]["global_token_ceiling"] == 96_000_000
    assert manifest["cells_total"] == 96
    for batch in (1, 2, 3):
        record = built["authorizations"][batch]
        assert record["authorizes_live_execution"] is False
        assert record["approved_by"] is None
        assert record["cost_stop_usd"] is None
        assert record["per_cell_cost_stop_usd"] is None
        assert record["plan_digest"] == built["plans"][batch]["digest"]
