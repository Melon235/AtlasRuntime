"""The deterministic provider the offline workbench uses to exercise the real runtime.

It answers from a fixed script. Everything below it is the real thing: the agent loop, the tool
registry and executor, the ownership policy, the delegation plane, the sandbox and the export. The
provider is the only substitute, and every record it touches says so.

Two properties are deliberate and are recorded rather than hidden:

* **Usage is estimated from the request it was given** (``ceil(chars / 4)``, the repository's own
  fallback). Every row carries ``token_estimate`` so no reader can mistake it for a billed count.
* **The cache split is a structural zero**, labelled as one: a stateless scripted provider has no
  prompt cache, so a 0 % hit rate here is a fact about this provider, not a measurement.

This module is not reachable through a request body: the service only builds it when it was
constructed with ``provider="deterministic"``, and the run record then names the provider.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from anycode.providers.fake import FakeAdapter
from anycode.types import (
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)

PROVIDER_NAME = "deterministic"
MODEL_NAME = "deterministic-scripted"
TOKEN_ESTIMATE_BASIS = "chars/4 (deterministic provider estimate, not a billed count)"

#: One scripted step: a tool call, or the assistant's final text.
Step = tuple[str, dict[str, Any]]
CHILD_PATH_PLACEHOLDER = "{child_path}"


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def message_text(messages: Sequence[Any]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in getattr(message, "content", ()) or ():
            text = getattr(block, "text", None)
            parts.append(str(text) if text else str(getattr(block, "input", "") or ""))
    return "\n".join(parts)


def trusted_identity(options: Any) -> dict[str, Any]:
    """The ids AnyCode passes through unchanged, or nothing when the caller carried none."""

    context = getattr(options, "execution_context", None)
    attributes = getattr(context, "attributes", None) if context is not None else None
    if not isinstance(attributes, Mapping):
        return {}
    return {
        "trusted_task_id": attributes.get("task_id"),
        "trusted_worker_id": attributes.get("worker_id"),
        "trusted_run_id": attributes.get("run_id"),
        "trusted_role": attributes.get("atlas_role"),
    }


def delegation_key(task_id: str) -> str:
    """The delegation key a child's trusted task id carries (``<run>-main:r1:<key>``)."""

    parts = str(task_id or "").split(":")
    return parts[-1] if len(parts) > 1 else ""


class ScriptedProviderFailure(RuntimeError):
    """The deterministic provider failing on purpose, so the failure path can be exercised."""


@dataclass
class Script:
    """What each agent role answers, and what it actually answered."""

    main_steps: list[Step]
    child_steps: list[Step] = field(default_factory=list)
    child_paths: dict[str, str] = field(default_factory=dict)
    default_child_path: str = ""
    final_text: str = "work completed and verified"
    #: 1-based index of the first request this provider raises on. From there on it fails every
    #: request, which is what a dead provider looks like: the loop's own retries fail too, so the
    #: failure is unrecoverable rather than a single hiccup. Raised from ``chat``, exactly where a
    #: transport failure would surface.
    fail_from_request: int | None = None
    #: Seconds to wait before each answer. Zero is the honest default (a scripted provider is
    #: instant); a demo raises it so a live timeline has something to show.
    step_delay_seconds: float = 0.0
    requests: list[dict[str, Any]] = field(default_factory=list)
    _cursors: dict[str, int] = field(default_factory=dict)

    def role_of(self, identity: Mapping[str, Any]) -> str:
        task = str(identity.get("trusted_task_id") or "")
        if ":r" in task:
            return "child"
        return "main"

    def steps_for(self, role: str, identity: Mapping[str, Any]) -> list[Step]:
        if role == "main":
            return list(self.main_steps)
        key = delegation_key(str(identity.get("trusted_task_id") or ""))
        path = self.child_paths.get(key, self.default_child_path)
        rendered: list[Step] = []
        for name, payload in self.child_steps:
            replaced = {
                field_name: str(value).replace(CHILD_PATH_PLACEHOLDER, path)
                for field_name, value in payload.items()
            }
            rendered.append((name, replaced))
        return rendered

    def next_step(self, role: str, identity: Mapping[str, Any]) -> Step | None:
        steps = self.steps_for(role, identity)
        # The cursor belongs to the *agent*, not to the role: two children share one script and
        # must each replay it from the beginning.
        cursor_key = str(identity.get("trusted_task_id") or role)
        cursor = self._cursors.get(cursor_key, 0)
        self._cursors[cursor_key] = cursor + 1
        return steps[cursor] if cursor < len(steps) else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": MODEL_NAME,
            "main_steps": len(self.main_steps),
            "child_steps": len(self.child_steps),
            "child_paths": dict(sorted(self.child_paths.items())),
            "requests": list(self.requests),
            "token_estimate": TOKEN_ESTIMATE_BASIS,
            "cache_split": "structural zero: this provider has no prompt cache",
        }


