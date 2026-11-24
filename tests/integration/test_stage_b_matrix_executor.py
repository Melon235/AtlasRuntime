"""Stage B S06: offline verification of the host-side matrix executor.

Most cells here are simulated, but the simulated evidence is shaped like the frozen runner's real
output: the full 15-item evidence contract, a manifest that covers exactly the published and raw
files, a claim whose identity matches the plan, and a Result whose status, failure kind, exit code
and raw session stop reason agree with each other. One test drives the real offline runner and hands
its evidence to the executor's audit.

No model, no sandbox, no benchmark cell.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from experiments.stage_b_executor import matrix_executor as mx  # noqa: E402
from experiments.stage_benchmark import protocol  # noqa: E402
from experiments.stage_benchmark.execution import (  # noqa: E402
    admission,
    offline,
    provision,
    runner,
)
from experiments.stage_benchmark.execution.models import CellRequest  # noqa: E402

EXECUTOR_DIR = REPO_ROOT / "experiments" / "stage_b_executor"
CONFIG_PATH = EXECUTOR_DIR / "matrix_config.json"
EXECUTOR_DIGEST = mx.digest_file(EXECUTOR_DIR / "matrix_executor.py")

GOOD_USAGE = {
    "main_prompt_hit_tokens": 1000,
    "main_prompt_miss_tokens": 200,
    "main_completion_tokens": 50,
    "child_total_prompt_tokens": 0,
    "child_completion_tokens": 0,
    "system_total_tokens": 1250,
}

COMPLETED = {
    "status": "COMPLETED",
    "failure_kind": "none",
    "execution_integrity": "OK",
    "goal_correctness": "PASS",
    "session_status": "succeeded",
    "stop_reason": "success",
    "claim_state": "COLLECTED",
    "oracle_ok": True,
    "public_status": "PASS",
    "returncode": 0,
}


def _failure(kind: str) -> dict[str, Any]:
    """The exact shape the frozen runner produces for each pre-registered failure."""

    base = {
        "status": "FAILED",
        "failure_kind": kind,
        "claim_state": "FAILED",
        "returncode": 1,
        "public_status": "PASS",
        "oracle_ok": True,
    }
    if kind == "oracle_failed":
        base.update(execution_integrity="OK", goal_correctness="FAIL", oracle_ok=False)
    elif kind == "public_tests_failed":
        base.update(
            execution_integrity="OK", goal_correctness="FAIL", public_status="FAIL"
        )
    elif kind == "budget_exhaustion":
        base.update(
            execution_integrity="FAILED",
            goal_correctness="UNKNOWN",
            stop_reason="budget_exceeded",
            rejection_code="budget_exhausted",
        )
    elif kind == "main_max_turns":
        base.update(
            execution_integrity="FAILED", goal_correctness="UNKNOWN", stop_reason="max_turns"
        )
    return base


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digests(root: Path) -> dict[str, str]:
    """Every file under a cell, so a read-only grader can be proven read-only."""

    return {
        str(path.relative_to(root)).replace("\\", "/"): _sha(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    base = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    base["evidence_root"] = str(tmp_path / "cells")
    base["runs_root"] = str(tmp_path / "runs")
    base["authorization_path"] = str(tmp_path / "authorization.json")
    base.update(overrides)
    return base


def _config_path(config: dict[str, Any], tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _authorization(config: dict[str, Any], config_digest: str, head: str, **overrides: Any):
    record: dict[str, Any] = {
        "status": "APPROVED",
        "approved_by": "Owner (test)",
        "approval_commit": head,
        "approved_at": "2026-11-24T00:00:00+00:00",
        "consumed": False,
        "cells": [cell["cell_id"] for cell in config["cells"]],
        "attempts_allowed": 1,
        "attempts_per_cell": 1,
        "cost_ceiling_usd": config["cost_stop_usd"],
        "stop_policy": config["stop_policy"],
        "approved_digests": {
            "protocol": config["protocol_digest"],
            "plan": config["plan_digest"],
            "runtime": config["runtime_digest"],
            "runner_source": config["harness_source_digest"],
        },
        "executor_digest": EXECUTOR_DIGEST,
        "config_digest": config_digest,
        "executor_commit": head,
        "freeze_binding": {
            "version": config["freeze_version"],
            "raw_sha256": config["freeze_raw_sha256"],
            "content_digest": config["freeze_content_digest"],
        },
        "c3_rerun_allowed": False,
    }
    record.update(overrides)
    return record


def _write_cell(
    cell_root: Path,
    *,
    cell: dict[str, Any],
    harness_digest: str,
    authorization_digest: str,
    scenario: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
    drop_from_manifest: tuple[str, ...] = (),
    empty_raw: bool = False,
    escape_path: bool = False,
    claim_overrides: dict[str, Any] | None = None,
    result_overrides: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    usage_drop: tuple[str, ...] = (),
    lifecycle: dict[str, Any] | None = None,
    session_overrides: dict[str, Any] | None = None,
    internal_claim_state: str = "RUNNING",
) -> None:
    scenario = {**COMPLETED, **(scenario or {})}
    evidence = cell_root / "evidence"
    raw = evidence / "_raw"
    (evidence / "trajectory" / "run-1").mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)
    (cell_root / "attempts").mkdir(parents=True, exist_ok=True)

    claim = {
        "state": scenario["claim_state"],
        "retries_allowed": False,
        "cell": {
            "cell_id": cell["cell_id"],
            "task_id": cell["task_id"],
            "arm_id": cell["arm_id"],
            "repetition": cell["repetition"],
        },
        "runner_source_digest": harness_digest,
        "authorization_digest": authorization_digest,
        "preflight_digest": "sha256:" + "9" * 64,
    }
    claim.update(claim_overrides or {})
    (cell_root / "attempts" / "attempt-1.json").write_text(json.dumps(claim), encoding="utf-8")

    result = {
        "status": scenario["status"],
        "failure_kind": scenario["failure_kind"],
        "execution_integrity": scenario["execution_integrity"],
        "goal_correctness": scenario["goal_correctness"],
        "secondary_failures": [],
        "cell": {
            "cell_id": cell["cell_id"],
            "task_id": cell["task_id"],
            "arm_id": cell["arm_id"],
            "repetition": cell["repetition"],
            "in_matrix": True,
        },
        "digests": {"runner_source": harness_digest},
    }
    result.update(result_overrides or {})
    (cell_root / "result.json").write_text(json.dumps(result), encoding="utf-8")

    session = {
        "status": scenario.get("session_status", "failed"),
        "main": {"stop_reason": scenario.get("stop_reason", "success")},
        "budget": {"rejection_code": scenario.get("rejection_code")},
        # An AGENT-stage run: the collector's not_applicable rule must not excuse a missing file.
        "metrics": {"main_requests": 12},
        "sandbox": {"created": True, "destroyed": True, "retained": False},
        "execution_integrity": {"sandbox_failure": None},
    }
    session.update(session_overrides or {})
    (evidence / "session.json").write_text(json.dumps(session), encoding="utf-8")

    internal_claim = dict(claim)
    internal_claim["state"] = internal_claim_state
    (evidence / "attempt-1.json").write_text(json.dumps(internal_claim), encoding="utf-8")

    (evidence / "transcript.jsonl").write_text('{"turn": 1}\n', encoding="utf-8")
    conversations = json.dumps({"run-main": []})
    (evidence / "tool_conversations.json").write_text(conversations, encoding="utf-8")
    (evidence / "workspace.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (evidence / "trajectory" / "run-1" / "meta.json").write_text("{}", encoding="utf-8")
    (evidence / "trajectory" / "run-1" / "transcript.jsonl").write_text(
        '{"turn": 1}\n', encoding="utf-8"
    )
    (evidence / "router_decisions.json").write_text(json.dumps({"decisions": []}), encoding="utf-8")
    (evidence / "worker_outcomes.json").write_text(json.dumps({"workers": []}), encoding="utf-8")
    (evidence / "handoff_references.json").write_text(json.dumps({"refs": {}}), encoding="utf-8")
    # ``runs`` is the ordered history of every call; ``public_runs`` exercises a repaired suite.
    statuses = scenario.get("public_runs") or [scenario["public_status"]]
    (evidence / "public_tests.json").write_text(
        json.dumps({"calls": len(statuses), "runs": [{"status": item} for item in statuses]}),
        encoding="utf-8",
    )
    oracle = {
        "ok": scenario["oracle_ok"],
        "source": "host:fake:executor-fixture",
        "executed_after_main_stops": True,
    }
    oracle.update(scenario.get("oracle_overrides") or {})
    (evidence / "oracle.json").write_text(json.dumps(oracle), encoding="utf-8")
    merged_usage = dict(GOOD_USAGE)
    merged_usage.update(usage or {})
    for key in usage_drop:
        merged_usage.pop(key, None)
    (evidence / "provider_usage.json").write_text(json.dumps(merged_usage), encoding="utf-8")
    merged_lifecycle = {
        "cleanup_failure": None,
        "sandbox": {"created": True, "destroyed": True, "retained": False},
    }
    merged_lifecycle.update(lifecycle or {})
    (evidence / "sandbox_lifecycle.json").write_text(json.dumps(merged_lifecycle), encoding="utf-8")
    analysis = {
        "status": "COMPLETE",
        "record": {
            "execution_view": {
                "goal_correctness": {
                    "status": scenario.get("analysis_goal", scenario["goal_correctness"])
                }
            },
            "usage": {"sum_matches_system_total": True},
        },
    }
    (evidence / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    (raw / "session.json").write_text(json.dumps(session), encoding="utf-8")
    (raw / "workspace.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")

    for name in drop:
        (evidence / name).unlink(missing_ok=True)

    published = {
        str(path.relative_to(evidence)).replace("\\", "/"): _sha(path)
        for path in sorted(evidence.rglob("*"))
        if path.is_file()
        and path.relative_to(evidence).parts[0] != mx.RAW_DIR
        and path.name != mx.MANIFEST
        and str(path.relative_to(evidence)).replace("\\", "/") not in drop_from_manifest
    }
    raw_digests = (
        {}
        if empty_raw
        else {
            str(path.relative_to(raw)).replace("\\", "/"): _sha(path)
            for path in sorted(raw.rglob("*"))
            if path.is_file()
        }
    )
    if escape_path:
        published["../escaped.json"] = "sha256:" + "0" * 64
    (evidence / mx.MANIFEST).write_text(
        json.dumps(
            {
                "files": published,
                "raw_digests": raw_digests,
                "publication": {"ok": True, "problems": []},
            }
        ),
        encoding="utf-8",
    )


def _launcher(calls: list[str], plans: dict[str, dict[str, Any]] | None = None, on_call=None):
    plans = plans or {}

    def launch(argv, *, cell_root, cwd, stdout_path, stderr_path, timeout):
        cell_root = Path(cell_root)
        cell_id = cell_root.parent.name
        calls.append(cell_id)
        plan = dict(plans.get(cell_id, {}))
        if on_call is not None:
            on_call(cell_id)
        if not plan.get("no_evidence"):
            repetition = int(cell_root.name.lstrip("r"))
            cell = {
                "cell_id": cell_id,
                "task_id": plan.get("task_id") or _TASK.get(cell_id, cell_id.split(".")[0]),
                "arm_id": plan.get("arm_id") or cell_id.split(".")[1],
                "repetition": repetition,
            }
            _write_cell(
                cell_root,
                cell=cell,
                harness_digest=_HARNESS[0],
                authorization_digest=_AUTH[0],
                scenario=plan.get("scenario"),
                drop=tuple(plan.get("drop") or ()),
                drop_from_manifest=tuple(plan.get("drop_from_manifest") or ()),
                empty_raw=bool(plan.get("empty_raw")),
                escape_path=bool(plan.get("escape_path")),
                claim_overrides=plan.get("claim_overrides"),
                result_overrides=plan.get("result_overrides"),
                usage=plan.get("usage"),
                usage_drop=tuple(plan.get("usage_drop") or ()),
                lifecycle=plan.get("lifecycle"),
                session_overrides=plan.get("session_overrides"),
                internal_claim_state=plan.get("internal_claim_state", "RUNNING"),
            )
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(f"cell {cell_id}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        scenario = {**COMPLETED, **(plan.get("scenario") or {})}
        return mx.LaunchResult(
            returncode=plan.get("returncode", scenario.get("returncode", 0)),
            pid=4242,
            timed_out=plan.get("timed_out", False),
            started_at="2026-11-24T00:00:00+00:00",
            finished_at="2026-11-24T00:00:10+00:00",
            duration_seconds=10.0,
        )

    return launch


_HARNESS: list[str] = [""]
_AUTH: list[str] = [""]
_TASK: dict[str, str] = {}


def _run(
    tmp_path: Path,
    config: dict[str, Any],
    *,
    authorization: Any = "DEFAULT",
    plans: dict[str, dict[str, Any]] | None = None,
    launcher: Any = None,
    on_call=None,
    write_authorization: bool = True,
):
    config_path = _config_path(config, tmp_path)
    config_digest = mx.digest_file(config_path)
    head = mx._git(REPO_ROOT, "rev-parse", "HEAD")

    if authorization == "DEFAULT":
        authorization = _authorization(config, config_digest, head)
    if write_authorization and authorization is not None:
        Path(config["authorization_path"]).write_text(json.dumps(authorization), encoding="utf-8")

    _HARNESS[0] = config["harness_source_digest"]
    _AUTH[0] = admission.canonical_digest(dict(authorization)) if authorization else ""
    _TASK.clear()
    _TASK.update({cell["cell_id"]: cell["task_id"] for cell in config["cells"]})

    calls: list[str] = []
    report = mx.run_matrix(
        config,
        repo_root=REPO_ROOT,
        authorization=authorization,
        config_digest=config_digest,
        executor_digest=EXECUTOR_DIGEST,
        launcher=launcher or _launcher(calls, plans, on_call),
        verify_pr=False,
    )
    return report, calls


# --------------------------------------------------------------------------- full matrix


def test_the_matrix_runs_all_36_cells_in_the_frozen_order(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(tmp_path, config)
    assert report["stop_reason"] is None
    assert calls == config["order"]
    assert report["cells_started"] == 36
    assert report["cells_observed"] == 36
    assert report["cells_passed"] == 36
    assert report["cells_failed"] == 0
    assert report["cells_invalid_or_unknown"] == 0
    assert report["cells_not_run"] == 0
    assert report["retry"] == "FORBIDDEN" and report["resume"] == "FORBIDDEN"


# --------------------------------------------------------------------------- measured failures


@pytest.mark.parametrize(
    "kind", ["oracle_failed", "public_tests_failed", "budget_exhaustion", "main_max_turns"]
)
def test_a_measured_agent_failure_continues(tmp_path: Path, kind: str) -> None:
    config = _config(tmp_path)
    failing = config["order"][3]
    report, calls = _run(tmp_path, config, plans={failing: {"scenario": _failure(kind)}})
    assert report["stop_reason"] is None, report["cells"][3]["problems"]
    assert calls == config["order"]
    assert report["cells_started"] == 36
    assert report["cells_passed"] == 35
    assert report["cells_failed"] == 1
    assert report["cells_invalid_or_unknown"] == 0
    failed = next(cell for cell in report["cells"] if cell["cell_id"] == failing)
    assert failed["verdict"] == mx.VERDICT_FAILED
    assert failed["failure_kind"] == kind


def test_a_measured_failure_keeps_the_frozen_verdict(tmp_path: Path) -> None:
    config = _config(tmp_path)
    failing = config["order"][0]
    _report, _calls = _run(
        tmp_path, config, plans={failing: {"scenario": _failure("budget_exhaustion")}}
    )
    result = json.loads(
        (Path(config["evidence_root"]) / failing / "r1" / "result.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "FAILED"
    assert result["failure_kind"] == "budget_exhaustion"
    assert result["execution_integrity"] == "FAILED"


# --------------------------------------------------------------------------- Task B: consistency


def test_a_result_for_another_cell_is_a_contradiction(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(
        tmp_path,
        config,
        plans={
            config["order"][0]: {
                "result_overrides": {
                    "cell": {
                        "cell_id": "other.cell",
                        "task_id": "other",
                        "arm_id": "P1",
                        "repetition": 1,
                    }
                }
            }
        },
    )
    assert report["stop_reason"] == mx.STOP_OBSERVATION_CONTRADICTION
    assert len(calls) == 1


def test_an_oracle_failure_with_a_passing_oracle_is_a_contradiction(tmp_path: Path) -> None:
    config = _config(tmp_path)
    scenario = {**_failure("oracle_failed"), "oracle_ok": True}
    report, _calls = _run(tmp_path, config, plans={config["order"][0]: {"scenario": scenario}})
    assert report["stop_reason"] == mx.STOP_OBSERVATION_CONTRADICTION


def test_a_completed_result_with_a_failing_oracle_is_a_contradiction(tmp_path: Path) -> None:
    """A pass cannot survive an authoritative host oracle FAIL."""

    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path, config, plans={config["order"][0]: {"scenario": {"oracle_ok": False}}}
    )
    assert report["stop_reason"] == mx.STOP_OBSERVATION_CONTRADICTION
    assert any("host oracle" in problem for problem in report["cells"][0]["problems"])


def test_a_completed_result_with_a_failing_analysis_is_a_contradiction(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path, config, plans={config["order"][0]: {"scenario": {"analysis_goal": "FAIL"}}}
    )
    assert report["stop_reason"] == mx.STOP_OBSERVATION_CONTRADICTION


def test_a_public_failure_whose_analysis_agrees_continues(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path, config, plans={config["order"][0]: {"scenario": _failure("public_tests_failed")}}
    )
    assert report["stop_reason"] is None


def test_a_budget_failure_without_a_budget_stop_is_a_contradiction(tmp_path: Path) -> None:
    config = _config(tmp_path)
    scenario = {**_failure("budget_exhaustion"), "stop_reason": "success", "rejection_code": None}
    report, _calls = _run(tmp_path, config, plans={config["order"][0]: {"scenario": scenario}})
    assert report["stop_reason"] == mx.STOP_OBSERVATION_CONTRADICTION


def test_a_completed_result_with_a_failed_claim_is_a_contradiction(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path, config, plans={config["order"][0]: {"scenario": {"claim_state": "FAILED"}}}
    )
    assert report["stop_reason"] == mx.STOP_OBSERVATION_CONTRADICTION


def test_a_public_failure_without_a_failing_run_is_a_contradiction(tmp_path: Path) -> None:
    config = _config(tmp_path)
    scenario = {**_failure("public_tests_failed"), "public_status": "PASS"}
    report, _calls = _run(tmp_path, config, plans={config["order"][0]: {"scenario": scenario}})
    assert report["stop_reason"] == mx.STOP_OBSERVATION_CONTRADICTION


def test_a_completed_cell_with_a_repaired_public_suite_is_accepted(tmp_path: Path) -> None:
    """The frozen analyser judges the last suite; two repaired failures are history, not a stop."""

    config = _config(tmp_path)
    repaired = {"scenario": {"public_runs": ["FAIL", "FAIL", "PASS"]}}
    report, _calls = _run(tmp_path, config, plans={config["order"][0]: repaired})
    assert report["stop_reason"] is None
    assert report["cells"][0]["verdict"] == mx.VERDICT_PASSED


def test_a_completed_cell_whose_final_public_run_fails_is_a_contradiction(tmp_path: Path) -> None:
    config = _config(tmp_path)
    regressed = {"scenario": {"public_runs": ["PASS", "FAIL"]}}
    report, _calls = _run(tmp_path, config, plans={config["order"][0]: regressed})
    assert report["stop_reason"] == mx.STOP_OBSERVATION_CONTRADICTION
    assert any("final public test run" in problem for problem in report["cells"][0]["problems"])


def test_a_public_failure_whose_final_run_passes_is_a_contradiction(tmp_path: Path) -> None:
    """``public_tests_failed`` is only consistent with a failing *final* suite."""

    config = _config(tmp_path)
    scenario = {**_failure("public_tests_failed"), "public_runs": ["FAIL", "PASS"]}
    report, _calls = _run(tmp_path, config, plans={config["order"][0]: {"scenario": scenario}})
    assert report["stop_reason"] == mx.STOP_OBSERVATION_CONTRADICTION


def test_a_public_failure_after_an_earlier_pass_continues(tmp_path: Path) -> None:
    config = _config(tmp_path)
    scenario = {**_failure("public_tests_failed"), "public_runs": ["PASS", "FAIL"]}
    report, _calls = _run(tmp_path, config, plans={config["order"][0]: {"scenario": scenario}})
    assert report["stop_reason"] is None
    assert report["cells"][0]["verdict"] == mx.VERDICT_FAILED
    assert report["cells"][0]["failure_kind"] == "public_tests_failed"


def test_an_internal_claim_snapshot_may_still_be_running(tmp_path: Path) -> None:
    """The frozen collector copies the marker before the terminal state is written."""

    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path, config, plans={config["order"][0]: {"internal_claim_state": "RUNNING"}}
    )
    assert report["stop_reason"] is None
    assert report["cells_passed"] == 36


# --------------------------------------------------------------------------- Task A: evidence


def test_a_missing_contract_file_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path, config, plans={config["order"][0]: {"drop": ("tool_conversations.json",)}}
    )
    assert report["stop_reason"] == mx.STOP_EVIDENCE_MISSING


def test_an_empty_raw_digest_set_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(tmp_path, config, plans={config["order"][0]: {"empty_raw": True}})
    assert report["stop_reason"] == mx.STOP_EVIDENCE_DRIFT
    assert any("raw digests" in problem for problem in report["cells"][0]["problems"])


def test_a_file_the_manifest_does_not_cover_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        plans={config["order"][0]: {"drop_from_manifest": ("transcript.jsonl",)}},
    )
    assert report["stop_reason"] == mx.STOP_EVIDENCE_DRIFT
    assert any("not covered" in problem for problem in report["cells"][0]["problems"])


def test_a_manifest_path_that_escapes_the_bundle_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(tmp_path, config, plans={config["order"][0]: {"escape_path": True}})
    assert report["stop_reason"] == mx.STOP_EVIDENCE_DRIFT
    assert any("escapes" in problem for problem in report["cells"][0]["problems"])


def test_a_secondary_failure_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        plans={
            config["order"][0]: {
                "result_overrides": {"secondary_failures": ["collector_failure: KeyError"]}
            }
        },
    )
    assert report["stop_reason"] == mx.STOP_SECONDARY_FAILURE


def test_an_unknown_failure_kind_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    scenario = {
        "status": "FAILED",
        "failure_kind": "provider_exception",
        "claim_state": "FAILED",
        "returncode": 1,
    }
    report, calls = _run(tmp_path, config, plans={config["order"][1]: {"scenario": scenario}})
    assert report["stop_reason"] == mx.STOP_OBSERVATION_CONTRADICTION
    assert len(calls) == 2


# --------------------------------------------------------------------------- exit codes / sandbox


def test_an_exit_code_that_contradicts_the_result_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(tmp_path, config, plans={config["order"][0]: {"returncode": 1}})
    assert report["stop_reason"] == mx.STOP_EXIT_MISMATCH


def test_a_failed_result_with_exit_one_is_consistent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(
        tmp_path, config, plans={config["order"][0]: {"scenario": _failure("oracle_failed")}}
    )
    assert report["stop_reason"] is None
    assert len(calls) == 36


def test_an_unobserved_exit_code_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(tmp_path, config, plans={config["order"][0]: {"returncode": None}})
    assert report["stop_reason"] == mx.STOP_EXIT_UNOBSERVED


def test_a_timeout_stops_without_a_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(
        tmp_path, config, plans={config["order"][1]: {"timed_out": True, "returncode": None}}
    )
    assert report["stop_reason"] == mx.STOP_TIMEOUT
    assert calls.count(config["order"][1]) == 1


def test_a_residual_sandbox_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        plans={config["order"][0]: {"lifecycle": {"sandbox": {"destroyed": False}}}},
    )
    assert report["stop_reason"] == mx.STOP_RESIDUAL_SANDBOX


# --------------------------------------------------------------------------- usage and cost


def test_worker_tokens_are_priced_into_the_total(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = {
        "main_prompt_hit_tokens": 0,
        "main_prompt_miss_tokens": 0,
        "main_completion_tokens": 0,
        "child_total_prompt_tokens": 100_000,
        "child_completion_tokens": 10_000,
        "system_total_tokens": 110_000,
    }
    plans = {cell_id: {"usage": worker} for cell_id in config["order"]}
    report, _calls = _run(tmp_path, config, plans=plans)
    assert report["cells_started"] == 36
    per_cell = 100_000 / 1e6 * 0.30 + 10_000 / 1e6 * 1.20
    assert abs(report["total_cost_usd"] - per_cell * 36) < 1e-6
    cost = report["cells"][0]["usage"]["cost"]
    assert cost["child_prompt_billed_as"] == "cache_miss_input"
    assert cost["child_cache_estimated"] is True


def test_a_missing_worker_counter_is_unknown_not_zero(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        plans={config["order"][0]: {"usage_drop": ("child_total_prompt_tokens",)}},
    )
    assert report["stop_reason"] == mx.STOP_USAGE_UNKNOWN


def test_a_negative_counter_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path, config, plans={config["order"][0]: {"usage": {"child_completion_tokens": -1}}}
    )
    assert report["stop_reason"] == mx.STOP_USAGE_UNKNOWN


def test_a_token_reconciliation_failure_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path, config, plans={config["order"][0]: {"usage": {"system_total_tokens": 999}}}
    )
    assert report["stop_reason"] == mx.STOP_USAGE_MISMATCH


def test_the_cost_stop_prevents_the_next_cell(tmp_path: Path) -> None:
    config = _config(tmp_path)
    expensive = {
        "main_prompt_hit_tokens": 0,
        "main_prompt_miss_tokens": 3_000_000,
        "main_completion_tokens": 50_000,
        "child_total_prompt_tokens": 500_000,
        "child_completion_tokens": 0,
        "system_total_tokens": 3_550_000,
    }
    plans = {cell_id: {"usage": expensive} for cell_id in config["order"]}
    report, calls = _run(tmp_path, config, plans=plans)
    assert report["stop_reason"] == mx.STOP_COST_REACHED
    assert report["stopped_at_order"] == 3
    assert len(calls) == 3


def test_the_token_stop_prevents_the_next_cell(tmp_path: Path) -> None:
    config = _config(tmp_path)
    heavy = {
        "main_prompt_hit_tokens": 6_000_000,
        "main_prompt_miss_tokens": 500_000,
        "main_completion_tokens": 90_000,
        "child_total_prompt_tokens": 400_000,
        "child_completion_tokens": 10_000,
        "system_total_tokens": 7_000_000,
    }
    plans = {cell_id: {"usage": heavy} for cell_id in config["order"]}
    report, calls = _run(tmp_path, config, plans=plans)
    assert report["stop_reason"] == mx.STOP_TOKENS_REACHED
    assert len(calls) == 3


# --------------------------------------------------------------------------- Task C: authorization


def test_the_object_must_match_the_file_it_claims_to_come_from(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_path = _config_path(config, tmp_path)
    head = mx._git(REPO_ROOT, "rev-parse", "HEAD")
    record = _authorization(config, mx.digest_file(config_path), head)
    Path(config["authorization_path"]).write_text(json.dumps(record), encoding="utf-8")
    changed = dict(record)
    changed["approved_at"] = "2026-11-24T00:00:01+00:00"
    with pytest.raises(mx.ExecutorError, match="does not match the file"):
        mx.run_matrix(
            config,
            repo_root=REPO_ROOT,
            authorization=changed,
            config_digest=mx.digest_file(config_path),
            executor_digest=EXECUTOR_DIGEST,
            launcher=_launcher([]),
            verify_pr=False,
        )


def test_an_authorization_replaced_before_the_first_cell_starts_nothing(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config_path = _config_path(config, tmp_path)
    head = mx._git(REPO_ROOT, "rev-parse", "HEAD")
    record = _authorization(config, mx.digest_file(config_path), head)
    path = Path(config["authorization_path"])
    path.write_text(json.dumps({**record, "approved_by": "someone else"}), encoding="utf-8")

    _HARNESS[0] = config["harness_source_digest"]
    _AUTH[0] = admission.canonical_digest(record)
    calls: list[str] = []
    # The file no longer is the record that passed the gate: the matrix refuses to start at all.
    with pytest.raises(mx.ExecutorError, match="does not match the file"):
        mx.run_matrix(
            config,
            repo_root=REPO_ROOT,
            authorization=record,
            config_digest=mx.digest_file(config_path),
            executor_digest=EXECUTOR_DIGEST,
            launcher=_launcher(calls),
            verify_pr=False,
        )
    assert calls == []


def test_an_authorization_replaced_between_cells_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = Path(config["authorization_path"])

    def swap(cell_id: str) -> None:
        if cell_id == config["order"][1]:
            record = json.loads(path.read_text(encoding="utf-8"))
            record["approved_by"] = "someone else"
            path.write_text(json.dumps(record), encoding="utf-8")

    report, calls = _run(tmp_path, config, on_call=swap)
    assert report["stop_reason"] == mx.STOP_AUTHORIZATION_CHANGED
    assert len(calls) == 2


def test_an_unsigned_authorization_starts_no_cell(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[str] = []
    with pytest.raises(mx.ExecutorError):
        _run(tmp_path, config, authorization=None, launcher=_launcher(calls))
    assert calls == []


def test_a_record_for_another_commit_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_path = _config_path(config, tmp_path)
    record = _authorization(config, mx.digest_file(config_path), "sha256:" + "3" * 64)
    Path(config["authorization_path"]).write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(mx.ExecutorError, match="executor commit"):
        mx.run_matrix(
            config,
            repo_root=REPO_ROOT,
            authorization=record,
            config_digest=mx.digest_file(config_path),
            executor_digest=EXECUTOR_DIGEST,
            launcher=_launcher([]),
            verify_pr=False,
        )


def test_a_cli_path_that_differs_from_the_config_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_path = _config_path(config, tmp_path)
    other = tmp_path / "other-authorization.json"
    other.write_text("{}", encoding="utf-8")
    code = mx.main(
        ["--config", str(config_path), "--authorization", str(other), "--repo-root", str(REPO_ROOT)]
    )
    assert code == 3


# --------------------------------------------------------------------------- locks and ledger


def test_a_second_executor_cannot_take_the_lock(tmp_path: Path) -> None:
    config = _config(tmp_path)
    evidence_root = Path(config["evidence_root"])
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / mx.LOCK_NAME).write_text('{"pid": 1}', encoding="utf-8")
    with pytest.raises(mx.MatrixLocked):
        _run(tmp_path, config)


def test_a_stop_keeps_the_dispatch_and_ledger_records(tmp_path: Path) -> None:
    config = _config(tmp_path)
    scenario = {
        "status": "FAILED",
        "failure_kind": "provider_exception",
        "claim_state": "FAILED",
        "returncode": 1,
    }
    report, _calls = _run(tmp_path, config, plans={config["order"][2]: {"scenario": scenario}})
    assert report["stop_reason"] == mx.STOP_OBSERVATION_CONTRADICTION
    dispatch = Path(report["dispatch_log"]).read_text(encoding="utf-8").strip().splitlines()
    ledger = Path(report["ledger_log"]).read_text(encoding="utf-8").strip().splitlines()
    assert len(dispatch) == 3 and len(ledger) == 3
    assert json.loads(ledger[-1])["verdict"] == mx.VERDICT_INVALID
    assert not (Path(config["evidence_root"]) / mx.LOCK_NAME).exists()


def test_an_existing_ledger_is_never_overwritten(tmp_path: Path) -> None:
    config = _config(tmp_path)
    evidence_root = Path(config["evidence_root"])
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / mx.LEDGER_LOG).write_text('{"order": 1}\n', encoding="utf-8")
    calls: list[str] = []
    with pytest.raises(mx.ExecutorError, match="ledger"):
        _run(tmp_path, config, launcher=_launcher(calls))
    assert calls == []


def test_a_consumed_claim_stops_before_launch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    blocked = config["cells"][1]
    cell_root = Path(config["evidence_root"]) / blocked["cell_id"] / f"r{blocked['repetition']}"
    (cell_root / "attempts").mkdir(parents=True, exist_ok=True)
    (cell_root / "attempts" / "attempt-1.json").write_text(
        json.dumps({"state": "FAILED", "retries_allowed": False}), encoding="utf-8"
    )
    report, calls = _run(tmp_path, config)
    assert report["stop_reason"] == mx.STOP_CLAIM_CONSUMED
    assert calls == [config["order"][0]]


def test_the_report_carries_the_real_authorization_digest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(tmp_path, config)
    record = json.loads(Path(config["authorization_path"]).read_text(encoding="utf-8"))
    assert report["authorization_digest"] == admission.canonical_digest(record)
    assert report["authorization_sha256"] == _sha(Path(config["authorization_path"]))


# --------------------------------------------------------------------------- config guards


def test_the_config_version_matches_the_executor() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["executor_version"] == mx.EXECUTOR_VERSION
    assert config["stop_policy"] == mx.POLICY_MEASURED_CONTINUE
    assert config["cost_stop_usd"] == 3.0
    assert config["pull_request"]["number"] == 6


def test_a_config_with_the_wrong_policy_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path, stop_policy="STOP_ON_ANY_FAILURE")
    with pytest.raises(mx.ExecutorError, match="stop_policy"):
        mx.load_config(_config_path(config, tmp_path))


def test_a_matrix_of_the_wrong_size_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["cells"] = config["cells"][:35]
    config["order"] = config["order"][:35]
    with pytest.raises(mx.ExecutorError, match="36 cells"):
        mx.load_config(_config_path(config, tmp_path))


def test_an_order_that_drifts_from_the_cell_list_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["order"] = list(reversed(config["order"]))
    with pytest.raises(mx.ExecutorError, match="order"):
        mx.load_config(_config_path(config, tmp_path))


# --------------------------------------------------------------------------- real runner


REAL_RUNNER_SCENARIOS = [
    ("COMPLETED", {}, True, "none", mx.VERDICT_PASSED),
    ("oracle_failed", {"oracle": "FAIL"}, False, "oracle_failed", mx.VERDICT_FAILED),
    ("public_tests_failed", {"public": "FAIL"}, False, "public_tests_failed", mx.VERDICT_FAILED),
    (
        "budget_exhaustion",
        {"status": "failed", "stop": "budget_exceeded"},
        False,
        "budget_exhaustion",
        mx.VERDICT_FAILED,
    ),
    (
        "main_max_turns",
        {"status": "failed", "stop": "max_turns"},
        False,
        "main_max_turns",
        mx.VERDICT_FAILED,
    ),
]


@pytest.mark.parametrize(
    ("label", "session_kwargs", "expect_ok", "expect_kind", "expect_verdict"),
    REAL_RUNNER_SCENARIOS,
)
def test_the_real_offline_runner_evidence_is_accepted(
    tmp_path: Path,
    label: str,
    session_kwargs: dict[str, Any],
    expect_ok: bool,
    expect_kind: str,
    expect_verdict: str,
) -> None:
    """The frozen runner produces the evidence; the executor only grades it."""

    document = protocol.read_protocol()
    cell = provision.cell_from_plan(document, "slugkit.P1.r1")

    async def scripted(options: Any) -> dict[str, Any]:
        payload = offline.synthetic_session(options, **session_kwargs)
        offline.write_synthetic_run(options, payload)
        return payload

    request = CellRequest(
        cell=cell,
        runs_root=tmp_path / "runs",
        evidence_root=tmp_path / "matrix",
        protocol_document=document,
        provider_factory=offline.FakeProvider().build,
        sandbox_factory=lambda _root: offline.FakeProvider(),
        run_fn=scripted,
        host_oracle={
            "source": "host:fake:executor-audit",
            "task_id": cell.task_id,
            "ok": label != "oracle_failed",
            "executed_after_main_stops": True,
        },
    )
    result = asyncio.run(runner.execute_cell(request))
    assert result.ok is expect_ok, (label, result.failure_kind, result.detail)
    assert result.failure_kind == expect_kind

    cell_root = tmp_path / "matrix" / cell.cell_id / f"r{cell.repetition}"
    before = _tree_digests(cell_root)
    outcome = mx.audit_cell(
        repo_root=REPO_ROOT,
        cell_root=cell_root,
        cell={
            "order": 1,
            "cell_id": cell.cell_id,
            "task_id": cell.task_id,
            "arm_id": cell.arm_id,
            "repetition": cell.repetition,
        },
        exit_code=0 if result.ok else 1,
        timed_out=False,
        harness_source_digest=admission.source_identity()["digest"],
        authorization_digest="OFFLINE",
    )
    assert outcome.problems == [], (label, outcome.problems)
    assert outcome.stops == []
    assert outcome.verdict == expect_verdict
    assert outcome.failure_kind == expect_kind
    assert outcome.usage["system_total_tokens"] > 0

    # The frozen verdict is graded, never rewritten.
    on_disk = json.loads((cell_root / "result.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == ("COMPLETED" if expect_ok else "FAILED")
    assert on_disk["failure_kind"] == expect_kind
    # Grading is read-only: not one byte of the cell moved.
    assert _tree_digests(cell_root) == before
