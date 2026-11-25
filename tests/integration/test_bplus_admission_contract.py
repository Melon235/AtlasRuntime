"""Stage B+ admission: the external oracle contract, the pre-claim gate and its classification.

v1 was dispatched once and stopped on the first cell because ``run_goal``'s live admission demanded
a dynamic acceptance command that the B+ contract deliberately does not have. These tests pin the
three things that fix it:

* the contract now *declares* its oracle mode, and admission verifies the declaration instead of
  inferring it from a missing command;
* the same preflight that admits a live run is applied to the six production configurations before
  the claim exists;
* an admission refusal is classified from the report it wrote, not guessed from an exception.

No test here calls a model or creates a sandbox.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from experiments.stage_b_plus import authority, contract, executor, offline

from atlasos.app.goal_verification import ORACLE_MODE_EXTERNAL, ORACLE_MODE_IN_RUN, GoalAcceptance
from atlasos.app.live_preflight import BLOCKED, READY, run_live_preflight

#: The digest v1 was authorized and consumed under. It is a constant here because the point of the
#: last test is that it can no longer be turned back into a dispatchable plan.
V1_PLAN_DIGEST = "sha256:7ae71273eea29f489edc657ada9a26c834e178289ac6744807350d674dc53575"


@pytest.fixture(autouse=True)
def _credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admission checks for the key's presence; nothing in these tests ever uses its value."""

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-presence-only-not-used")


def _production_options(tmp_path: Path, index: int = 2) -> tuple[Any, dict[str, Any]]:
    """The options the live runner would build for one cell, with a real per-cell fixture."""

    plan = contract.scope_plan(contract.CANARY_PLAN_ID)
    cell = plan["cells"][index]
    fixture = executor.prepare_fixture(
        contract.TASKS_BY_ID[cell["task_id"]], tmp_path / "fixture"
    )
    options = executor.production_goal_options(
        cell,
        evidence_root=tmp_path / "evidence",
        fixture_dir=tmp_path / "fixture",
        config=executor.RunnerConfig(),
        run_id="test-cell",
    )
    return options, fixture


def _with(options: Any, acceptance: GoalAcceptance) -> Any:
    return dataclasses.replace(options, acceptance=acceptance)


def _blocking(options: Any) -> set[str]:
    report = run_live_preflight(options)
    return set(report["blocking_failures"]) if report["status"] == BLOCKED else set()


# -- 1. the failure that stopped v1 --------------------------------------------------------------


def test_the_v1_contract_shape_is_still_refused(tmp_path: Path) -> None:
    options, _ = _production_options(tmp_path)
    v1 = GoalAcceptance(spec=options.acceptance.spec, dynamic_command=None, require_dynamic=False)

    blocking = _blocking(_with(options, v1))

    assert blocking == {"public_tests_separate_from_oracle", "acceptance_configured"}


# -- 2-4. the external contract is admitted ------------------------------------------------------


def test_the_external_oracle_contract_is_admitted(tmp_path: Path) -> None:
    options, _ = _production_options(tmp_path)

    report = run_live_preflight(options)

    assert report["status"] == READY, report["blocking_failures"]
    checks = {item["check_id"]: item for item in report["checks"]}
    assert checks["oracle_mode_known"]["ok"] is True
    assert checks["external_oracle_task_binding"]["ok"] is True
    assert checks["external_oracle_independent_sandbox"]["ok"] is True


def test_all_six_arms_are_admitted(tmp_path: Path) -> None:
    plan = contract.scope_plan(contract.CANARY_PLAN_ID)

    report = executor.run_admission_preflight(plan, scratch_root=tmp_path / "admission")

    assert report["contract_blocking_failures"] == []
    assert [item["cell_id"] for item in report["cells"]] == [
        cell["cell_id"] for cell in plan["cells"]
    ]
    assert all(item["status"] == READY for item in report["cells"])
    assert all(item["checks"] > 0 for item in report["cells"])


def test_the_admitted_options_inject_nothing(tmp_path: Path) -> None:
    """The admitted configuration must be the production one, not a test seam."""

    plan = contract.scope_plan(contract.CANARY_PLAN_ID)
    report = executor.run_admission_preflight(plan, scratch_root=tmp_path / "admission")

    assert all(item["provider_factory_injected"] is False for item in report["cells"])
    assert {item["oracle_mode"] for item in report["cells"]} == {ORACLE_MODE_EXTERNAL}
    assert [item["worker_exposure"] for item in report["cells"]] == [
        contract.ARMS_BY_ID[cell["arm_id"]].exposure for cell in plan["cells"]
    ]


