"""The ``delegate_tasks`` tool: how the Main actually asks for subworkers.

It is an ordinary core tool, so the Main reaches it through a real AnyCode tool call inside its own
``AgentRunner`` loop; the ``ToolResult`` returns to that same conversation and the loop continues.

The tool is a thin adapter over :class:`atlasos.delegation.service.DelegationService`:

* it performs no admission, no scheduling and no authorization of its own;
* it never raises — a refusal is a normal ``ToolResult`` the Main can read and act on;
* the service is bound after construction, because the tool must exist in the registry before the
  tool runtime (which the service needs) can be built.
"""

from __future__ import annotations

import json
from typing import Any

from anycode.types import ToolDefinition, ToolResult

from atlasos.delegation.models import (
    REASON_RUNTIME_UNAVAILABLE,
    DelegateTasksInput,
)

STAGE_M_DELEGATE_TOOL = "delegate_tasks"

#: The shipped description. Public so an ablation can append a disclosure to exactly these bytes
#: instead of copying them into another module, where they would drift.
DEFAULT_DESCRIPTION = (
    "Delegate one or more INDEPENDENT subtasks to subworkers that run concurrently in isolated "
    "contexts. Each subworker sees only the objective and background you pass here: it never sees "
    "this conversation, your file reads, or your tool logs. You receive a bounded summary per "
    "subworker plus a ref_id for the full result (read it with delegate.read). The runtime decides "
    "whether the request is admitted; a refused request returns route='direct' and you must do the "
    "work yourself. Subworkers are read-only unless the host granted them writable paths, cannot "
    "delegate further, and receive no results from each other. Supply a background for every "
    "subtask: without it the runtime refuses to create a worker. Do not set depends_on: this call "
    "is one parallel wave, and a declared dependency is refused rather than silently unmet."
)


class DelegateTasksTool:
    """Late-bound ``delegate_tasks`` definition."""

    def __init__(self, *, description: str = DEFAULT_DESCRIPTION) -> None:
        self._service: Any | None = None
        self._disabled_reason: str | None = None
        self._description = description

    def bind(self, service: Any) -> None:
        self._service = service

    def disable(self, reason: str) -> None:
        self._service = None
        self._disabled_reason = reason

    @property
    def bound(self) -> bool:
        return self._service is not None

    def definition(self) -> ToolDefinition:
        async def execute(validated: DelegateTasksInput, context: Any) -> ToolResult:
            if self._service is None:
                return ToolResult(
                    data=json.dumps(
                        {
                            "route": "direct",
                            "reason_codes": [self._disabled_reason or REASON_RUNTIME_UNAVAILABLE],
                            "children": [],
                            "next": "No subworker was created. Continue in this agent loop.",
                        },
                        sort_keys=True,
                    ),
                    is_error=True,
                )
            return await self._service.delegate(validated, context)

        return ToolDefinition(
            name=STAGE_M_DELEGATE_TOOL,
            description=self._description,
            input_model=DelegateTasksInput,
            execute=execute,
            side_effecting=False,
            idempotency_key_field=None,
        )


__all__ = ["DEFAULT_DESCRIPTION", "STAGE_M_DELEGATE_TOOL", "DelegateTasksTool"]
