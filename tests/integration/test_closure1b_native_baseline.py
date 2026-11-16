"""Closure-1B offline tests: the neutral lifecycle, the tool backend and the native wiring.

Everything here runs against ``FakeSandboxProvider`` — a local directory plus a real shell — so no
container is created and no model is called. The real-container acceptance lives in
``experiments.evaluation.closure1b.acceptance`` and is not part of the default test suite.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anycode.sandbox import SandboxCommandResult
from anycode.types import AgentInfo, ToolSecurityPolicy, ToolUseContext
from experiments.evaluation.closure1b.acceptance import _overlap, _pair_overlapped
from experiments.evaluation.closure1b.native_baseline import (
    ENGINE_ID,
    native_task_turn,
    run_native_sample,
)
from experiments.evaluation.closure1b.neutral_lifecycle import (
    NeutralCreateError,
    NeutralIsolationError,
    NeutralLifecycleError,
    close_neutral_sandbox,
    open_neutral_sandbox,
    probe_container_environment,
)
from experiments.evaluation.final4 import runner_entry as final4_entry
from experiments.evaluation.final4.neutral_adapter import (
    NEUTRAL_TOOLS,
    NeutralEngine,
    SandboxToolBackend,
    neutral_tool_definitions,
)
from experiments.evaluation.models import BenchmarkTask, WorkerStep
from experiments.evaluation.runner import RunnerSettings
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider

from atlasos.sandbox.models import SandboxProjectConfig

WORKSPACE = "/workspace/project"


class _IsolationReport:
    def __init__(self, *, passed: bool = True, failures: list[str] | None = None) -> None:
        self._passed = passed
        self.failures = failures or []

    @property
    def passed(self) -> bool:
        return self._passed

    def as_dict(self) -> dict[str, Any]:
        return {
            "staging_bundle_removed": self._passed,
            "isolation_checks_passed": self._passed,
            "failures": list(self.failures),
        }


@pytest.fixture(autouse=True)
def _stub_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The isolation sweep runs ``find /`` on the Host under the fake provider.

    That is a fake-provider artefact, not a property of the lifecycle, and it would scan the whole
    machine during a unit test. The sweep itself is covered by
    ``test_isolation_failure_never_returns_a_usable_sandbox``, which installs a failing report.
    """

    from experiments.evaluation.closure1b import neutral_lifecycle

    async def stub(provider: Any, handle: Any, *, timeout_seconds: int = 60) -> Any:
        return _IsolationReport()

    monkeypatch.setattr(neutral_lifecycle, "enforce_evaluation_isolation", stub)


def _git_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "c1b",
        "GIT_AUTHOR_EMAIL": "c1b@example.invalid",
        "GIT_COMMITTER_NAME": "c1b",
        "GIT_COMMITTER_EMAIL": "c1b@example.invalid",
    }
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "fixture"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True, env={**_env(), **env})
    return root


def _env() -> dict[str, str]:
    import os

    return dict(os.environ)


def _config() -> SandboxProjectConfig:
    return SandboxProjectConfig(
        provider_name="fake", image="atlas-uat-python:3.12", workspace_root=WORKSPACE
    )


def _context() -> Any:
    from anycode.identity import ExecutionContext

    return ExecutionContext(
        principal="system:closure1b-test",
        tenant_scope="atlas-evaluation",
        classification="internal",
        attributes={"run_id": "c1b-test"},
    )


async def _open(tmp_path: Path, provider: FakeSandboxProvider) -> Any:
    repo = _git_repo(tmp_path / "repo", {"calc.py": "def add(a, b):\n    return a + b\n"})
    return await open_neutral_sandbox(
        provider,
        _config(),
        run_id="c1b-test",
        task_id="sample",
        context=_context(),
        fixture_repo=repo,
    )


