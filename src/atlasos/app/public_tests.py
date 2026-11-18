"""The public test channel: a Host-authorized suite the Main may run on itself.

Two constraints shape it.

* **The command is Host-owned.** The model supplies a reason, never a command, so this tool cannot
  become a general shell. That is why it exists at all: ``R0.1.0`` refuses every interpreter
  invocation, and weakening that classifier to let ``pytest`` through would let every worker run
  arbitrary code.
* **A failing suite is data, not a stop.** The command completed and its exit status was observed,
  so the result is ``retry_safe=True``: the Main reads the failure, repairs, and calls again. A
  *transport* failure is the opposite — the command may have run — and stays ``retry_safe=False``.

This channel is not the oracle. The oracle runs after the Main stops, through a different method
that returns digests instead of text, and its verdict is what counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from anycode.types import ToolDefinition, ToolResult, ToolUseContext
from pydantic import BaseModel, ConfigDict, Field

from atlasos.actions.binding import canonical_digest
from atlasos.app.goal_sandbox import GoalSandboxError

PUBLIC_TEST_TOOL = "run_public_tests"
#: Action vocabulary entry. The catalog maps the tool to it; the policy must allow it explicitly.
PUBLIC_TEST_ACTION = "tests.public"

_MAX_REASON_CHARS = 500


class PublicCommandRunner(Protocol):
    """The one sandbox capability this tool needs: run a Host-fixed command, return the text.

    Narrow on purpose. The runner cannot create, export or destroy a sandbox, and it cannot run a
    command the model chose — the only command it ever passes comes from the contract.
    """

    async def run_public_command(
        self, command: str, *, timeout_seconds: int = ..., output_chars: int = ...
    ) -> dict[str, Any]: ...


class PublicTestInput(BaseModel):
    """No command field, by design: the model cannot choose what runs."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(
        default=None,
        max_length=_MAX_REASON_CHARS,
        description="Why you are running the suite. Recorded; it never selects the command.",
    )


@dataclass(frozen=True)
class PublicTestContract:
    """The Host's decision about what the public suite is."""

    test_id: str
    command: str
    timeout_seconds: int = 120
    max_output_chars: int = 4_000
    max_calls: int = 4

    def __post_init__(self) -> None:
        if not self.test_id.strip():
            raise ValueError("public test contract requires a test_id")
        if not self.command.strip():
            raise ValueError("public test contract requires a command")
        if self.max_calls < 1:
            raise ValueError("public test contract requires max_calls >= 1")
        if self.timeout_seconds < 1:
            raise ValueError("public test contract requires timeout_seconds >= 1")

    def payload(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "command": self.command,
            "timeout_seconds": self.timeout_seconds,
            "max_output_chars": self.max_output_chars,
            "max_calls": self.max_calls,
        }

    def command_digest(self) -> str:
        """Identity of *what runs*, independent of the limits around it."""

        return canonical_digest({"test_id": self.test_id, "command": self.command})


