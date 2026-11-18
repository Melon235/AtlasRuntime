"""Stage P0.1: the Stage P1 admission report refuses a live run it cannot justify."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from tests.helpers.stage_p import acceptance, config

from atlasos.app.goal_sandbox import GoalSandboxOptions
from atlasos.app.live_preflight import BLOCKED, READY, run_live_preflight
from atlasos.app.main_driven import REPO_ROOT, GoalRunOptions, run_goal
from atlasos.app.public_tests import PublicTestContract
from atlasos.delegation import DelegationConfig

GOOD_PUBLIC = PublicTestContract(test_id="public", command="python3 -B -m pytest -q tests/a.py")


def _git_repo(path: Path, *, dirty: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    if dirty:
        (path / "untracked.txt").write_text("dirty", encoding="utf-8")
    return path


def _options(tmp_path: Path, **overrides: Any) -> GoalRunOptions:
    source = overrides.pop("source", None) or _git_repo(tmp_path / "source")
    values: dict[str, Any] = {
        "goal": "Add subtract.",
        "runs_root": tmp_path / "runs",
        "workspace_root": tmp_path / "host",
        "config": config(),
        "delegation": DelegationConfig(),
        "sandbox": GoalSandboxOptions(provider="opensandbox", source_repo=source),
        "acceptance": acceptance(),
        "public_tests": GOOD_PUBLIC,
        "main_write_paths": ("app/calc.py",),
        "token_budget": 240_000,
        "model": config().provider.model,
    }
    values.update(overrides)
    return GoalRunOptions(**values)


def _failing(report: dict[str, Any]) -> set[str]:
    return set(report["blocking_failures"])


def test_a_complete_live_configuration_is_ready(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    report = run_live_preflight(_options(tmp_path))

    assert report["status"] == READY, report["blocking_failures"]
    assert report["blocking_failures"] == []
    checks = {item["check_id"]: item for item in report["checks"]}
    assert checks["sandbox_provider_real"]["ok"] is True
    assert checks["public_tests_separate_from_oracle"]["ok"] is True
    assert "admission" in report["notes"]["budget"]


def test_a_missing_public_suite_blocks(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    report = run_live_preflight(_options(tmp_path, public_tests=None))

    assert report["status"] == BLOCKED
    assert "public_tests_configured" in _failing(report)


def test_the_fake_provider_cannot_pass_as_isolation(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    source = _git_repo(tmp_path / "source")
    report = run_live_preflight(
        _options(
            tmp_path,
            source=source,
            sandbox=GoalSandboxOptions(
                provider="fake",
                source_repo=source,
                provider_factory=lambda _root: None,
                require_real=False,
            ),
        )
    )

    assert report["status"] == BLOCKED
    assert {"sandbox_provider_real", "sandbox_isolation_required"} <= _failing(report)


def test_a_dirty_source_repository_blocks(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    source = _git_repo(tmp_path / "dirty", dirty=True)
    report = run_live_preflight(_options(tmp_path, source=source))

    assert report["status"] == BLOCKED
    assert "source_repo_clean" in _failing(report)


def test_an_oracle_equal_to_the_public_suite_blocks(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    contract = acceptance()
    same = PublicTestContract(
        test_id="public", command=contract.dynamic_command or "python3 -m pytest -q"
    )
    report = run_live_preflight(_options(tmp_path, public_tests=same))

    assert report["status"] == BLOCKED
    assert "public_tests_separate_from_oracle" in _failing(report)


def test_a_frozen_run_directory_blocks(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    report = run_live_preflight(
        _options(tmp_path, runs_root=REPO_ROOT / "experiments" / "results" / "stage-p01")
    )

    assert report["status"] == BLOCKED
    assert "scratch_run_directory" in _failing(report)


def test_an_advisory_budget_is_named_as_advisory(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    report = run_live_preflight(_options(tmp_path))
    budget = next(item for item in report["checks"] if item["check_id"] == "budget_configured")

    assert budget["ok"] is True
    assert "not a hard" in budget["detail"]
    assert "zero-overrun" in report["notes"]["budget"]


def test_run_goal_refuses_to_build_a_live_provider_when_blocked(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The gate is the runtime, not the operator's memory: no provider factory -> preflight."""

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    source = _git_repo(tmp_path / "source")
    options = _options(tmp_path, source=source, public_tests=None)

    with pytest.raises(SystemExit) as exit_info:
        import asyncio

        asyncio.run(run_goal(options))

    message = str(exit_info.value)
    assert "BLOCKED" in message
    assert "public_tests_configured" in message
    report_path = Path(options.runs_root) / "preflight.json"
    if not report_path.is_file():
        # run_goal derives its own run id; find the single run directory instead.
        report_path = next(Path(options.runs_root).glob("*/preflight.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == BLOCKED
    assert not (report_path.parent / "session.json").exists()
