"""Stage B+ execution contract: identity, authority, claim, dispatch gate.

The properties under test are the ones that would let a run happen without permission or report a
broken matrix as complete:

* the canary is its own object and cannot be dispatched with a formal signature (or the reverse);
* an unsigned record is refused, and a signed one is refused the moment a bound field moves;
* a dispatch claim is created once and never reopened;
* a measurable goal failure continues while an infrastructure failure stops, including on the last
  cell;
* a counter the provider did not report stops the matrix instead of being written as zero.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from experiments.stage_b_plus import authority, contract, executor, offline, probe
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider


def _fake_provider(root: Path) -> FakeSandboxProvider:
    return FakeSandboxProvider(root)


def _plan(scope: str) -> dict[str, Any]:
    return contract.scope_plan(scope)


def _identity() -> dict[str, Any]:
    return executor.executor_identity()


def _digest() -> str:
    return authority.protocol_digest(contract.protocol_document())


def _statement(tmp_path: Path, body: dict[str, Any]) -> dict[str, Any]:
    path = tmp_path / "owner-statement.json"
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return {
        "kind": "external_file",
        "path": str(path),
        "sha256": "sha256:" + __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
        "content_digest": authority.digest_of(body),
        "body": body,
    }


def _signed(
    tmp_path: Path,
    scope: str,
    *,
    head: str | None = None,
    cost_stop: float = 1.0,
    per_cell: float = 0.5,
    **overrides: Any,
) -> dict[str, Any]:
    plan = _plan(scope)
    head = head if head is not None else executor.git_head()
    record = authority.sign(
        authority.pending_record(plan, protocol_digest_value=_digest(), executor=_identity()),
        approved_by="test",
        approved_at="2026-11-25T00:00:00Z",
        owner_statement="test signature, not an approval",
        reviewed_head=head,
        execution_head=head,
        cost_stop_usd=cost_stop,
        per_cell_cost_stop_usd=per_cell,
        provenance=_statement(tmp_path, {"statement": "test", "scope": scope}),
    )
    record.update(overrides)
    return record


def _verify(record: dict[str, Any] | None, scope: str, **kwargs: Any) -> dict[str, Any]:
    return authority.verify(
        record,
        plan=_plan(scope),
        head=kwargs.pop("head", executor.git_head()),
        executor=_identity(),
        protocol_digest_value=_digest(),
        repo_root=contract.REPO_ROOT,
        **kwargs,
    )


# -- plans --------------------------------------------------------------------------------------


def test_canary_is_six_cells_one_per_arm() -> None:
    cells = contract.canary_cells()
    assert len(cells) == 6
    assert len({cell["cell_id"] for cell in cells}) == 6
    assert sorted(cell["arm_id"] for cell in cells) == sorted(contract.ARM_IDS)
    assert {cell["task_id"] for cell in cells} == set(contract.CANARY_TASK_IDS)
    assert {cell["repetition"] for cell in cells} == {1}
    assert all(cell["is_benchmark_sample"] is False for cell in cells)
    assert plan_digest_of(cells) == _plan(contract.SCOPE_CANARY)["digest"]


def plan_digest_of(cells: list[dict[str, Any]]) -> str:
    from experiments.stage_benchmark import matrix

    return matrix.plan_digest(cells)


def test_canary_ids_cannot_collide_with_formal_ids() -> None:
    canary = {cell["cell_id"] for cell in contract.canary_cells()}
    formal = {cell["cell_id"] for cell in contract.cells()}
    assert canary.isdisjoint(formal)
    assert all(cell.startswith("canary.") for cell in canary)


def test_formal_plan_identity_is_unchanged() -> None:
    """Approving this revision must not silently move the formal matrix."""

    plan = _plan(contract.SCOPE_FORMAL)
    assert plan["digest"] == contract.REVISION["previous_formal_plan_digest"]
    assert plan["cells_count"] == 24
    assert set(plan["arms"]) == set(contract.FORMAL_ARM_IDS)


def test_canary_and_formal_scopes_are_named_apart() -> None:
    canary, formal = _plan(contract.SCOPE_CANARY), _plan(contract.SCOPE_FORMAL)
    assert canary["scope"] != formal["scope"]
    assert canary["claim_namespace"] != formal["claim_namespace"]
    assert canary["runs_subdir"] != formal["runs_subdir"]
    assert canary["evidence_subdir"] != formal["evidence_subdir"]


# -- authority ----------------------------------------------------------------------------------


def test_pending_record_is_refused() -> None:
    plan = _plan(contract.SCOPE_CANARY)
    pending = authority.pending_record(
        plan, protocol_digest_value=_digest(), executor=_identity()
    )
    verdict = _verify(pending, contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert len(verdict["problems"]) >= 7
    with pytest.raises(PermissionError):
        authority.require(
            pending,
            plan=plan,
            head=executor.git_head(),
            executor=_identity(),
            protocol_digest_value=_digest(),
            repo_root=contract.REPO_ROOT,
        )


def test_signed_record_is_accepted(tmp_path: Path) -> None:
    verdict = _verify(_signed(tmp_path, contract.SCOPE_CANARY), contract.SCOPE_CANARY)
    assert verdict["ready"] is True, verdict["problems"]


def test_scope_is_not_interchangeable(tmp_path: Path) -> None:
    canary_record = _signed(tmp_path, contract.SCOPE_CANARY)
    assert _verify(canary_record, contract.SCOPE_CANARY)["ready"] is True
    cross = _verify(canary_record, contract.SCOPE_FORMAL)
    assert cross["ready"] is False
    assert any("scope" in item or "plan_id" in item for item in cross["problems"])


def test_a_missing_or_extra_cell_refuses(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    short = dict(record, cell_ids=record["cell_ids"][:-1], cells_count=5)
    assert _verify(short, contract.SCOPE_CANARY)["ready"] is False
    extra = dict(record, cell_ids=[*record["cell_ids"], "canary.invented.C0.r1"])
    verdict = _verify(extra, contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("outside the plan" in item for item in verdict["problems"])


def test_a_duplicate_cell_refuses(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    duplicated = list(record["cell_ids"])
    duplicated[-1] = duplicated[0]
    verdict = _verify(dict(record, cell_ids=duplicated), contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("duplicate" in item for item in verdict["problems"])


def test_a_reordered_cell_list_refuses(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    reordered = list(reversed(record["cell_ids"]))
    verdict = _verify(dict(record, cell_ids=reordered), contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("order" in item for item in verdict["problems"])


@pytest.mark.parametrize("value", [0, -1.0, True, "1.0", float("inf"), float("nan")])
def test_an_illegal_cost_stop_refuses(tmp_path: Path, value: Any) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    verdict = _verify(dict(record, cost_stop_usd=value), contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("cost_stop_usd" in item for item in verdict["problems"])


def test_per_cell_stop_cannot_exceed_the_cumulative_stop(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY, cost_stop=0.1, per_cell=0.2)
    verdict = _verify(record, contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("per-cell" in item for item in verdict["problems"])


def test_attempt_limit_must_be_one(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    verdict = _verify(dict(record, attempt_limit=2), contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("attempt_limit" in item for item in verdict["problems"])


def test_a_consumed_claim_refuses(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    verdict = _verify(record, contract.SCOPE_CANARY, claim_consumed=True)
    assert verdict["ready"] is False
    assert any("claim" in item for item in verdict["problems"])


def test_provenance_must_still_hash(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    record["approval_provenance"]["sha256"] = "sha256:" + "0" * 64
    verdict = _verify(record, contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("hash" in item for item in verdict["problems"])


def test_a_missing_approval_file_refuses(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    Path(record["approval_provenance"]["path"]).unlink()
    verdict = _verify(record, contract.SCOPE_CANARY)
    assert verdict["ready"] is False
    assert any("does not exist" in item for item in verdict["problems"])


def test_an_approval_cannot_name_the_head_it_lives_in() -> None:
    """A commit provenance pointing at the execution head is a fixed point; it is refused."""

    head = executor.git_head()
    problems = authority._provenance_problems(
        {
            "kind": "commit",
            "commit": head,
            "head": head,
            "content_digest": authority.digest_of({"a": 1}),
            "body": {"a": 1},
        },
        repo_root=contract.REPO_ROOT,
    )
    assert any("predate" in item for item in problems)


# -- dispatch -----------------------------------------------------------------------------------


def _dispatch(
    evidence: Path,
    authority_path: Path | None,
    runner: Any,
    *,
    claim_dir: Path | None = None,
    mode: str = executor.MODE_OFFLINE_PROOF,
    head_override: str | None = None,
) -> dict[str, Any]:
    with executor.claim_root_override(claim_dir or evidence / "claims"):
        return asyncio.run(
            executor.dispatch(
                contract.SCOPE_CANARY,
                runs_root=evidence / "runs",
                evidence_root=evidence,
                authority_path=authority_path,
                cell_runner=runner,
                repo_root=contract.REPO_ROOT,
                mode=mode,
                head_override=head_override,
                clock=lambda: "2026-11-25T00:00:00Z",
            )
        )


def test_dispatch_without_authority_starts_nothing(tmp_path: Path) -> None:
    report = _dispatch(tmp_path / "evidence", None, offline._synthetic_runner({}))
    assert report["refused"] is True
    assert report["cells_started"] == 0
    assert report["stop_reason"] == executor.STOP_NOT_AUTHORIZED
    assert (tmp_path / "evidence" / "dispatch-refused.json").is_file()


def test_a_clean_dispatch_runs_every_cell(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    evidence = tmp_path / "evidence"
    report = _dispatch(evidence, path, offline._synthetic_runner({}))
    assert report["complete"] is True
    assert report["cells_started"] == 6
    assert report["cells_passed"] == 6
    assert report["stop_reason"] == executor.STOP_NONE
    assert report["is_benchmark_sample"] is False
    assert report["aggregate_includes_sample"] is False
    # the claim is global, not under the evidence root; every attempt marker exists exactly once
    assert not (evidence / "dispatch.claim").exists()
    claim = evidence / "claims" / f"{_plan(contract.SCOPE_CANARY)['digest'].split(':')[1]}.claim"
    assert claim.is_file()
    assert report["claim_path"] == str(claim)
    assert len(list((evidence / "cells").glob("*/attempt.json"))) == 6

    # a different evidence root, the same claim: refused before any cell starts
    again = _dispatch(evidence / "elsewhere", path, offline._synthetic_runner({}),
                      claim_dir=evidence / "claims")
    assert again["refused"] is True
    assert again["cells_started"] == 0
    assert again["stop_reason"] == executor.STOP_CLAIM_CONSUMED


def test_a_measured_failure_does_not_stop_the_matrix(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    cell = contract.canary_cells()[0]["cell_id"]
    spec = {cell: {"overrides": {"status": "failed", "failure": {"kind": "oracle_failed"}}}}
    report = _dispatch(tmp_path / "evidence", path, offline._synthetic_runner(spec))
    assert report["complete"] is True
    assert report["cells_started"] == 6
    assert report["cells"][0]["verdict"] != "PASS"


def test_an_unknown_failure_kind_stops(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    cell = contract.canary_cells()[0]["cell_id"]
    spec = {cell: {"overrides": {"status": "failed", "failure": {"kind": "something_new"}}}}
    report = _dispatch(tmp_path / "evidence", path, offline._synthetic_runner(spec))
    assert report["stop_reason"] == executor.STOP_UNKNOWN_FAILURE
    assert report["cells_started"] == 1


def test_the_last_cell_can_still_stop_the_matrix(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    last = contract.canary_cells()[-1]["cell_id"]
    spec = {last: {"overrides": {"sandbox": {"destroyed": False, "retained": True}}}}
    report = _dispatch(tmp_path / "evidence", path, offline._synthetic_runner(spec))
    assert report["cells_started"] == 6
    assert report["complete"] is False
    assert report["stop_reason"] == executor.STOP_SANDBOX_CLEANUP


def test_an_unknown_token_count_stops_instead_of_zeroing(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    cell = contract.canary_cells()[0]["cell_id"]
    spec = {cell: {"overrides": {"metrics": {"child_completion_tokens": None}}}}
    report = _dispatch(tmp_path / "evidence", path, offline._synthetic_runner(spec))
    assert report["stop_reason"] == executor.STOP_UNKNOWN_USAGE
    assert report["cells"][0]["usage"]["child_completion_tokens"] is None


def test_a_cost_stop_ends_the_matrix(tmp_path: Path) -> None:
    record = _signed(tmp_path, contract.SCOPE_CANARY, cost_stop=1e-9, per_cell=1e-9)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    report = _dispatch(tmp_path / "evidence", path, offline._synthetic_runner({}))
    assert report["stop_reason"] == executor.STOP_COST_REACHED
    assert report["cells_started"] == 1


# -- usage and cost -----------------------------------------------------------------------------


def test_usage_validation_rejects_what_is_not_a_counter() -> None:
    base = {
        "main_cumulative_input_tokens": 10,
        "main_completion_tokens": 5,
        "child_total_prompt_tokens": 0,
        "child_completion_tokens": 0,
        "system_total_tokens": 15,
    }
    assert executor.validate_usage(base)["ok"] is True
    for field, value in (
        ("main_completion_tokens", True),
        ("child_total_prompt_tokens", -1),
        ("system_total_tokens", None),
        ("main_cumulative_input_tokens", 9.5),
    ):
        patched = dict(base)
        patched[field] = value
        assert executor.validate_usage(patched)["ok"] is False, field


def test_usage_identity_is_checked_not_assumed() -> None:
    broken = {
        "main_cumulative_input_tokens": 10,
        "main_completion_tokens": 5,
        "child_total_prompt_tokens": 3,
        "child_completion_tokens": 1,
        "system_total_tokens": 15,
    }
    report = executor.validate_usage(broken)
    assert report["ok"] is False
    assert any("sum" in item for item in report["problems"])


def test_child_prompt_tokens_are_priced_as_misses() -> None:
    metrics = {
        "main_cumulative_input_tokens": 1_000_000,
        "main_cache_hit_tokens": 1_000_000,
        "main_cache_miss_tokens": 0,
        "child_total_prompt_tokens": 1_000_000,
        "main_completion_tokens": 0,
        "child_completion_tokens": 0,
        "system_total_tokens": 2_000_000,
    }
    prices = {
        "cache_hit_input_per_million": 0.006,
        "cache_miss_input_per_million": 0.3,
        "output_per_million": 1.2,
    }
    cost = executor.cell_cost(metrics, prices)
    assert cost == pytest.approx(0.006 + 0.3)


def test_executor_identity_covers_the_deciding_files() -> None:
    identity = _identity()
    assert identity["executor_version"] == executor.EXECUTOR_VERSION
    assert set(identity["files"]) == set(executor.EXECUTOR_FILES)
    assert identity["executor_digest"] == authority.digest_of(identity["files"])


# -- the live path: fixture, export, sandboxed oracle -------------------------------------------


def _seed_workspace(tmp_path: Path, task: Any) -> Path:
    target = tmp_path / "seed"
    import shutil

    shutil.copytree(executor.prepare_fixture(task, tmp_path / "prepared")["path"], target)
    return target


def test_fixture_is_a_clean_repository_matching_the_frozen_seed(tmp_path: Path) -> None:
    task = contract.TASKS[0]
    evidence = executor.prepare_fixture(task, tmp_path / "fixture")
    assert evidence["matches_frozen_seed"] is True
    assert evidence["seed_digest"] == evidence["frozen_seed_digest"]
    assert len(evidence["commit"]) == 40
    root = Path(evidence["path"])
    assert (root / ".git").is_dir()
    # nothing hidden is staged: the oracle files are absent from the agent's tree
    for name in task.oracle_tests:
        assert not (root / name).exists()
    for name in task.reference:
        if name not in task.seed:
            assert not (root / name).exists()


def test_fixture_can_be_prepared_twice_into_the_same_directory(tmp_path: Path) -> None:
    """A git object is read-only; a second preparation must not be blocked by the first.

    Regression: ``prepare_fixture`` used ``shutil.rmtree``, which on Windows refuses the read-only
    object files git writes. A rebuild over the same scratch directory — or a re-prepared per-cell
    fixture — failed for a filesystem reason rather than a runtime one.
    """

    task = contract.TASKS[0]
    destination = tmp_path / "fixture"
    first = executor.prepare_fixture(task, destination)
    second = executor.prepare_fixture(task, destination)
    assert second["seed_digest"] == first["seed_digest"]
    assert second["matches_frozen_seed"] is True
    assert len(second["commit"]) == 40
    assert (destination / ".git").is_dir()


def test_oracle_scores_the_export_not_the_seed(tmp_path: Path) -> None:
    """The seed must FAIL and the reference must PASS, or the oracle is not measuring anything."""

    task = contract.TASKS[0]
    seed = _seed_workspace(tmp_path, task)
    reference = tmp_path / "reference"
    import shutil

    shutil.copytree(seed, reference)
    for name, text in task.reference.items():
        target = reference / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    seed_verdict = executor.judge_export(
        seed,
        task,
        provider=_fake_provider(tmp_path / "oracle-seed"),
        run_id="seed",
        image="atlas-uat-python:3.12",
    )
    reference_verdict = executor.judge_export(
        reference,
        task,
        provider=_fake_provider(tmp_path / "oracle-reference"),
        run_id="reference",
        image="atlas-uat-python:3.12",
    )
    assert seed_verdict["status"] == "FAIL", seed_verdict
    assert seed_verdict["execution_mode"] == "sandbox"
    assert seed_verdict["sandbox_id"]
    assert seed_verdict["cleanup_failure"] is None
    assert reference_verdict["status"] == "PASS", reference_verdict
    # each verdict came from its own throwaway sandbox, created and destroyed inside the call
    assert seed_verdict["sandbox_id"] and reference_verdict["sandbox_id"]
    assert seed_verdict["timings_ms"]["creation_ms"] >= 0


def test_oracle_refuses_a_workspace_that_contains_the_hidden_suite(tmp_path: Path) -> None:
    task = contract.TASKS[0]
    seed = _seed_workspace(tmp_path, task)
    name = sorted(task.oracle_tests)[0]
    (seed / name).parent.mkdir(parents=True, exist_ok=True)
    (seed / name).write_text("def test_leak():\n    assert True\n", encoding="utf-8")
    verdict = executor.judge_export(
        seed,
        task,
        provider=_fake_provider(tmp_path / "oracle-leak"),
        run_id="leak",
        image="atlas-uat-python:3.12",
    )
    assert verdict["status"] == "ERROR"
    assert verdict["kind"] == "HIDDEN_FILES_IN_WORKSPACE"
    assert name in verdict["leaked_paths"]


def test_one_cell_through_the_live_runner(tmp_path: Path) -> None:
    """The production runner, with only the model and the sandbox replaced."""

    cell = contract.canary_cells()[2]
    scripted = probe.ScriptedProvider(
        main_script=[lambda _messages: [probe.TextBlock(text="REPORT: nothing to do.")]],
        child_scripts={},
    )
    config = executor.RunnerConfig(
        llm_provider="fake",
        llm_factory=lambda: scripted,
        sandbox_provider_name="fake",
        sandbox_factory=_fake_provider,
        require_real_sandbox=False,
        main_max_turns=2,
    )
    runner = executor.live_cell_runner(config)
    context = {
        "plan": _plan(contract.SCOPE_CANARY),
        "runs_root": tmp_path / "runs",
        "evidence_root": tmp_path / "evidence",
    }
    run = asyncio.run(runner(cell, context))

    assert run.error is None, run.error
    # the fixture is per cell, clean, and unchanged by the run
    fixture = run.fixture or {}
    assert cell["cell_id"] in str(fixture["path"])
    assert fixture["matches_frozen_seed"] is True
    seed_after = executor.canonical_digest(
        {name: (Path(fixture["path"]) / name).read_text(encoding="utf-8")
         for name in fixture["files_list"]}
    )
    assert seed_after == fixture["seed_digest"]
    # the export exists, matches its digest, and is what the oracle scored
    export = run.export or {}
    assert Path(export["workspace_path"]).is_dir()
    assert export["workspace_digest"]
    assert export["workspace_files"] > 0
    oracle = run.oracle or {}
    assert oracle["execution_mode"] == "sandbox"
    assert oracle["status"] in {"PASS", "FAIL"}
    assert oracle["sandbox_id"]
    assert oracle["cleanup_failure"] is None
    # the hidden suite never reached the agent's workspace
    for name in contract.TASKS_BY_ID[cell["task_id"]].oracle_tests:
        assert not (Path(fixture["path"]) / name).exists()
    # the recorded run identity is the one the gate will check
    assert run.expected_run_id == "bplus-c03"
    assert run.session is not None and run.session["run_id"] == run.expected_run_id


def _audit(cell: Mapping[str, Any], run: executor.CellRun) -> dict[str, Any]:
    return executor.audit_cell(
        cell=cell,
        run=run,
        plan=_plan(contract.SCOPE_CANARY),
        usage_before=dict.fromkeys(executor.REQUIRED_USAGE_FIELDS, 0),
        cost_before=0.0,
    )


def test_audit_stops_when_the_export_disappears(tmp_path: Path) -> None:
    import shutil

    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    shutil.rmtree(run.export["workspace_path"])
    outcome = _audit(cell, run)
    assert executor.STOP_EXPORT in outcome["stops"]
    assert outcome["verdict"] == "STOPPED"


def test_audit_stops_when_the_fixture_loses_a_file(tmp_path: Path) -> None:
    """A deleted fixture file is a stop, not a traceback inside the gate."""

    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    fixture_root = Path((run.fixture or {})["path"])
    victim = sorted((run.fixture or {})["files_list"])[0]
    (fixture_root / victim).unlink()
    outcome = _audit(cell, run)
    assert executor.STOP_FIXTURE in outcome["stops"]
    assert outcome["verdict"] == "STOPPED"


def test_audit_stops_when_the_oracle_sandbox_is_retained(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path, behaviour="oracle_cleanup_failure")
    outcome = _audit(cell, run)
    assert executor.STOP_ORACLE_CLEANUP in outcome["stops"]
    assert outcome["verdict"] == "STOPPED"


def test_audit_stops_when_the_oracle_environment_fails(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path, behaviour="oracle_error")
    outcome = _audit(cell, run)
    assert executor.STOP_ORACLE_ENVIRONMENT in outcome["stops"]


def test_audit_scores_agent_success_with_oracle_failure_as_measured(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path, behaviour="oracle_fail")
    outcome = _audit(cell, run)
    assert outcome["stops"] == []
    assert outcome["agent_success"] is True
    assert outcome["oracle_passed"] is False
    assert outcome["goal_correctness"] is False
    assert outcome["executor_verdict"] == "MEASURED_FAILURE"
    assert outcome["execution_integrity"] == "PASS"


def test_audit_stops_when_the_session_identity_is_another_cell(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(
        cell, tmp_path, overrides={"run_id": "bplus-c99"}
    )
    outcome = _audit(cell, run)
    assert executor.STOP_IDENTITY_MISMATCH in outcome["stops"]


def test_audit_stops_when_the_sandbox_is_not_the_contract_sandbox(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    plan = dict(run.sandbox_plan or {})
    plan["cpu"] = "4"
    run.sandbox_plan = plan
    outcome = _audit(cell, run)
    assert executor.STOP_SANDBOX_CONFIG in outcome["stops"]


def test_audit_stops_when_the_isolation_probe_says_failed(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(
        cell, tmp_path, overrides={"sandbox": {"network_probe": "FAILED"}}
    )
    outcome = _audit(cell, run)
    assert executor.STOP_HOST_ISOLATION in outcome["stops"]


def test_audit_stops_when_the_isolation_probe_did_not_run(tmp_path: Path) -> None:
    cell = contract.canary_cells()[0]
    run = offline.synthetic_cell_run(
        cell, tmp_path, overrides={"sandbox": {"host_separation": {}}}
    )
    outcome = _audit(cell, run)
    assert executor.STOP_HOST_ISOLATION in outcome["stops"]


def test_audit_stops_when_the_applied_exposure_differs() -> None:
    cell = contract.canary_cells()[0]
    session, run_dir, fixture, artifacts = offline._synthetic_session(  # noqa: SLF001
        cell=cell, root=Path(tempfile.mkdtemp())
    )
    # pretend the runtime applied the card when the arm asked for native
    session["delegation_controls"]["exposure"]["level"] = "card"
    run = executor.CellRun(
        session=session,
        run_dir=run_dir,
        fixture_root=Path(fixture["path"]),
        oracle={
            "status": "PASS",
            "kind": "HIDDEN_TESTS_PASSED",
            "execution_mode": "sandbox",
            "sandbox_id": "s1",
        },
        fixture=fixture,
        export=artifacts,
        sandbox_plan=executor.sandbox_plan(require_real=False, provider_name="fake").as_dict(),
    )
    outcome = executor.audit_cell(
        cell=cell,
        run=run,
        plan=_plan(contract.SCOPE_CANARY),
        usage_before=dict.fromkeys(executor.REQUIRED_USAGE_FIELDS, 0),
        cost_before=0.0,
    )
    assert executor.STOP_IDENTITY_MISMATCH in outcome["stops"]


def test_audit_stops_when_the_worker_cap_is_wrong() -> None:
    """The effective cap is part of the arm: 6 instead of 20 must stop, not be reported."""

    cell = contract.canary_cells()[0]
    session, run_dir, fixture, artifacts = offline._synthetic_session(  # noqa: SLF001
        cell=cell,
        root=Path(tempfile.mkdtemp()),
        overrides={
            "delegation_controls": {
                "profiles": {contract.RESEARCH: {"profile_id": contract.RESEARCH,
                                                 "effective_max_turns": 6}}
            }
        },
    )
    run = executor.CellRun(
        session=session,
        run_dir=run_dir,
        fixture_root=Path(fixture["path"]),
        oracle={
            "status": "PASS",
            "kind": "HIDDEN_TESTS_PASSED",
            "execution_mode": "sandbox",
            "sandbox_id": "s1",
        },
        fixture=fixture,
        export=artifacts,
        sandbox_plan=executor.sandbox_plan(require_real=False, provider_name="fake").as_dict(),
    )
    outcome = executor.audit_cell(
        cell=cell,
        run=run,
        plan=_plan(contract.SCOPE_CANARY),
        usage_before=dict.fromkeys(executor.REQUIRED_USAGE_FIELDS, 0),
        cost_before=0.0,
    )
    assert executor.STOP_IDENTITY_MISMATCH in outcome["stops"]
    assert outcome["verdict"] == "STOPPED"