# -- 5-8. the negative controls ---------------------------------------------------------------


def test_a_missing_external_contract_is_refused(tmp_path: Path) -> None:
    options, _ = _production_options(tmp_path)
    declared_but_absent = GoalAcceptance(
        spec=options.acceptance.spec,
        oracle_mode=ORACLE_MODE_EXTERNAL,
        external_oracle=None,
    )

    assert "acceptance_configured" in _blocking(_with(options, declared_but_absent))


def test_an_overlapping_hidden_suite_is_refused(tmp_path: Path) -> None:
    options, _ = _production_options(tmp_path)
    external = options.acceptance.external_oracle
    overlapping = dataclasses.replace(
        external, hidden_paths=tuple(sorted(external.public_paths))
    )

    blocking = _blocking(
        _with(
            options,
            GoalAcceptance(
                spec=options.acceptance.spec,
                oracle_mode=ORACLE_MODE_EXTERNAL,
                external_oracle=overlapping,
            ),
        )
    )

    assert "public_tests_separate_from_oracle" in blocking


def test_a_tampered_oracle_identity_is_refused(tmp_path: Path) -> None:
    options, _ = _production_options(tmp_path)
    external = options.acceptance.external_oracle

    tampered = _blocking(
        _with(
            options,
            GoalAcceptance(
                spec=options.acceptance.spec,
                oracle_mode=ORACLE_MODE_EXTERNAL,
                external_oracle=dataclasses.replace(
                    external, declared_digest="sha256:" + "0" * 64
                ),
            ),
        )
    )
    unbound = _blocking(
        _with(
            options,
            GoalAcceptance(
                spec=options.acceptance.spec,
                oracle_mode=ORACLE_MODE_EXTERNAL,
                external_oracle=dataclasses.replace(external, task_id="some-other-task"),
            ),
        )
    )

    assert "acceptance_configured" in tampered
    assert "external_oracle_task_binding" in unbound


def test_an_undeclared_verification_spec_is_refused(tmp_path: Path) -> None:
    options, _ = _production_options(tmp_path)
    spec = dataclasses.replace(options.acceptance.spec, declared_digest=None)

    blocking = _blocking(
        _with(options, GoalAcceptance(spec=spec, oracle_mode=ORACLE_MODE_EXTERNAL))
    )

    assert "acceptance_digest_declared" in blocking


# -- 9-10. the in-run contract is unchanged ----------------------------------------------------


def test_the_in_run_contract_still_passes(tmp_path: Path) -> None:
    options, _ = _production_options(tmp_path)
    in_run = GoalAcceptance(
        spec=options.acceptance.spec,
        dynamic_command="python3 -m pytest -q test_hidden_pipeline.py",
    )

    report = run_live_preflight(_with(options, in_run))

    assert report["status"] == READY, report["blocking_failures"]
    assert in_run.oracle_mode == ORACLE_MODE_IN_RUN


def test_an_in_run_contract_without_a_command_is_refused(tmp_path: Path) -> None:
    options, _ = _production_options(tmp_path)
    no_command = GoalAcceptance(spec=options.acceptance.spec, dynamic_command=None)

    blocking = _blocking(_with(options, no_command))

    assert blocking == {"public_tests_separate_from_oracle", "acceptance_configured"}


# -- 11. a refused admission costs no attempt --------------------------------------------------


def test_a_blocked_admission_consumes_no_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = contract.scope_plan(contract.CANARY_PLAN_ID)
    blocked = {"status": BLOCKED, "blocking_failures": ["x"], "cells": []}
    monkeypatch.setattr(executor, "run_admission_preflight", lambda *a, **k: blocked)

    with executor.claim_root_override(tmp_path / "claims"):
        claim = executor.claim_path(plan["digest"])
        report = executor.admission_gate(plan)

        assert report is not None
        assert report["status"] == BLOCKED
        assert claim.exists() is False


def test_an_admitted_gate_returns_nothing_to_refuse(tmp_path: Path) -> None:
    plan = contract.scope_plan(contract.CANARY_PLAN_ID)

    with executor.claim_root_override(tmp_path / "claims"):
        claim = executor.claim_path(plan["digest"])
        assert executor.admission_gate(plan, repo_root=contract.REPO_ROOT) is None
        # Admission itself never writes the claim; only dispatch does, after the gate passes.
        assert claim.exists() is False


# -- 12. the offline path is not affected --------------------------------------------------------


def _statement(tmp_path: Path, body: dict[str, Any]) -> dict[str, Any]:
    path = tmp_path / "owner-statement.json"
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return {
        "kind": "external_file",
        "path": str(path),
        "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "content_digest": authority.digest_of(body),
        "body": body,
    }


