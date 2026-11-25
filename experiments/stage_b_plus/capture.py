"""Capture the exact Main provider request, offline, from the real composition.

Why a capture and not a config dump
-----------------------------------
``GoalRunOptions`` describes the *configuration* of a run. What the model reads is another thing:
AnyCode composes the system prompt, the filtered tool surface and the first user turn from it, and
only the composition decides which tools are actually offered. A Stage B+ arm is defined by what the
Main can see, so the audit is done on the wire payload, not on the options object.

How it is done
--------------
``run_goal`` is executed with a fake provider factory. The adapter records ``LLMChatOptions`` (which
carries ``system_prompt`` and ``tools``) plus the messages of the first request, then answers with a
plain text block. One turn, no tool call, no model, no sandbox: the request is real and the run
terminates immediately.

The fingerprint keeps digests and small extracts. The full text is written next to it so a reviewer
can read the payload that the numbers describe.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycode.types import (
    LLMChatOptions,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
)

from atlasos.app.main_driven import GoalRunOptions, MainPromptSpec, run_goal
from atlasos.config import AtlasConfig, ToolsConfig
from atlasos.delegation.config import DelegationConfig

STAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = STAGE_DIR.parents[1]

#: A goal unrelated to any benchmark task: the capture measures the composition, not the task.
CAPTURE_GOAL = (
    "Summarise the repository layout of the current workspace in one short paragraph. "
    "Do not modify any file."
)


def digest_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


_digest = digest_text


@dataclass
class CapturingAdapter:
    """Records the first request and answers with text. Never calls a provider."""

    answer: str = "Captured."
    #: AnyCode's provider protocol requires an identity; it is never sent anywhere.
    name: str = "bplus-capture"
    requests: list[dict[str, Any]] = field(default_factory=list)

    def _record(self, messages: Sequence[Any], options: Any) -> None:
        self.requests.append(
            {
                "model": options.model,
                "system_prompt": options.system_prompt,
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    }
                    for tool in (options.tools or ())
                ],
                "max_tokens": getattr(options, "max_tokens", None),
                "temperature": getattr(options, "temperature", None),
                "enable_prompt_cache": getattr(options, "enable_prompt_cache", None),
                "messages": [
                    {
                        "role": getattr(message, "role", None),
                        "content": [
                            {
                                "type": getattr(block, "type", type(block).__name__),
                                "text": getattr(block, "text", None),
                            }
                            for block in (getattr(message, "content", ()) or ())
                        ],
                    }
                    for message in messages
                ],
            }
        )

    def _response(self, options: Any) -> LLMResponse:
        return LLMResponse(
            id="capture-0",
            content=[TextBlock(text=self.answer)],
            model=options.model,
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

    async def chat(self, messages: list[Any], options: LLMChatOptions) -> LLMResponse:
        self._record(messages, options)
        return self._response(options)

    def stream(self, messages: list[Any], options: LLMStreamOptions):
        self._record(messages, options)
        response = self._response(options)

        async def _gen():
            for block in response.content:
                yield StreamEvent(type="text", data=block.text)
            yield StreamEvent(type="done", data=response)

        return _gen()


def fingerprint(request: Mapping[str, Any]) -> dict[str, Any]:
    """The arm-visible features, as numbers and digests a diff can be read from."""

    system_prompt = str(request["system_prompt"] or "")
    tools = list(request["tools"])
    delegate = next((tool for tool in tools if tool["name"] == "delegate_tasks"), None)
    first_user = ""
    for message in request["messages"]:
        if message["role"] == "user":
            first_user = "\n".join(
                block["text"] or "" for block in message["content"] if block["text"]
            )
            break
    schema = dict(delegate["input_schema"]) if delegate is not None else {}
    properties = dict(schema.get("properties") or {})
    task_items = dict((properties.get("tasks") or {}).get("items") or {})
    # Pydantic encodes a nested model as ``$ref`` + ``$defs``, so the per-task fields — including
    # the ``profile_id`` default, which is itself a capability statement — are not inline.
    reference = str(task_items.get("$ref") or "")
    if reference.startswith("#/$defs/"):
        task_items = dict((schema.get("$defs") or {}).get(reference.removeprefix("#/$defs/")) or {})
    task_properties = dict(task_items.get("properties") or {})
    return {
        "model": request["model"],
        "system_prompt": {
            "chars": len(system_prompt),
            "digest": _digest(system_prompt),
            "head": system_prompt[:120],
        },
        "tools": {
            "count": len(tools),
            "names": sorted(tool["name"] for tool in tools),
            "chars": sum(len(tool["description"] or "") for tool in tools),
            "digest": _digest(
                json.dumps(tools, sort_keys=True, separators=(",", ":"), default=str)
            ),
        },
        "delegate_tasks": None
        if delegate is None
        else {
            "description_chars": len(delegate["description"]),
            "description_digest": _digest(delegate["description"]),
            "schema_digest": _digest(
                json.dumps(schema, sort_keys=True, separators=(",", ":"), default=str)
            ),
            "schema_chars": len(json.dumps(schema, sort_keys=True, separators=(",", ":"))),
            "profile_default": (task_properties.get("profile_id") or {}).get("default"),
            "profile_enum": (task_properties.get("profile_id") or {}).get("enum"),
            "task_fields": sorted(task_properties),
        },
        "first_user_turn": {
            "chars": len(first_user),
            "digest": _digest(first_user),
            "head": first_user[:160],
        },
        "messages": len(request["messages"]),
    }


def _fingerprint_diff(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[str]:
    """Field-level differences between two fingerprints, as readable paths."""

    changes: list[str] = []

    def walk(path: str, a: Any, b: Any) -> None:
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            for key in sorted(set(a) | set(b)):
                walk(f"{path}.{key}" if path else str(key), a.get(key), b.get(key))
            return
        if a != b:
            changes.append(path)

    walk("", left, right)
    return changes


@dataclass(frozen=True)
class CaptureArm:
    arm_id: str
    prompt: MainPromptSpec
    worker_profiles: tuple[str, ...]
    worker_exposure: str
    child_write_grants: Mapping[str, tuple[str, ...]]
    worker_max_turns: int | None = None


def _options(
    arm: CaptureArm,
    *,
    root: Path,
    adapter: CapturingAdapter,
    goal: str,
) -> GoalRunOptions:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "package").mkdir(exist_ok=True)
    (workspace / "package" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    return GoalRunOptions(
        goal=goal,
        runs_root=root / "runs",
        workspace_root=workspace,
        config=AtlasConfig(tools=ToolsConfig(mode="progressive")),
        delegation=DelegationConfig(
            enabled=True,
            max_parallel_children=2,
            max_children_per_call=2,
            max_delegation_rounds=3,
            child_timeout_seconds=300.0,
            child_max_turns=20,
            result_preview_chars=900,
        ),
        provider="fake",
        model="deepseek-chat",
        max_turns=1,
        main_write_paths=(),
        child_write_grants=dict(arm.child_write_grants),
        worker_profiles=arm.worker_profiles,
        worker_max_turns=arm.worker_max_turns,
        worker_exposure=arm.worker_exposure,
        provider_factory=lambda: adapter,
        # Unique per invocation: the provider registry is process-global and refuses a duplicate.
        run_id=f"bplus-capture-{arm.arm_id}-{uuid.uuid4().hex[:8]}",
        main_prompt=arm.prompt,
    )


def capture(arm: CaptureArm, *, goal: str = CAPTURE_GOAL) -> dict[str, Any]:
    """Run one arm's composition and return ``{"fingerprint", "request", "session"}``."""

    adapter = CapturingAdapter()
    with tempfile.TemporaryDirectory(prefix=f"bplus-capture-{arm.arm_id}-") as temporary:
        root = Path(temporary)
        options = _options(arm, root=root, adapter=adapter, goal=goal)
        session = asyncio.run(run_goal(options))
    if not adapter.requests:
        raise RuntimeError(
            f"arm {arm.arm_id}: the composition made no provider request "
            f"(status={session.get('status')!r}, failure={session.get('failure')!r})"
        )
    request = adapter.requests[0]
    return {
        "arm_id": arm.arm_id,
        "fingerprint": fingerprint(request),
        "request": request,
        "session": {
            key: session[key]
            for key in (
                "status",
                "failure",
                "main_prompt",
                "delegation_controls",
                "tool_surface",
                "delegation_enabled",
            )
        },
    }


def compare(captures: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pairwise fingerprint differences. The report's §G table is read from this."""

    by_arm = {item["arm_id"]: item["fingerprint"] for item in captures}
    pairs: dict[str, list[str]] = {}
    ids = list(by_arm)
    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            pairs[f"{left}->{right}"] = _fingerprint_diff(by_arm[left], by_arm[right])
    return {"arms": ids, "pairwise_changed_fields": pairs}


def write_capture(payload: Mapping[str, Any], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return destination


__all__ = [
    "CAPTURE_GOAL",
    "CaptureArm",
    "CapturingAdapter",
    "capture",
    "compare",
    "fingerprint",
    "write_capture",
]
