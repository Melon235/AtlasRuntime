"""Parent-scoped handoff access for Main-driven delegation.

The static-DAG :class:`atlasos.handoff.HandoffReader` authorizes a *consumer node* to read results
produced by one of its *direct dependencies*. That rule is meaningless here: the Main has no
dependency edge to a child it spawned at runtime, and a child has no edge to a sibling.

Stage M therefore adds a second, strictly narrower reader over the **same** run-scoped
:class:`atlasos.handoff.HandoffStore`:

* storage is unchanged — content-addressed, ``ho-<24 hex>`` references, digest-verified;
* authorization comes from a trusted registry written by the runtime when the child finished, and
  the caller must be the delegation authority (``atlas_role="main"``, depth 0, same run, same parent
  task) that produced that child.

A child cannot read a sibling's result, and the DAG reader is not weakened: it stays the only reader
of DAG handoffs and is not even registered on this path (the tool name is different).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from anycode.types import ToolDefinition, ToolResult

from atlasos.delegation.models import DelegationCaller
from atlasos.handoff import (
    READ_MAX_RETURN_CHARS,
    HandoffIntegrityError,
    HandoffNotFound,
    HandoffReadInput,
    HandoffStore,
)

DELEGATE_READ_TOOL = "delegate.read"
_INDEX_SCHEMA_VERSION = 1
_INDEX_DIRNAME = "delegation"
_INDEX_FILENAME = "handoffs.json"


class DelegatedResultIndex:
    """Durable ``ref_id -> producer identity`` table for one run.

    The table is written by the runtime after a child terminates and is never derived from model
    output. A missing entry is an authorization failure, not a "not found" convenience.
    """

    def __init__(self, *, run_id: str, path: str | Path | None) -> None:
        if not run_id.strip():
            raise ValueError("a delegated result index requires a trusted run id")
        self.run_id = run_id
        self.path = Path(path) if path is not None else None
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != _INDEX_SCHEMA_VERSION:
            raise ValueError("unsupported delegated handoff index schema")
        if payload.get("run_id") != self.run_id:
            raise ValueError("delegated handoff index belongs to a different run")
        records = payload.get("refs")
        if isinstance(records, dict):
            self._records = {
                str(ref): dict(record)
                for ref, record in records.items()
                if isinstance(record, dict)
            }

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _INDEX_SCHEMA_VERSION,
            "run_id": self.run_id,
            "refs": self._records,
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    # -- records --------------------------------------------------------------

    def record(
        self,
        *,
        ref_id: str,
        parent_task_id: str,
        parent_agent: str,
        worker_id: str,
        key: str,
    ) -> None:
        self._records[ref_id] = {
            "parent_task_id": parent_task_id,
            "parent_agent": parent_agent,
            "worker_id": worker_id,
            "key": key,
            "run_id": self.run_id,
        }
        self._persist()

    def lookup(self, ref_id: str) -> dict[str, Any] | None:
        record = self._records.get(ref_id)
        return dict(record) if record is not None else None

    def refs_for_parent(self, parent_task_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                ref_id
                for ref_id, record in self._records.items()
                if record.get("parent_task_id") == parent_task_id
            )
        )


class DelegatedResultReader:
    """Read-only paged access to results produced by the caller's own children."""

    def __init__(
        self,
        *,
        store: HandoffStore,
        index: DelegatedResultIndex,
        parent_task_id: str,
        parent_agent: str,
    ) -> None:
        self.store = store
        self.index = index
        self.parent_task_id = parent_task_id
        self.parent_agent = parent_agent
        self.read_calls = 0
        self.read_success = 0
        self.read_denied = 0

    def authorize(self, ref_id: str, caller: DelegationCaller) -> str | None:
        """Return a denial reason, or ``None`` when the caller may read ``ref_id``."""

        if not caller.is_main:
            return "caller is not the delegation authority"
        if caller.task_id != self.parent_task_id:
            return f"caller task {caller.task_id!r} is not the delegation root"
        record = self.index.lookup(ref_id)
        if record is None:
            return f"no delegated child of this run produced {ref_id}"
        if record.get("parent_task_id") != self.parent_task_id:
            return f"{ref_id} was not produced by a child of this task"
        return None

    async def read(self, validated: HandoffReadInput, context: Any) -> ToolResult:
        self.read_calls += 1
        caller = caller_from_context(context)
        denial = self.authorize(validated.ref_id, caller)
        if denial is not None:
            self.read_denied += 1
            return _error(f"delegate_read_denied: {denial}")
        if not self.store.is_ref_id(validated.ref_id):
            self.read_denied += 1
            return _error(f"delegate_read_denied: {validated.ref_id!r} is not a valid reference")
        try:
            ref, content = self.store.load(validated.ref_id)
        except HandoffNotFound as error:
            self.read_denied += 1
            return _error(f"delegate_ref_not_found: {error}")
        except HandoffIntegrityError as error:
            self.read_denied += 1
            return _error(f"delegate_read_integrity: {error}")
        total = len(content)
        if validated.offset > total:
            self.read_denied += 1
            return _error(
                f"delegate_read_invalid: offset {validated.offset} is beyond the end of "
                f"{ref.ref_id} ({total} chars)"
            )
        page = content[validated.offset : validated.offset + validated.limit][
            :READ_MAX_RETURN_CHARS
        ]
        end = validated.offset + len(page)
        self.read_success += 1
        return ToolResult(
            data=json.dumps(
                {
                    "ref_id": ref.ref_id,
                    "producer": ref.producer_key,
                    "producer_title": ref.producer_title,
                    "offset": validated.offset,
                    "limit": validated.limit,
                    "returned_chars": len(page),
                    "total_chars": total,
                    "next_offset": end if end < total else None,
                    "complete": end >= total and validated.offset == 0,
                    "content": page,
                },
                sort_keys=True,
            ),
            is_error=False,
        )

    def tool_definition(self) -> ToolDefinition:
        async def execute(validated: HandoffReadInput, context: Any) -> ToolResult:
            return await self.read(validated, context)

        return ToolDefinition(
            name=DELEGATE_READ_TOOL,
            description=(
                "Read the complete result of a subworker this agent delegated to in this run. Pass "
                "the ref_id from the delegate_tasks result plus an offset; pages are at most 3000 "
                "characters and next_offset continues from the last page. Only results produced "
                "by your own subworkers in this run are readable."
            ),
            input_model=HandoffReadInput,
            execute=execute,
            side_effecting=False,
            idempotency_key_field=None,
        )

    def metrics(self) -> dict[str, int]:
        return {
            "read_calls": self.read_calls,
            "read_success": self.read_success,
            "read_denied": self.read_denied,
        }


