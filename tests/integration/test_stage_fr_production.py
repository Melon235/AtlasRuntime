"""The registered F-R contract without model traffic or production claims."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from anycode.types import LLMChatOptions, LLMStreamOptions
from experiments.evaluation import runner as eval_runner
from experiments.evaluation.closure1b import native_baseline
from experiments.evaluation.runner import load_runner_config
from experiments.stage_b_plus import authority, contract, executor
from experiments.stage_f import cumulative, plan, runners
from experiments.stage_f import release_candidate as fr
from experiments.stage_f.tasks import revisions

from atlasos.app import main_driven
from atlasos.budget import RunBudgetExhausted, RunBudgetState
from atlasos.integrations.deepseek import DeepSeekAdapter, UsageParseError
from atlasos.metrics.requests import JournaledAdapter, RequestJournal


@pytest.mark.parametrize("arm", ["A0", "A1", "A2", "A3"])
async def test_registered_runner_model_reaches_adapter_request(arm, tmp_path, monkeypatch):
    registered = contract.scope_plan(fr.BATCH_IDS[1])
    cell = next(cell for cell in registered["cells"] if cell["arm_id"] == arm)
    if arm in {"A0", "A2"}:
        settings, _ = runners._settings(cell, runs_root=tmp_path, multi=arm == "A2")
        config = load_runner_config(settings, settings.config_path)
        model = settings.model_override
        assert config.pricing == fr.pricing_config()
    else:
        active = executor.cell_runner_config(cell, executor.RunnerConfig())
        options = executor.production_goal_options(
            cell, evidence_root=tmp_path, fixture_dir=tmp_path, config=active, run_id="test"
        )
        config, model = options.config, options.model
        assert options.provider_factory is None and options.provider_retries is False
        assert options.require_reported_usage is True
    assert config.provider.model == model == fr.REQUESTED_MODEL
    captured = []

    async def create(**body):
        captured.append(body)
        return SimpleNamespace(
            id="synthetic-transport-response", model=fr.REQUESTED_MODEL,
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop"
            )],
            usage={"prompt_tokens": 10, "completion_tokens": 2},
        )

    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    adapter = DeepSeekAdapter(
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        model=model, require_reported_usage=True,
    )
    journal = RequestJournal(run_id="test", provider="deepseek", requested_model=model)
    await JournaledAdapter(adapter, journal).chat([], LLMChatOptions(model=model))
    assert [row["model"] for row in captured] == [fr.REQUESTED_MODEL]
    assert journal.snapshot()["records"][0]["returned_model"] == fr.REQUESTED_MODEL


async def test_missing_usage_is_a_failed_request_not_zero():
    async def create(**_):
        return SimpleNamespace(
            id="missing", model=fr.REQUESTED_MODEL,
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop"
            )],
            usage=None,
        )

    adapter = DeepSeekAdapter(
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        require_reported_usage=True,
    )
    journal = RequestJournal(run_id="test", provider="deepseek", requested_model=fr.REQUESTED_MODEL)
    with pytest.raises(UsageParseError):
        await JournaledAdapter(adapter, journal).chat([], LLMChatOptions(model=fr.REQUESTED_MODEL))
    record, = journal.snapshot()["records"]
    assert record["status"] == "FAILED"
    assert record["usage"] is None


async def test_terminal_stream_error_is_recorded_before_the_iterator_closes():
    calls = []

    async def create(**_):
        calls.append(1)
        raise ValueError("non-retryable request rejection")

    adapter = DeepSeekAdapter(
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        require_reported_usage=True,
    )
    journal = RequestJournal(run_id="test", provider="deepseek", requested_model=fr.REQUESTED_MODEL)
    options = LLMStreamOptions(model=fr.REQUESTED_MODEL)
    stream = JournaledAdapter(adapter, journal).stream([], options)
    event = await anext(stream)
    assert event.type == "error"
    await stream.aclose()
    record, = journal.snapshot()["records"]
    assert record["exception_type"] == "ValueError"
    assert record["status"] == "FAILED"
    assert calls == [1]


def test_historic_model_and_task_order_remain_bound():
    old = plan.batch_plan(1)
    new = contract.scope_plan(fr.BATCH_IDS[1])
    assert old["digest"] != new["digest"]
    assert old["budget"]["model"] == "deepseek-chat"
    assert [cell["goal_digest"] for cell in old["cells"]] == [
        cell["goal_digest"] for cell in new["cells"]
    ]
    altered = {**new["cells"][0], "budget": old["budget"]}
    with pytest.raises(ValueError, match="model contract"):
        fr.requested_model(altered)
    with pytest.raises((KeyError, ValueError)):
        contract.scope_plan("stage-fr-final-96-v1-b1")
    with pytest.raises((KeyError, ValueError)):
        contract.scope_plan("stage-fr-final-96-v2-b1")
    same_order = [contract.scope_plan(plan_id)["cells"][0] for plan_id in fr.BATCH_IDS.values()]
    assert len({fr.goal_run_id(cell, "c01") for cell in same_order}) == 3


@pytest.mark.parametrize("task_id", revisions.REVISIONS)
@pytest.mark.parametrize("arm", ["A0", "A1", "A2", "A3"])
async def test_runner_uses_the_bound_revision_before_model_execution(
    arm, task_id, tmp_path, monkeypatch,
):
    registered = contract.scope_plan(fr.BATCH_IDS[1])
    cell = next(
        row for row in registered["cells"] if row["arm_id"] == arm and row["task_id"] == task_id
    )
    revised = revisions.task_at_revision(task_id, 2)
    historical = revisions.task_at_revision(task_id, 1)
    expected_contract = revised.seed["docs/INTERFACE.md"]
    assert expected_contract != historical.seed["docs/INTERFACE.md"]
    assert revised.oracle_tests != historical.oracle_tests
    assert cell["task_revision"] == 2

    class ReachedOracleBoundary(Exception):
        pass

    async def benchmark_boundary(benchmark, **kwargs):
        assert benchmark.visible_files["docs/INTERFACE.md"] == expected_contract
        assert benchmark.hidden_files == revised.oracle_tests
        if arm == "A2":
            assert benchmark.workers == runners._benchmark_task(cell, multi=True).workers
            by_id = {worker["task_id"]: worker for worker in revised.workers}
            assignment = {node["task_id"]: node["assignment"] for node in cell["static_dag"]}
            for step in benchmark.workers:
                assert step.prompt == assignment[step.task_id] == by_id[step.task_id]["assignment"]
        source = tmp_path / "cells" / f"c{cell['order']:02d}" / "fixture"
        assert (source / "docs/INTERFACE.md").read_text("utf-8") == expected_contract
        assert all(not (source / name).exists() for name in revised.oracle_tests)
        raise ReachedOracleBoundary

    async def goal_boundary(options):
        source = options.sandbox.source_repo
        assert (source / "docs/INTERFACE.md").read_text("utf-8") == expected_contract
        assert all(not (source / name).exists() for name in revised.oracle_tests)
        assert options.public_tests == contract.public_test_spec(revised)
        return {"artifacts": {"workspace_path": str(source)}}

    def oracle_boundary(workspace, task, **kwargs):
        assert task.oracle_tests == revised.oracle_tests
        assert (workspace / "docs/INTERFACE.md").read_text("utf-8") == expected_contract
        raise ReachedOracleBoundary

    monkeypatch.setattr(native_baseline, "run_native_sample", benchmark_boundary)
    monkeypatch.setattr(eval_runner, "run_sample", benchmark_boundary)
    monkeypatch.setattr(main_driven, "run_goal", goal_boundary)
    monkeypatch.setattr(executor, "judge_export", oracle_boundary)
    monkeypatch.setattr(executor, "_sandbox_provider", lambda *_: None)
    runner = executor.cell_runner_for(cell)
    with pytest.raises(ReachedOracleBoundary):
        await runner(cell, {"evidence_root": tmp_path, "runs_root": tmp_path / "runs"})


def test_v3_task_revision_and_dag_identity_fail_closed():
    registered = contract.scope_plan(fr.BATCH_IDS[1])
    cell = next(
        row for row in registered["cells"]
        if row["arm_id"] == "A2" and row["task_revision"] == 2
    )
    for key, value in (
        ("task_revision", 1), ("release_experiment", None), ("task_revision_digest", "wrong"),
        ("task_oracle_digest", "wrong"), ("fixture_seed_digest", "wrong"),
        ("static_dag_digest", "wrong"), ("static_dag", []),
    ):
        with pytest.raises(ValueError):
            executor.task_for_cell({**cell, key: value})


def test_v4_preserves_v3_cells_and_has_separate_plan_and_claim_identities():
    identity = {"executor_version": fr.EXPERIMENT, "executor_digest": "test"}
    revised = {**identity, "executor_version": fr.V4_EXPERIMENT}
    head = "test"
    old_manifest = fr._manifest(head, identity)
    new_manifest = fr._manifest(head, revised)
    for key in ("task_facts", "task_revision_digest", "qualification_digest", "budget_digest",
                "pricing_digest", "stop_policy_digest", "settlement_digest"):
        assert old_manifest[key] == new_manifest[key]
    version_fields = {"cell_id", "plan_id", "batch_id", "scope", "release_experiment"}
    for index in fr.BATCH_IDS:
        previous = contract.scope_plan(fr.BATCH_IDS[index])
        current = contract.scope_plan(fr.V4_BATCH_IDS[index])
        assert previous["digest"] != current["digest"]
        assert previous["claim_key"] != current["claim_key"]
        for old, new in zip(previous["cells"], current["cells"], strict=True):
            assert {k: v for k, v in old.items() if k not in version_fields} == {
                k: v for k, v in new.items() if k not in version_fields
            }
            assert executor.task_for_cell(old) == executor.task_for_cell(new)
            assert old["cell_id"].replace("fr3.", "fr4.", 1) == new["cell_id"]


async def test_cell_budget_proves_rejection_without_a_provider_request():
    state = RunBudgetState(limit_tokens=100, safety_factor=1)
    admitted = await state.admit(worker="main", prompt_estimate=25, completion_reserve=5)
    await state.settle(admitted, prompt_tokens=40, completion_tokens=5)
    with pytest.raises(RunBudgetExhausted):
        await state.admit(worker="main", prompt_estimate=60, completion_reserve=5)
    ledger = state.finalize()
    cell = {"token_budget": 100}
    observed = {"prompt": 40, "output": 5, "records": 1}
    metrics = {"system_total_tokens": 45}
    assert runners._cell_budget_problems(cell, ledger, observed, metrics, True) == []
    assert ledger["budget_overrun_tokens"] == 15  # reservation estimate, still under the cell cap
    assert ledger["remaining_tokens"] == 55
    changed = {**ledger, "last_admission_rejection": {
        **ledger["last_admission_rejection"], "requests_started": 0,
    }}
    assert runners._cell_budget_problems(cell, changed, observed, metrics, True) == [
        "terminal pre-request admission rejection is not established"
    ]
    assert runners._cell_budget_problems(
        cell, {**ledger, "used_tokens": 101}, observed, metrics, True
    )


def test_cumulative_reads_new_reports_and_enforces_frozen_headroom(tmp_path: Path):
    identity = {"executor_version": fr.EXPERIMENT, "executor_digest": "test-digest"}
    manifest = fr._manifest("test-head", identity)

    def check(batch):
        return cumulative.settlement(
            manifest, batch=batch, evidence_root=tmp_path,
            executor_digest=identity["executor_digest"], claim_consumed=lambda _: True,
        )

    assert check(1)["allowed_cost_usd"] == 10
    assert not check(2)["may_start"]
    for batch in manifest["batches"][:2]:
        root = tmp_path / batch["plan_id"]
        root.mkdir()
        report = {
            "plan_id": batch["plan_id"],
            "plan_digest": batch["plan_digest"],
            "executor_digest": identity["executor_digest"],
            "execution_head": manifest["execution_head"],
            "pricing_digest": manifest["pricing_digest"],
            "settlement_digest": manifest["settlement_digest"],
            "stop_policy_digest": manifest["stop_policy_digest"],
            "stop_reason": "NONE",
            "complete": True,
            "usage_completeness": "COMPLETE",
            "total_tokens": 12_000_000,
            "total_cost_usd": 8.5,
        }
        (root / "matrix-report.json").write_text(json.dumps(report), encoding="utf-8")
    assert check(2)["allowed_cost_usd"] == 10
    assert check(3)["allowed_cost_usd"] == 3
    assert check(3)["allowed_tokens"] == 32_000_000
    report["stop_reason"] = "COST_STOP_REACHED"
    (root / "matrix-report.json").write_text(json.dumps(report), encoding="utf-8")
    assert not check(3)["may_start"]
    report.update(stop_reason="NONE", total_cost_usd=11.5)
    (root / "matrix-report.json").write_text(json.dumps(report), encoding="utf-8")
    assert not check(3)["may_start"]  # no global cost headroom
    report.update(total_cost_usd=8.5, total_tokens=84_000_000)
    (root / "matrix-report.json").write_text(json.dumps(report), encoding="utf-8")
    assert not check(3)["may_start"]  # no global token headroom


@pytest.mark.asyncio
async def test_unsigned_dispatch_refuses_and_rejects_a_tampered_plan(tmp_path: Path):
    head = executor.git_head()
    identity = executor.executor_identity(experiment_id=fr.EXPERIMENT)
    manifest = fr._manifest(head, identity)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for plan_id in fr.BATCH_IDS.values():
        batch = fr.frozen_batch(plan_id, identity)
        (tmp_path / f"{plan_id}.plan.json").write_text(json.dumps(batch), encoding="utf-8")
    plan_id = fr.BATCH_IDS[1]
    batch = fr.frozen_batch(plan_id, identity)
    pending = authority.pending_record(
        batch, protocol_digest_value=authority.digest_of(manifest), executor=identity
    )
    path = tmp_path / "pending.json"
    path.write_text(json.dumps(pending), encoding="utf-8")
    with executor.claim_root_override(tmp_path / "claims"):
        result = await executor.dispatch(
            plan_id, evidence_root=tmp_path, runs_root=tmp_path / "runs", authority_path=path,
            mode=executor.MODE_OFFLINE_PROOF,
        )
        assert result["stop_reason"] == executor.STOP_NOT_AUTHORIZED
        assert not executor.claim_path(batch["digest"]).exists()
        batch["digest"] = "tampered"
        (tmp_path / f"{plan_id}.plan.json").write_text(json.dumps(batch), encoding="utf-8")
        with pytest.raises(ValueError, match="differs from the registered plan"):
            await executor.dispatch(
                plan_id, evidence_root=tmp_path, runs_root=tmp_path / "runs", authority_path=path,
                mode=executor.MODE_OFFLINE_PROOF,
            )


@pytest.mark.asyncio
async def test_dispatch_uses_release_pricing_and_finishes_only_one_failed_attempt(
    tmp_path: Path, monkeypatch
):
    head = executor.git_head()
    identity = executor.executor_identity(experiment_id=fr.EXPERIMENT)
    manifest = fr._manifest(head, identity)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for plan_id in fr.BATCH_IDS.values():
        batch = fr.frozen_batch(plan_id, identity)
        (tmp_path / f"{plan_id}.plan.json").write_text(json.dumps(batch), encoding="utf-8")
    plan_id = fr.BATCH_IDS[1]
    batch = fr.frozen_batch(plan_id, identity)
    pending = authority.pending_record(
        batch, protocol_digest_value=authority.digest_of(manifest), executor=identity
    )
    body = {"testing_only": True, "plan_id": plan_id}
    statement = tmp_path / "offline-statement.json"
    statement.write_text(json.dumps(body), encoding="utf-8")
    signed = authority.sign(
        pending, approved_by="offline-test", approved_at="2026-11-27T00:00:00Z",
        owner_statement="offline proof only", reviewed_head=head, execution_head=head,
        cost_stop_usd=10, per_cell_cost_stop_usd=1,
        provenance={
            "kind": "external_file", "body": body, "content_digest": authority.digest_of(body),
            "path": str(statement), "sha256": "sha256:" + hashlib.sha256(
                statement.read_bytes()
            ).hexdigest(),
        },
    )
    path = tmp_path / "offline-authority.json"
    path.write_text(json.dumps(signed), encoding="utf-8")
    visited = []

    async def failed_runner(cell, context):
        assert context["prices"] == fr.PRICING
        visited.append(cell["cell_id"])
        raise RuntimeError("simulated execution infrastructure failure")

    monkeypatch.setattr(executor, "cell_runner_for", lambda *args, **kwargs: failed_runner)
    with executor.claim_root_override(tmp_path / "claims"):
        for attempt in (1, 2):
            result = await executor.dispatch(
                plan_id, evidence_root=tmp_path, runs_root=tmp_path / "runs", authority_path=path,
                mode=executor.MODE_OFFLINE_PROOF,
            )
            assert not result["complete"]
            if attempt == 1:
                assert result["cells_started"] == 1
                assert result["pricing_digest"] == manifest["pricing_digest"]
                assert result["total_cost_usd"] is None
            else:
                assert result["stop_reason"] == executor.STOP_CLAIM_CONSUMED
    assert len(visited) == 1
