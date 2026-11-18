"""Closure-2B-1: the canary's offline entry points, its isolation from the formal pilot, and its
own oracle.

No test here creates a sandbox, builds a provider adapter or calls a model. The two facts that do
execute code are: the canary fixture's own pytest run (a six-line arithmetic helper), and the
eighteen-cell dry run of the frozen benchmark, which is planning only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from experiments.evaluation.closure2 import connectivity_canary as canary
from experiments.evaluation.closure2 import pilot_runner
from experiments.evaluation.closure2.pilot_ledger import PilotAlreadyClaimed, PilotLedger

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMITTED_DRY_RUN = REPO_ROOT / "experiments" / "results" / "closure2" / "pilot-dry-run.json"

APPROVED_BY = "integration-test"


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The production canary ledger is per-user and permanent; a test must never touch it."""

    path = tmp_path / "canary-ledger.sqlite"
    monkeypatch.setattr(canary, "_ledger_factory", lambda: PilotLedger(path))
    return path


def approve(monkeypatch: pytest.MonkeyPatch, *, ceiling: float = 0.05) -> canary.CanaryPlan:
    plan = dataclasses.replace(canary.CANARY_PLAN, estimated_cost_ceiling_usd=ceiling)
    monkeypatch.setattr(canary, "CANARY_PLAN", plan)
    monkeypatch.setattr(
        canary,
        "CANARY_APPROVAL",
        {
            "status": "APPROVED",
            "approved_plan_digest": plan.digest(),
            "approved_by": APPROVED_BY,
            "approval_commit": "0" * 40,
            "approved_at": "2026-11-18T00:00:00+00:00",
        },
    )
    return plan


def fake_result() -> Any:
    oracle = SimpleNamespace(
        passed=True,
        environment_failure=False,
        failure_kind="HIDDEN_TESTS_PASSED",
        execution_mode="sandbox",
        cleanup_failure=None,
        details={"hidden_tests": {"sandbox_id": "oracle-sandbox-1"}},
    )
    metrics = SimpleNamespace(
        prompt_tokens=2100,
        completion_tokens=180,
        cache_hit_tokens=1024,
        cache_miss_tokens=1076,
        estimated_cost_usd=0.0005,
        sandbox_created=True,
        sandbox_destroyed=True,
        tool_calls=4,
        model_turns=5,
        latency_ms=3210.0,
        extra={"cache_split_status": "COMPLETE", "cache_metrics_complete": True},
    )
    identity = SimpleNamespace(
        run_id="canary-run-integration",
        model=canary.CANARY_MODEL,
        provider="deepseek",
        git_commit="0" * 40,
        git_dirty=False,
        repetition_id=0,
    )
    return SimpleNamespace(
        metrics=metrics,
        oracle=oracle,
        identity=identity,
        environment={"sandbox_image": "atlas-uat-python:3.12", "agent_latency_ms": 3000.0},
        safety={"cleanup_error": None},
        failure_class="TASK_SUCCESS",
        failure_reason=None,
        success=True,
        agent_success=True,
        run_dir="experiments/runs/closure2-canary/canary-add-function/r0-canary-run-integration",
    )


# ------------------------------------------------------------------------------- offline precheck


def test_the_precheck_is_offline_and_never_authorizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-integration-test")
    out = tmp_path / "canary-precheck.json"
    exit_code = canary.main(
        [
            "--precheck",
            "--out",
            str(out),
            "--runs-root",
            str(tmp_path / "runs"),
            "--results-root",
            str(tmp_path / "results"),
        ]
    )
    assert exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "OFFLINE_PRECHECK"
    assert payload["precheck_ready"] is True
    assert payload["live_authorized"] is False
    assert payload["approval"]["status"] != "APPROVED"
    assert "CANARY_APPROVAL_NOT_APPROVED" in payload["approval"]["reason_codes"]
    assert payload["model_calls"] == 0
    assert payload["sandbox_runs"] == 0
    assert payload["provider"]["model"] == canary.CANARY_MODEL
    assert payload["sandbox"]["provider"] == "opensandbox"
    assert payload["resource_limits"] == canary.CANARY_PLAN.limits()
    assert payload["hard_ceilings"] == canary.CANARY_HARD_CEILINGS
    assert str(REPO_ROOT) not in out.read_text(encoding="utf-8")
    assert "sk-integration-test" not in out.read_text(encoding="utf-8")


