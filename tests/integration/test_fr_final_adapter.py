"""Exercise the serialized budget contract through the real static DAG Runner."""

from __future__ import annotations

import os
from copy import deepcopy

import pytest
from anycode.types import TokenUsage
from experiments.evaluation import runner as benchmark
from experiments.stage_b_plus import executor
from experiments.stage_f import proof, runners, scripted
from experiments.stage_f import release_candidate as release


@pytest.mark.skipif(
    os.getenv("ATLAS_ADAPTER_REAL_SANDBOX") != "1", reason="requires local OpenSandbox"
)
@pytest.mark.parametrize("scenario", ["pass", "fail", "exhausted"])
async def test_actual_dag_adapter_budget_contract(tmp_path, monkeypatch, scenario):
    plan = release.batch_plan(release.V4_BATCH_IDS[1])
    cell = next(
        row
        for row in plan["cells"]
        if row["arm_id"] == "A2" and row["task_id"] == "f-c1-config-lifecycle"
    )
    cell = {**cell, "order": 1}
    if scenario == "exhausted":
        cell["token_budget"] = 24_000
    task = executor.task_for_cell(cell)
    steps: dict[str, list[scripted.ScriptStep]] = (
        proof.dag_scripts(task)
        if scenario == "pass"
        else {worker["task_id"]: [("list_files", {"path": "."})] for worker in task.workers}
    )
    script = scripted.ScriptedRun(scripts=steps, markers=proof.node_markers(task))
    genuine_run = benchmark.run_sample
    actual = []

    async def record(*args, **kwargs):
        result = await genuine_run(*args, **kwargs)
        actual.append(result)
        return result

    class Adapter(scripted.StageFScriptedAdapter):
        async def chat(self, messages, options):
            reply = await super().chat(messages, options)
            if scenario == "exhausted" and len(script.requests) == 1:
                request = script.requests[0]
                input_tokens = request["prompt_tokens"]
                output_tokens = cell["token_budget"] - input_tokens - 1_000
                request["completion_tokens"] = output_tokens
                return reply.model_copy(
                    update={
                        "usage": TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
                    }
                )
            return reply

    monkeypatch.setattr(benchmark, "run_sample", record)
    run = runners.static_dag_cell_runner(
        provider_factory=lambda: Adapter(script, name="adapter-offline"),
        scripted_run=script,
    )
    outcome = await run(cell, {"evidence_root": tmp_path})
    (result,) = actual
    payload = result.as_dict()
    ledger = result.metrics.extra["budget"]
    assert payload["metrics"]["budget"] == ledger
    assert "extra" not in payload["metrics"]
    assert outcome.session is not None
    assert outcome.session["cell_budget_ledger"] == ledger

    def audit(candidate):
        return runners.audit_stage_f_cell(
            cell=cell,
            plan=plan,
            run=candidate,
            prices=release.PRICING,
            cost_before=0,
        )

    checked = audit(outcome)
    assert checked["verdict"] == ("PASS" if scenario == "pass" else "MEASURED_FAILURE"), checked
    assert checked["oracle_status"] == ("PASS" if scenario == "pass" else "FAIL")
    assert checked["cell_budget_exhaustion_verified"] is (scenario == "exhausted")
    if scenario != "pass":
        return
    for test, ledger_update, expected in (
        ("missing", None, executor.STOP_INTEGRITY),
        ("unknown", {}, executor.STOP_UNKNOWN_USAGE),
        (
            "conflicting",
            {"remaining_tokens": ledger["remaining_tokens"] + 1},
            executor.STOP_INTEGRITY,
        ),
        ("invalid", {"reserved_tokens": -1}, executor.STOP_INTEGRITY),
        ("over-cap", {"used_tokens": ledger["limit_tokens"] + 1}, executor.STOP_INTEGRITY),
    ):
        candidate = deepcopy(outcome)
        assert candidate.session is not None
        if test == "unknown":
            candidate.session["provider_observation"]["coverage_complete"] = False
        else:
            copy = deepcopy(result)
            if ledger_update is None:
                copy.metrics.extra.pop("budget")
            else:
                copy.metrics.extra["budget"].update(ledger_update)
            candidate.session.pop("cell_budget_ledger")
            runners._attach_cell_budget(
                cell, candidate.session, copy.as_dict()["metrics"].get("budget")
            )
        check = audit(candidate)
        assert check["verdict"] == "STOPPED" and expected in check["stops"], check