# -- lifecycle ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_neutral_lifecycle_provisions_a_verified_checkout(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    sandbox = await _open(tmp_path, provider)

    assert sandbox.provider_name == "fake"
    checkout = provider.workspaces[sandbox.handle.id] / "workspace" / "project"
    assert (checkout / "calc.py").is_file()
    assert (checkout / ".git").exists()
    head = await asyncio.to_thread(
        lambda: subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    assert head == sandbox.source_commit
    # The isolation report is attached to the sandbox; the in-container sweep itself is stubbed here
    # because it runs ``find /`` on the Host under the fake provider.
    assert sandbox.isolation["isolation_checks_passed"] is True
    assert not any(path.suffix == ".bundle" for path in (tmp_path / "repo").parent.glob("*.bundle"))


@pytest.mark.asyncio
async def test_create_failure_is_a_lifecycle_error_with_no_container(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    provider.fail_create = True
    repo = _git_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
    with pytest.raises(NeutralCreateError):
        await open_neutral_sandbox(
            provider,
            _config(),
            run_id="c1b-test",
            task_id="sample",
            context=_context(),
            fixture_repo=repo,
        )
    assert provider.destroy_count == 0


@pytest.mark.asyncio
async def test_provision_failure_destroys_the_container_and_reports_cleanup(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    # Every provider command fails, so provisioning cannot get past its Git preflight.
    provider.fail_operations = True
    repo = _git_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
    with pytest.raises(NeutralLifecycleError) as raised:
        await open_neutral_sandbox(
            provider,
            _config(),
            run_id="c1b-test",
            task_id="sample",
            context=_context(),
            fixture_repo=repo,
        )
    assert provider.destroy_count == 1
    assert raised.value.cleanup_failure is None
    assert not any(path.exists() for path in provider.workspaces.values())


@pytest.mark.asyncio
async def test_a_failed_cleanup_is_reported_alongside_the_primary_failure(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    provider.fail_operations = True
    provider.fail_destroy = True
    repo = _git_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
    with pytest.raises(NeutralLifecycleError) as raised:
        await open_neutral_sandbox(
            provider,
            _config(),
            run_id="c1b-test",
            task_id="sample",
            context=_context(),
            fixture_repo=repo,
        )
    assert raised.value.cleanup_failure is not None
    assert "cleanup also failed" in str(raised.value)


@pytest.mark.asyncio
async def test_isolation_failure_never_returns_a_usable_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from experiments.evaluation.closure1b import neutral_lifecycle

    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    repo = _git_repo(tmp_path / "repo", {"a.py": "x = 1\n"})

    async def failing(provider_: Any, handle: Any, *, timeout_seconds: int = 60) -> Any:
        return _IsolationReport(passed=False, failures=["private artifact names reachable"])

    monkeypatch.setattr(neutral_lifecycle, "enforce_evaluation_isolation", failing)
    with pytest.raises(NeutralIsolationError):
        await open_neutral_sandbox(
            provider,
            _config(),
            run_id="c1b-test",
            task_id="sample",
            context=_context(),
            fixture_repo=repo,
        )
    assert provider.destroy_count == 1


@pytest.mark.asyncio
async def test_an_unconfirmed_destroy_is_not_reported_as_success(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    sandbox = await _open(tmp_path, provider)
    provider.fail_destroy = True
    confirmed, error = await close_neutral_sandbox(provider, sandbox)
    assert confirmed is False
    assert error is not None


@pytest.mark.asyncio
async def test_the_environment_probe_reads_one_fact_per_command(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(tmp_path / "sandboxes", shell="git-bash")
    sandbox = await _open(tmp_path, provider)
    before = provider.execute_calls
    facts = await probe_container_environment(provider, sandbox)
    assert provider.execute_calls - before == 4
    assert facts["head_commit"] == sandbox.source_commit
    assert facts["workspace_root"] == WORKSPACE


# -- tool backend -------------------------------------------------------------------------------


def _backend(provider: FakeSandboxProvider, sandbox: Any) -> SandboxToolBackend:
    return SandboxToolBackend(provider=provider, handle=sandbox.handle, workspace_root=WORKSPACE)


@pytest.mark.asyncio
async def test_hidden_file_names_are_not_rewritten(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    sandbox = await _open(tmp_path, provider)
    backend = _backend(provider, sandbox)

    await backend.awrite(".env", "A=1\n")
    checkout = provider.workspaces[sandbox.handle.id] / "workspace" / "project"
    assert (checkout / ".env").is_file()
    assert not (checkout / "env").exists()
    assert await backend.aread(".env") == "A=1\n"


@pytest.mark.asyncio
async def test_paths_outside_the_workspace_are_refused(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    sandbox = await _open(tmp_path, provider)
    backend = _backend(provider, sandbox)

    for path in ("../outside.txt", "/etc/passwd", "a/../../b.txt"):
        with pytest.raises(ValueError):
            backend._absolute(path)  # noqa: SLF001 - the jail is the behaviour under test
    # An absolute path *inside* the workspace is accepted unchanged.
    assert backend._absolute(f"{WORKSPACE}/calc.py") == f"{WORKSPACE}/calc.py"  # noqa: SLF001
    assert backend._absolute("sub/../calc.py") == f"{WORKSPACE}/calc.py"  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_provider_error_is_never_reported_as_exit_zero(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    sandbox = await _open(tmp_path, provider)
    backend = _backend(provider, sandbox)
    await backend._ensure()  # noqa: SLF001 - prepare before the provider starts failing
    provider.fail_operations = True

    with pytest.raises(RuntimeError, match="sandbox command failed"):
        await backend.arun("true")

    tools = {tool.name: tool for tool in neutral_tool_definitions(backend)}
    result = await tools["bash"].execute(  # type: ignore[attr-defined]
        SimpleNamespace(command="true"), _tool_context()
    )
    assert result.is_error is True
    assert "sandbox command failed" in str(result.data)


@pytest.mark.asyncio
async def test_an_unknown_exit_status_is_a_tool_error(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    sandbox = await _open(tmp_path, provider)
    backend = _backend(provider, sandbox)

    await backend._ensure()  # noqa: SLF001 - prepare before replacing the provider seam

    async def unknown(handle: Any, command: Any) -> Any:
        return SandboxCommandResult(ok=True, exit_code=None, stdout="partial output")

    provider.execute = unknown  # type: ignore[method-assign]
    tools = {tool.name: tool for tool in neutral_tool_definitions(backend)}
    result = await tools["bash"].execute(  # type: ignore[attr-defined]
        SimpleNamespace(command="anything"), _tool_context()
    )
    assert result.is_error is True
    assert "exit status was not reported" in str(result.data)


@pytest.mark.asyncio
async def test_a_non_zero_exit_is_a_tool_error_with_its_code(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(tmp_path / "sandboxes", shell="git-bash")
    sandbox = await _open(tmp_path, provider)
    backend = _backend(provider, sandbox)
    tools = {tool.name: tool for tool in neutral_tool_definitions(backend)}

    result = await tools["bash"].execute(  # type: ignore[attr-defined]
        SimpleNamespace(command="exit 3"), _tool_context()
    )
    assert result.is_error is True
    assert "exit code 3" in str(result.data)

    ok = await tools["bash"].execute(  # type: ignore[attr-defined]
        SimpleNamespace(command="echo hello"), _tool_context()
    )
    assert ok.is_error is False
    assert "hello" in str(ok.data)


def _tool_context() -> ToolUseContext:
    return ToolUseContext(
        agent=AgentInfo(name="native:test", role="coder", model="scripted"), cwd=WORKSPACE
    )


# -- native wiring ------------------------------------------------------------------------------


def _task(multi: bool) -> BenchmarkTask:
    workers = (
        (
            WorkerStep(task_id="w1", title="one", prompt="own one", shared_context="SHARED"),
            WorkerStep(
                task_id="w2",
                title="two",
                prompt="own two",
                depends_on=("w1",),
                shared_context="SHARED",
            ),
        )
        if multi
        else (WorkerStep(task_id="main", title="main", prompt="whole task"),)
    )
    return BenchmarkTask(
        sample_id="synthetic",
        category="feature",
        mode="multi_worker" if multi else "coding",
        prompt="whole task",
        target="",
        visible_files={"a.py": "x = 1\n"},
        workers=workers,
    )


def test_the_native_task_turn_carries_the_same_public_facts_once() -> None:
    multi = _task(multi=True)
    turn = native_task_turn(multi.workers[1])
    assert turn.startswith("SHARED\n\n")
    assert turn.count("SHARED") == 1
    assert turn.endswith("own two")

    single = _task(multi=False)
    assert native_task_turn(single.workers[0]) == "whole task"


@pytest.mark.asyncio
async def test_a_non_real_provider_blocks_the_run_instead_of_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from experiments.evaluation.closure1b import native_baseline

    fake = FakeSandboxProvider(tmp_path / "sandboxes")
    monkeypatch.setattr(native_baseline, "_build_provider", lambda config: fake)

    settings = RunnerSettings(
        benchmark_id="c1b-test",
        dataset_version="test",
        runs_root=tmp_path / "runs",
        timeout_seconds=30,
    )
    result = await run_native_sample(
        _task(multi=False),
        settings=settings,
        runs_root=tmp_path / "runs",
        provider_name="scripted",
    )
    assert result.failure_class == "ENVIRONMENT_FAILURE"
    assert "not a real provider" in (result.failure_reason or "")
    assert result.metrics.sandbox_created is False
    assert fake.create_count == 0
    assert result.safety["host_fallback_used"] is False
    assert result.environment["workspace_isolation"] == "remote_opensandbox_container"


def test_the_native_engine_registers_exactly_the_six_basic_tools(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    backend = SandboxToolBackend(
        provider=provider, handle=SimpleNamespace(id="s"), workspace_root=WORKSPACE
    )
    engine = NeutralEngine(backend)
    names = sorted(tool.name for tool in engine._neutral_registry.list())  # noqa: SLF001
    assert names == sorted(NEUTRAL_TOOLS)
    # No Atlas increment: no discovery, dispatch, history or handoff tool.
    assert "tool_search" not in names
    assert "dispatch_action" not in names
    assert "history.read" not in names
    assert "handoff.read" not in names


def test_the_engine_identity_names_the_adapter() -> None:
    assert "neutral-opensandbox-tool-adapter" in ENGINE_ID
    assert "anycode-0.9.0" in ENGINE_ID


# -- historical final-4 N0 entry ----------------------------------------------------------------


def test_the_historical_n0_unit_now_resolves_its_route() -> None:
    settings = final4_entry.settings_for("N0", runs_root=Path("experiments/runs/final4"))
    assert settings.delegation_route_mode == "off"
    assert final4_entry.UNITS["N0"]["runner"] == "baseline"


@pytest.mark.asyncio
async def test_the_historical_n0_unit_passes_the_baseline_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from experiments.evaluation import baseline as baseline_module

    captured: dict[str, Any] = {}

    async def stub(task: Any, *, settings: Any, repetition_id: int, baseline_root: Path) -> Any:
        captured.update(
            {"task": task, "repetition_id": repetition_id, "baseline_root": baseline_root}
        )
        return SimpleNamespace(
            run_dir=str(tmp_path / "run"),
            identity=SimpleNamespace(run_id="run-1"),
            oracle=SimpleNamespace(passed=True),
            success=True,
            failure_class="TASK_SUCCESS",
            failure_reason=None,
            environment={"workspace_isolation": "disposable_host_copy"},
        )

    monkeypatch.setattr(baseline_module, "run_baseline_sample", stub)
    payload = await final4_entry.run_unit("validation-serial", "N0", runs_root=tmp_path / "runs")
    assert captured["baseline_root"] == tmp_path / "runs"
    assert payload["runner"] == "baseline"
    assert payload["historical_host_mode"] is True
    assert payload["oracle_pass"] is True


# -- concurrency detection ----------------------------------------------------------------------


def _events(*pairs: tuple[str, str, str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for agent, event_type, ts in pairs:
        events.append({"type": event_type, "agent": agent, "ts": ts, "title": None})
    return events


def test_overlap_is_computed_from_agent_events_not_the_dag() -> None:
    overlapping = _events(
        ("native:w1", "agent_start", "2026-01-01T00:00:00+00:00"),
        ("native:w2", "agent_start", "2026-01-01T00:00:01+00:00"),
        ("native:w2", "agent_complete", "2026-01-01T00:00:05+00:00"),
        ("native:w1", "agent_complete", "2026-01-01T00:00:06+00:00"),
    )
    overlap = _overlap(overlapping)
    assert overlap["workers_started"] == 2
    assert overlap["workers_completed"] == 2
    assert _pair_overlapped(overlap["starts"], overlap["ends"], "native:w1", "native:w2")

    sequential = _events(
        ("native:w1", "agent_start", "2026-01-01T00:00:00+00:00"),
        ("native:w1", "agent_complete", "2026-01-01T00:00:05+00:00"),
        ("native:w2", "agent_start", "2026-01-01T00:00:05+00:00"),
        ("native:w2", "agent_complete", "2026-01-01T00:00:09+00:00"),
    )
    overlap = _overlap(sequential)
    assert not _pair_overlapped(overlap["starts"], overlap["ends"], "native:w1", "native:w2")


def test_tool_security_policy_is_available_for_the_native_agent() -> None:
    # The native agent is configured with the sandbox workspace root, never the Host directory.
    policy = ToolSecurityPolicy(workspace_root=WORKSPACE)
    assert policy.workspace_root == WORKSPACE