def _signed(tmp_path: Path) -> dict[str, Any]:
    plan = contract.scope_plan(contract.CANARY_PLAN_ID)
    head = executor.git_head()
    return authority.sign(
        authority.pending_record(
            plan,
            protocol_digest_value=authority.protocol_digest(contract.protocol_document()),
            executor=executor.executor_identity(),
        ),
        approved_by="test",
        approved_at="2026-11-25T00:00:00Z",
        owner_statement="test signature, not an approval",
        reviewed_head=head,
        execution_head=head,
        cost_stop_usd=1.0,
        per_cell_cost_stop_usd=0.5,
        provenance=_statement(tmp_path, {"statement": "test"}),
    )


def test_the_fake_provider_path_still_runs_six_cells(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(_signed(tmp_path)), encoding="utf-8")
    with executor.claim_root_override(tmp_path / "claims"):
        report = asyncio.run(
            executor.dispatch(
                contract.CANARY_PLAN_ID,
                runs_root=tmp_path / "runs",
                evidence_root=tmp_path / "evidence",
                authority_path=path,
                cell_runner=offline._synthetic_runner({}),
                repo_root=contract.REPO_ROOT,
                mode=executor.MODE_OFFLINE_PROOF,
                clock=lambda: "2026-11-25T00:00:00Z",
            )
        )

    assert report["complete"] is True, report.get("stop_reason")
    assert report["cells_started"] == 6
    assert report["cells_passed"] == 6


# -- 13. no oracle, no PASS --------------------------------------------------------------------


def test_a_cell_without_an_oracle_cannot_pass(tmp_path: Path) -> None:
    plan = contract.scope_plan(contract.CANARY_PLAN_ID)
    cell = plan["cells"][0]
    run = offline.synthetic_cell_run(cell, tmp_path)
    run.oracle = None

    outcome = executor.audit_cell(
        cell=cell,
        run=run,
        plan=plan,
        usage_before=dict.fromkeys(executor.REQUIRED_USAGE_FIELDS, 0),
        cost_before=0.0,
    )

    assert executor.STOP_ORACLE_ENVIRONMENT in outcome["stops"]
    assert outcome["verdict"] == "STOPPED"
    assert outcome["goal_correctness"] is not True


# -- 14. the consumed v1 plan cannot come back -------------------------------------------------


def test_the_consumed_v1_plan_cannot_be_dispatched(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="superseded"):
        contract.scope_plan(contract.CANARY_PLAN_ID_V1)

    with executor.claim_root_override(tmp_path / "claims"):
        claim = tmp_path / "claims" / (V1_PLAN_DIGEST.split(":", 1)[1] + ".claim")
        claim.parent.mkdir(parents=True, exist_ok=True)
        claim.write_text('{"historical": true}\n', encoding="utf-8")
        with pytest.raises(ValueError):
            asyncio.run(
                executor.dispatch(
                    contract.CANARY_PLAN_ID_V1,
                    runs_root=tmp_path / "runs",
                    evidence_root=tmp_path / "evidence",
                    mode=executor.MODE_OFFLINE_PROOF,
                )
            )
        assert claim.read_text(encoding="utf-8") == '{"historical": true}\n'


# -- the classification that v1 got wrong -------------------------------------------------------


def test_an_admission_refusal_is_not_a_provider_failure(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "preflight.json").write_text(
        json.dumps({"status": BLOCKED, "blocking_failures": ["acceptance_configured"]}),
        encoding="utf-8",
    )

    kind = executor._exception_kind(SystemExit("live preflight is BLOCKED"), run_dir)
    stop = executor._exception_stop(executor.CellRun(None, None, None, None, error_kind=kind))

    assert kind == "preflight"
    assert stop == executor.STOP_LIVE_PREFLIGHT
    assert stop != executor.STOP_PROVIDER_FAILURE


def test_a_provider_transport_error_is_still_a_provider_failure(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    class ProviderTransportError(RuntimeError):
        pass

    kind = executor._exception_kind(ProviderTransportError("boom"), run_dir)

    assert kind == "provider"
    assert executor._exception_stop(executor.CellRun(None, None, None, None, error_kind=kind)) == (
        executor.STOP_PROVIDER_FAILURE
    )


def test_a_missing_preflight_report_falls_back_to_the_type(tmp_path: Path) -> None:
    kind = executor._exception_kind(SystemExit("anything"), tmp_path / "absent")

    assert kind == "exception"
    assert executor._exception_stop(executor.CellRun(None, None, None, None, error_kind=kind)) == (
        executor.STOP_EXECUTION_EXCEPTION
    )
