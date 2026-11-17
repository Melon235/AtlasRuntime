"""Closure-2B-0: the dry-run entry point, and the two refusals that guard a live run.

The dry run is executed for real (in-process and through ``python -m``); the live path is never
executed here. What is proved about live is structural: no sandbox provider is constructed, no
provider adapter is built, and the executor is not reached while the approval is pending.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from experiments.evaluation.closure2 import pilot_runner, protocol
from experiments.evaluation.closure2.pilot_runner import (
    Cell,
    build_dry_run_manifest,
    run_live_pilot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMITTED_DRY_RUN = REPO_ROOT / "experiments" / "results" / "closure2" / "pilot-dry-run.json"


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return build_dry_run_manifest()


def test_the_dry_run_manifest_is_complete(manifest: dict[str, Any]) -> None:
    assert manifest["mode"] == "DRY_RUN"
    assert manifest["cell_count"] == 18
    assert manifest["contract_ok"] is True
    assert manifest["execution_ready"] is False
    assert manifest["approval"]["status"] == "PENDING_OWNER_APPROVAL"
    assert {cell["arm_id"] for cell in manifest["cells"]} == set(pilot_runner.ARM_ORDER)
    assert len({cell["task_id"] for cell in manifest["cells"]}) == 3


def test_a_dry_run_builds_no_sandbox_and_no_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strongest available offline proof: constructing either one raises."""

    from atlasos.sandbox import provider as sandbox_provider

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the dry run must not construct a sandbox provider")

    monkeypatch.setattr(sandbox_provider, "build_sandbox_provider", forbidden)
    monkeypatch.setattr(pilot_runner, "_default_executor", forbidden, raising=False)
    manifest = build_dry_run_manifest()
    assert manifest["cell_count"] == 18
    assert manifest["sandbox_runs"] == 0


def test_the_dry_run_is_reproducible(tmp_path: Path) -> None:
    first = build_dry_run_manifest()
    second = build_dry_run_manifest()
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


def test_the_cli_writes_the_manifest(tmp_path: Path) -> None:
    out = tmp_path / "pilot-dry-run.json"
    exit_code = pilot_runner.main(["--dry-run", "--out", str(out), "--runs-root", str(tmp_path)])
    assert exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "DRY_RUN"
    assert payload["cell_count"] == 18
    assert payload["contains_live_outcomes"] is False
    assert str(pilot_runner.REPO_ROOT) not in out.read_text(encoding="utf-8")


def test_the_module_entry_point_runs(tmp_path: Path) -> None:
    out = tmp_path / "manifest.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "experiments.evaluation.closure2.pilot_runner",
            "--dry-run",
            "--out",
            str(out),
            "--runs-root",
            str(tmp_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["cell_count"] == 18
    assert payload["execution_ready"] is False


def test_the_cli_refuses_live_without_the_second_confirmation(tmp_path: Path) -> None:
    out = tmp_path / "never.json"
    exit_code = pilot_runner.main(["--live", "--out", str(out), "--runs-root", str(tmp_path)])
    assert exit_code == 2
    assert not out.exists()


def test_the_cli_refuses_live_while_the_approval_is_pending(tmp_path: Path) -> None:
    out = tmp_path / "never.json"
    exit_code = pilot_runner.main(
        ["--live", "--confirm-live", "--out", str(out), "--runs-root", str(tmp_path)]
    )
    assert exit_code == 3
    assert protocol.APPROVAL_RECORD["status"] == "PENDING_OWNER_APPROVAL"


def test_a_live_run_never_reaches_the_executor_while_pending(tmp_path: Path) -> None:
    calls: list[Any] = []

    async def executor(cell: Cell, **kwargs: Any) -> Any:
        calls.append(cell)
        raise AssertionError("unreachable")

    with pytest.raises(PermissionError):
        asyncio.run(
            run_live_pilot(
                (Cell("closure2-pipeline-migration", "A0"),),
                runs_root=tmp_path,
                executor=executor,
                live_intent=True,
            )
        )
    assert calls == []


def test_the_committed_artifact_matches_a_fresh_dry_run(manifest: dict[str, Any]) -> None:
    """The committed manifest is evidence: a stale copy must fail rather than drift silently."""

    assert COMMITTED_DRY_RUN.is_file(), "the dry-run artifact is part of the evidence"
    committed = json.loads(COMMITTED_DRY_RUN.read_text(encoding="utf-8"))
    fresh = json.loads(json.dumps(manifest))
    committed.pop("generated_at", None)
    fresh.pop("generated_at", None)
    assert committed == fresh