def test_the_module_entry_point_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-integration-test")
    out = tmp_path / "precheck.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "experiments.evaluation.closure2.connectivity_canary",
            "--precheck",
            "--out",
            str(out),
            "--runs-root",
            str(tmp_path / "runs"),
            "--results-root",
            str(tmp_path / "results"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "OFFLINE_PRECHECK"
    assert payload["live_authorized"] is False


def test_the_cli_refuses_live_without_the_second_confirmation(tmp_path: Path) -> None:
    exit_code = canary.main(
        [
            "--live",
            "--runs-root",
            str(tmp_path / "runs"),
            "--results-root",
            str(tmp_path / "results"),
        ]
    )
    assert exit_code == 2
    assert not (tmp_path / "results" / "canary-summary.json").exists()


def test_the_cli_refuses_live_after_the_authorization_is_consumed(
    tmp_path: Path, isolated_ledger: Path
) -> None:
    """The one authorized attempt is spent; the CLI must refuse before any claim is taken.

    The formal approval record is deliberately not asserted here: it is a different object, decided
    by the owner, and this refusal must not depend on it.
    """

    exit_code = canary.main(
        [
            "--live",
            "--confirm-live",
            "--runs-root",
            str(tmp_path / "runs"),
            "--results-root",
            str(tmp_path / "results"),
        ]
    )
    assert exit_code == 3
    assert not (tmp_path / "results" / "canary-summary.json").exists()
    assert not isolated_ledger.exists()
    assert canary.CANARY_APPROVAL["status"] == "CONSUMED_BY_EXECUTION"


def test_the_cli_estimate_is_offline_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results"
    exit_code = canary.main(["--estimate", "--results-root", str(results)])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ESTIMATE_CONDITIONAL_ON_ASSUMPTION"
    assert payload["pricing"]["pricing_status"] == "PRICING_NOT_LIVE_VERIFIED"
    assert not results.exists()


# --------------------------------------------------------------------- the gated live path, mocked


def test_the_full_canary_path_writes_isolated_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = approve(monkeypatch)
    formal_before = sorted(path.name for path in canary.FORMAL_RESULTS_ROOT.iterdir())
    seen: dict[str, Any] = {}

    async def executor(task: Any, *, settings: Any, repetition_id: int, config_path: Path) -> Any:
        seen.update(
            task=task, settings=settings, repetition_id=repetition_id, config_path=config_path
        )
        return fake_result()

    summary = asyncio.run(
        canary.run_connectivity_canary(
            runs_root=tmp_path / "runs",
            results_root=tmp_path / "results",
            live_intent=True,
            executor=executor,
        )
    )

    assert summary["passed"] is True
    assert summary["status"] == "CANARY_PASSED"
    assert summary["record"]["run_id"] == "canary-run-integration"
    assert summary["record"]["benchmark_success_claimed"] is False
    assert summary["execution_claim"]["plan_digest"] == plan.digest()
    assert summary["counts_toward_formal_results"] is False

    evidence = tmp_path / "results" / "canary-summary.json"
    assert evidence.is_file()
    written = json.loads(evidence.read_text(encoding="utf-8"))
    assert written["execution_claim"]["claim_status"] == "CONSUMED"
    assert written["is_official_pilot"] is False

    # The canary used the shipped runner's settings, in its own namespace, with its own budget.
    settings = seen["settings"]
    assert settings.benchmark_id == canary.CANARY_BENCHMARK_ID
    assert settings.runs_root == (tmp_path / "runs").resolve()
    assert settings.model_override == canary.CANARY_MODEL
    assert seen["task"].sample_id == canary.CANARY_TASK_ID
    assert seen["repetition_id"] == 0
    assert seen["config_path"].name == Path(canary.CANARY_CONFIG).name

    # Nothing appeared in the formal evidence tree.
    assert sorted(path.name for path in canary.FORMAL_RESULTS_ROOT.iterdir()) == formal_before


def test_the_cli_live_path_passes_with_an_approved_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return fake_result()

    monkeypatch.setattr(canary, "_default_engine_executor", executor)
    results = tmp_path / "results"
    exit_code = canary.main(
        [
            "--live",
            "--confirm-live",
            "--runs-root",
            str(tmp_path / "runs"),
            "--results-root",
            str(results),
        ]
    )
    assert exit_code == 0
    written = json.loads((results / "canary-summary.json").read_text(encoding="utf-8"))
    assert written["status"] == "CANARY_PASSED"
    assert written["execution_integrity_status"] == "COMPLETE"


def test_a_canary_run_cannot_write_into_the_formal_results_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approve(monkeypatch)
    calls: list[Any] = []

    async def executor(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return fake_result()

    with pytest.raises(canary.CanaryIsolationError):
        asyncio.run(
            canary.run_connectivity_canary(
                runs_root=canary.FORMAL_RUNS_ROOT / "canary",
                results_root=canary.FORMAL_RESULTS_ROOT,
                live_intent=True,
                executor=executor,
            )
        )
    assert calls == []


def test_a_second_canary_run_is_refused_by_its_own_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve(monkeypatch)

    async def executor(*args: Any, **kwargs: Any) -> Any:
        return fake_result()

    kwargs = {
        "runs_root": tmp_path / "runs",
        "results_root": tmp_path / "results",
        "live_intent": True,
        "executor": executor,
    }
    asyncio.run(canary.run_connectivity_canary(**kwargs))
    with pytest.raises(PilotAlreadyClaimed):
        asyncio.run(canary.run_connectivity_canary(**kwargs))


# ------------------------------------------------------------------------------- the canary oracle


def _run_pytest(workspace: Path, targets: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=180,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "PYTHONPATH": str(workspace),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )


def test_the_canary_oracle_accepts_a_correct_implementation(tmp_path: Path) -> None:
    for relative, content in canary.CANARY_VISIBLE_FILES.items():
        (tmp_path / relative).write_text(content, encoding="utf-8")
    for relative, content in canary.CANARY_HIDDEN_FILES.items():
        (tmp_path / relative).write_text(content, encoding="utf-8")

    stub = _run_pytest(tmp_path, ["test_public.py", "test_acceptance.py"])
    assert stub.returncode != 0, "the stub must not pass the public or hidden tests"

    (tmp_path / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8"
    )
    passed = _run_pytest(tmp_path, ["test_public.py", "test_acceptance.py"])
    assert passed.returncode == 0, passed.stdout + passed.stderr


def test_the_canary_fixture_needs_no_third_party_package() -> None:
    """The task's only import is its own module; pytest is the oracle's tool, not the task's."""

    assert set(canary.CANARY_VISIBLE_FILES) == {"calculator.py", "test_public.py"}
    for content in canary.CANARY_VISIBLE_FILES.values():
        assert "import " not in content.replace("from calculator import add", "")
    assert "from calculator import add" in canary.CANARY_HIDDEN_TESTS
    assert "NotImplementedError" in canary.CANARY_VISIBLE_FILES["calculator.py"]


# ------------------------------------------------------------------------- the frozen benchmark


def test_the_formal_dry_run_is_still_the_frozen_eighteen_cells() -> None:
    manifest = pilot_runner.build_dry_run_manifest()
    assert manifest["cell_count"] == 18
    assert manifest["contract_ok"] is True
    assert manifest["model_calls"] == 0
    assert manifest["agent_sandbox_runs_total"] == 0
    assert manifest["execution_ready"] is False
    assert manifest["pilot_execution_ready"] is False
    assert manifest["approval"]["status"] == "PENDING_OWNER_APPROVAL"


def test_the_committed_dry_run_artifact_still_matches() -> None:
    assert COMMITTED_DRY_RUN.is_file()
    committed = json.loads(COMMITTED_DRY_RUN.read_text(encoding="utf-8"))
    fresh = pilot_runner.build_dry_run_manifest()
    committed.pop("generated_at", None)
    fresh.pop("generated_at", None)
    assert committed == fresh
