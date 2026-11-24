"""Stage B S06: offline verification of the host-side matrix executor.

Every cell is simulated. The fake launcher writes a minimal but real-shaped evidence bundle into the
cell directory, so the executor's post-cell audit (claim, result, manifest, publication, sandbox
cleanup, usage) runs exactly as it will on a live cell. No model, no sandbox, no benchmark cell.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from experiments.stage_b_executor import matrix_executor as mx  # noqa: E402

EXECUTOR_DIR = REPO_ROOT / "experiments" / "stage_b_executor"
CONFIG_PATH = EXECUTOR_DIR / "matrix_config.json"
EXECUTOR_DIGEST = mx.digest_file(EXECUTOR_DIR / "matrix_executor.py")

GOOD_USAGE = {
    "main_prompt_hit_tokens": 1000,
    "main_prompt_miss_tokens": 200,
    "main_completion_tokens": 50,
    "main_requests": 2,
    "system_total_tokens": 1250,
}


def _config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    base = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    base["evidence_root"] = str(tmp_path / "cells")
    base["runs_root"] = str(tmp_path / "runs")
    base["authorization_path"] = str(tmp_path / "authorization.json")
    base.update(overrides)
    return base


def _config_digest(config: dict[str, Any], tmp_path: Path) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    return mx.digest_file(path)


def _authorization(
    config: dict[str, Any], config_digest: str, **overrides: Any
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "status": "APPROVED",
        "approved_by": "Owner (test)",
        "approval_commit": "f164fcb65fcde857751c960ab30d6ceab4a69c4c",
        "approved_at": "2026-11-24T00:00:00+00:00",
        "consumed": False,
        "cells": [cell["cell_id"] for cell in config["cells"]],
        "attempts_allowed": 1,
        "attempts_per_cell": 1,
        "cost_ceiling_usd": config["cost_stop_usd"],
        "approved_digests": {
            "protocol": config["protocol_digest"],
            "plan": config["plan_digest"],
            "runtime": config["runtime_digest"],
            "runner_source": config["harness_source_digest"],
        },
        "executor_digest": EXECUTOR_DIGEST,
        "config_digest": config_digest,
        "freeze_binding": {
            "version": config["freeze_version"],
            "raw_sha256": config["freeze_raw_sha256"],
            "content_digest": config["freeze_content_digest"],
        },
        "c3_rerun_allowed": False,
    }
    record.update(overrides)
    return record


def _write_evidence(cell_root: Path, **overrides: Any) -> None:
    """A minimal bundle that satisfies every post-cell check."""

    evidence = cell_root / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (cell_root / "attempts").mkdir(parents=True, exist_ok=True)
    (cell_root / "attempts" / "attempt-1.json").write_text(
        json.dumps({"state": "COLLECTED", "retries_allowed": False}), encoding="utf-8"
    )
    result = {"status": "COMPLETED", "failure_kind": "none", "execution_integrity": "OK"}
    result.update(overrides.get("result") or {})
    (cell_root / "result.json").write_text(json.dumps(result), encoding="utf-8")

    analysis = {
        "status": "COMPLETE",
        "record": {
            "execution_view": {"goal_correctness": {"status": "PASS"}},
            "usage": {"sum_matches_system_total": True},
        },
    }
    (evidence / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    (evidence / "evidence_manifest.json").write_text(
        json.dumps({"publication": {"ok": True, "problems": []}}), encoding="utf-8"
    )
    usage = dict(GOOD_USAGE)
    usage.update(overrides.get("usage") or {})
    (evidence / "provider_usage.json").write_text(json.dumps(usage), encoding="utf-8")
    (evidence / "session.json").write_text("{}", encoding="utf-8")
    lifecycle = {
        "cleanup_failure": None,
        "sandbox": {"created": True, "destroyed": True, "retained": False},
    }
    lifecycle.update(overrides.get("lifecycle") or {})
    (evidence / "sandbox_lifecycle.json").write_text(json.dumps(lifecycle), encoding="utf-8")


def _launcher(calls: list[str], behaviour: dict[str, dict[str, Any]] | None = None):
    behaviour = behaviour or {}

    def launch(argv, *, cell_root, cwd, stdout_path, stderr_path, timeout):
        cell_id = argv[argv.index("--execute-cell") + 1]
        calls.append(cell_id)
        plan = behaviour.get(cell_id, {})
        if not plan.get("no_evidence"):
            _write_evidence(
                Path(cell_root),
                result=plan.get("result"),
                usage=plan.get("usage"),
                lifecycle=plan.get("lifecycle"),
            )
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(f"cell {cell_id}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return mx.LaunchResult(
            returncode=plan.get("returncode", 0),
            pid=4242,
            timed_out=plan.get("timed_out", False),
            started_at="2026-11-24T00:00:00+00:00",
            finished_at="2026-11-24T00:00:10+00:00",
            duration_seconds=10.0,
        )

    return launch


def _run(
    tmp_path: Path,
    config: dict[str, Any],
    *,
    authorization: Any = "DEFAULT",
    behaviour: dict[str, dict[str, Any]] | None = None,
    launcher: Any = None,
):
    digest = _config_digest(config, tmp_path)
    if authorization == "DEFAULT":
        authorization = _authorization(config, digest)
    calls: list[str] = []
    report = mx.run_matrix(
        config,
        repo_root=REPO_ROOT,
        authorization=authorization,
        config_digest=digest,
        executor_digest=EXECUTOR_DIGEST,
        launcher=launcher or _launcher(calls, behaviour),
    )
    return report, calls


# --------------------------------------------------------------------------- happy path


def test_the_matrix_runs_all_36_cells_in_the_frozen_order(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(tmp_path, config)
    assert report["stop_reason"] is None
    assert report["cells_started"] == 36
    assert report["cells_completed"] == 36
    assert calls == config["order"]
    assert [cell["order"] for cell in report["cells"]] == list(range(1, 37))
    assert report["retry"] == "FORBIDDEN" and report["resume"] == "FORBIDDEN"
    assert Path(report["report_path"]).is_file()


# --------------------------------------------------------------------------- stop on a bad cell


def test_the_matrix_stops_at_the_first_cell_that_is_not_completed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    failing = config["order"][16]
    report, calls = _run(
        tmp_path,
        config,
        behaviour={failing: {"result": {"status": "FAILED"}}},
    )
    assert report["stop_reason"] == mx.STOP_CELL_FAILED
    assert report["stopped_at_order"] == 17
    assert len(calls) == 17 and calls[-1] == failing
    assert set(config["order"][17:]).isdisjoint(calls)


def test_a_non_zero_exit_code_stops_the_matrix(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(
        tmp_path, config, behaviour={config["order"][4]: {"returncode": 1}}
    )
    assert report["stop_reason"] == mx.STOP_EXIT_NONZERO
    assert len(calls) == 5


def test_an_unobserved_exit_code_stops_the_matrix(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(
        tmp_path, config, behaviour={config["order"][2]: {"returncode": None}}
    )
    assert report["stop_reason"] == mx.STOP_EXIT_UNOBSERVED
    assert len(calls) == 3


def test_a_timeout_stops_the_matrix(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(
        tmp_path,
        config,
        behaviour={config["order"][1]: {"timed_out": True, "returncode": None}},
    )
    assert report["stop_reason"] == mx.STOP_TIMEOUT
    assert len(calls) == 2


# --------------------------------------------------------------------------- claims and locks


def test_a_consumed_claim_stops_the_matrix_before_launch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    blocked = config["cells"][1]
    cell_root = Path(config["evidence_root"]) / blocked["cell_id"] / f"r{blocked['repetition']}"
    (cell_root / "attempts").mkdir(parents=True, exist_ok=True)
    (cell_root / "attempts" / "attempt-1.json").write_text(
        json.dumps({"state": "FAILED", "retries_allowed": False}), encoding="utf-8"
    )
    report, calls = _run(tmp_path, config)
    assert report["stop_reason"] == mx.STOP_CLAIM_CONSUMED
    assert report["stopped_at_order"] == 2
    assert calls == [config["order"][0]]
    assert (cell_root / "attempts" / "attempt-1.json").is_file()


def test_a_second_executor_cannot_take_the_lock(tmp_path: Path) -> None:
    config = _config(tmp_path)
    evidence_root = Path(config["evidence_root"])
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / mx.LOCK_NAME).write_text('{"pid": 1}', encoding="utf-8")
    with pytest.raises(mx.MatrixLocked):
        _run(tmp_path, config)


def test_the_lock_is_released_after_a_stop(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path, config, behaviour={config["order"][0]: {"returncode": 2}}
    )
    assert report["stop_reason"] == mx.STOP_EXIT_NONZERO
    assert not (Path(config["evidence_root"]) / mx.LOCK_NAME).exists()


# --------------------------------------------------------------------------- audit stops


def test_unknown_usage_stops_the_matrix(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        behaviour={config["order"][3]: {"usage": {"main_prompt_hit_tokens": None}}},
    )
    assert report["stop_reason"] == mx.STOP_USAGE_UNKNOWN


def test_a_residual_sandbox_stops_the_matrix(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(
        tmp_path,
        config,
        behaviour={config["order"][5]: {"lifecycle": {"sandbox": {"destroyed": False}}}},
    )
    assert report["stop_reason"] == mx.STOP_RESIDUAL_SANDBOX


def test_missing_evidence_stops_the_matrix(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, calls = _run(
        tmp_path, config, behaviour={config["order"][6]: {"no_evidence": True}}
    )
    assert report["stop_reason"] == mx.STOP_EVIDENCE_MISSING
    assert len(calls) == 7


def test_a_publication_refusal_stops_the_matrix(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[str] = []

    def leaky(argv, *, cell_root, cwd, stdout_path, stderr_path, timeout):
        calls.append(argv[argv.index("--execute-cell") + 1])
        _write_evidence(Path(cell_root))
        (Path(cell_root) / "evidence" / "evidence_manifest.json").write_text(
            json.dumps({"publication": {"ok": False, "problems": ["leak"]}}), encoding="utf-8"
        )
        return mx.LaunchResult(0, 4242, False, "a", "b", 1.0)

    report, _calls = _run(tmp_path, config, launcher=leaky)
    assert report["stop_reason"] == mx.STOP_PUBLICATION_REFUSED
    assert len(calls) == 1


# --------------------------------------------------------------------------- cost stop


def test_the_cost_stop_prevents_the_next_cell(tmp_path: Path) -> None:
    config = _config(tmp_path)
    expensive = {
        "main_prompt_hit_tokens": 0,
        "main_prompt_miss_tokens": 2_000_000,
        "main_completion_tokens": 83_333,
    }
    behaviour = {cell_id: {"usage": expensive} for cell_id in config["order"]}
    report, calls = _run(tmp_path, config, behaviour=behaviour)
    assert report["stop_reason"] == mx.STOP_COST_REACHED
    assert report["stopped_at_order"] == 3
    assert len(calls) == 3
    assert report["total_cost_usd"] >= config["cost_stop_usd"]


def test_the_cost_of_a_clean_run_matches_the_frozen_price_list(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report, _calls = _run(tmp_path, config)
    expected = mx.cost_of(GOOD_USAGE, config["pricing"]) * 36
    assert abs(report["total_cost_usd"] - expected) < 1e-9
    assert report["total_tokens"] == GOOD_USAGE["system_total_tokens"] * 36


# --------------------------------------------------------------------------- authorization


def test_an_unsigned_authorization_starts_no_cell(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[str] = []
    with pytest.raises(mx.ExecutorError):
        _run(tmp_path, config, authorization=None, launcher=_launcher(calls))
    assert calls == []
    assert not (Path(config["evidence_root"]) / mx.LOCK_NAME).exists()


def test_a_ceiling_that_differs_from_the_executor_target_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    digest = _config_digest(config, tmp_path)
    record = _authorization(config, digest, cost_ceiling_usd=1.0)
    with pytest.raises(mx.ExecutorError, match="ceiling"):
        mx.run_matrix(
            config,
            repo_root=REPO_ROOT,
            authorization=record,
            config_digest=digest,
            executor_digest=EXECUTOR_DIGEST,
            launcher=_launcher([]),
        )


def test_a_record_that_does_not_name_the_executor_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    digest = _config_digest(config, tmp_path)
    record = _authorization(config, digest, executor_digest="sha256:" + "0" * 64)
    with pytest.raises(mx.ExecutorError, match="executor digest"):
        mx.run_matrix(
            config,
            repo_root=REPO_ROOT,
            authorization=record,
            config_digest=digest,
            executor_digest=EXECUTOR_DIGEST,
            launcher=_launcher([]),
        )


def test_a_cell_list_that_drifts_from_the_order_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["order"] = list(reversed(config["order"]))
    path = tmp_path / "config-drift.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(mx.ExecutorError, match="order"):
        mx.load_config(path)


def test_a_matrix_of_the_wrong_size_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["cells"] = config["cells"][:35]
    config["order"] = config["order"][:35]
    path = tmp_path / "config-short.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(mx.ExecutorError, match="36 cells"):
        mx.load_config(path)


def test_an_unapproved_cost_stop_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path, cost_stop_usd=None)
    path = tmp_path / "config-null.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(mx.ExecutorError, match="cost_stop_usd"):
        mx.load_config(path)
