"""DEV-2 evidence: how OpenSandbox actually delivers command output, in a real container.

The unit tests hold the adapter to the SDK's fragment rule; this probe records what the live server
sends, so the rule is not a guess. For every case it captures

* the raw ``OutputMessage`` fragments (one ``repr`` per message),
* what the pre-fix ``"".join(...)`` produced,
* what the adapter produces now,

and compares both against the bytes the command was told to emit.

    python experiments/reliability_closure/probe_output_fidelity.py

No model is called; the container is destroyed at the end.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from anycode.identity.context import ExecutionContext
from anycode.sandbox import SandboxCommand
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from atlasos.app.goal_sandbox import GoalSandbox, GoalSandboxOptions  # noqa: E402
from atlasos.config import AtlasConfig, ToolsConfig  # noqa: E402
from atlasos.sandbox.opensandbox_provider import _join_log_messages  # noqa: E402

RUN_ID = "reliability-output-probe"

#: name, command, expected stdout. ``None`` for cases whose content is not fixed in advance or
#: whose reconstruction is a recorded limitation rather than an expectation (see the checks).
CASES: tuple[tuple[str, str, str | None], ...] = (
    ("single_line", "printf 'hello'", "hello"),
    ("multi_line", "printf 'alpha\\nbeta\\ngamma\\n'", "alpha\nbeta\ngamma"),
    ("no_trailing_newline", "printf 'one\\ntwo'", "one\ntwo"),
    ("blank_line_inside", "printf 'a\\n\\nb\\n'", None),
    ("blank_lines_multi", "printf 'a\\n\\n\\n\\nb\\n'", None),
    ("crlf", "printf 'x\\r\\ny\\r\\n'", "x\ny"),
    ("empty_output", "true", ""),
    ("stderr_only", "printf 'boom: missing config\\n' >&2; exit 2", ""),
    ("python_traceback", "python3 -c \"raise ValueError('bad input')\"", None),
    ("git_log", "git log --oneline | head -3", None),
    (
        # Reset first, so the two executions of this case (raw SDK + provider) see the same state.
        "git_diff",
        "git checkout -- README.md 2>/dev/null; printf 'second\\n' >> README.md; "
        "git diff -- README.md",
        None,
    ),
    ("long_single_line", "python3 -c \"print('x'*4000)\"", "x" * 4000),
)


def build_repo(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "README.md").write_text("# probe\n", encoding="utf-8")
    commands = (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=probe@example.invalid",
            "-c",
            "user.name=Probe",
            "commit",
            "-q",
            "-m",
            "fixture: probe repository",
        ],
    )
    for command in commands:
        subprocess.run(command, cwd=destination, check=True, capture_output=True)
    return destination


async def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    scratch = Path(tempfile.mkdtemp(prefix="atlas-output-probe-"))
    repo = build_repo(scratch / "repo")
    run_dir = scratch / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    sandbox = GoalSandbox(
        GoalSandboxOptions(
            source_repo=repo,
            image="atlas-uat-python:3.12",
            workspace_root="/workspace/project",
        ),
        config=AtlasConfig(tools=ToolsConfig(mode="progressive")),
        run_id=RUN_ID,
        run_dir=run_dir,
    )
    evidence: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "opensandbox",
        "image": "atlas-uat-python:3.12",
        "cases": [],
    }
    await sandbox.start(task_id=f"{RUN_ID}-task", context=_context(), worker_id=f"{RUN_ID}-w")
    evidence["sandbox"] = sandbox.evidence.as_dict()
    try:
        provider = sandbox._provider  # noqa: SLF001 - evidence probe, read-only
        binding = sandbox.binding
        for name, command, expected in CASES:
            execution = await _raw_execution(provider, binding, command)
            fragments = [str(item.text) for item in execution.logs.stdout]
            stderr_fragments = [str(item.text) for item in execution.logs.stderr]
            adapter = _join_log_messages(execution.logs.stdout)
            naive = "".join(fragments)
            result = await provider.execute(
                binding.sandbox_handle,
                SandboxCommand(
                    command=command,
                    cwd=binding.workspace_root,
                    timeout_seconds=120,
                ),
            )
            evidence["cases"].append(
                {
                    "name": name,
                    "command": command,
                    "exit_code": result.exit_code,
                    "ok": result.ok,
                    "stdout_fragments": [repr(item) for item in fragments],
                    "stdout_fragment_count": len(fragments),
                    "stdout_fragments_ending_in_newline": sum(
                        1 for item in fragments if item.endswith("\n")
                    ),
                    "stderr_fragments": [repr(item) for item in stderr_fragments],
                    "naive_join_matches_expected": None if expected is None else naive == expected,
                    "adapter_matches_expected": None if expected is None else adapter == expected,
                    "adapter_equals_naive_join": adapter == naive,
                    "adapter_equals_provider_result": adapter == (result.stdout or ""),
                    "adapter_stdout_chars": len(adapter),
                    "adapter_stderr_chars": len(result.stderr or ""),
                    "expected_chars": None if expected is None else len(expected),
                }
            )
    finally:
        evidence["destroyed"] = await sandbox.close()

    cases = evidence["cases"]
    by_name = {case["name"]: case for case in cases}
    checks = {
        "multi_fragment_stream_observed": any(case["stdout_fragment_count"] > 1 for case in cases),
        "naive_join_fails_live_cases": any(
            case["naive_join_matches_expected"] is False for case in cases
        ),
        "adapter_matches_expected_everywhere": all(
            case["adapter_matches_expected"] is not False for case in cases
        ),
        "adapter_changes_at_least_one_case": any(
            not case["adapter_equals_naive_join"] for case in cases
        ),
        "adapter_result_is_what_the_tool_reads": all(
            case["adapter_equals_provider_result"] for case in cases
        ),
        "long_single_line_not_split": by_name["long_single_line"]["stdout_fragment_count"] == 1,
        "traceback_reached_stderr": by_name["python_traceback"]["adapter_stderr_chars"] > 0,
        "git_diff_is_multi_line": by_name["git_diff"]["stdout_fragment_count"] > 3
        and by_name["git_diff"]["stdout_fragments"][0].startswith("'diff --git"),
        # Recorded limits of the provider's encoding, not adapter expectations:
        # 1) the final newline the command emitted never reaches Atlas: the fragment list carries
        #    no trailing empty message, so the adapter reproduces exactly the pre-newline content.
        "server_strips_final_newline": by_name["multi_line"]["adapter_stdout_chars"]
        == by_name["multi_line"]["expected_chars"],
        # 2) an interior blank line arrives as a newline-only fragment, so one boundary is lost.
        "blank_line_collapsed_to_single_boundary": by_name["blank_line_inside"][
            "adapter_stdout_chars"
        ]
        == 3,
        "sandbox_destroyed": evidence["destroyed"] is True,
    }
    evidence["expectations"] = checks
    evidence["ready"] = all(checks.values())
    HERE.joinpath("output_fidelity.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                case["name"]: {
                    "fragments": case["stdout_fragment_count"],
                    "naive_ok": case["naive_join_matches_expected"],
                    "adapter_ok": case["adapter_matches_expected"],
                }
                for case in cases
            },
            indent=1,
        )
    )
    print(json.dumps(checks, indent=2, sort_keys=True))
    print(f"READY={evidence['ready']}  scratch={scratch}")
    return 0 if evidence["ready"] else 1


async def _raw_execution(provider: Any, binding: Any, command: str) -> Any:
    """Call the SDK directly so the raw ``Execution`` object can be inspected."""

    from opensandbox.models.execd import RunCommandOpts

    sandbox = provider._sandbox(binding.sandbox_handle)  # noqa: SLF001 - evidence probe
    return await sandbox.commands.run(
        command,
        opts=RunCommandOpts(
            working_directory=binding.workspace_root,
            timeout=timedelta(seconds=120),
        ),
    )


def _context() -> ExecutionContext:
    return ExecutionContext(
        principal="user:probe",
        tenant_scope="probe",
        classification="internal",
        attributes={
            "run_id": RUN_ID,
            "task_id": f"{RUN_ID}-task",
            "worker_id": f"{RUN_ID}-w",
            "worker_profile": "coding_worker",
            "operating_scope": "coding",
            "atlas_role": "main",
            "delegation_depth": 0,
            "delegation_root": RUN_ID,
        },
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
