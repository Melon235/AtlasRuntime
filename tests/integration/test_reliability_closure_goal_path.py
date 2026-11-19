"""Reliability closure: tool-error semantics and terminating-turn evidence on the goal path.

Stage P1b ran a real Main against a real DeepSeek model and lost the run on the ninth turn: the Main
executed a ``bash`` command, OpenSandbox reported exit code 2, the sandbox tool returned
``is_error=True`` with ``retry_safe`` unset, AnyCode normalized that to ``False`` for a
side-effecting tool and the loop stopped with ``side_effect_unknown`` — before any integration, and
without persisting the command that ended it.

Two fixes are exercised here, both through the production stack (``run_goal`` → ``GoalSandbox`` →
``SandboxWorkspaceTools`` → real ``ToolExecutor`` / M3 gate / M4 bridge / ``AtlasAgentRunner``):

* DEV-3 — the goal path now selects the existing G3-1 ``corrected`` mode, so a command whose exit
  status was observed is a known error the Main can read; an unobserved outcome still stops it.
* DEV-4 — every proposed tool input is written to the run's private payload corpus before it
  executes, so the call that ends a failed run stays recoverable from its ``input_ref``.

Only the model provider and the sandbox provider are faked.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from anycode.sandbox import SandboxCommand, SandboxCommandResult
from anycode.types import ToolUseBlock
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider
from tests.helpers.stage_p import PipelineRig, config, git_repo, sandbox_options, text, tool_call

from atlasos.app.main_driven import GoalRunOptions, run_goal
from atlasos.delegation import DelegationConfig
from atlasos.sandbox.tools import TOOL_ERROR_MODE_CORRECTED, TOOL_ERROR_MODE_LEGACY
from atlasos.trajectory.payloads import PRIVATE_PAYLOAD_FILE, PRIVATE_PAYLOAD_ROOT, read_corpus

GOAL = "Report what the workspace contains. Do not change any file."
#: A failing command the ownership policy can prove read-only: ``ls`` on a missing relative path.
FAILING_COMMAND = "ls missing-entry"
#: This one is refused *before* execution: a shell metacharacter the classifier cannot analyse.
DENIED_COMMAND = 'cd /app 2>/dev/null || cd "$(pwd)"; cat README.md'


class _ScriptedProvider(FakeSandboxProvider):
    """Fake provider whose failures are triggered by the command itself, not by call order."""

    def __init__(
        self,
        root: Path,
        *,
        fail_commands: tuple[str, ...] = (),
        lost_commands: tuple[str, ...] = (),
    ) -> None:
        super().__init__(root)
        self.fail_commands = fail_commands
        self.lost_commands = lost_commands

    async def execute(self, handle: Any, command: SandboxCommand) -> SandboxCommandResult:
        if any(marker in command.command for marker in self.fail_commands):
            self.execute_calls += 1
            self.commands.append((command.command, command.cwd))
            return SandboxCommandResult(ok=False, error=None)
        if any(marker in command.command for marker in self.lost_commands):
            self.lose(handle)
        return await super().execute(handle, command)


async def _run(
    tmp_path: Path,
    *,
    script: list[Any],
    provider: FakeSandboxProvider,
    tool_error_mode: str = TOOL_ERROR_MODE_CORRECTED,
    payload_recording: bool = True,
) -> dict[str, Any]:
    rig = PipelineRig()
    rig.main_script = script
    source = git_repo(tmp_path / "source")
    options = GoalRunOptions(
        goal=GOAL,
        runs_root=tmp_path / "runs",
        workspace_root=tmp_path / "host-workspace",
        config=config(),
        delegation=DelegationConfig(enabled=False),
        provider_factory=rig.build,
        run_id=f"reliability-{uuid.uuid4().hex[:10]}",
        main_write_paths=("app/",),
        sandbox=sandbox_options(source, lambda _root: provider),
        tool_error_mode=tool_error_mode,
        tool_payload_recording=payload_recording,
    )
    session = await run_goal(options)
    session["_requests"] = rig.main_requests()
    return session


def _results(request: dict[str, Any]) -> dict[str, Any]:
    """``tool_use_id`` -> tool result block, from a recorded provider request."""

    found: dict[str, Any] = {}
    for message in request["messages"]:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_result":
                found[block["tool_use_id"]] = block
    return found


def _transcript(run_dir: str, run_id: str) -> list[dict[str, Any]]:
    path = Path(run_dir) / "trajectory" / run_id / "transcript.jsonl"
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line)["payload"]["atlas_trajectory"])
    return events


def _corpus(run_dir: str) -> dict[str, dict[str, Any]]:
    return read_corpus(Path(run_dir) / PRIVATE_PAYLOAD_ROOT / PRIVATE_PAYLOAD_FILE)


def _ledger_rows(run_dir: Path) -> list[dict[str, Any]]:
    """The durable M4 records, flattened: top-level columns plus the JSON ``payload``."""

    connection = sqlite3.connect(run_dir / "ledger.db")
    try:
        cursor = connection.cursor()
        columns = [row[1] for row in cursor.execute("pragma table_info(action_ledger)")]
        rows = []
        for row in cursor.execute("select * from action_ledger order by created_at"):
            record = dict(zip(columns, row, strict=True))
            record["payload"] = json.loads(record.get("payload") or "{}")
            rows.append(record)
        return rows
    finally:
        connection.close()


# ------------------------------------------------------------------ DEV-3


async def test_completed_non_zero_exit_is_feedback_not_termination(tmp_path: Path) -> None:
    """The P1b scenario, repaired: exit 2 reaches the model and the run continues."""

    provider = _ScriptedProvider(tmp_path / "sandboxes")
    script = [
        tool_call("bash", {"command": FAILING_COMMAND}, "call-fail"),
        tool_call("list_files", {"path": "."}, "call-list"),
        text("REPORT: the entry is missing; nothing else to do."),
    ]
    session = await _run(tmp_path, script=script, provider=provider)

    # 1. Three model turns happened: a completed error no longer ends the loop.
    requests = session["_requests"]
    assert len(requests) == 3
    assert session["status"] == "succeeded", session["failure"]
    assert session["execution_succeeded"] is True
    assert session["main"]["stop_reason"] == "success"

    # 2. The error, its exit code and its stderr were delivered to the next turn.
    second = _results(requests[1])
    assert set(second) == {"call-fail"}
    assert second["call-fail"]["is_error"] is True
    assert "exit code" in str(second["call-fail"]["content"])

    # 3. The mode that produced this behaviour is in the run evidence, not inferred.
    assert session["tool_error_mode"] == TOOL_ERROR_MODE_CORRECTED
    assert session["sandbox"]["tool_error_mode"] == TOOL_ERROR_MODE_CORRECTED

    # 4. Executed once: the runtime never replays a side-effecting call.
    assert sum(1 for command, _ in provider.commands if FAILING_COMMAND in command) == 1


async def test_legacy_mode_reproduces_the_p1b_termination(tmp_path: Path) -> None:
    """Control: the same fixture and command under ``legacy`` reproduces stage P1b exactly."""

    provider = _ScriptedProvider(tmp_path / "sandboxes")
    session = await _run(
        tmp_path,
        script=[
            tool_call("bash", {"command": FAILING_COMMAND}, "call-fail"),
            text("never reached"),
        ],
        provider=provider,
        tool_error_mode=TOOL_ERROR_MODE_LEGACY,
    )

    assert len(session["_requests"]) == 1
    assert session["status"] == "failed"
    assert session["main"]["stop_reason"] == "side_effect_unknown"
    assert session["tool_error_mode"] == TOOL_ERROR_MODE_LEGACY
    assert session["sandbox"]["tool_error_mode"] == TOOL_ERROR_MODE_LEGACY
    # Exactly one execution: the stop is a classification, not a retry.
    assert sum(1 for command, _ in provider.commands if FAILING_COMMAND in command) == 1


async def test_unobserved_transport_failure_still_stops_the_run(tmp_path: Path) -> None:
    provider = _ScriptedProvider(tmp_path / "sandboxes", fail_commands=(FAILING_COMMAND,))
    session = await _run(
        tmp_path,
        script=[
            tool_call("bash", {"command": FAILING_COMMAND}, "call-fail"),
            text("never reached"),
        ],
        provider=provider,
    )

    assert len(session["_requests"]) == 1
    assert session["status"] == "failed"
    assert session["main"]["stop_reason"] == "side_effect_unknown"
    assert "sandbox execution failed" in session["main"]["stop_message"]


async def test_lost_sandbox_still_stops_the_run(tmp_path: Path) -> None:
    provider = _ScriptedProvider(tmp_path / "sandboxes", lost_commands=(FAILING_COMMAND,))
    session = await _run(
        tmp_path,
        script=[
            tool_call("bash", {"command": FAILING_COMMAND}, "call-fail"),
            text("never reached"),
        ],
        provider=provider,
    )

    assert len(session["_requests"]) == 1
    assert session["status"] == "failed"
    assert session["main"]["stop_reason"] == "side_effect_unknown"


async def test_pre_execution_denial_stays_non_fatal(tmp_path: Path) -> None:
    """P0.1 must not regress: a refusal the tool never executed is data, not a stop."""

    provider = _ScriptedProvider(tmp_path / "sandboxes")
    before = provider.execute_calls
    script = [
        tool_call("bash", {"command": DENIED_COMMAND}, "call-denied"),
        tool_call("list_files", {"path": "."}, "call-list"),
        text("REPORT: the shell refusal is understood."),
    ]
    session = await _run(tmp_path, script=script, provider=provider)

    assert len(session["_requests"]) == 3
    assert session["main"]["stop_reason"] == "success"

    denials = session["ownership"]["denials"]
    assert len(denials) == 1
    assert denials[0]["reason_code"] == "OWNERSHIP_UNCLASSIFIABLE_SHELL"
    # Not executed: the refusal happened before the tool body, so no command carries the marker.
    assert not any(DENIED_COMMAND in command for command, _ in provider.commands)
    assert provider.execute_calls >= before


async def test_batch_with_one_known_failure_keeps_every_result(tmp_path: Path) -> None:
    provider = _ScriptedProvider(tmp_path / "sandboxes")

    def batch(_messages: Any) -> list[Any]:
        return [
            ToolUseBlock(id="call-fail", name="bash", input={"command": FAILING_COMMAND}),
            ToolUseBlock(id="call-read", name="file_read", input={"path": "app/calc.py"}),
        ]

    session = await _run(tmp_path, script=[batch, text("REPORT: done.")], provider=provider)

    results = _results(session["_requests"][1])
    assert set(results) == {"call-fail", "call-read"}
    assert results["call-fail"]["is_error"] is True
    assert results["call-read"]["is_error"] is False
    assert "return a + b" in str(results["call-read"]["content"])
    assert session["main"]["stop_reason"] == "success"


async def test_ledger_records_the_completed_error_without_authorising_replay(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(tmp_path / "sandboxes")
    session = await _run(
        tmp_path,
        script=[
            tool_call("bash", {"command": FAILING_COMMAND}, "call-fail"),
            text("REPORT: done."),
        ],
        provider=provider,
    )
    assert session["main"]["stop_reason"] == "success"

    ledger = _ledger_rows(Path(session["run_dir"]))
    assert ledger
    bash_rows = [row for row in ledger if row["tool_name"] == "bash"]
    failing = [row for row in bash_rows if row["state"] == "UNCERTAIN"]
    # M4 stays conservative: a completed command error is not a settled, replayable action.
    assert failing, ledger
    for row in failing:
        assert row["completion_kind"] == "EXECUTED_ERROR"
        assert row["payload"]["retry_safe"] is False
    # Nothing was replayed, and no row claims a settled completion for the failing command.
    assert not [row for row in ledger if row["completion_kind"] == "REPLAYED"]
    assert not [row for row in bash_rows if row["completion_kind"] == "COMPLETED"]


# ------------------------------------------------------------------ DEV-4


async def test_terminating_turn_input_survives_side_effect_unknown(tmp_path: Path) -> None:
    """The failing call is recoverable from the corpus even though the run died inside the turn."""

    provider = _ScriptedProvider(tmp_path / "sandboxes")
    session = await _run(
        tmp_path,
        script=[
            tool_call("bash", {"command": FAILING_COMMAND}, "call-fail"),
            text("never reached"),
        ],
        provider=provider,
        tool_error_mode=TOOL_ERROR_MODE_LEGACY,
    )
    assert session["main"]["stop_reason"] == "side_effect_unknown"

    # 1. The evidence says a private corpus exists and how many payloads it holds.
    integrity = session["execution_integrity"]
    assert integrity["mode"] == "record"
    assert integrity["payload_file"] == "private/tool_payloads.jsonl"
    assert integrity["write_failures"] == 0
    assert integrity["authority"] == "evidence_only"
    assert integrity["records"] >= 1

    # 2. The proposal for the terminating call is in the transcript, with a resolvable reference.
    events = _transcript(session["run_dir"], session["run_id"])
    proposed = [
        event["data"]
        for event in events
        if event["event_type"] == "action_proposed" and event["data"]["call_id"] == "call-fail"
    ]
    assert len(proposed) == 1
    proposal = proposed[0]
    assert proposal["tool_name"] == "bash"
    assert proposal["input_state"] == "RECORDED"
    assert proposal["input_ref"]
    assert proposal["input_digest"]

    # 3. The exact command that ended the run is in the corpus, under that reference.
    corpus = _corpus(session["run_dir"])
    assert proposal["input_ref"] in corpus
    record = corpus[proposal["input_ref"]]
    assert record["payload"] == {"command": FAILING_COMMAND}
    assert record["call_id"] == "call-fail"
    assert record["tool_id"] == "bash"
    assert record["input_digest"] == proposal["input_digest"]

    # 4. The error classification is traceable too, correlated to the same call id.
    assert session["main"]["stop_reason"] == "side_effect_unknown"
    outcomes = [
        event["data"]
        for event in events
        if event["event_type"] == "execution_outcome"
        and event["correlation"].get("call_id") == "call-fail"
    ]
    assert outcomes, "the failed execution left no execution_outcome record"
    assert outcomes[-1]["state"] == "UNCERTAIN"
    assert outcomes[-1]["completion_kind"] == "EXECUTED_ERROR"


async def test_payload_recording_can_be_switched_off(tmp_path: Path) -> None:
    """The evidence mechanism is removable, and says so instead of pretending to be empty."""

    provider = _ScriptedProvider(tmp_path / "sandboxes")
    session = await _run(
        tmp_path,
        script=[tool_call("list_files", {"path": "."}, "call-list"), text("REPORT: done.")],
        provider=provider,
        payload_recording=False,
    )

    assert session["execution_integrity"]["mode"] == "off"
    assert session["execution_integrity"]["records"] == 0
    assert not (Path(session["run_dir"]) / PRIVATE_PAYLOAD_ROOT).exists()

    events = _transcript(session["run_dir"], session["run_id"])
    proposals = [event["data"] for event in events if event["event_type"] == "action_proposed"]
    assert proposals
    assert all(proposal["input_ref"] is None for proposal in proposals)
