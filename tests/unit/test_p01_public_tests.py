"""Stage P0.1: the public test channel — Host-fixed command, bounded calls, non-terminal FAIL."""

from __future__ import annotations

import json
from typing import Any

import pytest
from anycode.identity.context import ExecutionContext
from anycode.types import AgentInfo, ToolUseContext
from pydantic import ValidationError

from atlasos.app.public_tests import (
    PUBLIC_TEST_TOOL,
    PublicTestContract,
    PublicTestInput,
    PublicTestRunner,
)

CONTRACT = PublicTestContract(
    test_id="public",
    command="python3 -m pytest -q tests/test_public.py",
    timeout_seconds=60,
    max_output_chars=200,
    max_calls=2,
)


class _Sandbox:
    ready = True

    def __init__(self, *, status: str, output: str = "collected 2 items") -> None:
        self.status = status
        self.output = output
        self.commands: list[str] = []

    async def run_public_command(
        self, command: str, *, timeout_seconds: int = 120, output_chars: int = 4000
    ) -> dict[str, Any]:
        del timeout_seconds
        self.commands.append(command)
        text = self.output
        truncated = len(text) > output_chars
        return {
            "command": command,
            "cwd": "/workspace/project",
            "ok": self.status != "ERROR",
            "exit_code": None if self.status == "ERROR" else (0 if self.status == "PASS" else 1),
            "status": self.status,
            "output": text if not truncated else text[:output_chars],
            "output_chars": len(text),
            "output_truncated": truncated,
            "output_digest": "sha256:abc",
            "elapsed_ms": 12.5,
            "error": None if self.status != "ERROR" else "transport lost",
        }


def _context(*, role: str = "main", task_id: str = "run-1-main") -> ToolUseContext:
    return ToolUseContext(
        agent=AgentInfo(name="atlas-main", role="main", model="fake"),
        execution_context=ExecutionContext(
            principal="user:test",
            tenant_scope="atlas",
            attributes={
                "run_id": "run-1",
                "task_id": task_id,
                "worker_id": f"{task_id}-worker",
                "atlas_role": role,
            },
        ),
    )


def _runner(sandbox: _Sandbox, contract: PublicTestContract = CONTRACT) -> PublicTestRunner:
    return PublicTestRunner(sandbox, contract, task_id="run-1-main", agent_name="atlas-main")


def test_the_command_cannot_be_supplied_by_the_model() -> None:
    with pytest.raises(ValidationError):
        PublicTestInput.model_validate({"command": "rm -rf /"})
    assert PublicTestInput.model_validate({"reason": "first attempt"}).reason == "first attempt"


async def test_a_passing_run_is_reported_with_its_exit_code() -> None:
    sandbox = _Sandbox(status="PASS")
    runner = _runner(sandbox)

    result = await runner.run_public_tests(PublicTestInput(), _context())

    payload = json.loads(result.data)
    assert not result.is_error
    assert result.retry_safe is True
    assert payload["tool"] == PUBLIC_TEST_TOOL
    assert payload["test_id"] == "public"
    assert payload["command"] == CONTRACT.command
    assert payload["status"] == "PASS"
    assert payload["exit_code"] == 0
    assert payload["calls_remaining"] == 1
    assert sandbox.commands == [CONTRACT.command]


async def test_a_failing_run_is_data_the_main_can_act_on() -> None:
    sandbox = _Sandbox(status="FAIL", output="FAILED tests/test_public.py::test_subtract")
    runner = _runner(sandbox)

    result = await runner.run_public_tests(PublicTestInput(reason="verify"), _context())

    payload = json.loads(result.data)
    assert result.is_error is True
    # The suite completed and its exit status was observed: the loop must continue.
    assert result.retry_safe is True
    assert payload["status"] == "FAIL"
    assert payload["exit_code"] == 1
    assert "test_subtract" in payload["output"]


async def test_a_transport_failure_is_not_downgraded() -> None:
    sandbox = _Sandbox(status="ERROR")
    runner = _runner(sandbox)

    result = await runner.run_public_tests(PublicTestInput(), _context())

    assert result.is_error is True
    assert result.retry_safe is False
    assert "transport lost" in json.loads(result.data)["error"]


async def test_a_child_agent_cannot_run_the_public_suite() -> None:
    sandbox = _Sandbox(status="PASS")
    runner = _runner(sandbox)

    result = await runner.run_public_tests(
        PublicTestInput(), _context(role="worker", task_id="run-1-main:r1:calc")
    )

    payload = json.loads(result.data)
    assert payload["status"] == "REFUSED"
    assert payload["refusal_code"] == "public_test_denied"
    assert payload["executed"] is False
    assert result.retry_safe is True
    assert sandbox.commands == []


async def test_the_call_cap_refuses_without_executing() -> None:
    sandbox = _Sandbox(status="FAIL")
    runner = _runner(sandbox)

    first = await runner.run_public_tests(PublicTestInput(), _context())
    second = await runner.run_public_tests(PublicTestInput(), _context())
    third = await runner.run_public_tests(PublicTestInput(), _context())

    assert first.retry_safe is True
    assert second.retry_safe is True
    payload = json.loads(third.data)
    assert payload["status"] == "REFUSED"
    assert payload["refusal_code"] == "public_test_budget_exhausted"
    assert len(sandbox.commands) == 2
    assert len(runner.evidence()) == 2


async def test_the_evidence_records_the_command_identity_without_its_text() -> None:
    sandbox = _Sandbox(status="PASS")
    runner = _runner(sandbox)

    await runner.run_public_tests(PublicTestInput(), _context())
    record = runner.evidence()[0]

    assert record["test_id"] == "public"
    assert record["command_digest"] == CONTRACT.command_digest()
    assert record["status"] == "PASS"
    assert "command" not in record
