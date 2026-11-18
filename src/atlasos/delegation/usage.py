"""Per-agent token attribution from the provider response.

Why this exists
---------------
AnyCode 0.9.0 merges usage with ``anycode.helpers.usage_tracker.merge_usage``, which sums only
``input_tokens`` and ``output_tokens``. ``AgentRunResult.token_usage`` therefore cannot carry cache
hit/creation tokens, no matter what the provider reported.

A ``TurnHook.after_turn`` receives the raw ``LLMResponse`` *before* that merge, so the split is
observable exactly once per request. This hook records it per agent, which is what stage M needs:
Main and each child must be attributable separately, and cache metrics must never be fabricated.

Usage is observation only. Nothing here admits, blocks or changes a request.
"""

from __future__ import annotations

from typing import Any

from anycode.types import AgentInfo, LLMMessage, LLMResponse

from atlasos.budget import prompt_tokens_from_usage


class UsageAttributionHook:
    """Accumulate the provider-reported usage of exactly one agent."""

    def __init__(self) -> None:
        self.requests = 0
        self.prompt_tokens = 0
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0
        self.completion_tokens = 0
        #: Full prompt size of each request, in provider order. The observed context length, not
        #: the billed miss side: a cache hit makes a request cheaper, never shorter.
        self.context_tokens: list[int] = []

    async def before_turn(self, messages: list[LLMMessage], context: AgentInfo) -> list[LLMMessage]:
        del context
        return messages

    async def after_turn(self, response: LLMResponse, context: AgentInfo) -> LLMResponse:
        del context
        usage = getattr(response, "usage", None)
        if usage is not None:
            prompt = prompt_tokens_from_usage(usage)
            self.requests += 1
            self.prompt_tokens += prompt
            self.context_tokens.append(prompt)
            self.cache_hit_tokens += max(0, int(usage.cache_read_input_tokens))
            self.cache_miss_tokens += max(0, int(usage.input_tokens))
            self.completion_tokens += max(0, int(usage.output_tokens))
        return response

    @property
    def peak_context_tokens(self) -> int:
        return max(self.context_tokens, default=0)

    def as_dict(self) -> dict[str, int]:
        """Repository token vocabulary.

        ``prompt_tokens`` keeps its historical meaning here: the cache-miss side.
        ``total_prompt_tokens`` is the full prompt, comparable with a provider ``prompt_tokens``.
        """

        return {
            "requests": self.requests,
            "prompt_tokens": self.cache_miss_tokens,
            "total_prompt_tokens": self.prompt_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "completion_tokens": self.completion_tokens,
        }

    def as_evidence(self) -> dict[str, Any]:
        return {
            **self.as_dict(),
            "peak_context_tokens": self.peak_context_tokens,
            "context_tokens_by_request": list(self.context_tokens),
        }


__all__ = ["UsageAttributionHook"]
