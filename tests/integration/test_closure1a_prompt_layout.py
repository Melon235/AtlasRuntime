"""Closure-1A integration tests: what the provider really receives, in both layouts.

The engine, the DAG, the worker factory, the selector, the renderer and the request-evidence
wrapper are the real ones. Only the provider is a deterministic fake and there is no sandbox.

Every assertion is made on a request that actually reached an adapter boundary, because a
correctly-built intermediate object that never becomes the request is exactly the defect this
stage exists to fix.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anycode import OrchestratorConfig, TaskSpec
from anycode.identity.context import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.providers.fake import FakeAdapter, FakeResponse
from anycode.types import LLMChatOptions, LLMMessage, TeamConfig, TextBlock
from experiments.evaluation.final4 import runner_entry
from experiments.evaluation.final4.validation_tasks import TASKS as VALIDATION_TASKS
from experiments.evaluation.final4.validation_tasks import Task
from experiments.evaluation.runner import (
    PROMPT_LAYOUT_LEGACY,
    PROMPT_LAYOUT_SHARED_PREFIX,
    EvaluationEnvironmentError,
    ModelRecordingAdapter,
    RunnerSettings,
    _context_evidence,
    _context_prompt_builder,
    _enforce_prompt_layout,
    _environment_note,
    _request_linkage,
    _RequestEvidence,
    _task_turn_parts,
)

from atlasos.config import AtlasConfig, ContextConfig, ContextTransformConfig
from atlasos.context import InMemoryContextStore
from atlasos.context.renderer import render_dependency_block
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerCatalog, WorkerStore
from atlasos.workers.renderer import render_worker_context_hash

SERIAL = next(task for task in VALIDATION_TASKS if task.task_id == "validation-serial")
PARALLEL = next(task for task in VALIDATION_TASKS if task.task_id == "validation-parallel")
ENVIRONMENT_NOTE = _environment_note("/workspace/project")

CAPTURED: dict[str, list[dict[str, Any]]] = {}
#: AnyCode refuses to re-register a provider name with a different factory, so every arm of every
#: test gets its own name. Deterministic within a test, never asserted on.
_RUN_SEQ = [0]


# -- harness ------------------------------------------------------------------------------------


class _RecordingFake(FakeAdapter):
    """The provider itself: it answers and records nothing about evidence.

    Observation, completion and usage attachment are the production wrapper's job, so the test
    exercises exactly the path a real run takes.
    """

    def __init__(self, reply: str, sink: list[dict[str, Any]]) -> None:
        super().__init__(responses=[FakeResponse(text=reply)])
        self._sink = sink

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions):  # type: ignore[override]
        self._sink.append(
            {
                "messages": list(messages),
                "system_prompt": options.system_prompt or "",
                "tools": tuple(getattr(options, "tools", ()) or ()),
                "options": options,
                "execution_context": options.execution_context,
            }
        )
        return await super().chat(messages, options)


def _install(provider: str, reply: str, evidence: _RequestEvidence, models: list[str]) -> None:
    async def factory(**_: Any) -> Any:
        inner = _RecordingFake(reply, CAPTURED.setdefault(provider, []))
        return ModelRecordingAdapter(inner, models, evidence)

    register_provider_factory(provider, factory)


def _config(layout: str) -> AtlasConfig:
    return AtlasConfig(
        context=ContextConfig(
            policy="task_scoped",
            renderer="stable_deterministic",
            prompt_layout=cast("Any", layout),
        )
    )


def _workers(entry: Any, layout: str, tmp_path: Any) -> dict[str, Any]:
    factory = ScopedWorkerFactory(
        model="fake",
        provider="fake",
        store=WorkerStore(tmp_path / f"workers-{layout}.json"),
    )
    parent = ExecutionContext(
        principal="user:evaluation",
        tenant_scope="atlas-evaluation",
        classification="internal",
        attributes={"run_id": "run-1", "sample_id": "sample"},
    )
    workers: dict[str, Any] = {}
    for step in entry.workers:
        assignment = (
            step.prompt
            if layout == PROMPT_LAYOUT_SHARED_PREFIX
            else (f"{ENVIRONMENT_NOTE}\n\n{step.prompt}")
        )
        workers[step.task_id] = factory.create(
            parent_context=parent,
            parent_agent="atlas-evaluation",
            assignment=WorkerAssignment(profile_id="coding_worker", task=assignment),
            worker_id=f"run-1-{step.task_id}",
            task_id=f"run-1-{step.task_id}",
            prompt_layout=layout,
        )
    return workers


async def _run(task: Task, *, layout: str, tmp_path: Any) -> dict[str, Any]:
    CAPTURED.clear()
    entry = runner_entry.build_task(task, multi=True)
    workers = _workers(entry, layout, tmp_path)
    evidence = _RequestEvidence()

    parts = {
        step.task_id: _task_turn_parts(
            step, steps=entry.workers, sample_id=entry.sample_id, mode="legacy"
        )
        for step in entry.workers
    }
    task_texts = {
        task_id: (f"{shared}\n\n{body}" if shared else body)
        for task_id, (shared, body) in parts.items()
    }
    title_by_id = {step.task_id: f"{task.task_id}-{step.task_id}" for step in entry.workers}
    plane = _context_prompt_builder(
        config=_config(layout),
        task=entry,
        title_by_id=title_by_id,
        task_texts=task_texts,
        worker_by_task=workers,
        prompt_layout=layout,
        task_bodies={task_id: body for task_id, (_, body) in parts.items()},
        shared_context_by_task={
            task_id: (f"{ENVIRONMENT_NOTE}\n\n{shared}" if shared else ENVIRONMENT_NOTE)
            for task_id, (shared, _) in parts.items()
        },
    )
    assert plane is not None
    builder, store = plane
    evidence.bind_store(store)

    _RUN_SEQ[0] += 1
    prefix = f"c1a-{_RUN_SEQ[0]}-{layout}-{task.task_id}"
    models: list[str] = []
    for step in entry.workers:
        provider = f"{prefix}-{step.task_id}"
        _install(provider, f"RESULT-OF-{step.task_id}", evidence, models)
        # One provider per worker so the reply identifies the worker deterministically. Only the
        # provider name changes; the system prompt and the tool list stay exactly as created.
        workers[step.task_id] = workers[step.task_id].model_copy(
            update={
                "agent_config": workers[step.task_id].agent_config.model_copy(
                    update={"provider": provider}
                )
            }
        )

    engine = AtlasContextEngine(OrchestratorConfig(), prompt_builder=builder)
    team = engine.create_team(
        "c1a",
        TeamConfig(name="c1a", agents=[workers[s.task_id].agent_config for s in entry.workers]),
    )
    specs = [
        TaskSpec(
            title=title_by_id[step.task_id],
            description=task_texts[step.task_id],
            assignee=workers[step.task_id].worker_name,
            depends_on=[title_by_id[item] for item in step.depends_on],
        )
        for step in entry.workers
    ]
    try:
        result = await engine.run_tasks(team, specs)
    finally:
        await engine.close()
    assert result.success is True

    captured = {key: list(value) for key, value in CAPTURED.items() if key.startswith(prefix)}
    evidence_payload = _context_evidence(
        builder=builder,
        store=store,
        task=entry,
        task_texts=task_texts,
        observations=[],
        request_records=evidence.records,
        workers=workers,
        # The inner adapter answers once per completed call: an independent response-side count.
        responses_observed=sum(len(items) for items in captured.values()),
    )
    return {
        "entry": entry,
        "workers": workers,
        "builder": builder,
        "store": store,
        "evidence": evidence,
        "context_evidence": evidence_payload,
        "captured": captured,
        "layout": layout,
        "provider_prefix": prefix,
        "models": models,
    }


def _request(harness: dict[str, Any], step_id: str, index: int = 0) -> dict[str, Any]:
    return harness["captured"][f"{harness['provider_prefix']}-{step_id}"][index]


def _text(request: dict[str, Any]) -> str:
    return "\n".join(
        str(getattr(block, "text", ""))
        for message in request["messages"]
        for block in message.content
    )


def _initial(request: dict[str, Any]) -> str:
    return "\n".join(str(getattr(block, "text", "")) for block in request["messages"][0].content)


# -- A. two identical-profile workers, one shared public context ---------------------------------


@pytest.mark.asyncio
async def test_legacy_arm_keeps_task_data_in_the_system_prompt(tmp_path: Any) -> None:
    harness = await _run(PARALLEL, layout=PROMPT_LAYOUT_LEGACY, tmp_path=tmp_path)

    first = _request(harness, "w1")
    second = _request(harness, "w2")
    assert "TRUSTED WORKER PROFILE" in first["system_prompt"]
    assert "YOUR ASSIGNMENT" in first["system_prompt"]
    assert ENVIRONMENT_NOTE in first["system_prompt"]
    assert first["system_prompt"] != second["system_prompt"]
    # Historical body shape: the title comes first, so the public block cannot be a shared prefix.
    assert _initial(first).startswith(f"Task: {PARALLEL.task_id}-w1")


@pytest.mark.asyncio
async def test_shared_prefix_arm_removes_the_duplicate_body_and_shares_one_system_prompt(
    tmp_path: Any,
) -> None:
    harness = await _run(PARALLEL, layout=PROMPT_LAYOUT_SHARED_PREFIX, tmp_path=tmp_path)

    w1 = _request(harness, "w1")
    w2 = _request(harness, "w2")
    w3 = _request(harness, "w3")
    systems = {request["system_prompt"] for request in (w1, w2, w3)}
    assert len(systems) == 1
    system = systems.pop()
    assert "TRUSTED WORKER PROFILE" in system
    assert "YOUR ASSIGNMENT" not in system
    assert "ASSIGNED TASK" not in system
    assert ENVIRONMENT_NOTE not in system

    # The effective system prompt on the AgentConfig is the one that was sent, not a side artefact.
    assert harness["workers"]["w1"].agent_config.system_prompt == system
    assert harness["workers"]["w2"].agent_config.system_prompt == system

    # Public facts first, then the task title, then that worker's own objective.
    initial = _initial(w1)
    assert initial.startswith(ENVIRONMENT_NOTE)
    assert initial.index(PARALLEL.shared_context.strip()[:80]) < initial.index(
        f"Task: {PARALLEL.task_id}-w1"
    )
    assert initial.count(PARALLEL.shared_context) == 1
    # The objective is neither lost nor duplicated: it is no longer in the system prompt, and it is
    # in the user turn exactly once.
    objective = str(PARALLEL.workers[0]["assignment"]).splitlines()[0]
    assert objective in initial
    assert system.count(objective) == 0


@pytest.mark.asyncio
async def test_effective_configuration_distinguishes_the_two_layouts(tmp_path: Any) -> None:
    legacy = await _run(PARALLEL, layout=PROMPT_LAYOUT_LEGACY, tmp_path=tmp_path)
    shared = await _run(PARALLEL, layout=PROMPT_LAYOUT_SHARED_PREFIX, tmp_path=tmp_path)

    legacy_renders = legacy["builder"].renders()
    shared_renders = shared["builder"].renders()
    assert {item["prompt_layout"] for item in legacy_renders.values()} == {"legacy"}
    assert {item["prompt_layout"] for item in shared_renders.values()} == {"shared_prefix"}
    assert shared_renders["w1"]["shared_context_chars"] > 0
    assert legacy_renders["w1"]["shared_context_chars"] == 0

    legacy_workers = legacy["workers"]["w1"].record
    shared_workers = shared["workers"]["w1"].record
    assert legacy_workers.prompt_layout == "legacy"
    assert shared_workers.prompt_layout == "shared_prefix"
    # The persisted hash is still the legacy render of that worker's own task context, so the
    # resume contract does not move. It is not the hash of what ran.
    profile = WorkerCatalog().resolve("coding_worker")
    assert shared_workers.rendered_context_hash == render_worker_context_hash(
        profile, shared_workers.task_context
    )
    assert shared_workers.effective_system_prompt_hash != shared_workers.rendered_context_hash
    assert legacy_workers.effective_system_prompt_hash == legacy_workers.rendered_context_hash

    legacy_digest = RunnerSettings(prompt_layout=PROMPT_LAYOUT_LEGACY).digest()
    shared_digest = RunnerSettings(prompt_layout=PROMPT_LAYOUT_SHARED_PREFIX).digest()
    assert legacy_digest != shared_digest


# -- B/C. dependency delivery and trusted identity under a shared system prompt --------------------


@pytest.mark.asyncio
async def test_dependency_output_reaches_the_consumer_once_in_both_layouts(tmp_path: Any) -> None:
    for layout in (PROMPT_LAYOUT_LEGACY, PROMPT_LAYOUT_SHARED_PREFIX):
        harness = await _run(SERIAL, layout=layout, tmp_path=tmp_path)
        consumer = _initial(_request(harness, "w2"))
        producer = _initial(_request(harness, "w1"))

        assert "RESULT-OF-w1" in consumer
        assert consumer.count("RESULT-OF-w1") == 1
        assert "RESULT-OF-w1" not in producer
        assert PARALLEL.shared_context not in consumer  # a different task's public block
        assert "PUBLIC INTERFACE CONTRACT" in consumer

        linkage = harness["context_evidence"]["request_linkage"]
        assert linkage["status"] == "REQUEST_CONTENT_VERIFIED", (layout, linkage)
        assert linkage["reason_codes"] == []
        assert linkage["observed_task_ids"] == ["w1", "w2"]
        assert linkage["dependency_failures"] == []


@pytest.mark.asyncio
async def test_concurrent_workers_with_one_system_prompt_keep_their_real_identity(
    tmp_path: Any,
) -> None:
    harness = await _run(PARALLEL, layout=PROMPT_LAYOUT_SHARED_PREFIX, tmp_path=tmp_path)
    linkage = harness["context_evidence"]["request_linkage"]

    assert linkage["status"] == "REQUEST_CONTENT_VERIFIED"
    assert linkage["observed_task_ids"] == ["w1", "w2", "w3"]
    assert linkage["requests_with_trusted_identity"] == linkage["requests_observed"]
    # Every request is attributed to its own worker even though all three share one system prompt.
    by_call = {item["call_index"]: item for item in linkage["requests"]}
    assert len({item["system_prompt_digest"] for item in by_call.values()}) == 1
    assert {(item["matched_task_id"], item["trusted_worker_id"]) for item in by_call.values()} == {
        ("w1", "run-1-w1"),
        ("w2", "run-1-w2"),
        ("w3", "run-1-w3"),
    }


@pytest.mark.asyncio
async def test_a_missing_task_is_reported_as_partial_not_verified(tmp_path: Any) -> None:
    harness = await _run(PARALLEL, layout=PROMPT_LAYOUT_SHARED_PREFIX, tmp_path=tmp_path)
    records = harness["evidence"].records
    producer_calls = [record for record in records if record["trusted_task_id"] == "run-1-w1"]
    assert producer_calls

    linkage = _request_linkage(
        harness["context_evidence"]["tasks"],
        [record for record in records if record not in producer_calls],
        harness["workers"],
        responses_observed=len(records),
    )
    assert linkage["status"] == "REQUEST_CONTENT_PARTIAL"
    assert "TASK_NEVER_OBSERVED" in linkage["reason_codes"]
    assert linkage["missing_task_ids"] == ["w1"]


@pytest.mark.asyncio
async def test_an_unidentified_request_prevents_a_full_verified_label(tmp_path: Any) -> None:
    harness = await _run(SERIAL, layout=PROMPT_LAYOUT_SHARED_PREFIX, tmp_path=tmp_path)
    records = [dict(record) for record in harness["evidence"].records]
    for record in records:
        record["trusted_task_id"] = None
        record["trusted_worker_id"] = None
    # Content fallback still links the request to its render, but the identity is not trusted.
    linkage = _request_linkage(
        harness["context_evidence"]["tasks"],
        records,
        {"w1": SimpleNamespace()},
        responses_observed=len(records),
    )
    assert linkage["status"] == "REQUEST_CONTENT_PARTIAL"
    assert "IDENTITY_NOT_TRUSTED" in linkage["reason_codes"]
    assert linkage["requests_with_trusted_identity"] == 0
    assert all(item["identity_status"] == "NO_TRUSTED_IDENTITY" for item in linkage["requests"])


# -- the actual DeepSeek kwargs ---------------------------------------------------------------


class _StubCompletions:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return SimpleNamespace(
            id="stub",
            model="deepseek-chat",
            usage={"prompt_tokens": 10, "completion_tokens": 2},
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=None),
                    finish_reason="stop",
                )
            ],
        )


@pytest.mark.asyncio
async def test_the_serialised_request_carries_the_new_layout(tmp_path: Any) -> None:
    harness = await _run(SERIAL, layout=PROMPT_LAYOUT_SHARED_PREFIX, tmp_path=tmp_path)
    requests = harness["captured"][f"{harness['provider_prefix']}-w2"]
    request = requests[-1]

    completions = _StubCompletions()
    adapter = DeepSeekAdapter(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="deepseek-chat",
    )
    options = SimpleNamespace(
        model="deepseek-chat",
        system_prompt=request["system_prompt"],
        tools=None,
        max_tokens=None,
        temperature=None,
        reasoning_effort=None,
    )
    await adapter.chat(cast("Any", request["messages"]), cast("Any", options))

    assert completions.kwargs is not None
    messages = completions.kwargs["messages"]
    assert messages[0] == {"role": "system", "content": request["system_prompt"]}
    assert "ASSIGNED TASK" not in messages[0]["content"]
    assert messages[1]["role"] == "user"
    body = messages[1]["content"]
    assert body.startswith(ENVIRONMENT_NOTE)
    assert "RESULT-OF-w1" in body


# -- evidence strictness ----------------------------------------------------------------------


def test_a_dependency_that_matches_only_in_its_head_is_not_verified() -> None:
    from atlasos.context import ContextBlock, ContextKind

    store = InMemoryContextStore()
    evidence = _RequestEvidence()
    evidence.bind_store(store)
    head = "LINE-ONE-IDENTICAL\n" * 3
    store.put(
        ContextBlock(
            block_id="task-output:w1",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            version=1,
            content=head + "REAL-TAIL\n" * 20,
            task_id="w1",
            owner_agent_id="w1",
        )
    )
    truncated = head + "DIFFERENT-TAIL\n" * 20
    evidence.observe(
        [SimpleNamespace(role="user", content=[SimpleNamespace(text=truncated)])],
        SimpleNamespace(system_prompt="S", tools=()),
    )
    probe = evidence.records[0]["dependency_probes"]["task-output:w1"]
    assert probe["content_present"] is False
    assert probe["present"] is False

    linkage = _request_linkage(
        [
            {
                "task_id": "w2",
                "task_prompt_sha256": hashlib.sha256(truncated.encode()).hexdigest(),
                "declared_dependencies": ["w1"],
            }
        ],
        evidence.records,
        {},
    )
    assert linkage["status"] == "REQUEST_CONTENT_PARTIAL"
    assert "DEPENDENCY_NOT_FULLY_DELIVERED" in linkage["reason_codes"]


def test_the_structured_block_is_the_probe_unit() -> None:
    from atlasos.context import ContextBlock, ContextKind

    store = InMemoryContextStore()
    evidence = _RequestEvidence()
    evidence.bind_store(store)
    block = ContextBlock(
        block_id="task-output:w1",
        kind=ContextKind.DEPENDENCY_OUTPUT,
        version=1,
        content="RESULT",
        task_id="w1",
    )
    store.put(block)
    # Raw content without the delimiters is not the same evidence as the rendered block.
    evidence.observe(
        [SimpleNamespace(role="user", content=[SimpleNamespace(text="RESULT")])],
        SimpleNamespace(system_prompt="S", tools=()),
    )
    probe = evidence.records[0]["dependency_probes"]["task-output:w1"]
    assert probe["content_present"] is True
    assert probe["present"] is False

    store2 = InMemoryContextStore()
    evidence2 = _RequestEvidence()
    evidence2.bind_store(store2)
    store2.put(
        ContextBlock(
            block_id="task-output:w1",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            version=1,
            content="RESULT",
            task_id="w1",
        )
    )
    rendered = render_dependency_block(task_id="w1", version=1, content="RESULT")
    evidence2.observe(
        [SimpleNamespace(role="user", content=[SimpleNamespace(text=rendered)])],
        SimpleNamespace(system_prompt="S", tools=()),
    )
    assert evidence2.records[0]["dependency_probes"]["task-output:w1"]["present"] is True


# -- configuration guards ---------------------------------------------------------------------


def test_a_layout_claim_the_yaml_does_not_resolve_is_refused() -> None:
    with pytest.raises(EvaluationEnvironmentError, match="prompt_layout"):
        _enforce_prompt_layout(
            PROMPT_LAYOUT_SHARED_PREFIX,
            config=_config(PROMPT_LAYOUT_LEGACY),
            config_file=runner_entry.CONTEXT_CONFIG,
        )
    with pytest.raises(EvaluationEnvironmentError, match="prompt_layout"):
        _enforce_prompt_layout(
            PROMPT_LAYOUT_LEGACY,
            config=_config(PROMPT_LAYOUT_SHARED_PREFIX),
            config_file=runner_entry.CONTEXT_CONFIG,
        )
    assert (
        _enforce_prompt_layout(
            PROMPT_LAYOUT_SHARED_PREFIX,
            config=_config(PROMPT_LAYOUT_SHARED_PREFIX),
            config_file=runner_entry.CONTEXT_CONFIG,
        )
        == PROMPT_LAYOUT_SHARED_PREFIX
    )


def test_the_shipped_context_config_is_legacy() -> None:
    from atlasos.config import load_config

    config = load_config(runner_entry.CONTEXT_CONFIG)
    assert config.context.prompt_layout == "legacy"
    assert RunnerSettings().prompt_layout == "legacy"


def test_an_unknown_layout_name_is_refused_not_defaulted() -> None:
    with pytest.raises(EvaluationEnvironmentError, match="unknown prompt_layout"):
        _enforce_prompt_layout(
            "compact",
            config=_config(PROMPT_LAYOUT_LEGACY),
            config_file=runner_entry.CONTEXT_CONFIG,
        )


def test_shared_prefix_is_refused_on_an_unvalidated_transform() -> None:
    with pytest.raises(ValueError, match="identity"):
        ContextConfig(
            policy="task_scoped",
            renderer="stable_deterministic",
            prompt_layout="shared_prefix",
            transform=ContextTransformConfig(name="contextpilot_dedup"),
        )


# -- counterexamples: what must never be reported as verified -----------------------------------


def _linkage(
    harness: dict[str, Any],
    records: list[dict[str, Any]],
    workers: Any = None,
    *,
    responses_observed: int | None = None,
):
    return _request_linkage(
        harness["context_evidence"]["tasks"],
        records,
        harness["workers"] if workers is None else workers,
        responses_observed=(len(records) if responses_observed is None else responses_observed),
    )


def _item(linkage: dict[str, Any], call_index: int) -> dict[str, Any]:
    return next(item for item in linkage["requests"] if item["call_index"] == call_index)


@pytest.mark.asyncio
async def test_a_modified_user_prompt_is_rejected_with_correct_task_identity(
    tmp_path: Any,
) -> None:
    harness = await _run(SERIAL, layout=PROMPT_LAYOUT_SHARED_PREFIX, tmp_path=tmp_path)
    records = [dict(record) for record in harness["evidence"].records]
    target = next(item for item in records if item["trusted_task_id"] == "run-1-w2")
    target["first_message_digest"] = hashlib.sha256(b"A DIFFERENT USER TURN").hexdigest()

    linkage = _linkage(harness, records)
    assert linkage["status"] == "REQUEST_CONTENT_PARTIAL"
    assert "REQUEST_CONTENT_MISMATCH" in linkage["reason_codes"]
    assert linkage["task_prompt_mismatch_requests"] == [target["call_index"]]
    # The identity was fine; only the content failed, which is exactly why both are checked.
    assert all(item["identity_status"] == "TRUSTED" for item in linkage["requests"])
    assert _item(linkage, target["call_index"])["content_status"] == "TASK_PROMPT_MISMATCH"


@pytest.mark.asyncio
async def test_a_modified_system_prompt_is_rejected(tmp_path: Any) -> None:
    harness = await _run(SERIAL, layout=PROMPT_LAYOUT_SHARED_PREFIX, tmp_path=tmp_path)
    records = [dict(record) for record in harness["evidence"].records]
    target = next(item for item in records if item["trusted_task_id"] == "run-1-w1")
    target["system_prompt_digest"] = hashlib.sha256(b"OTHER INSTRUCTIONS").hexdigest()

    linkage = _linkage(harness, records)
    assert linkage["status"] == "REQUEST_CONTENT_PARTIAL"
    assert "SYSTEM_PROMPT_MISMATCH" in linkage["reason_codes"]
    assert linkage["system_prompt_mismatch_requests"] == [target["call_index"]]
    assert _item(linkage, target["call_index"])["content_status"] == "SYSTEM_PROMPT_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "key"),
    [
        ("worker_id", "trusted_worker_id"),
        ("profile_id", "trusted_profile_id"),
        ("run_id", "trusted_run_id"),
    ],
)
async def test_any_identity_field_mismatch_drops_the_trusted_identity(
    tmp_path: Any, field: str, key: str
) -> None:
    harness = await _run(SERIAL, layout=PROMPT_LAYOUT_SHARED_PREFIX, tmp_path=tmp_path)
    records = [dict(record) for record in harness["evidence"].records]
    target = next(item for item in records if item["trusted_task_id"] == "run-1-w1")
    target[key] = f"not-the-{field}"

    linkage = _linkage(harness, records)
    assert linkage["status"] == "REQUEST_CONTENT_PARTIAL"
    assert "IDENTITY_NOT_TRUSTED" in linkage["reason_codes"]
    assert "IDENTITY_MISMATCH" in linkage["reason_codes"]
    item = _item(linkage, target["call_index"])
    assert item["identity_status"] == "IDENTITY_MISMATCH"
    assert item["identity_mismatches"] == [field]
    # The digest may still say which render this probably was, but that is diagnosis, not identity.
    assert item["identity_source"] != "trusted_execution_context"
    assert item["matched_worker_task_id"] is None


@pytest.mark.asyncio
async def test_a_request_without_a_host_worker_record_is_not_trusted(tmp_path: Any) -> None:
    harness = await _run(SERIAL, layout=PROMPT_LAYOUT_SHARED_PREFIX, tmp_path=tmp_path)
    records = [dict(record) for record in harness["evidence"].records]

    linkage = _linkage(harness, records, workers={})
    assert linkage["status"] == "REQUEST_CONTENT_PARTIAL"
    assert "IDENTITY_NOT_TRUSTED" in linkage["reason_codes"]
    assert all(item["identity_status"] == "NO_HOST_WORKER_RECORD" for item in linkage["requests"])
    assert linkage["requests_with_trusted_identity"] == 0


@pytest.mark.asyncio
async def test_an_extra_request_of_one_task_leaves_the_set_unreconciled(tmp_path: Any) -> None:
    harness = await _run(SERIAL, layout=PROMPT_LAYOUT_SHARED_PREFIX, tmp_path=tmp_path)
    records = [dict(record) for record in harness["evidence"].records]

    # A second turn of the same task: same initial prompt, next request index.
    second_turn = dict(records[-1])
    second_turn["call_index"] = len(records)
    second_turn["request_index"] = 1
    with_second_turn = [*records, second_turn]

    control = _linkage(harness, with_second_turn)
    assert control["status"] == "REQUEST_CONTENT_VERIFIED"  # every request has an answer
    assert control["coverage"]["status"] == "RECONCILED"

    # Drop one of that task's two requests. The task is still observed, so task coverage alone
    # would still look complete; the request set is what proves it is not.
    dropped = [record for record in with_second_turn if record is not second_turn]
    linkage = _linkage(harness, dropped, responses_observed=len(with_second_turn))
    assert linkage["missing_task_ids"] == []
    assert linkage["coverage"]["status"] == "UNRECONCILED"
    assert "REQUEST_COVERAGE_UNVERIFIED" in linkage["reason_codes"]
    assert linkage["status"] == "REQUEST_CONTENT_PARTIAL"


@pytest.mark.asyncio
async def test_a_consumer_request_without_the_probe_is_not_skipped(tmp_path: Any) -> None:
    harness = await _run(SERIAL, layout=PROMPT_LAYOUT_SHARED_PREFIX, tmp_path=tmp_path)
    records = [dict(record) for record in harness["evidence"].records]
    consumer = next(item for item in records if item["trusted_task_id"] == "run-1-w2")
    consumer["dependency_probes"] = {}

    linkage = _linkage(harness, records)
    assert "DEPENDENCY_NOT_FULLY_DELIVERED" in linkage["reason_codes"]
    assert [failure["reason"] for failure in linkage["dependency_failures"]] == [
        "NO_PROBE_ON_CONSUMER_REQUEST"
    ]


def test_an_empty_dependency_body_is_still_a_structured_block() -> None:
    from atlasos.context import ContextBlock, ContextKind

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
    rendered = render_dependency_block(task_id="w1", version=1, content="")
    index = evidence.observe(
        [SimpleNamespace(role="user", content=[SimpleNamespace(text=rendered)])],
        SimpleNamespace(system_prompt="S", tools=()),
    )
    evidence.mark_completed(index)
    probe = evidence.records[0]["dependency_probes"]["task-output:w1"]
    assert probe["present"] is True
    assert probe["count"] == 1
    assert probe["content_empty"] is True
    # No body to match, so the content probe reports "nothing to compare" instead of a bogus True.
    assert probe["content_present"] is None


def test_a_dependency_quoted_in_the_history_is_not_a_second_injection() -> None:
    from atlasos.context import ContextBlock, ContextKind

    store = InMemoryContextStore()
    evidence = _RequestEvidence()
    evidence.bind_store(store)
    store.put(
        ContextBlock(
            block_id="task-output:w1",
            kind=ContextKind.DEPENDENCY_OUTPUT,
            version=1,
            content="RESULT-OF-w1",
            task_id="w1",
        )
    )
    block = render_dependency_block(task_id="w1", version=1, content="RESULT-OF-w1")
    index = evidence.observe(
        [
            SimpleNamespace(role="user", content=[SimpleNamespace(text=block)]),
            SimpleNamespace(role="assistant", content=[SimpleNamespace(text=f"quoting:\n{block}")]),
        ],
        SimpleNamespace(system_prompt="S", tools=()),
    )
    evidence.mark_completed(index)
    probe = evidence.records[0]["dependency_probes"]["task-output:w1"]
    assert probe["count"] == 1
    assert probe["later_history_mentions"] == 1
    assert probe["request_count"] == 2


@pytest.mark.asyncio
async def test_the_adapter_stream_path_is_observed_once_with_identity_and_usage() -> None:
    from anycode.types import LLMChatOptions

    from atlasos.actions.catalog import ActionCatalog
    from atlasos.actions.policy import ActionPolicy
    from atlasos.tools.runtime import AtlasToolRuntime

    runtime = AtlasToolRuntime(
        "c1a-adapter",
        action_policy=ActionPolicy(name="c1a-adapter", default="DENY", rules=()),
        action_catalog=ActionCatalog(),
    )
    tools = tuple(runtime.registry.to_tool_defs())[:2]
    completions = _StubCompletions()
    evidence = _RequestEvidence()
    models: list[str] = []
    adapter = ModelRecordingAdapter(
        DeepSeekAdapter(
            client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            model="deepseek-chat",
        ),
        models,
        evidence,
    )
    options = LLMChatOptions(
        model="deepseek-chat",
        system_prompt="EFFECTIVE-SYSTEM",
        tools=list(tools),
        execution_context=ExecutionContext(
            principal="user:evaluation",
            tenant_scope="atlas-evaluation",
            classification="internal",
            attributes={
                "task_id": "run-w1",
                "worker_id": "run-w1",
                "profile_id": "coding_worker",
                "run_id": "run",
            },
        ),
    )
    messages = [LLMMessage(role="user", content=[TextBlock(text="TASK TURN")])]

    stream = adapter.stream(messages, options)
    assert evidence.records == []  # not consumed yet

    async for _ in stream:
        pass

    assert completions.kwargs is not None
    kwargs = completions.kwargs
    assert kwargs["messages"][0] == {"role": "system", "content": "EFFECTIVE-SYSTEM"}
    assert kwargs["messages"][1]["role"] == "user"
    mapped = kwargs["tools"]
    assert len(mapped) == len(tools)
    names = [tool.name for tool in tools]
    assert all(name in str(entry) for name, entry in zip(names, mapped, strict=True))
    assert len(evidence.records) == 1  # stream → inner chat, counted once
    record = evidence.records[0]
    assert record["trusted_task_id"] == "run-w1"
    assert record["trusted_run_id"] == "run"
    assert record["status"] == "COMPLETED"
    assert record["usage"] is not None
    assert record["usage"]["prompt_tokens"] == 10
    assert len(models) == 1
