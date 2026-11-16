"""Prompt renderers: HOW selected context becomes a stable, ordered prompt.

Two Stage 2 renderers share one layout contract:

- ``NaiveExecutionOrderRenderer`` preserves the incoming block order, which is projection /
  completion order. It exists to expose layout instability under parallel execution.
- ``StablePromptRenderer`` ignores arrival order and sorts by kind, stable task id, version, then
  block id, so equivalent logical context always renders identically.

Rendering is deterministic and free of timestamps, run ids, and metrics.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from anycode.types import LLMMessage, TextBlock

from atlasos.context.fingerprints import context_layout_hash
from atlasos.context.models import ContextBlock, ContextKind, ContextRef
from atlasos.metrics.fingerprint import full_prompt_hash, stable_prefix_hash
from atlasos.workflow import AtlasTaskSpec


class PromptLayout(StrEnum):
    """Where per-task dynamic text sits relative to the cross-task public text.

    ``LEGACY`` reproduces the historical body exactly: the task title first, then every selected
    block. ``SHARED_PREFIX`` renders the shared public context before the task title so two workers
    of the same profile agree on a longer byte prefix. It changes ordering only; selection is the
    selector's decision and is identical in both layouts.
    """

    LEGACY = "legacy"
    SHARED_PREFIX = "shared_prefix"


@dataclass(frozen=True)
class RenderedPrompt:
    messages: list[dict[str, str]]
    stable_prefix_text: str
    context_layout: tuple[ContextRef, ...]
    stable_prefix_hash: str
    context_layout_hash: str
    full_prompt_hash: str
    stable_prefix_tokens: int | None = None
    context_block_tokens: int | None = None
    dynamic_tail_tokens: int | None = None


class PromptRenderer(Protocol):
    def render(
        self,
        *,
        stable_system_prefix: str,
        agent_role: str,
        task: AtlasTaskSpec,
        context_blocks: Sequence[ContextBlock],
        dynamic_tail: str | None = None,
        shared_context: str = "",
    ) -> RenderedPrompt: ...


_KIND_PRIORITY: dict[ContextKind, int] = {
    ContextKind.SYSTEM: 0,
    ContextKind.TASK: 1,
    ContextKind.DEPENDENCY_OUTPUT: 2,
    ContextKind.MESSAGE: 3,
    ContextKind.TOOL_RESULT: 4,
    ContextKind.ARTIFACT: 5,
    ContextKind.MEMORY: 6,
    ContextKind.SUMMARY: 7,
}


def render_dependency_block(*, task_id: str, version: int, content: str) -> str:
    """The exact text one dependency output is rendered as inside the task body.

    One function, two callers: the body renderer and the request-evidence probe. The probe has to
    match the full block, so it must not re-derive the delimiters independently.
    """

    return f'<dependency task_id="{task_id}" version="{version}">\n{content}\n</dependency>'


def stable_block_key(block: ContextBlock) -> tuple[int, str, int, str]:
    """Total order independent of completion timing, insertion order, or random ids."""

    return (
        _KIND_PRIORITY[block.kind],
        block.task_id or "",
        block.version,
        block.block_id,
    )


class _RendererBase:
    def _order(self, blocks: Sequence[ContextBlock]) -> list[ContextBlock]:
        raise NotImplementedError

    def render(
        self,
        *,
        stable_system_prefix: str,
        agent_role: str,
        task: AtlasTaskSpec,
        context_blocks: Sequence[ContextBlock],
        dynamic_tail: str | None = None,
        shared_context: str = "",
    ) -> RenderedPrompt:
        ordered = self._order(context_blocks)
        body = _render_body(task, ordered, dynamic_tail, shared_context)

        system_text = stable_system_prefix
        if agent_role and f"Role: {agent_role}" not in system_text:
            system_text = f"{system_text}\n\nRole: {agent_role}"

        refs = tuple(
            ContextRef(block_id=block.block_id, version=block.version) for block in ordered
        )
        messages = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": body},
        ]
        return RenderedPrompt(
            messages=messages,
            stable_prefix_text=system_text,
            context_layout=refs,
            stable_prefix_hash=stable_prefix_hash(system_text),
            context_layout_hash=context_layout_hash(refs),
            full_prompt_hash=full_prompt_hash(
                system_text,
                [LLMMessage(role="user", content=[TextBlock(text=body)])],
            ),
        )


class NaiveExecutionOrderRenderer(_RendererBase):
    """Preserve incoming order; deliberately unstable under parallel completion."""

    def _order(self, blocks: Sequence[ContextBlock]) -> list[ContextBlock]:
        return list(blocks)


class StablePromptRenderer(_RendererBase):
    """Deterministic order by kind, stable task id, version, and block id.

    With ``preserve_input_order=True`` the caller has already applied the stable order and an
    optimizer (ContextPilot) may have reordered blocks deliberately; the renderer then preserves
    that explicit order instead of re-sorting it back.
    """

    def __init__(self, *, preserve_input_order: bool = False) -> None:
        self._preserve_input_order = preserve_input_order

    def _order(self, blocks: Sequence[ContextBlock]) -> list[ContextBlock]:
        if self._preserve_input_order:
            return list(blocks)
        return sorted(blocks, key=stable_block_key)


def _render_body(
    task: AtlasTaskSpec,
    blocks: Sequence[ContextBlock],
    dynamic_tail: str | None,
    shared_context: str = "",
) -> str:
    """Render the user turn.

    ``shared_context`` is the cross-task public text. When it is supplied it is emitted first, so
    every worker of a profile shares that prefix before any task-specific text; the ``Task:`` title
    therefore never interrupts the common prefix. With the default empty value the body is
    byte-identical to the historical layout.
    """

    parts: list[str] = []
    if shared_context:
        parts.append(shared_context)
    parts.append(f"Task: {task.title}")
    for block in blocks:
        if block.kind is ContextKind.TASK:
            parts.append(block.content)
        elif block.kind is ContextKind.DEPENDENCY_OUTPUT:
            parts.append(
                render_dependency_block(
                    task_id=block.task_id or "unknown",
                    version=block.version,
                    content=block.content,
                )
            )
    if dynamic_tail:
        parts.append(dynamic_tail)
    return "\n\n".join(parts)
