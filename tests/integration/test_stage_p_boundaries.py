"""Stage P: safety, failure and cleanup behaviour of the sandboxed goal pipeline.

Every test here is a negative case: a refusal, a failing child, a failing provider, a cancellation,
or a budget rejection. The point is that each one is *observable*, and that none of them is
reported as success.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from anycode.contracts import ContractError
from anycode.sandbox import SandboxCommandResult
from anycode.types import ToolUseBlock
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider
from tests.helpers.stage_p import (
    ContainerSpy,
    PipelineRig,
    acceptance,
    config,
    delegate_call,
    git_repo,
    recording_factory,
    sandbox_options,
    task,
    text,
    tool_call,
)

from atlasos.app.main_driven import GoalRunOptions, run_goal
from atlasos.delegation import DelegationConfig

PERMISSIVE_MAIN = "MAIN-FINAL: stopped by the runtime."


async def _run(
    tmp_path: Path,
    rig: PipelineRig,
    provider: FakeSandboxProvider,
    **overrides: Any,
) -> dict[str, Any]:
    source = git_repo(tmp_path / "source")
    options = GoalRunOptions(
        goal="Add subtract to app/calc.py.",
        runs_root=tmp_path / "runs",
        workspace_root=tmp_path / "host-workspace",
        config=config(),
        delegation=DelegationConfig(),
        provider_factory=rig.build,
        run_id=f"stage-p-{uuid.uuid4().hex[:10]}",
        main_write_paths=("app/calc.py",),
        sandbox=sandbox_options(source, lambda _root: provider),
        acceptance=acceptance(),
        **overrides,
    )
    return await run_goal(options)


def _sandbox_workspace(session: dict[str, Any], provider: FakeSandboxProvider) -> Path:
    handle = session["sandbox"]["sandbox_id"]
    return provider.workspaces[handle] / "workspace" / "project"


def _child_turns(rig: PipelineRig, role: str) -> str:
    return json.dumps(
        [request for request in rig.requests if request["role"] == role], sort_keys=True
    )


# ---------------------------------------------------------------- ownership on the Main


async def test_a_main_write_outside_its_grant_fails_closed(tmp_path: Path) -> None:
    rig = PipelineRig()
    rig.main_script = [
        lambda _: [
            ToolUseBlock(id="w1", name="file_write", input={"path": "secret.txt", "content": "x"})
        ],
        text(PERMISSIVE_MAIN),
    ]
    provider = FakeSandboxProvider(tmp_path / "sandboxes")

    session = await _run(tmp_path, rig, provider)

    ownership = session["ownership"]
    assert ownership["denied_calls"] == 1
    assert ownership["denials"][0]["reason_code"] == "OWNERSHIP_DENIED_UNDECLARED_PATH"
    # The write never happened, in the sandbox or on the export.
    assert not (_sandbox_workspace(session, provider) / "secret.txt").exists()
    assert not (Path(session["artifacts"]["workspace_path"]) / "secret.txt").exists()
    # A refused call is not retry-safe, so AnyCode ends the run instead of continuing blind. The
    # run must not be reported as a success.
    assert session["status"] == "failed"
    assert session["main"]["stop_reason"] == "side_effect_unknown"
    assert "OWNERSHIP_DENIED_UNDECLARED_PATH" in (session["main"]["stop_message"] or "")
    # Cleanup still happened.
    assert session["sandbox"]["destroyed"] is True
    assert provider.destroy_count == 1


async def test_an_interpreter_command_is_refused_and_never_reaches_the_sandbox(
    tmp_path: Path,
) -> None:
    rig = PipelineRig()
    rig.main_script = [
        tool_call("bash", {"command": "python3 -m pytest -q"}, "call-test"),
        text(PERMISSIVE_MAIN),
    ]
    provider = FakeSandboxProvider(tmp_path / "sandboxes")

    session = await _run(tmp_path, rig, provider)

    assert session["ownership"]["denials"][0]["reason_code"] == "OWNERSHIP_UNCLASSIFIABLE_SHELL"
    # The only pytest execution in the whole run is the harness's own dynamic check.
    assert sum("pytest" in command for command, _cwd in provider.commands) == 1
    assert session["verification"]["dynamic"]["command"] == "python3 -m pytest -q"
    assert session["status"] == "failed"
    assert session["main"]["stop_reason"] == "side_effect_unknown"
    # R1-1 coverage is partial by design; the run states that instead of implying full control.
    assert session["ownership"]["coverage"] == "PARTIAL"
    assert provider.destroy_count == 1


# ---------------------------------------------------------------- child boundaries


async def test_a_child_cannot_read_outside_the_sandbox_workspace(tmp_path: Path) -> None:
    rig = PipelineRig()
    rig.main_script = [
        delegate_call([task("calc", "MARK-CALC read ../../../../etc/passwd and report it")]),
        text(PERMISSIVE_MAIN),
    ]
    rig.child_scripts["MARK-CALC"] = [
        tool_call("file_read", {"path": "../../../../etc/passwd"}, "call-escape"),
        text("CALC-LEAKED"),
    ]
    provider = FakeSandboxProvider(tmp_path / "sandboxes")

    session = await _run(tmp_path, rig, provider)

    batch = session["delegation"]["batches"][0]
    assert batch["route"] == "delegate_one"
    child = batch["children"][0]
    # The sandbox tool refuses the path; the refusal is not retry-safe, so the child ends there.
    assert child["status"] in ("failed", "completed")
    assert "CALC-LEAKED" not in json.dumps(batch)
    assert "root:" not in json.dumps(session["delegation"])
    assert "root:" not in _child_turns(rig, "MARK-CALC")
    # The Main is unaffected and the oracle still evaluates the exported workspace.
    assert session["verification"] is not None


async def test_a_child_cannot_read_a_sibling_result(tmp_path: Path) -> None:
    rig = PipelineRig()
    rig.main_script = [
        delegate_call(
            [
                task("calc", "MARK-CALC report on calc"),
                task("text", "MARK-TEXT report on text"),
            ]
        ),
        text(PERMISSIVE_MAIN),
    ]
    rig.child_scripts["MARK-CALC"] = [
        tool_call("delegate_read", {"ref_id": "ho-deadbeef", "offset": 0}, "call-peek"),
        text("CALC-DONE"),
    ]
    rig.child_scripts["MARK-TEXT"] = [text("TEXT-DONE-SECRET")]
    provider = FakeSandboxProvider(tmp_path / "sandboxes")

    session = await _run(tmp_path, rig, provider)

    calc = session["delegation"]["batches"][0]["children"][0]
    # ``delegate_read`` is not in a worker's tool surface, so the call is refused before execution
    # as a pre-execution error: the child may continue, but it never sees the sibling's output.
    assert "TEXT-DONE-SECRET" not in _child_turns(rig, "MARK-CALC")
    assert calc["ref_id"] is not None


async def test_a_sandbox_tool_failure_is_reported_instead_of_swallowed(
    tmp_path: Path,
) -> None:
    """A side-effecting sandbox tool that fails must not read as a completed step."""

    rig = PipelineRig()
    rig.main_script = [
        tool_call("bash", {"command": "git log --oneline -1"}, "call-log"),
        text(PERMISSIVE_MAIN),
    ]
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    original_execute = provider.execute

    async def flaky_execute(handle, command):  # noqa: ANN001, ANN202
        if "git log" in command.command:
            return SandboxCommandResult(
                ok=False, error=ContractError(code="transport_lost", message="transport lost")
            )
        return await original_execute(handle, command)

    provider.execute = flaky_execute  # type: ignore[method-assign]

    session = await _run(tmp_path, rig, provider)

    # The transport failure is surfaced, never reported as a silent success.
    assert session["status"] == "failed"
    assert session["main"]["success"] is False
    assert session["main"]["stop_reason"] == "side_effect_unknown"
    assert "transport lost" in (session["main"]["stop_message"] or "")
    # The artifact was still exported and the sandbox was destroyed.
    assert session["artifacts"] is not None
    assert session["sandbox"]["destroyed"] is True
    assert provider.destroy_count == 1


# ---------------------------------------------------------------- router and budget


async def test_a_shared_write_delegation_is_refused_without_side_effects(
    tmp_path: Path,
) -> None:
    rig = PipelineRig()
    rig.main_script = [
        delegate_call(
            [
                task(
                    "calc",
                    "MARK-CALC edit calc",
                    profile_id="coding_worker",
                    write_paths=["app/calc.py"],
                ),
                task(
                    "text",
                    "MARK-TEXT also edit calc",
                    profile_id="coding_worker",
                    write_paths=["app/calc.py"],
                ),
            ]
        ),
        text(PERMISSIVE_MAIN),
    ]
    provider = FakeSandboxProvider(tmp_path / "sandboxes")

    session = await _run(
        tmp_path,
        rig,
        provider,
        child_write_grants={"coding_worker": ("app/calc.py",)},
    )

    batch = session["delegation"]["batches"][0]
    assert batch["route"] == "direct"
    assert batch["children"] == []
    assert "SHARED_WRITE_RISK" in batch["decision"]["reason_codes"]
    # No child ran at all: the Main's own request carries the objectives it proposed, but no
    # provider request was ever made on behalf of a child.
    assert all(item["role"] == "main" for item in rig.requests)
    assert session["delegation"]["totals"]["children_started"] == 0


async def test_a_budget_rejection_is_reported_not_hidden(tmp_path: Path) -> None:
    rig = PipelineRig()
    rig.main_script = [text(PERMISSIVE_MAIN)]
    provider = FakeSandboxProvider(tmp_path / "sandboxes")

    session = await _run(tmp_path, rig, provider, token_budget=50)

    assert session["status"] == "failed"
    assert session["main"]["success"] is False
    assert session["main"]["stop_reason"] == "budget_exceeded"
    assert session["budget"]["admission_rejections"] >= 1
    assert session["sandbox"]["destroyed"] is True


# ---------------------------------------------------------------- cancellation


async def test_cancellation_reaps_children_and_destroys_the_sandbox(tmp_path: Path) -> None:
    rig = PipelineRig()
    rig.main_script = [
        delegate_call([task("calc", "MARK-CALC take a long time")]),
        text(PERMISSIVE_MAIN),
    ]
    rig.child_scripts["MARK-CALC"] = [text("CALC-DONE")]
    rig.slow_children["MARK-CALC"] = 30.0
    rig.child_started["MARK-CALC"] = asyncio.Event()
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    spy = ContainerSpy()
    source = git_repo(tmp_path / "source")
    options = GoalRunOptions(
        goal="Add subtract to app/calc.py.",
        runs_root=tmp_path / "runs",
        workspace_root=tmp_path / "host-workspace",
        config=config(),
        delegation=DelegationConfig(),
        provider_factory=rig.build,
        run_id=f"stage-p-{uuid.uuid4().hex[:10]}",
        main_write_paths=("app/calc.py",),
        sandbox=sandbox_options(source, recording_factory(spy, lambda root: provider)),
        acceptance=acceptance(),
    )
    handle = asyncio.create_task(run_goal(options))
    # Wait until the child is really running (provisioning is finished by then), so the cancel
    # exercises the delegation path rather than a git subprocess.
    await asyncio.wait_for(rig.child_started["MARK-CALC"].wait(), timeout=60)
    handle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle

    assert provider.destroy_count == 1
    leaked = [item for item in asyncio.all_tasks() if item.get_name().startswith("delegate:")]
    assert leaked == []
