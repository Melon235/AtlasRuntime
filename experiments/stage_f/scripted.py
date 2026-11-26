"""Stage F — the deterministic scripted provider every runner proof executes against.

No model is called. The provider answers with a fixed script, but everything downstream is the real
runtime: the agent loop, the tool registry and executor, the sandbox, the export, the oracle, the
budget guard and the audit. The provider is the only substitute, and it says so in every record it
writes.

Two things are deliberate:

* **Usage is estimated from the request it was given.** ``ceil(chars / 4)`` is the repository's own
  heuristic fallback and the only honest number available without a vendor tokenizer. Every record
  carries ``token_estimate: "chars/4"`` so no reader can mistake it for a billed count.
* **The cache split is a structural zero, labelled as one.** A stateless deterministic provider has
  no prompt cache, so ``hit=0`` and ``miss=prompt`` are facts about this provider, not a measured
  0 % hit rate on a real model.

The adapter also records one row per *request* it answered. That row is the per-request usage
evidence the audit reads, and it is what makes "the budget guard refused and the next request was
never sent" checkable: the provider counts what it actually received.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from anycode.plugins.registry import register_provider_factory
from anycode.providers.fake import FakeAdapter
from anycode.types import (
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)

#: One scripted step: a tool call, or ``None`` for the assistant's final text.
ScriptStep = tuple[str | None, dict[str, Any]]

TOKEN_ESTIMATE_BASIS = "chars/4 (scripted provider estimate, not a billed count)"


def estimate_tokens(text: str) -> int:
    """The repository's heuristic tokenizer fallback, applied to a real request body."""

    return max(1, math.ceil(len(text) / 4))


def message_text(messages: Sequence[Any]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in getattr(message, "content", ()) or ():
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
            else:
                parts.append(str(getattr(block, "input", "") or ""))
    return "\n".join(parts)


def agent_identity(options: Any) -> dict[str, Any]:
    """The trusted ids AnyCode passes through unchanged, or empty when none are carried."""

    context = getattr(options, "execution_context", None)
    attributes = getattr(context, "attributes", None) if context is not None else None
    if not isinstance(attributes, Mapping):
        return {}
    return {
        "trusted_task_id": attributes.get("task_id"),
        "trusted_worker_id": attributes.get("worker_id"),
        "trusted_profile_id": attributes.get("profile_id"),
        "trusted_run_id": attributes.get("run_id"),
    }


@dataclass
class ScriptedRun:
    """One process-wide scripted provider: its scripts, its requests and its agent cursors."""

    scripts: dict[str, list[ScriptStep]]
    markers: dict[str, str] = field(default_factory=dict)
    default_marker: str | None = None
    final_text: str = "implemented, checked and reported"
    model_name: str = "scripted-stage-f"
    requests: list[dict[str, Any]] = field(default_factory=list)
    _cursors: dict[str, int] = field(default_factory=dict)

    def marker_for(self, messages: Sequence[Any]) -> str:
        """Which script this turn belongs to, decided from the task turn it was actually given."""

        first = messages[0] if messages else None
        blocks = getattr(first, "content", ()) or ()
        text = "\n".join(str(getattr(block, "text", "")) for block in blocks)
        for marker in sorted(self.scripts, key=len, reverse=True):
            probe = self.markers.get(marker, marker)
            if probe and probe in text:
                return marker
        if self.default_marker is not None:
            return self.default_marker
        raise KeyError(
            "no scripted assignment marker matched the task turn; "
            f"markers={sorted(self.markers.values())}"
        )

    def next_step(self, marker: str) -> ScriptStep:
        cursor = self._cursors.get(marker, 0)
        script = self.scripts[marker]
        if cursor < len(script):
            self._cursors[marker] = cursor + 1
            return script[cursor]
        self._cursors[marker] = cursor + 1
        return None, {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "scripts": {key: len(value) for key, value in sorted(self.scripts.items())},
            "markers": dict(sorted(self.markers.items())),
            "requests": list(self.requests),
            "token_estimate": TOKEN_ESTIMATE_BASIS,
            "cache_split": "structural zero: this provider has no prompt cache",
        }


class StageFScriptedAdapter(FakeAdapter):
    """Replays one script per agent and records every request it answered.

    ``name`` must be unique per run. AnyCode keys its process-wide provider capacity registry by
    ``adapter.name``, so two runs that both call themselves ``fake`` collide as soon as their
    concurrency limits differ — the second run fails before its first request. A deterministic
    provider therefore identifies itself the way a real one does.
    """

    def __init__(self, run: ScriptedRun, *, name: str = "scripted-stage-f") -> None:
        super().__init__(responses=[])
        self.run = run
        self.model_name = run.model_name
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        started = time.perf_counter()
        marker = self.run.marker_for(messages)
        tool_name, payload = self.run.next_step(marker)
        request_text = message_text(messages)
        prompt_tokens = estimate_tokens(request_text)
        identity = agent_identity(options)
        blocks: list[Any] = []
        text = ""
        if tool_name:
            blocks.append(
                ToolUseBlock(
                    id=f"toolu-{uuid.uuid4().hex[:8]}",
                    name=tool_name,
                    input=dict(payload),
                )
            )
        else:
            text = self.run.final_text
            blocks.append(TextBlock(text=text))
        completion_tokens = estimate_tokens(text or str(payload))
        latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        self.run.requests.append(
            {
                "index": len(self.run.requests),
                "script": marker,
                "tool": tool_name,
                "prompt_chars": len(request_text),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "token_estimate": TOKEN_ESTIMATE_BASIS,
                "latency_ms": latency_ms,
                **identity,
            }
        )
        return LLMResponse(
            id=f"scripted-{uuid.uuid4().hex[:8]}",
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


def register(name: str, run: ScriptedRun) -> str:
    """Register one scripted provider under a name unique to this proof, and return it."""

    async def factory(**_: Any) -> StageFScriptedAdapter:
        return StageFScriptedAdapter(run, name=name)

    register_provider_factory(name, factory)
    return name


def factory_for(run: ScriptedRun, name: str) -> Any:
    """A provider factory that identifies itself as ``name``, for an injected seam."""

    def build() -> StageFScriptedAdapter:
        return StageFScriptedAdapter(run, name=name)

    build.provider_name = name  # type: ignore[attr-defined]
    return build


def per_agent_peaks(requests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Peak prompt size per agent, and the system-wide maximum, from the request rows.

    An agent whose identity was not carried is reported under ``unattributed`` and never merged into
    a named agent's peak.
    """

    peaks: dict[str, int] = {}
    counts: dict[str, int] = {}
    for row in requests:
        task_id = row.get("trusted_task_id") or "unattributed"
        tokens = int(row.get("prompt_tokens") or 0)
        peaks[str(task_id)] = max(peaks.get(str(task_id), 0), tokens)
        counts[str(task_id)] = counts.get(str(task_id), 0) + 1
    return {
        "per_agent_peak_context_tokens": dict(sorted(peaks.items())),
        "per_agent_requests": dict(sorted(counts.items())),
        "system_max_agent_context": max(peaks.values()) if peaks else None,
    }


__all__ = [
    "TOKEN_ESTIMATE_BASIS",
    "ScriptStep",
    "ScriptedRun",
    "StageFScriptedAdapter",
    "agent_identity",
    "estimate_tokens",
    "factory_for",
    "message_text",
    "per_agent_peaks",
    "register",
]
