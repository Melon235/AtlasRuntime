"""Stage P3: the full main-driven coding pipeline, offline, inside a provisioned sandbox.

The provider is scripted. The Main really runs on ``AtlasContextEngine`` → ``AnyCode`` wave →
``AgentRunner``, the children really run through ``DelegationService``, and every file/shell tool
really goes through ``SandboxWorkspaceTools`` against a provisioned workspace. The oracle runs after
the Main stops, so nothing it reports can be influenced by the Main's own text.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from anycode.types import ToolUseBlock
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider
from tests.helpers.stage_p import (
    PipelineRig,
    acceptance,
    config,
    delegate_call,
    git_repo,
    sandbox_options,
    task,
    text,
    tool_call,
    tool_calls,
)

from atlasos.app.main_driven import GoalRunOptions, run_goal
from atlasos.app.public_tests import PUBLIC_TEST_TOOL, PublicTestContract
from atlasos.delegation import DelegationConfig

GOAL = (
    "Add subtract(a, b) to app/calc.py so the public test suite passes. "
    "Investigate the two modules before you edit anything."
)


def _main_script() -> list[Any]:
    return [
        # 1) Delegate two independent investigations. One tool call, two children.
        delegate_call(
            [
                task(
                    "calc",
                    "MARK-CALC read app/calc.py and report exactly which functions it defines",
                ),
                task(
                    "text",
                    "MARK-TEXT read app/text.py and report exactly which functions it defines",
                ),
            ]
        ),
        # 2) Read the file the Main is about to change.
        tool_call("file_read", {"path": "app/calc.py"}, "call-read"),
        # 3) Integrate: append the missing function.
        tool_call(
            "file_edit",
            {
                "path": "app/calc.py",
                "old_string": "    return a + b\n",
                "new_string": (
                    "    return a + b\n"
                    "\n"
                    "\n"
                    "def subtract(a, b):\n"
                    '    """Return the difference of two numbers."""\n'
                    "    return a - b\n"
                ),
            },
            "call-edit",
        ),
        # 4) Verify inside the sandbox with a shell command the ownership policy can prove
        #    read-only. An interpreter command would be refused, and a refused call terminates the
        #    run (AnyCode reads ``retry_safe=False`` as "state unknown"), so the harness owns the
        #    suite. ``git diff`` exists on both a POSIX sandbox and the deterministic provider.
        tool_call("bash", {"command": "git diff -- app/calc.py"}, "call-verify"),
        text("MAIN-FINAL: subtract integrated; the harness runs the acceptance suite."),
    ]


def _rig(*, barrier: bool = True) -> PipelineRig:
    rig = PipelineRig()
    if barrier:
        rig.barrier = asyncio.Barrier(2)
    rig.child_scripts["MARK-CALC"] = [
        tool_calls(("file_read", {"path": "app/calc.py"})),
        text("CALC-REPORT: app/calc.py defines add(a, b) only; subtract is missing."),
    ]
    rig.child_scripts["MARK-TEXT"] = [
        tool_calls(("file_read", {"path": "app/text.py"})),
        text("TEXT-REPORT: app/text.py defines normalize(value)."),
    ]
    rig.main_script = _main_script()
    return rig


async def _run(
    tmp_path: Path,
    rig: PipelineRig,
    provider: FakeSandboxProvider,
    **overrides: Any,
) -> dict[str, Any]:
    source = git_repo(tmp_path / "source")
    options = GoalRunOptions(
        goal=GOAL,
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


# ---------------------------------------------------------------- the whole pipeline


async def test_the_full_pipeline_passes_an_independent_oracle(tmp_path: Path) -> None:
    rig = _rig()
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    session = await _run(tmp_path, rig, provider)

    # --- 1) the run itself -------------------------------------------------
    assert session["status"] == "succeeded", session["failure"]
    assert session["isolation"] == "sandbox"
    assert session["acceptance"] == "PASS"
    assert session["execution_succeeded"] is True
    assert session["workspace_tool_root"] == "/workspace/project"

    # --- 2) sandbox lifecycle ---------------------------------------------
    sandbox = session["sandbox"]
    assert sandbox["provider"] == "fake"
    assert sandbox["provider_is_real"] is False
    assert sandbox["created"] is True
    assert sandbox["binding_state"] == "READY"
    # The deterministic provider runs on the Host, so isolation probes are refused rather than
    # faked. Real-provider evidence is produced by the stage-P smoke run, not by this test.
    assert sandbox["host_separation"] == {"status": "NOT_APPLICABLE_FAKE_PROVIDER"}
    assert sandbox["network_probe"] == "NOT_APPLICABLE_FAKE_PROVIDER"
    assert sandbox["destroyed"] is True
    assert provider.destroy_count == 1

    # --- 3) every tool call really ran against the sandbox ---------------
    # The children read the two modules, the Main read and edited calc.py.
    assert provider.read_calls >= 3
    assert provider.write_calls >= 1
    executed = [command for command, _cwd in provider.commands]
    assert any("clone" in command for command in executed), "provisioning ran in the sandbox"
    assert any("rev-parse HEAD" in command for command in executed)
    # The Main's own shell command was executed by the sandbox, not by AnyCode on the Host.
    assert any("git diff -- app/calc.py" in command for command in executed)
    # The public suite is the harness's dynamic check, and it runs exactly once, in the sandbox.
    assert sum("pytest" in command for command in executed) == 1
    assert session["verification"]["dynamic"]["command"] == "python3 -m pytest -q"

    # --- 4) the delegation really happened -------------------------------
    delegation = session["delegation"]
    batch = delegation["batches"][0]
    assert batch["route"] == "delegate_parallel"
    assert batch["max_observed_concurrency"] == 2
    calc, text_child = batch["children"]
    assert calc["key"] == "calc" and calc["status"] == "completed"
    assert text_child["key"] == "text" and text_child["status"] == "completed"
    assert calc["started_offset_ms"] < text_child["finished_offset_ms"]
    assert text_child["started_offset_ms"] < calc["finished_offset_ms"]
    assert calc["ref_id"].startswith("ho-")

    # --- 5) the Main resumed its own loop after the tool result ----------
    main_requests = rig.main_requests()
    assert len(main_requests) == 5
    resumed = json.dumps(main_requests[1]["messages"], sort_keys=True)
    assert "CALC-REPORT" in resumed
    assert "tool_result" in resumed

    # --- 6) the artifact exists, in the sandbox and in the export --------
    exported_workspace = Path(session["artifacts"]["workspace_path"])
    calc_file = (exported_workspace / "app" / "calc.py").read_text(encoding="utf-8")
    assert "def subtract(a, b):" in calc_file
    assert "return a - b" in calc_file
    assert session["artifacts"]["patch_changed_files"] >= 1
    patch = await asyncio.to_thread(
        Path(session["artifacts"]["patch_path"]).read_text, encoding="utf-8"
    )
    assert "subtract" in patch
    # The Host source repository is never written to.
    assert "subtract" not in (tmp_path / "source" / "app" / "calc.py").read_text(encoding="utf-8")

    # --- 7) the oracle is independent of the Main's answer --------------
    verification = session["verification"]
    assert verification["verdict"] == "PASS"
    assert verification["static_status"] == "PASS"
    assert verification["dynamic"]["status"] == "PASS"
    assert verification["dynamic"]["exit_code"] == 0
    assert verification["dynamic"]["command"] == "python3 -m pytest -q"
    assert verification["dynamic"]["stdout_digest"].startswith("sha256:")
    # The Main never claimed a test result: it says the harness runs the suite.
    assert "pytest" not in session["main"]["output"]

    # --- 8) ownership and the action gate participated ------------------
    ownership = session["ownership"]
    assert ownership["mode"] == "enforce"
    assert ownership["denied_calls"] == 0, ownership["denials"]
    main_declaration = ownership["workers"][session["main"]["worker_id"]]
    assert main_declaration["paths"] == ["app/calc.py"]
    # Two children were declared read-only: they own no path at all.
    child_declarations = [
        entry
        for worker_id, entry in ownership["workers"].items()
        if worker_id != session["main"]["worker_id"]
    ]
    assert len(child_declarations) == 2
    assert all(entry["paths"] == [] for entry in child_declarations)

    # --- 9) no leaked tasks ---------------------------------------------
    leaked = [task_ for task_ in asyncio.all_tasks() if task_.get_name().startswith("delegate:")]
    assert leaked == []


@pytest.mark.skipif(
    os.environ.get("ATLAS_STAGE_P_REAL_SANDBOX") != "1",
    reason="opt-in: set ATLAS_STAGE_P_REAL_SANDBOX=1 to run against a live OpenSandbox",
)
async def test_the_pipeline_runs_inside_a_real_opensandbox_container(tmp_path: Path) -> None:
    """The same pipeline against the self-hosted provider. Not part of the default suite.

    This is the only test that can claim isolation, and it claims it from probes rather than from
    the provider's name.
    """

    from atlasos.app.goal_sandbox import GoalSandboxOptions
    from atlasos.config import SandboxConfig

    rig = _rig()
    # Same script as the deterministic test: a wrong fix, a real failing run in the container, a
    # repair, a passing run. The public channel is exercised against the real provider too.
    rig.main_script = _public_fix_script()
    source = git_repo(tmp_path / "source")
    sandbox_config = SandboxConfig(
        enabled=True,
        provider="opensandbox",
        endpoint=os.environ.get("OPEN_SANDBOX_ENDPOINT", "localhost:8080"),
        protocol="http",
        image=os.environ.get("ATLAS_STAGE_P_IMAGE", "atlas-uat-python:3.12"),
        workspace_root="/workspace/project",
        network="none",
        timeout_minutes=20,
        ready_timeout_seconds=240,
        use_server_proxy=True,
    )
    options = GoalRunOptions(
        goal=GOAL,
        runs_root=tmp_path / "runs",
        workspace_root=tmp_path / "host-workspace",
        config=config(sandbox=sandbox_config),
        delegation=DelegationConfig(),
        provider_factory=rig.build,
        run_id=f"stage-p-real-{uuid.uuid4().hex[:8]}",
        main_write_paths=("app/calc.py",),
        sandbox=GoalSandboxOptions(
            provider="opensandbox",
            source_repo=source,
            image=sandbox_config.image,
        ),
        acceptance=acceptance(),
        public_tests=_public_test_contract(),
    )

    session = await run_goal(options)

    assert session["status"] == "succeeded", session["failure"]
    sandbox = session["sandbox"]
    assert sandbox["provider_is_real"] is True
    assert sandbox["created"] is True
    assert sandbox["destroyed"] is True
    # Isolation is claimed from probes, not from the provider's name.
    assert sandbox["network_probe"] == "PASS"
    assert sandbox["host_separation"] == {
        "host_only_visible": False,
        "sandbox_only_visible_on_host": False,
        "sandbox_only_read_back": True,
    }
    assert session["acceptance"] == "PASS"
    assert session["verification"]["dynamic"]["exit_code"] == 0
    assert session["delegation"]["batches"][0]["max_observed_concurrency"] == 2
    # The public channel ran in the same container: one failing run, one repair, one passing run.
    assert [run["status"] for run in session["public_tests"]["runs"]] == ["FAIL", "PASS"]
    assert (
        session["public_tests"]["runs"][0]["output_digest"]
        != (session["public_tests"]["runs"][1]["output_digest"])
    )
    # The Host never received the change: only the export did.
    assert "subtract" not in (tmp_path / "source" / "app" / "calc.py").read_text(encoding="utf-8")
    print(json.dumps(session["public_tests"], indent=2))
    print(json.dumps(session["sandbox"], indent=2))
    print(json.dumps(session["metrics"], indent=2))
    print(json.dumps(session["verification"]["dynamic"], indent=2))


async def test_a_failed_child_does_not_stop_the_integration(tmp_path: Path) -> None:
    # No barrier here: a child that raises never reaches it, and the sibling would time out
    # waiting, which would test the barrier rather than the failure path.
    rig = _rig(barrier=False)
    rig.fail_children["MARK-TEXT"] = "provider exploded"
    provider = FakeSandboxProvider(tmp_path / "sandboxes")

    session = await _run(tmp_path, rig, provider)

    batch = session["delegation"]["batches"][0]
    calc, text_child = batch["children"]
    assert calc["status"] == "completed"
    # AnyCode turns a provider exception into an `error` event and returns without a `done` one, so
    # the child is classified as a silent failure rather than reported as an empty success.
    assert text_child["status"] == "failed"
    assert text_child["success"] is False
    assert text_child["error_code"] == "child_no_result"
    # The Main still integrated and the oracle still passed.
    assert session["status"] == "succeeded", session["failure"]
    assert session["delegation"]["totals"]["children_failed"] == 1
    assert session["acceptance"] == "PASS"


# ---------------------------------------------------------------- public test loop (P0.1)


def _public_test_contract() -> PublicTestContract:
    # ``-B`` matters: a repair that keeps the file size identical can land inside the same
    # filesystem second as the failing run, and a ``.pyc`` written by that run stores the mtime in
    # seconds — the second run would then import the pre-repair bytecode and report the old
    # failure. No bytecode is written, so there is nothing stale to read.
    return PublicTestContract(
        test_id="public",
        command="python3 -B -m pytest -q tests/test_public.py",
        timeout_seconds=120,
        max_calls=4,
    )


def _public_fix_script() -> list[Any]:
    """Delegation, a wrong fix, a real failing run, a repair, a passing run."""

    buggy = (
        "    return a + b\n"
        "\n"
        "\n"
        "def subtract(a, b):\n"
        '    """Return the difference of two numbers."""\n'
        "    return a + b\n"
    )
    fixed = (
        "    return a + b\n"
        "\n"
        "\n"
        "def subtract(a, b):\n"
        '    """Return the difference of two numbers."""\n'
        "    return a - b\n"
    )
    return [
        delegate_call(
            [
                task("calc", "MARK-CALC report what app/calc.py defines"),
                task("text", "MARK-TEXT report what app/text.py defines"),
            ]
        ),
        lambda _: [
            ToolUseBlock(
                id="e1",
                name="file_edit",
                input={
                    "path": "app/calc.py",
                    "old_string": "    return a + b\n",
                    "new_string": buggy,
                },
            )
        ],
        lambda _: [ToolUseBlock(id="t1", name=PUBLIC_TEST_TOOL, input={"reason": "first attempt"})],
        lambda _: [
            ToolUseBlock(
                id="e2",
                name="file_edit",
                input={"path": "app/calc.py", "old_string": buggy, "new_string": fixed},
            )
        ],
        lambda _: [ToolUseBlock(id="t2", name=PUBLIC_TEST_TOOL, input={"reason": "after repair"})],
        text("MAIN-FINAL: subtract implemented; the public suite passes."),
    ]


async def test_the_main_repairs_after_a_failing_public_run(tmp_path: Path) -> None:
    rig = PipelineRig()
    rig.barrier = asyncio.Barrier(2)
    rig.child_scripts["MARK-CALC"] = [text("CALC-REPORT: add only.")]
    rig.child_scripts["MARK-TEXT"] = [text("TEXT-REPORT: normalize.")]
    rig.main_script = _public_fix_script()
    provider = FakeSandboxProvider(tmp_path / "sandboxes")

    session = await _run(tmp_path, rig, provider, public_tests=_public_test_contract())

    public = session["public_tests"]
    assert [run["status"] for run in public["runs"]] == ["FAIL", "PASS"]
    assert public["runs"][0]["exit_code"] == 1
    assert public["runs"][1]["exit_code"] == 0
    assert public["calls"] == 2
    assert public["oracle_is_separate"] is True

    # The failing run did not end the loop: the Main saw real pytest output from the sandbox.
    main_requests = rig.main_requests()
    assert len(main_requests) == 6
    failure_turn = json.dumps(main_requests[3])
    assert "test_subtract" in failure_turn or "assert" in failure_turn
    assert "FAILED" in failure_turn

    # The repair happened inside the sandbox, and the oracle then ran its own command.
    assert session["status"] == "succeeded", session["failure"]
    assert session["execution_succeeded"] is True
    assert session["acceptance"] == "PASS"
    assert session["verification"]["dynamic"]["command"] == "python3 -m pytest -q"
    assert session["verification"]["dynamic"]["exit_code"] == 0

    # Two pytest executions by the Main plus one by the oracle; nothing else ran pytest.
    executed = [command for command, _cwd in provider.commands]
    assert sum("pytest" in command for command in executed) == 3
    assert sum("tests/test_public.py" in command for command in executed) == 2
    assert session["sandbox"]["destroyed"] is True
