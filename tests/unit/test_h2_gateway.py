"""Main-driven gateway: configuration, lifecycle, cancellation and admission profile."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlasos.app.deterministic import (
    delegated_script,
    direct_script,
    estimate_tokens,
    trusted_identity,
)
from atlasos.app.live_preflight import (
    PROFILE_BENCHMARK,
    PROFILE_PRODUCT,
    READY,
    run_live_preflight,
)
from atlasos.app.service import (
    ApplicationError,
    AtlasApplicationService,
    CancelUnavailable,
    ConcurrentRunRejected,
    WorkspaceRejected,
)
from atlasos.config import AtlasConfig, ToolsConfig
from atlasos.delegation import DelegationConfig


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AtlasApplicationService:
    monkeypatch.setenv("ATLASOS_WORKSPACE_ROOTS", str(tmp_path / "fixtures"))
    return AtlasApplicationService(
        runs_root=tmp_path / "runs",
        provider="deterministic",
        sandbox_enabled=False,
    )


def _workspace(tmp_path: Path, name: str = "demo") -> None:
    target = tmp_path / "fixtures" / name
    (target / "solution").mkdir(parents=True, exist_ok=True)
    (target / "solution" / "keep.txt").write_text("keep\n", encoding="utf-8")
    (target / "README.md").write_text("# demo\n", encoding="utf-8")


# ------------------------------------------------------------------ configuration


def test_submission_rejects_an_unknown_mode(service: AtlasApplicationService) -> None:
    with pytest.raises(ApplicationError):
        service.submit("do it", mode="main_magic", workspace="demo")


def test_submission_requires_a_workspace_name(service: AtlasApplicationService) -> None:
    with pytest.raises(ApplicationError):
        service.submit("do it", mode="main_direct")


def test_submission_refuses_an_unknown_workspace(
    service: AtlasApplicationService, tmp_path: Path
) -> None:
    _workspace(tmp_path)
    with pytest.raises(WorkspaceRejected):
        service.submit("do it", mode="main_direct", workspace="not-there")


@pytest.mark.parametrize("budget", [0, -5, 2_000_001])
def test_submission_refuses_an_unbounded_or_invalid_budget(
    service: AtlasApplicationService, tmp_path: Path, budget: int
) -> None:
    _workspace(tmp_path)
    with pytest.raises(ApplicationError):
        service.submit("do it", mode="main_direct", workspace="demo", budget_tokens=budget)


def test_the_run_configuration_records_what_will_execute(
    service: AtlasApplicationService, tmp_path: Path
) -> None:
    _workspace(tmp_path)
    record = service.submit(
        "do it", mode="main_dynamic", workspace="demo", budget_tokens=1234, max_turns=7
    )
    config = record["config"]
    assert config["budget_tokens"] == 1234
    assert config["max_turns"] == 7
    assert config["delegation"]["enabled"] is True
    assert config["provider_kind"] == "DETERMINISTIC"
    assert config["workspace"]["name"] == "demo"
    assert config["workspace"]["seed_digest"].startswith("sha256:")
    assert config["sandbox"]["enabled"] is False
    # The workspace menu states the same defaults the record used.
    menu = service.workspaces()
    assert menu["provider_kind"] == "DETERMINISTIC"
    assert menu["defaults"]["max_budget_tokens"] == 2_000_000


def test_direct_mode_builds_no_delegation_tool(
    service: AtlasApplicationService, tmp_path: Path
) -> None:
    _workspace(tmp_path)
    record = service.submit("do it", mode="main_direct", workspace="demo", budget_tokens=1000)
    assert record["config"]["delegation"]["enabled"] is False
    assert record["mode"] == "main_direct"


# ------------------------------------------------------------------ lifecycle


def test_a_run_without_terminal_evidence_is_interrupted_not_running(
    service: AtlasApplicationService,
) -> None:
    run_dir = service.runs_root / "app-orphan0001"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": "app-orphan0001",
                "mode": "main_direct",
                "task": "a task whose process died",
                "created_at": "2026-11-27T00:00:00+00:00",
                "updated_at": "2026-11-27T00:00:00+00:00",
                "status": "RUNNING",
            }
        ),
        encoding="utf-8",
    )

    detail = service.detail("app-orphan0001")

    assert detail["status"] == "INTERRUPTED"
    assert "without writing a terminal status" in (detail["error"] or "")
    reloaded = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert reloaded["status"] == "INTERRUPTED"
    assert any(event["type"] == "run_interrupted" for event in detail["events"])


def test_cancel_is_explicitly_unavailable(service: AtlasApplicationService, tmp_path: Path) -> None:
    _workspace(tmp_path)
    record = service.submit("do it", mode="main_direct", workspace="demo", budget_tokens=1000)
    with pytest.raises(CancelUnavailable) as error:
        service.cancel(record["run_id"])
    assert "CANCEL_UNAVAILABLE" in str(error.value)
    # The refusal never changes the status.
    assert service.detail(record["run_id"])["status"] in {"SUBMITTED", "RUNNING", "COMPLETED"}
    _wait(service, record["run_id"])


def _wait(service: AtlasApplicationService, run_id: str, *, timeout: float = 120.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = service.detail(run_id)
        if detail["status"] in {"COMPLETED", "FAILED", "INTERRUPTED"}:
            return detail
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish in {timeout}s")


# ------------------------------------------------------------------ admission profile


def _clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "solution").mkdir(parents=True)
    (repo / "solution" / "keep.txt").write_text("keep\n", encoding="utf-8")
    for command in (
        ["git", "init", "--quiet", "--initial-branch=main"],
        ["git", "config", "user.email", "test@atlas.local"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "--all"],
        ["git", "commit", "--quiet", "-m", "seed"],
    ):
        subprocess.run(["git", "-C", str(repo), *command[1:]], check=True, capture_output=True)
    return repo


def _preflight_options(tmp_path: Path, *, profile: str, repo: Path) -> SimpleNamespace:
    sandbox = SimpleNamespace(
        provider="opensandbox",
        require_real=True,
        workspace_root="/workspace/project",
        source_repo=repo,
    )
    return SimpleNamespace(
        config=AtlasConfig(provider=_provider_config(), tools=ToolsConfig(mode="progressive")),
        sandbox=sandbox,
        public_tests=None,
        acceptance=None,
        token_budget=10_000,
        main_write_paths=("solution/",),
        delegation=DelegationConfig(),
        runs_root=tmp_path / "scratch",
        provider="deepseek",
        model="deepseek-chat",
        provider_factory=None,
        preflight_profile=profile,
    )


def _provider_config():
    from atlasos.config import ProviderConfig

    return ProviderConfig(model="deepseek-chat")


def test_product_profile_admits_an_ordinary_task_without_an_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    repo = _clean_repo(tmp_path)

    product = run_live_preflight(_preflight_options(tmp_path, profile=PROFILE_PRODUCT, repo=repo))
    benchmark = run_live_preflight(
        _preflight_options(tmp_path, profile=PROFILE_BENCHMARK, repo=repo)
    )

    assert product["profile"] == PROFILE_PRODUCT
    assert product["status"] == READY, product["blocking_failures"]
    assert benchmark["status"] != READY
    # The evaluation checks are reported, not removed: they simply stop blocking in product mode.
    product_checks = {check["check_id"]: check for check in product["checks"]}
    benchmark_checks = {check["check_id"]: check for check in benchmark["checks"]}
    for check_id in ("acceptance_configured", "public_tests_configured"):
        assert product_checks[check_id]["ok"] is False
        assert product_checks[check_id]["blocking"] is False
        assert "product profile" in product_checks[check_id]["detail"]
        assert benchmark_checks[check_id]["blocking"] is True
    # Safety checks stay blocking in both profiles.
    for check_id in ("sandbox_provider_real", "source_repo_clean", "budget_configured"):
        assert product_checks[check_id]["blocking"] is True
        assert product_checks[check_id]["ok"] is True


def test_a_dirty_source_repository_blocks_even_in_product_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    repo = _clean_repo(tmp_path)
    (repo / "solution" / "keep.txt").write_text("dirty\n", encoding="utf-8")

    report = run_live_preflight(_preflight_options(tmp_path, profile=PROFILE_PRODUCT, repo=repo))

    assert report["status"] != READY
    assert "source_repo_clean" in report["blocking_failures"]


# ------------------------------------------------------------------ deterministic provider


def test_direct_script_writes_one_file_and_answers() -> None:
    script = direct_script(write_path="solution/answer.txt")
    assert [name for name, _ in script.main_steps] == ["file_write", "text"]
    assert script.main_steps[0][1]["path"] == "solution/answer.txt"
    assert script.child_steps == []


def test_delegated_script_gives_each_child_its_own_path() -> None:
    script = delegated_script(
        child_paths={"alpha": "solution/alpha.txt", "beta": "solution/beta.txt"},
        main_entry_path="solution/integration.txt",
    )
    steps = script.steps_for("child", {"trusted_task_id": "goal-x-main:r1:alpha"})
    beta = script.steps_for("child", {"trusted_task_id": "goal-x-main:r1:beta"})
    assert steps[0][1]["path"] == "solution/alpha.txt"
    assert beta[0][1]["path"] == "solution/beta.txt"
    # The report names the file, so two children never return identical results.
    assert steps[1][1]["text"] != beta[1][1]["text"]
    assert [name for name, _ in script.main_steps] == ["delegate_tasks", "file_write", "text"]


def test_each_child_replays_the_script_from_the_start() -> None:
    script = delegated_script(child_paths={"alpha": "a.txt", "beta": "b.txt"})
    alpha = {"trusted_task_id": "run-main:r1:alpha"}
    beta = {"trusted_task_id": "run-main:r1:beta"}

    assert script.next_step("child", alpha)[0] == "file_write"
    assert script.next_step("child", beta)[0] == "file_write"
    assert script.next_step("child", alpha)[0] == "text"
    assert script.next_step("child", beta)[0] == "text"


def test_usage_is_an_estimate_and_the_cache_split_is_a_structural_zero() -> None:
    assert estimate_tokens("abcdefgh") == 2
    record = direct_script(write_path="a.txt").as_dict()
    assert "chars/4" in record["token_estimate"]
    assert "structural zero" in record["cache_split"]


def test_trusted_identity_reads_only_what_the_runtime_passed() -> None:
    context = SimpleNamespace(attributes={"task_id": "t", "worker_id": "w", "run_id": "r"})
    options = SimpleNamespace(execution_context=context)
    assert trusted_identity(options) == {
        "trusted_task_id": "t",
        "trusted_worker_id": "w",
        "trusted_run_id": "r",
        "trusted_role": None,
    }
    assert trusted_identity(SimpleNamespace(execution_context=None)) == {}


def test_concurrent_submission_is_refused_while_one_run_holds_the_slot(
    service: AtlasApplicationService, tmp_path: Path
) -> None:
    _workspace(tmp_path)
    first = service.submit("first", mode="main_direct", workspace="demo", budget_tokens=1000)
    try:
        with pytest.raises(ConcurrentRunRejected):
            service.submit("second", mode="main_dynamic", workspace="demo", budget_tokens=1000)
    finally:
        _wait(service, first["run_id"])
