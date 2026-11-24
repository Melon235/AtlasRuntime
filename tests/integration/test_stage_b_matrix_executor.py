"""Stage B S06: offline verification of the host-side matrix executor.

Every cell is simulated. The fake launcher writes a real-shaped evidence bundle (manifest digests,
claim identity, usage reconciliation) into the cell directory, so the executor's post-cell audit
runs exactly as it will on a live cell. No model, no sandbox, no benchmark cell.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from experiments.stage_b_executor import matrix_executor as mx  # noqa: E402
from experiments.stage_benchmark.execution import admission  # noqa: E402

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


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


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
    cell_id: str,
    repetition: int,
    harness_digest: str,
    authorization_digest: str,
    status: str = "COMPLETED",
    failure_kind: str = "none",
    execution_integrity: str = "OK",
    secondary: list[str] | None = None,
    usage: dict[str, Any] | None = None,
    usage_drop: tuple[str, ...] = (),
    lifecycle: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
    drift_manifest: bool = False,
    claim_overrides: dict[str, Any] | None = None,
) -> None:
    evidence = cell_root / "evidence"
    raw = evidence / "_raw"
    raw.mkdir(parents=True, exist_ok=True)
    (cell_root / "attempts").mkdir(parents=True, exist_ok=True)

    claim = {
        "state": "COLLECTED" if status == "COMPLETED" else "FAILED",
        "retries_allowed": False,
        "cell": {"cell_id": cell_id, "repetition": repetition},
        "runner_source_digest": harness_digest,
        "authorization_digest": authorization_digest,
    }
    claim.update(claim_overrides or {})
    (cell_root / "attempts" / "attempt-1.json").write_text(json.dumps(claim), encoding="utf-8")

    (cell_root / "result.json").write_text(
        json.dumps(
            {
                "status": status,
                "failure_kind": failure_kind,
                "execution_integrity": execution_integrity,
                "secondary_failures": secondary or [],
                "digests": {"runner_source": harness_digest},
            }
        ),
        encoding="utf-8",
    )

    (evidence / "session.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    (evidence / "attempt-1.json").write_text(json.dumps(claim), encoding="utf-8")
    (evidence / "transcript.jsonl").write_text('{"turn": 1}\n', encoding="utf-8")
    (evidence / "public_tests.json").write_text(json.dumps({"calls": 1}), encoding="utf-8")
    oracle = {"ok": status == "COMPLETED"}
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
    (evidence / "sandbox_lifecycle.json").write_text(
        json.dumps(merged_lifecycle), encoding="utf-8"
    )
    (evidence / "analysis.json").write_text(
        json.dumps(
            {
                "status": "COMPLETE",
                "record": {
                    "execution_view": {"goal_correctness": {"status": "PASS"}},
                    "usage": {"sum_matches_system_total": True},
                },
            }
        ),
        encoding="utf-8",
    )
    (raw / "session.json").write_text(json.dumps({"status": status}), encoding="utf-8")

    published = {
        path.name: _sha(path)
        for path in sorted(evidence.glob("*"))
        if path.is_file() and path.name != "evidence_manifest.json" and path.name not in drop
    }
    for name in drop:
        target = evidence / name
        if target.is_file():
            target.unlink()
    manifest = {
        "files": published,
        "raw_digests": {"session.json": _sha(raw / "session.json")},
        "publication": {"ok": True, "problems": []},
    }
    if drift_manifest and published:
        first = next(iter(published))
        manifest["files"][first] = "sha256:" + "0" * 64
    (evidence / "evidence_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


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
            _write_cell(
                cell_root,
                cell_id=cell_id,
                repetition=int(cell_root.name.lstrip("r")),
                harness_digest=plan.get("harness_digest") or _HARNESS[0],
                authorization_digest=plan.get("authorization_digest") or _AUTH[0],
                status=plan.get("status", "COMPLETED"),
                failure_kind=plan.get("failure_kind", "none"),
                execution_integrity=plan.get("execution_integrity", "OK"),
                secondary=plan.get("secondary"),
                usage=plan.get("usage"),
                usage_drop=tuple(plan.get("usage_drop") or ()),
                lifecycle=plan.get("lifecycle"),
                drop=tuple(plan.get("drop") or ()),
                drift_manifest=bool(plan.get("drift_manifest")),
                claim_overrides=plan.get("claim_overrides"),
            )
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(f"cell {cell_id}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return mx.LaunchResult(
            returncode=plan.get(
                "returncode", 0 if plan.get("status", "COMPLETED") == "COMPLETED" else 1
            ),
            pid=4242,
            timed_out=plan.get("timed_out", False),
            started_at="2026-11-24T00:00:00+00:00",
            finished_at="2026-11-24T00:00:10+00:00",
            duration_seconds=10.0,
        )

    return launch


# The launcher needs the identities the executor will grade against; they are filled per run.
_HARNESS: list[str] = [""]
_AUTH: list[str] = [""]


def _run(
    tmp_path: Path,
    config: dict[str, Any],
    *,
    authorization: Any = "DEFAULT",
    plans: dict[str, dict[str, Any]] | None = None,
    launcher: Any = None,
    on_call=None,
):
    config_path = _config_path(config, tmp_path)
    config_digest = mx.digest_file(config_path)
    head = mx._git(REPO_ROOT, "rev-parse", "HEAD")

    if authorization == "DEFAULT":
        authorization = _authorization(config, config_digest, head)
        Path(config["authorization_path"]).write_text(json.dumps(authorization), encoding="utf-8")

    _HARNESS[0] = config["harness_source_digest"]
    _AUTH[0] = admission.canonical_digest(dict(authorization)) if authorization else ""

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


# --------------------------------------------------------------------------- 1. full matrix


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
    assert [cell["order"] for cell in report["cells"]] == list(range(1, 37))
    assert report["retry"] == "FORBIDDEN" and report["resume"] == "FORBIDDEN"


# --------------------------------------------------------------------------- 2-3. measured failures


@pytest.mark.parametrize(
    ("kind", "integrity"),
    [
        ("oracle_failed", "OK"),
        ("public_tests_failed", "OK"),
        ("budget_exhaustion", "FAILED"),
        ("main_max_turns", "FAILED"),
    ],
)
def test_a_measured_agent_failure_continues(
    tmp_path: Path, kind: str, integrity: str
) -> None:
    config = _config(tmp_path)
    failing = config["order"][3]
    report, calls = _run(
        tmp_path,
        config,
        plans={
            failing: {
                "status": "FAILED",
                "failure_kind": kind,
                "execution_integrity": integrity,
            }
        },
    )
    assert report["stop_reason"] is None
    assert calls == config["order"]
    assert report["cells_started"] == 36
    assert report["cells_passed"] == 35
    assert report["cells_failed"] == 1
    assert report["cells_invalid_or_unknown"] == 0
    failed = [cell for cell in report["cells"] if cell["cell_id"] == failing][0]
    assert failed["verdict"] == mx.VERDICT_FAILED
    assert failed["failure_kind"] == kind


def test_a_measured_failure_is_not_rewritten_as_passed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    failing = config["order"][0]
    report, _calls = _run(
        tmp_path, config, plans={failing: {"status": "FAILED", "failure_kind": "oracle_failed"}}
    )
    failed = report["cells"][0]
    assert failed["verdict"] == "FAILED"
    result = json.loads(
        (Path(config["evidence_root"]) / failing / "r1" / "result.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "FAILED"
    assert result["failure_kind"] == "oracle_failed"


# --------------------------------------------------------------------------- 4. secondary faults


def test_a_secondary_failure_with_a_measured_failure_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    failing = config["order"][2]
    report, calls = _run(
        tmp_path,
        config,
        plans={
            failing: {
                "status": "FAILED",
                "failure_kind": "oracle_failed",
                "secondary": ["collector_failure: KeyError"],
            }
        },
    )
    assert report["stop_reason"] == mx.STOP_SECONDARY_FAILURE
    assert report["stopped_at_order"] == 3
    assert len(calls) == 3
    assert report["cells_invalid_or_unknown"] == 1
    assert report["cells_not_run"] == 33


def test_an_unknown_failure_kind_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(
        tmp_path,
        config,
        plans={config["order"][1]: {"status": "FAILED", "failure_kind": "provider_exception"}},
    )
    assert report["stop_reason"] == mx.STOP_UNKNOWN_FAILURE_KIND
    assert len(calls) == 2


def test_an_infrastructure_failure_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        plans={config["order"][0]: {"status": "FAILED", "failure_kind": "sandbox_creation"}},
    )
    assert report["stop_reason"] == mx.STOP_UNKNOWN_FAILURE_KIND


# --------------------------------------------------------------------------- 5. exit codes


def test_an_exit_code_that_contradicts_the_result_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(tmp_path, config, plans={config["order"][0]: {"returncode": 1}})
    assert report["stop_reason"] == mx.STOP_EXIT_MISMATCH
    assert len(calls) == 1


def test_a_non_zero_exit_code_on_a_failed_result_is_consistent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    failing = config["order"][0]
    report, calls = _run(
        tmp_path,
        config,
        plans={failing: {"status": "FAILED", "failure_kind": "oracle_failed", "returncode": 1}},
    )
    assert report["stop_reason"] is None
    assert len(calls) == 36


def test_an_unobserved_exit_code_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(
        tmp_path, config, plans={config["order"][0]: {"returncode": None}}
    )
    assert report["stop_reason"] == mx.STOP_EXIT_UNOBSERVED
    assert len(calls) == 1


def test_a_timeout_stops_without_a_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(
        tmp_path,
        config,
        plans={config["order"][1]: {"timed_out": True, "returncode": None}},
    )
    assert report["stop_reason"] == mx.STOP_TIMEOUT
    assert calls.count(config["order"][1]) == 1
    assert len(calls) == 2


# --------------------------------------------------------------------------- 6-7. usage and cost


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
    expected_per_cell = 100_000 / 1e6 * 0.30 + 10_000 / 1e6 * 1.20
    assert abs(report["total_cost_usd"] - expected_per_cell * 36) < 1e-6
    cost = report["cells"][0]["usage"]["cost"]
    assert cost["child_prompt_billed_as"] == "cache_miss_input"
    assert cost["child_cache_estimated"] is True
    assert cost["basis"].startswith("frozen estimate.json")


def test_missing_worker_counters_are_unknown_not_zero(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        plans={config["order"][0]: {"usage_drop": ("child_total_prompt_tokens",)}},
    )
    assert report["stop_reason"] == mx.STOP_USAGE_UNKNOWN
    assert report["cells_invalid_or_unknown"] == 1


def test_a_negative_counter_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        plans={config["order"][0]: {"usage": {"child_completion_tokens": -1}}},
    )
    assert report["stop_reason"] == mx.STOP_USAGE_UNKNOWN


def test_a_token_reconciliation_failure_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        plans={config["order"][0]: {"usage": {"system_total_tokens": 999}}},
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
    assert report["total_cost_usd"] >= config["cost_stop_usd"]


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
    assert report["stopped_at_order"] == 3
    assert len(calls) == 3


# --------------------------------------------------------------------- 8-9. evidence, identity


def test_a_missing_required_evidence_file_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path, config, plans={config["order"][0]: {"drop": ("oracle.json",)}}
    )
    assert report["stop_reason"] == mx.STOP_EVIDENCE_MISSING


def test_a_manifest_digest_drift_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path, config, plans={config["order"][0]: {"drift_manifest": True}}
    )
    assert report["stop_reason"] == mx.STOP_EVIDENCE_DRIFT


def test_a_claim_for_another_cell_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        plans={
            config["order"][0]: {
                "claim_overrides": {"cell": {"cell_id": "other.cell", "repetition": 1}}
            }
        },
    )
    assert report["stop_reason"] == mx.STOP_IDENTITY_MISMATCH


def test_a_claim_with_another_authorization_digest_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        plans={
            config["order"][0]: {"claim_overrides": {"authorization_digest": "sha256:" + "1" * 64}}
        },
    )
    assert report["stop_reason"] == mx.STOP_IDENTITY_MISMATCH


def test_a_residual_sandbox_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        plans={config["order"][0]: {"lifecycle": {"sandbox": {"destroyed": False}}}},
    )
    assert report["stop_reason"] == mx.STOP_RESIDUAL_SANDBOX


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


# --------------------------------------------------------------------------- 10. identities


def test_a_header_identity_mismatch_starts_no_cell(tmp_path: Path) -> None:
    config = _config(tmp_path, harness_source_digest="sha256:" + "0" * 64)
    calls: list[str] = []
    with pytest.raises(mx.ExecutorError, match="preflight"):
        _run(tmp_path, config, launcher=_launcher(calls))
    assert calls == []


def test_a_config_that_does_not_match_the_plan_starts_no_cell(tmp_path: Path) -> None:
    config = _config(tmp_path, plan_digest="sha256:" + "2" * 64)
    calls: list[str] = []
    with pytest.raises(mx.ExecutorError, match="plan digest"):
        _run(tmp_path, config, launcher=_launcher(calls))
    assert calls == []


# --------------------------------------------------------------------------- 11. authorization


def test_the_authorization_file_is_re_checked_after_every_cell(tmp_path: Path) -> None:
    config = _config(tmp_path)
    authorization_path = Path(config["authorization_path"])

    def swap(cell_id: str) -> None:
        if cell_id == config["order"][1]:
            record = json.loads(authorization_path.read_text(encoding="utf-8"))
            record["approved_by"] = "someone else"
            authorization_path.write_text(json.dumps(record), encoding="utf-8")

    report, calls = _run(tmp_path, config, on_call=swap)
    assert report["stop_reason"] == mx.STOP_AUTHORIZATION_CHANGED
    assert len(calls) == 2


def test_an_unsigned_authorization_starts_no_cell(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[str] = []
    with pytest.raises(mx.ExecutorError):
        _run(tmp_path, config, authorization=None, launcher=_launcher(calls))
    assert calls == []
    assert not (Path(config["evidence_root"]) / mx.LOCK_NAME).exists()


def test_a_record_that_does_not_name_the_executor_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_path = _config_path(config, tmp_path)
    head = mx._git(REPO_ROOT, "rev-parse", "HEAD")
    record = _authorization(
        config, mx.digest_file(config_path), head, executor_digest="sha256:" + "0" * 64
    )
    with pytest.raises(mx.ExecutorError, match="executor digest"):
        mx.run_matrix(
            config,
            repo_root=REPO_ROOT,
            authorization=record,
            config_digest=mx.digest_file(config_path),
            executor_digest=EXECUTOR_DIGEST,
            launcher=_launcher([]),
            verify_pr=False,
        )


def test_a_record_for_another_commit_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_path = _config_path(config, tmp_path)
    record = _authorization(config, mx.digest_file(config_path), "sha256:" + "3" * 64)
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


def test_a_ceiling_that_differs_from_the_target_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_path = _config_path(config, tmp_path)
    head = mx._git(REPO_ROOT, "rev-parse", "HEAD")
    record = _authorization(config, mx.digest_file(config_path), head, cost_ceiling_usd=1.0)
    with pytest.raises(mx.ExecutorError, match="ceiling"):
        mx.run_matrix(
            config,
            repo_root=REPO_ROOT,
            authorization=record,
            config_digest=mx.digest_file(config_path),
            executor_digest=EXECUTOR_DIGEST,
            launcher=_launcher([]),
            verify_pr=False,
        )


# --------------------------------------------------------------------------- 12-13. ledger


def test_a_second_executor_cannot_take_the_lock(tmp_path: Path) -> None:
    config = _config(tmp_path)
    evidence_root = Path(config["evidence_root"])
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / mx.LOCK_NAME).write_text('{"pid": 1}', encoding="utf-8")
    with pytest.raises(mx.MatrixLocked):
        _run(tmp_path, config)


def test_a_stop_keeps_the_dispatch_and_ledger_records(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        plans={config["order"][2]: {"status": "FAILED", "failure_kind": "provider_exception"}},
    )
    assert report["stop_reason"] == mx.STOP_UNKNOWN_FAILURE_KIND
    dispatch = Path(report["dispatch_log"]).read_text(encoding="utf-8").strip().splitlines()
    ledger = Path(report["ledger_log"]).read_text(encoding="utf-8").strip().splitlines()
    assert len(dispatch) == 3
    assert len(ledger) == 3
    assert json.loads(ledger[-1])["verdict"] == mx.VERDICT_INVALID
    assert json.loads(ledger[-1])["cumulative_cost_usd"] > 0
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


def test_the_report_carries_the_real_authorization_digest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(tmp_path, config)
    record = json.loads(Path(config["authorization_path"]).read_text(encoding="utf-8"))
    assert report["authorization_digest"] == admission.canonical_digest(record)
    assert report["authorization_sha256"] == _sha(Path(config["authorization_path"]))
    assert report["authorization_digest"].startswith("sha256:")


# --------------------------------------------------------------------------- config guards


def test_a_config_with_the_wrong_policy_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path, stop_policy="STOP_ON_ANY_FAILURE")
    path = _config_path(config, tmp_path)
    with pytest.raises(mx.ExecutorError, match="stop_policy"):
        mx.load_config(path)


def test_a_matrix_of_the_wrong_size_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["cells"] = config["cells"][:35]
    config["order"] = config["order"][:35]
    path = _config_path(config, tmp_path)
    with pytest.raises(mx.ExecutorError, match="36 cells"):
        mx.load_config(path)


def test_an_order_that_drifts_from_the_cell_list_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["order"] = list(reversed(config["order"]))
    path = _config_path(config, tmp_path)
    with pytest.raises(mx.ExecutorError, match="order"):
        mx.load_config(path)


def test_a_cost_stop_that_is_not_positive_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path, cost_stop_usd=0)
    path = _config_path(config, tmp_path)
    with pytest.raises(mx.ExecutorError, match="cost_stop_usd"):
        mx.load_config(path)
