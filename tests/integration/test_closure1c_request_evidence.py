"""Closure-1C: request-evidence completeness, index integrity, cancellation and usage sources.

Everything here is deterministic: the provider is a fake, the client is a stub, and no container is
created. The assertions are about what the evidence is allowed to *claim*, not about model output.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from anycode.types import TokenUsage
from experiments.evaluation.runner import (
    REQUEST_CANCELLED,
    REQUEST_COMPLETED,
    REQUEST_FAILED,
    ModelRecordingAdapter,
    _aggregate_observations,
    _estimate_cost,
    _request_linkage,
    _RequestEvidence,
)

from atlasos.config import Pricing
from atlasos.context import InMemoryContextStore
from atlasos.context.models import ContextBlock, ContextKind
from atlasos.context.renderer import render_dependency_block
from atlasos.metrics.models import InferenceObservation, InferenceUsage


class _RaisingInner:
    """An adapter that raises a chosen exception on the first call."""

    name = "raising"

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    async def chat(self, messages: Any, options: Any) -> Any:
        self.calls += 1
        raise self._error

    def stream(self, messages: Any, options: Any) -> Any:  # pragma: no cover - not used here
        raise self._error


def _options(*, task: str = "run-w1", request: int = 0) -> Any:
    del request
    return SimpleNamespace(
        system_prompt="SYSTEM",
        tools=(),
        execution_context=SimpleNamespace(
            attributes={
                "task_id": task,
                "worker_id": task,
                "profile_id": "coding_worker",
                "run_id": "run",
            }
        ),
    )


def _messages(text: str) -> list[Any]:
    return [SimpleNamespace(role="user", content=[SimpleNamespace(text=text)])]


def _worker(task_id: str = "run-w1", profile: str = "coding_worker") -> Any:
    return SimpleNamespace(
        agent_config=SimpleNamespace(system_prompt="SYSTEM"),
        record=SimpleNamespace(
            task_id=task_id,
            worker_id=task_id,
            profile_id=profile,
            effective_system_prompt_hash=hashlib.sha256(b"SYSTEM").hexdigest(),
            execution_context=SimpleNamespace(attributes={"run_id": "run"}),
        ),
    )


def _render(*, dependency: str, content: str, task_id: str = "w2") -> tuple[dict[str, Any], str]:
    prompt = "TASK TURN\n\n" + render_dependency_block(
        task_id=dependency, version=1, content=content
    )
    return (
        {
            "task_id": task_id,
            "task_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "declared_dependencies": [dependency],
        },
        prompt,
    )


def _observe(
    evidence: _RequestEvidence, text: str, *, task: str = "run-w2", completed: bool = True
) -> int:
    index = evidence.observe(_messages(text), _options(task=task))
    if completed:
        evidence.mark_completed(index)
    return index


# -- A1: every consumer request must carry the dependency probe --------------------------------


def test_a_probe_missing_on_the_middle_request_is_reported() -> None:
    store = InMemoryContextStore()
    evidence = _RequestEvidence()
    evidence.bind_store(store)
    per_task, prompt = _render(dependency="w1", content="RESULT-OF-w1")

    store.put(
        ContextBlock(
            block_id="task-output:w1",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            version=1,
            content="RESULT-OF-w1",
            task_id="w1",
        )
    )
    _observe(evidence, prompt)
    _observe(evidence, prompt)
    _observe(evidence, prompt)
    # Drop the probe from the middle request only: the first and last remain correct.
    evidence.records[1]["dependency_probes"] = {}

    linkage = _request_linkage(
        [per_task], evidence.records, {"w2": _worker("run-w2")}, responses_observed=3
    )
    assert linkage["status"] == "REQUEST_CONTENT_PARTIAL"
    assert "DEPENDENCY_NOT_FULLY_DELIVERED" in linkage["reason_codes"]
    failure = linkage["dependency_failures"][0]
    assert failure["reason"] == "PROBE_MISSING_ON_CONSUMER_REQUEST"
    assert failure["calls_without_probe"] == [1]


def test_a_probe_missing_on_the_first_request_is_reported() -> None:
    store = InMemoryContextStore()
    evidence = _RequestEvidence()
    evidence.bind_store(store)
    per_task, prompt = _render(dependency="w1", content="RESULT-OF-w1")

    _observe(evidence, prompt)
    store.put(
        ContextBlock(
            block_id="task-output:w1",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            version=1,
            content="RESULT-OF-w1",
            task_id="w1",
        )
    )
    _observe(evidence, prompt)
    evidence.records[0]["dependency_probes"] = {}

    linkage = _request_linkage(
        [per_task], evidence.records, {"w2": _worker("run-w2")}, responses_observed=2
    )
    assert linkage["dependency_failures"][0]["calls_without_probe"] == [0]
    assert linkage["status"] == "REQUEST_CONTENT_PARTIAL"


def test_every_request_carrying_the_probe_is_verified() -> None:
    store = InMemoryContextStore()
    evidence = _RequestEvidence()
    evidence.bind_store(store)
    per_task, prompt = _render(dependency="w1", content="RESULT-OF-w1")
    store.put(
        ContextBlock(
            block_id="task-output:w1",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            version=1,
            content="RESULT-OF-w1",
            task_id="w1",
        )
    )
    for _ in range(3):
        _observe(evidence, prompt)

    linkage = _request_linkage(
        [per_task], evidence.records, {"w2": _worker("run-w2")}, responses_observed=3
    )
    assert linkage["status"] == "REQUEST_CONTENT_VERIFIED", linkage["reason_codes"]
    assert linkage["dependency_failures"] == []


def test_an_empty_dependency_body_is_still_a_structured_block() -> None:
    store = InMemoryContextStore()
    evidence = _RequestEvidence()
    evidence.bind_store(store)
    store.put(
        ContextBlock(
            block_id="task-output:w1",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            version=1,
            content="",
            task_id="w1",
        )
    )
    per_task, prompt = _render(dependency="w1", content="")
    _observe(evidence, prompt)

    probe = evidence.records[0]["dependency_probes"]["task-output:w1"]
    assert probe["present"] is True
    assert probe["count"] == 1
    assert probe["content_empty"] is True
    assert probe["content_present"] is None

    linkage = _request_linkage(
        [per_task], evidence.records, {"w2": _worker("run-w2")}, responses_observed=1
    )
    assert linkage["status"] == "REQUEST_CONTENT_VERIFIED", linkage["reason_codes"]


def test_a_dependency_quoted_in_the_history_is_redundancy_not_a_second_injection() -> None:
    store = InMemoryContextStore()
    evidence = _RequestEvidence()
    evidence.bind_store(store)
    block = ContextBlock(
        block_id="task-output:w1",
        kind=ContextKind.DEPENDENCY_OUTPUT,
        version=1,
        content="RESULT-OF-w1",
        task_id="w1",
    )
    store.put(block)
    structured = render_dependency_block(task_id="w1", version=1, content="RESULT-OF-w1")
    index = evidence.observe(
        [
            SimpleNamespace(role="user", content=[SimpleNamespace(text=structured)]),
            SimpleNamespace(
                role="assistant", content=[SimpleNamespace(text=f"quoting:\n{structured}")]
            ),
        ],
        _options(task="run-w2"),
    )
    evidence.mark_completed(index)

    probe = evidence.records[0]["dependency_probes"]["task-output:w1"]
    assert probe["count"] == 1
    assert probe["later_history_mentions"] == 1
    per_task = [
        {
            "task_id": "w2",
            "task_prompt_sha256": hashlib.sha256(
                _messages(structured)[0].content[0].text.encode("utf-8")
            ).hexdigest(),
            "declared_dependencies": ["w1"],
        }
    ]
    linkage = _request_linkage(
        per_task, evidence.records, {"w2": _worker("run-w2")}, responses_observed=1
    )
    assert linkage["dependency_failures"] == []
    assert linkage["status"] == "REQUEST_CONTENT_VERIFIED"


# -- A2: index integrity and cancellation ------------------------------------------------------


def _simple_records(
    count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    store = InMemoryContextStore()
    evidence = _RequestEvidence()
    evidence.bind_store(store)
    prompt = "TASK TURN"
    for _ in range(count):
        _observe(evidence, prompt)
    per_task = [
        {
            "task_id": "w1",
            "task_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "declared_dependencies": [],
        }
    ]
    return evidence.records, per_task, {"w1": _worker("run-w1")}


def test_a_duplicate_call_index_fails_the_reconciliation() -> None:
    records, per_task, workers = _simple_records(2)
    records[1]["call_index"] = 0
    linkage = _request_linkage(per_task, records, workers, responses_observed=2)
    integrity = linkage["coverage"]["index_integrity"]
    assert integrity["call_index_unique"] is False
    assert integrity["duplicate_call_indices"] == [0]
    assert "REQUEST_INDEX_INTEGRITY_FAILED" in linkage["reason_codes"]
    assert linkage["status"] == "REQUEST_CONTENT_PARTIAL"


def test_a_missing_call_index_fails_the_reconciliation() -> None:
    records, per_task, workers = _simple_records(3)
    del records[1]
    linkage = _request_linkage(per_task, records, workers, responses_observed=3)
    integrity = linkage["coverage"]["index_integrity"]
    assert integrity["call_index_contiguous"] is False
    # Index 1 is no longer represented: the set has one record at 0 and one at 2.
    assert integrity["missing_call_indices"] == [1]
    assert linkage["coverage"]["status"] == "UNRECONCILED"


def test_a_duplicate_request_index_within_a_task_fails_the_reconciliation() -> None:
    records, per_task, workers = _simple_records(2)
    records[1]["request_index"] = 0
    linkage = _request_linkage(per_task, records, workers, responses_observed=2)
    problems = linkage["coverage"]["index_integrity"]["request_index_problems"]
    assert problems["w1"]["duplicate_request_indices"] == [0]
    assert linkage["status"] == "REQUEST_CONTENT_PARTIAL"


@pytest.mark.asyncio
async def test_a_cancelled_chat_is_cancelled_not_failed() -> None:
    evidence = _RequestEvidence()
    adapter = ModelRecordingAdapter(_RaisingInner(asyncio.CancelledError()), [], evidence)
    with pytest.raises(asyncio.CancelledError):
        await adapter.chat(_messages("TASK"), _options())
    assert evidence.records[0]["status"] == REQUEST_CANCELLED
    assert evidence.records[0]["error_type"] is None


@pytest.mark.asyncio
async def test_a_provider_exception_is_failed_not_cancelled() -> None:
    evidence = _RequestEvidence()
    adapter = ModelRecordingAdapter(_RaisingInner(RuntimeError("provider said no")), [], evidence)
    with pytest.raises(RuntimeError):
        await adapter.chat(_messages("TASK"), _options())
    assert evidence.records[0]["status"] == REQUEST_FAILED
    assert evidence.records[0]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_an_unconsumed_stream_records_nothing_and_a_started_one_can_be_cancelled() -> None:
    evidence = _RequestEvidence()

    class _SlowInner:
        name = "slow"

        def __init__(self) -> None:
            self.released = asyncio.Event()

        async def chat(self, messages: Any, options: Any) -> Any:  # pragma: no cover
            raise AssertionError("stream must be used")

        def stream(self, messages: Any, options: Any) -> Any:
            async def _gen() -> Any:
                yield SimpleNamespace(type="text", data="first")
                await self.released.wait()
                yield SimpleNamespace(
                    type="done", data=SimpleNamespace(model="m", usage=TokenUsage())
                )

            return _gen()

    inner = _SlowInner()
    adapter = ModelRecordingAdapter(inner, [], evidence)

    abandoned = adapter.stream(_messages("TASK"), _options())
    assert evidence.records == []
    await abandoned.aclose()
    assert evidence.records == []

    stream = adapter.stream(_messages("TASK"), _options())
    assert evidence.records == []
    await stream.__anext__()
    assert evidence.records[0]["status"] == "OBSERVED"
    await stream.aclose()
    assert evidence.records[0]["status"] == REQUEST_CANCELLED


@pytest.mark.asyncio
async def test_a_completed_request_stays_completed_when_the_generator_is_closed() -> None:
    evidence = _RequestEvidence()

    class _DoneInner:
        name = "done"

        async def chat(self, messages: Any, options: Any) -> Any:  # pragma: no cover
            raise AssertionError

        def stream(self, messages: Any, options: Any) -> Any:
            async def _gen() -> Any:
                yield SimpleNamespace(
                    type="done", data=SimpleNamespace(model="m", usage=TokenUsage())
                )

            return _gen()

    stream = ModelRecordingAdapter(_DoneInner(), [], evidence).stream(_messages("TASK"), _options())
    async for _ in stream:
        pass
    await stream.aclose()
    assert evidence.records[0]["status"] == REQUEST_COMPLETED


# -- B1: usage sources -------------------------------------------------------------------------


def test_normalized_usage_never_claims_a_cache_split() -> None:
    evidence = _RequestEvidence()
    index = evidence.observe(_messages("TASK"), _options())
    # A normalized object whose cache-read happens to be 0 could be either a reported 0-hit split or
    # no split at all; both must read as unknown.
    evidence.mark_completed(index, TokenUsage(input_tokens=2000, output_tokens=10))
    usage = evidence.records[0]["usage"]
    assert usage["prompt_tokens"] == 2000
    assert usage["completion_tokens"] == 10
    assert usage["cache_hit_tokens"] is None
    assert usage["cache_miss_tokens"] is None
    assert usage["cache_split_known"] is False
    assert "normalized" in usage["source"]


def _observation(
    *, prompt: int = 2000, hit: int | None = None, miss: int | None = None, completion: int = 10
) -> InferenceObservation:
    return InferenceObservation(
        usage=InferenceUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_hit_tokens=hit,
            cache_miss_tokens=miss,
            latency_ms=5.0,
        ),
        stable_prefix_hash="p",
        full_prompt_hash="f",
    )


def test_a_provider_reported_zero_hit_split_is_complete_and_zero() -> None:
    aggregate = _aggregate_observations([_observation(hit=0, miss=2000)])
    assert aggregate["cache_split_status"] == "COMPLETE"
    assert aggregate["cache_hit_tokens"] == 0
    assert aggregate["cache_miss_tokens"] == 2000
    assert aggregate["cache_hit_ratio"] == 0.0
    assert aggregate["prompt_token_consistency"]["consistent"] is True


def test_a_provider_reported_non_zero_hit_split_is_complete() -> None:
    aggregate = _aggregate_observations([_observation(hit=800, miss=1200)])
    assert aggregate["cache_split_status"] == "COMPLETE"
    assert aggregate["cache_hit_ratio"] == pytest.approx(0.4)


def test_an_unreported_split_stays_unknown() -> None:
    aggregate = _aggregate_observations([_observation()])
    assert aggregate["cache_split_status"] == "NONE"
    assert aggregate["cache_hit_tokens"] is None
    assert aggregate["cache_miss_tokens"] is None
    assert aggregate["cache_hit_ratio"] is None
    assert aggregate["cache_metrics_complete"] is False


def test_no_observations_is_not_a_zero_hit_run() -> None:
    aggregate = _aggregate_observations([])
    assert aggregate["cache_split_status"] == "NO_OBSERVATIONS"
    assert aggregate["cache_hit_tokens"] is None
    assert aggregate["cache_miss_tokens"] is None
    assert aggregate["cache_metrics_complete"] is False


def test_a_partially_reported_split_is_partial_and_not_summed() -> None:
    aggregate = _aggregate_observations([_observation(hit=0, miss=2000), _observation()])
    assert aggregate["cache_split_status"] == "PARTIAL"
    assert aggregate["cache_hit_tokens"] is None
    assert aggregate["cache_miss_tokens"] is None
    assert aggregate["cache_hit_ratio"] is None


def test_cost_stays_unknown_when_any_split_is_missing() -> None:
    pricing = Pricing(
        provider="deepseek",
        cache_hit_input_per_million=0.006,
        cache_miss_input_per_million=0.3,
        output_per_million=1.2,
        snapshot_date=date(2026, 11, 6),
    )
    assert _estimate_cost([], pricing) is None
    assert _estimate_cost([_observation()], pricing) is None
    assert _estimate_cost([_observation(hit=0, miss=2000)], pricing) is not None


def test_a_provider_split_that_does_not_add_up_is_recorded_not_corrected() -> None:
    aggregate = _aggregate_observations([_observation(prompt=2000, hit=100, miss=500)])
    assert aggregate["cache_split_status"] == "COMPLETE"
    assert aggregate["cache_hit_tokens"] == 100
    assert aggregate["cache_miss_tokens"] == 500
    consistency = aggregate["prompt_token_consistency"]
    assert consistency["consistent"] is False
    assert consistency["delta"] == 1400