class DeterministicAdapter(FakeAdapter):
    """Replays the script and records every request it answered.

    ``name`` is unique per instance: AnyCode keys its process-wide provider capacity registry by
    ``adapter.name`` and refuses a second registration with different limits, so two agents of one
    run must not share a name.
    """

    def __init__(self, script: Script, *, run_id: str) -> None:
        super().__init__(responses=[])
        self.script = script
        self.run_id = run_id
        self.model_name = MODEL_NAME
        self._name = f"{PROVIDER_NAME}-{uuid.uuid4().hex[:8]}"

    @property
    def name(self) -> str:
        return self._name

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        started = time.perf_counter()
        identity = trusted_identity(options)
        role = self.script.role_of(identity)
        if self.script.step_delay_seconds > 0:
            import asyncio

            await asyncio.sleep(self.script.step_delay_seconds)
        failing = self.script.fail_from_request
        if failing is not None and len(self.script.requests) + 1 >= failing:
            self.script.requests.append(
                {
                    "index": len(self.script.requests),
                    "role": role,
                    "tool": None,
                    "error": "ScriptedProviderFailure",
                    "token_estimate": TOKEN_ESTIMATE_BASIS,
                    **trusted_identity(options),
                }
            )
            raise ScriptedProviderFailure(
                "the deterministic provider was asked to fail on this request"
            )
        step = self.script.next_step(role, identity)

        request_text = message_text(messages)
        prompt_tokens = estimate_tokens(request_text)
        blocks: list[Any] = []
        text = ""
        tool_name: str | None = None
        if step is not None and step[0] != "text":
            tool_name, payload = step
            blocks.append(
                ToolUseBlock(
                    id=f"toolu-{uuid.uuid4().hex[:8]}", name=tool_name, input=dict(payload)
                )
            )
        else:
            text = str(step[1].get("text")) if step is not None else self.script.final_text
            blocks.append(TextBlock(text=text))
        completion_tokens = estimate_tokens(text or tool_name or "tool")
        self.script.requests.append(
            {
                "index": len(self.script.requests),
                "role": role,
                "key": delegation_key(str(identity.get("trusted_task_id") or "")),
                "tool": tool_name,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "token_estimate": TOKEN_ESTIMATE_BASIS,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                **identity,
            }
        )
        return LLMResponse(
            id=f"deterministic-{uuid.uuid4().hex[:8]}",
            content=blocks,
            model=options.model,
            stop_reason="tool_use" if tool_name else "end_turn",
            usage=TokenUsage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )


def direct_script(*, write_path: str) -> Script:
    """One file write, then a final answer. No delegation tool is offered to this run."""

    return Script(
        main_steps=[
            ("file_write", {"path": write_path, "content": "direct completion of the goal\n"}),
            ("text", {"text": "wrote the deliverable at the requested path and reported it"}),
        ],
        final_text="direct execution finished",
    )


def delegated_script(
    *,
    child_paths: Mapping[str, str],
    profile_id: str = "coding_worker",
    main_entry_path: str = "",
) -> Script:
    """One ``delegate_tasks`` call with two independent children, then a final answer.

    ``main_entry_path`` is where the Main records the integration it performed after the children
    returned; when it is empty the Main only answers in text.
    """

    tasks = [
        {
            "key": key,
            "objective": (
                f"Write the file {path} containing one line that names your part ({key}). "
                "Then report the path you wrote."
            ),
            "background": (
                "This is one independent slice of a two-part goal. Write only the file named in "
                "the objective, then report what you wrote."
            ),
            "profile_id": profile_id,
            "write_paths": [path],
            "depends_on": [],
        }
        for key, path in sorted(child_paths.items())
    ]
    main_steps: list[Step] = [
        (
            "delegate_tasks",
            {
                "tasks": tasks,
                "reason": "two independent files can be produced in parallel",
            },
        )
    ]
    if main_entry_path:
        main_steps.append(
            (
                "file_write",
                {
                    "path": main_entry_path,
                    "content": "both parts delegated, received and integrated by the Main\n",
                },
            )
        )
    main_steps.append(("text", {"text": "both parts delegated, integrated and reported"}))
    return Script(
        main_steps=main_steps,
        child_steps=[
            ("file_write", {"path": CHILD_PATH_PLACEHOLDER, "content": "part written\n"}),
            # The report names the file it wrote: two children must not return byte-identical
            # results, which a content-addressed handoff would collapse into one ref.
            ("text", {"text": "wrote " + CHILD_PATH_PLACEHOLDER}),
        ],
        child_paths=dict(child_paths),
        default_child_path=next(iter(child_paths.values()), ""),
        final_text="delegated execution finished",
    )


def factory(script: Script, *, run_id: str):
    """A ``GoalRunOptions.provider_factory`` that identifies itself honestly."""

    def build() -> DeterministicAdapter:
        return DeterministicAdapter(script, run_id=run_id)

    return build


__all__ = [
    "CHILD_PATH_PLACEHOLDER",
    "DeterministicAdapter",
    "MODEL_NAME",
    "PROVIDER_NAME",
    "Script",
    "Step",
    "TOKEN_ESTIMATE_BASIS",
    "delegated_script",
    "delegation_key",
    "direct_script",
    "estimate_tokens",
    "factory",
    "trusted_identity",
]