def caller_from_context(context: Any) -> DelegationCaller:
    """Read the delegation identity from a trusted ``ToolUseContext``."""

    execution_context = getattr(context, "execution_context", None)
    attributes = getattr(execution_context, "attributes", None)
    if not isinstance(attributes, dict):
        return DelegationCaller()
    nested = attributes.get("atlas_worker")
    role = attributes.get("atlas_role")
    depth = attributes.get("delegation_depth")
    # ``worker_id`` alone is not a worker signal: the delegation authority is also a declared
    # worker so that R1-1 can scope its writes. Only the factory-issued metadata identifies a
    # child.
    return DelegationCaller(
        role=str(role) if role is not None else None,
        depth=_as_int(depth),
        is_worker=isinstance(nested, dict) or str(role or "") == "worker",
        task_id=_as_text(attributes.get("task_id")),
        root=_as_text(attributes.get("delegation_root")),
        worker_id=_as_text(attributes.get("worker_id")),
    )


def index_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / _INDEX_DIRNAME / _INDEX_FILENAME


def handoffs_root(run_dir: str | Path) -> Path:
    return Path(run_dir) / "handoffs"


def _error(message: str) -> ToolResult:
    return ToolResult(data=message, is_error=True)


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and value.strip().isdigit():
        return max(0, int(value.strip()))
    return 0


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


__all__ = [
    "DELEGATE_READ_TOOL",
    "DelegatedResultIndex",
    "DelegatedResultReader",
    "caller_from_context",
    "handoffs_root",
    "index_path",
]
