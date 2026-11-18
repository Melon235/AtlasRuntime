"""Stage P unit tests: context accounting, sandbox options, and the acceptance verdict."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from atlasos.app.goal_sandbox import GoalSandboxOptions
from atlasos.app.goal_verification import GoalAcceptance, verify_goal
from atlasos.app.main_driven import _context_metrics
from atlasos.delegation import ChildOutcome, ChildStatus, DelegationTotals
from atlasos.verification.models import CheckKind, CheckSpec, VerificationSpec


def _batch(result_chars: int, concurrency: int) -> Any:
    @dataclass
    class _Batch:
        result_chars: int
        max_observed_concurrency: int

    return _Batch(result_chars=result_chars, max_observed_concurrency=concurrency)


@dataclass
class _Evidence:
    totals: DelegationTotals
    batches: tuple[Any, ...]


# --------------------------------------------------------------------------- metrics


def test_context_metrics_are_per_request_and_additive() -> None:
    evidence = _Evidence(
        totals=DelegationTotals(total_prompt_tokens=1220, completion_tokens=80),
        batches=(_batch(400, 2), _batch(150, 1)),
    )
    metrics = _context_metrics(
        {
            "prompt_tokens": 240,
            "total_prompt_tokens": 840,
            "cache_hit_tokens": 240,
            "cache_miss_tokens": 600,
            "completion_tokens": 60,
        },
        main_requests=6,
        main_context_tokens=[140, 140, 140, 140, 140, 140],
        delegation_evidence=evidence,
        rounds=2,
    )

    assert metrics["main_requests"] == 6
    assert metrics["main_peak_context_tokens"] == 140
    assert metrics["main_cumulative_input_tokens"] == 840
    assert metrics["main_context_tokens_by_request"] == [140] * 6
    assert metrics["main_cache_hit_tokens"] == 240
    assert metrics["main_cache_miss_tokens"] == 600
    assert metrics["main_completion_tokens"] == 60
    assert metrics["child_total_prompt_tokens"] == 1220
    assert metrics["child_completion_tokens"] == 80
    # Prompt + completion for both sides; cache hits are inside the prompt totals already.
    assert metrics["system_total_tokens"] == 840 + 60 + 1220 + 80
    assert metrics["handoff_payload_chars"] == 550
    assert metrics["max_observed_child_concurrency"] == 2
    assert metrics["delegation_rounds"] == 2
    assert metrics["unavailable_fields"] == []


def test_unobserved_main_usage_is_reported_as_unavailable_not_as_zero() -> None:
    metrics = _context_metrics(
        {},
        main_requests=0,
        main_context_tokens=[],
        delegation_evidence=None,
        rounds=0,
    )

    assert metrics["main_requests"] == 0
    assert metrics["main_peak_context_tokens"] is None
    assert metrics["main_cumulative_input_tokens"] is None
    assert metrics["main_context_tokens_by_request"] is None
    assert metrics["main_cache_hit_tokens"] is None
    assert "main_peak_context_tokens" in metrics["unavailable_fields"]
    # Child and system values stay truthful even when the Main was not observed.
    assert metrics["child_total_prompt_tokens"] == 0
    assert metrics["system_total_tokens"] == 0


def test_a_child_outcome_only_counts_its_own_usage() -> None:
    child = ChildOutcome(
        key="calc",
        worker_id="w",
        profile_id="research_worker",
        status=ChildStatus.COMPLETED,
        success=True,
        total_prompt_tokens=500,
        cache_miss_tokens=400,
        cache_hit_tokens=100,
        completion_tokens=40,
    )
    assert child.prompt_tokens == 0  # not set here; nothing is inferred
    assert child.total_prompt_tokens == 500


# --------------------------------------------------------------------------- options


def test_the_fake_provider_cannot_claim_isolation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="require_real=True"):
        GoalSandboxOptions(provider="fake", source_repo=tmp_path, require_real=True)
    with pytest.raises(ValueError, match="provider_factory"):
        GoalSandboxOptions(provider="fake", source_repo=tmp_path, require_real=False)
    with pytest.raises(ValueError, match="require_real=False"):
        GoalSandboxOptions(provider="opensandbox", source_repo=tmp_path, require_real=False)


def test_a_real_provider_is_verified_by_default(tmp_path: Path) -> None:
    options = GoalSandboxOptions(provider="opensandbox", source_repo=tmp_path)
    assert options.require_real is True
    assert options.workspace_root == "/workspace/project"


# --------------------------------------------------------------------------- acceptance


class _SandboxStub:
    def __init__(self, outcome: dict[str, Any], *, ready: bool = True) -> None:
        self.outcome = outcome
        self.ready = ready

    async def run_check(self, command: str, *, timeout_seconds: int = 0) -> dict[str, Any]:
        del timeout_seconds
        return {"command": command, **self.outcome}


def _acceptance() -> GoalAcceptance:
    spec = VerificationSpec(
        spec_id="unit",
        version="1.0.0",
        deliverable_type="python-package",
        required_artifacts=("app/calc.py",),
        checks=(
            CheckSpec(
                check_id="present",
                category="PRESENCE",
                kind=CheckKind.FILE_PRESENT,
                artifact="app/calc.py",
            ),
        ),
    ).with_declared_digest()
    return GoalAcceptance(spec=spec, dynamic_command="python3 -m pytest -q")


async def test_acceptance_requires_both_the_artifact_and_the_dynamic_check(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "app").mkdir(parents=True)
    (workspace / "app" / "calc.py").write_text("def subtract(a, b):\n    return a - b\n")

    passing = await verify_goal(
        _acceptance(),
        snapshot=workspace,
        sandbox=_SandboxStub({"ok": True, "exit_code": 0}),
    )
    assert passing["verdict"] == "PASS"
    assert passing["static_status"] == "PASS"
    assert passing["dynamic"]["status"] == "PASS"

    failing = await verify_goal(
        _acceptance(),
        snapshot=workspace,
        sandbox=_SandboxStub({"ok": True, "exit_code": 1}),
    )
    assert failing["verdict"] == "FAIL"
    assert failing["dynamic"]["status"] == "FAIL"


async def test_a_missing_dynamic_check_is_inconclusive_never_pass(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "app").mkdir(parents=True)
    (workspace / "app" / "calc.py").write_text("def subtract(a, b):\n    return a - b\n")

    result = await verify_goal(_acceptance(), snapshot=workspace, sandbox=None)

    assert result["static_status"] == "PASS"
    assert result["verdict"] == "INCONCLUSIVE"
    assert result["dynamic"]["status"] == "INCONCLUSIVE"


async def test_an_absent_artifact_fails_the_static_check(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await verify_goal(_acceptance(), snapshot=workspace, sandbox=None)

    assert result["static_status"] == "FAIL"
    assert result["verdict"] == "FAIL"