class PublicTestRunner:
    """Wrap one Host-fixed command as a Main-only tool with bounded calls."""

    def __init__(
        self,
        sandbox: PublicCommandRunner,
        contract: PublicTestContract,
        *,
        task_id: str,
        agent_name: str,
    ) -> None:
        self.sandbox = sandbox
        self.contract = contract
        self.task_id = task_id
        self.agent_name = agent_name
        self.calls = 0
        self._evidence: list[dict[str, Any]] = []

    # -- evidence ------------------------------------------------------------

    def evidence(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._evidence]

    # -- tool ----------------------------------------------------------------

    def tool_definition(self) -> ToolDefinition:
        contract = self.contract

        async def execute(validated: PublicTestInput, context: ToolUseContext) -> ToolResult:
            return await self.run_public_tests(validated, context)

        return ToolDefinition(
            name=PUBLIC_TEST_TOOL,
            description=(
                "Run the project's authorized public test suite inside the sandbox and read its "
                f"output. The command is fixed by the host ({contract.command!r}); you cannot pass "
                "a different one. A failing suite returns the exit code and the output tail so you "
                "can repair the code and call this tool again "
                f"(at most {contract.max_calls} times per run). This is not the acceptance "
                "oracle: the harness verifies the deliverable independently after you stop."
            ),
            input_model=PublicTestInput,
            execute=execute,
        )

    async def run_public_tests(
        self, validated: PublicTestInput, context: ToolUseContext
    ) -> ToolResult:
        denial = self._identity_denial(context)
        if denial is not None:
            return _refusal("public_test_denied", denial)
        if self.calls >= self.contract.max_calls:
            return _refusal(
                "public_test_budget_exhausted",
                f"the public suite may be run {self.contract.max_calls} time(s) per run; "
                "this call was refused and nothing executed",
            )

        self.calls += 1
        try:
            outcome = await self.sandbox.run_public_command(
                self.contract.command,
                timeout_seconds=self.contract.timeout_seconds,
                output_chars=self.contract.max_output_chars,
            )
        except GoalSandboxError as error:
            return _refusal("public_test_unavailable", str(error))

        status = str(outcome.get("status") or "ERROR")
        record = {
            "test_id": self.contract.test_id,
            "command_digest": self.contract.command_digest(),
            "call_index": self.calls,
            "status": status,
            "exit_code": outcome.get("exit_code"),
            "duration_ms": outcome.get("elapsed_ms"),
            "output_digest": outcome.get("output_digest"),
            "output_chars": outcome.get("output_chars"),
            "output_truncated": outcome.get("output_truncated"),
            "reason": validated.reason,
        }
        self._evidence.append(record)

        payload = {
            "tool": PUBLIC_TEST_TOOL,
            "test_id": self.contract.test_id,
            "command": outcome.get("command"),
            "status": status,
            "exit_code": outcome.get("exit_code"),
            "duration_ms": outcome.get("elapsed_ms"),
            "call_index": self.calls,
            "calls_remaining": max(self.contract.max_calls - self.calls, 0),
            "output": outcome.get("output") or "",
            "output_truncated": bool(outcome.get("output_truncated")),
        }
        # A completed run has an observed exit status, so the loop may continue either way.
        # ``ERROR`` means the sandbox never reported a status: the command may have run.
        if status == "ERROR":
            payload["error"] = outcome.get("error")
            return ToolResult(
                data=json.dumps(payload, sort_keys=True), is_error=True, retry_safe=False
            )
        return ToolResult(
            data=json.dumps(payload, sort_keys=True),
            is_error=status != "PASS",
            retry_safe=True,
        )

    # -- identity ------------------------------------------------------------

    def _identity_denial(self, context: ToolUseContext | None) -> str | None:
        """The runtime decides who may run the suite, not the prompt."""

        if context is None or context.execution_context is None:
            return "run_public_tests requires a trusted ExecutionContext"
        attributes = context.execution_context.attributes
        role = attributes.get("atlas_role")
        task_id = attributes.get("task_id")
        if role != "main":
            return f"run_public_tests is reserved for the main agent; caller role is {role!r}"
        if task_id != self.task_id:
            return "run_public_tests is reserved for the task that owns this goal run"
        return None


def _refusal(code: str, message: str) -> ToolResult:
    """A refusal that provably did not execute anything, so the loop may continue."""

    return ToolResult(
        data=json.dumps(
            {
                "tool": PUBLIC_TEST_TOOL,
                "status": "REFUSED",
                "refusal_code": code,
                "message": message,
                "executed": False,
            },
            sort_keys=True,
        ),
        is_error=True,
        retry_safe=True,
    )


__all__ = [
    "PUBLIC_TEST_ACTION",
    "PUBLIC_TEST_TOOL",
    "PublicTestContract",
    "PublicTestInput",
    "PublicTestRunner",
]
