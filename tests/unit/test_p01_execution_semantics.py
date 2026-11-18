"""Stage P0.1: pre-execution refusals are deterministic; unknown side effects stay terminal.

The distinction under test is narrow and must not drift:

* the gate refuses a call **before** the tool body runs -> nothing happened -> the agent may read
  the reason and continue (``retry_safe=True``);
* a tool that ran and whose outcome cannot be confirmed -> the agent must stop
  (``retry_safe=False``).

Every test drives the real ``ActionSafetyGate`` (and, where relevant, the real M4 bridge and
ledger) rather than a private helper, so the assertions cover the seam the runner actually reads.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from anycode.identity.context import ExecutionContext
from anycode.tools.executor import ToolExecutor
from anycode.tools.idempotency import InMemoryToolIdempotencyStore
from anycode.tools.registry import ToolRegistry
from anycode.types import AgentInfo, ToolDefinition, ToolResult, ToolUseContext
from pydantic import BaseModel

from atlasos.actions import ActionCatalog, ActionPolicy, ActionRule, ActionSafetyGate
from atlasos.ledger import DurableActionBridge, SQLiteActionLedgerStore
from atlasos.ownership import (
    OwnedPathDeclaration,
    build_manifest,
    build_ownership_enforcer,
)

ROOT = "/workspace/project"
RUN_ID = "run-p01"


class WriteInput(BaseModel):
    path: str
    content: str = "x"


def _context(*, worker: str = "main") -> ToolUseContext:
    task_id = f"{RUN_ID}-{worker}"
    return ToolUseContext(
        agent=AgentInfo(name="atlas-main", role="main", model="fake"),
        execution_context=ExecutionContext(
            principal="user:test",
            tenant_scope="atlas",
            attributes={
                "run_id": RUN_ID,
                "task_id": task_id,
                "worker_id": task_id,
                "profile_id": "coding_worker",
                "operating_scope": "coding",
                "atlas_role": "main" if worker == "main" else "worker",
                "atlas_worker": {
                    "worker_id": task_id,
                    "profile_id": "coding_worker",
                    "task_id": task_id,
                    "parent_agent": "atlas-main",
                },
            },
        ),
    )


def _deny_policy() -> ActionPolicy:
    return ActionPolicy(
        name="test-deny",
        default="DENY",
        rules=(ActionRule(id="allow-shell", decision="ALLOW", action="shell.execute"),),
    )


def _registry(executed: list[dict[str, Any]], *, side_effecting: bool = True) -> ToolRegistry:
    registry = ToolRegistry()

    async def write(validated: WriteInput, context: ToolUseContext) -> ToolResult:
        del context
        executed.append(validated.model_dump(mode="json"))
        return ToolResult(data="written")

    registry.register(
        ToolDefinition(
            name="file_write",
            description="write",
            input_model=WriteInput,
            execute=write,
            side_effecting=side_effecting,
        )
    )
    return registry


def _gate(
    executed: list[dict[str, Any]],
    *,
    pre_execution_retry_safe: bool,
    inner: Any | None = None,
    registry: ToolRegistry | None = None,
) -> ActionSafetyGate:
    registry = registry or _registry(executed)
    executor = inner or ToolExecutor(registry, idempotency_store=InMemoryToolIdempotencyStore())
    return ActionSafetyGate(
        executor,
        registry,
        policy=_deny_policy(),
        catalog=ActionCatalog(),
        pre_execution_denials_retry_safe=pre_execution_retry_safe,
    )


# ---------------------------------------------------------------- the refusal contract


def test_a_rule_denial_is_a_non_terminal_refusal_when_the_host_opts_in() -> None:
    executed: list[dict[str, Any]] = []
    gate = _gate(executed, pre_execution_retry_safe=True)

    result = asyncio.run(gate.execute("file_write", {"path": "a.py"}, _context()))

    assert result.is_error is True
    assert result.retry_safe is True
    assert "action_denied" in result.data
    assert executed == []
    assert gate.audits[-1].executed is False
    assert gate.audits[-1].decision == "DENY"


def test_the_same_denial_stays_terminal_by_default() -> None:
    executed: list[dict[str, Any]] = []
    gate = _gate(executed, pre_execution_retry_safe=False)

    result = asyncio.run(gate.execute("file_write", {"path": "a.py"}, _context()))

    assert result.is_error is True
    assert result.retry_safe is False
    assert executed == []


def test_an_unregistered_tool_is_also_a_pre_execution_refusal() -> None:
    executed: list[dict[str, Any]] = []
    gate = _gate(executed, pre_execution_retry_safe=True)

    result = asyncio.run(gate.execute("file_ghost", {}, _context()))

    assert result.is_error is True
    assert result.retry_safe is True
    assert "not registered" in result.data or "action_hard_denied" in result.data
    assert executed == []


def test_invalid_tool_input_is_a_pre_execution_refusal() -> None:
    executed: list[dict[str, Any]] = []
    gate = _gate(executed, pre_execution_retry_safe=True)

    result = asyncio.run(gate.execute("file_write", {"wrong": 1}, _context()))

    assert result.is_error is True
    assert result.retry_safe is True
    assert executed == []


def test_a_denied_revalidated_execution_does_not_run_the_tool() -> None:
    executed: list[dict[str, Any]] = []
    gate = _gate(executed, pre_execution_retry_safe=True)
    evaluation = asyncio.run(gate.evaluate("file_write", {"path": "a.py"}, _context()))

    result = asyncio.run(gate.execute_revalidated(evaluation, _context()))

    assert evaluation.outcome == "DENY"
    assert result.is_error is True
    assert result.retry_safe is True
    assert executed == []


def test_a_tool_that_ran_and_failed_is_not_downgraded() -> None:
    """The unknown case is the one thing this switch must never touch."""

    executed: list[dict[str, Any]] = []
    registry = ToolRegistry()

    async def explode(validated: WriteInput, context: ToolUseContext) -> ToolResult:
        del validated, context
        return ToolResult(data="transport lost after the write", is_error=True)

    registry.register(
        ToolDefinition(
            name="file_write",
            description="write",
            input_model=WriteInput,
            execute=explode,
            side_effecting=True,
        )
    )
    executor = ToolExecutor(registry, idempotency_store=InMemoryToolIdempotencyStore())
    policy = ActionPolicy(
        name="allow",
        default="DENY",
        rules=(ActionRule(id="allow-write", decision="ALLOW", action="filesystem.write"),),
    )
    gate = ActionSafetyGate(
        executor,
        registry,
        policy=policy,
        catalog=ActionCatalog(),
        pre_execution_denials_retry_safe=True,
    )

    result = asyncio.run(
        gate.execute("file_write", {"path": "a.py"}, _context(), idempotency_key="k1")
    )

    # A side-effecting tool that returned an error without stating retry safety is normalised by
    # AnyCode to "unsafe", and the gate passes that verdict through untouched.
    assert result.is_error is True
    assert result.retry_safe is False
    assert executed == []


# ---------------------------------------------------------------- the ledger record


def test_the_durable_ledger_records_no_row_for_a_refused_call(tmp_path: Path) -> None:
    """M4 records executions. A refusal never reaches it, and that absence is the record."""

    executed: list[dict[str, Any]] = []
    registry = _registry(executed)
    ledger = SQLiteActionLedgerStore(tmp_path / "ledger.db")
    bridge = DurableActionBridge(
        ToolExecutor(registry, idempotency_store=InMemoryToolIdempotencyStore()),
        registry,
        ledger,
        runtime_instance_id="p01",
    )
    gate = ActionSafetyGate(
        cast(Any, bridge),
        registry,
        policy=_deny_policy(),
        catalog=ActionCatalog(),
        pre_execution_denials_retry_safe=True,
    )

    result = asyncio.run(gate.execute("file_write", {"path": "a.py"}, _context()))
    records = ledger.list()

    assert result.retry_safe is True
    assert executed == []
    assert records == ()
    assert gate.audits[-1].executed is False


def test_the_durable_ledger_records_a_completed_execution_as_before(tmp_path: Path) -> None:
    """Control: the same wiring still records a real execution, so the test above is meaningful."""

    executed: list[dict[str, Any]] = []
    registry = _registry(executed)
    ledger = SQLiteActionLedgerStore(tmp_path / "ledger.db")
    bridge = DurableActionBridge(
        ToolExecutor(registry, idempotency_store=InMemoryToolIdempotencyStore()),
        registry,
        ledger,
        runtime_instance_id="p01",
    )
    policy = ActionPolicy(
        name="allow",
        default="DENY",
        rules=(ActionRule(id="allow-write", decision="ALLOW", action="filesystem.write"),),
    )
    gate = ActionSafetyGate(
        cast(Any, bridge),
        registry,
        policy=policy,
        catalog=ActionCatalog(),
        pre_execution_denials_retry_safe=True,
    )

    result = asyncio.run(
        gate.execute("file_write", {"path": "a.py"}, _context(), idempotency_key="k1")
    )
    records = ledger.list()

    assert not result.is_error
    assert len(executed) == 1
    assert len(records) == 1
    assert records[0].state == "COMPLETED"
    assert records[0].completion_kind == "EXECUTED_SUCCESS"


# ---------------------------------------------------------------- ownership integration


def test_an_ownership_denial_is_refused_before_the_body_and_is_non_terminal() -> None:
    executed: list[dict[str, Any]] = []
    registry = _registry(executed)
    manifest = build_manifest(
        run_id=RUN_ID,
        workspace_root=ROOT,
        declarations=(
            OwnedPathDeclaration(
                worker_id=f"{RUN_ID}-main",
                task_id=f"{RUN_ID}-main",
                paths=("allowed.py",),
                provenance="trusted_host_declaration",
            ),
        ),
    )
    enforcer, policy = build_ownership_enforcer(manifest, mode="enforce")
    assert policy is not None
    gate = ActionSafetyGate(
        ToolExecutor(registry, idempotency_store=InMemoryToolIdempotencyStore()),
        registry,
        policy=ActionPolicy(
            name="allow",
            default="DENY",
            rules=(ActionRule(id="allow-write", decision="ALLOW", action="filesystem.write"),),
        ),
        catalog=ActionCatalog(),
        external_policy=enforcer,
        pre_execution_denials_retry_safe=True,
    )

    result = asyncio.run(gate.execute("file_write", {"path": "other.py"}, _context()))

    assert result.is_error is True
    assert result.retry_safe is True
    assert "OWNERSHIP_DENIED_UNDECLARED_PATH" in result.data
    assert executed == []
    assert policy.evidence.denied_calls == 1
