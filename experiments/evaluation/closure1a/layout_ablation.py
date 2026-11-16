# ruff: noqa: E501

"""Closure-1A: deterministic A/B of the effective request layout. No model is called.

What this measures
------------------
Two layouts of the *same* run, on the *same* public task facts:

* ``P0 legacy`` — the shipped layout: the worker system prompt carries the environment note plus the
  worker's own objective inside an untrusted JSON block, and the task body starts with
  ``Task: <title>``.
* ``P1 shared_prefix`` — a static trusted system prompt per profile, the shared public block placed
  before the task title, and the objective only in the user turn.

Every number is taken from the API ``kwargs`` that ``DeepSeekAdapter.chat`` would send, captured
through the real adapter with a stub transport. Selection, the DAG, the task facts, the dependency
outputs and the tool definitions are identical in both arms; only the layout differs.

What this does **not** measure
------------------------------
Provider cache hit/miss, cache-miss prefill, latency or monetary cost. Nothing here is a benchmark
of model behaviour, and a longer or shorter common prefix is *not* a KV-cache claim.

    python -m experiments.evaluation.closure1a.layout_ablation
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from anycode import OrchestratorConfig, TaskSpec
from anycode.identity.context import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.providers.fake import FakeAdapter, FakeResponse
from anycode.types import LLMChatOptions, LLMMessage, TeamConfig
from experiments.evaluation.final4 import runner_entry
from experiments.evaluation.final4.validation_tasks import TASKS as VALIDATION_TASKS
from experiments.evaluation.final4.validation_tasks import Task
from experiments.evaluation.runner import (
    PROMPT_LAYOUT_LEGACY,
    PROMPT_LAYOUT_SHARED_PREFIX,
    ModelRecordingAdapter,
    _context_evidence,
    _context_prompt_builder,
    _environment_note,
    _RequestEvidence,
    _task_turn_parts,
)

from atlasos.actions.catalog import ActionCatalog
from atlasos.actions.policy import ActionPolicy
from atlasos.config import AtlasConfig, ContextConfig
from atlasos.context.renderer import render_dependency_block
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.tools.runtime import AtlasToolRuntime
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerStore

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT_PATH = REPO_ROOT / "experiments" / "results" / "closure1a" / "layout_ablation.json"

TOKENIZER_NAME = "tiktoken cl100k_base"
TOKENIZER_VERSION = "0.14.0"
ENVIRONMENT_NOTE = _environment_note("/workspace/project")

#: A: two independent workers of one profile, one shared public block (no dependency).
#: B: a serial pair, so a real dependency output must travel.
#: C: two independent workers plus an integration task that consumes both.
WORKLOADS: dict[str, dict[str, Any]] = {
    "A": {"task": "validation-parallel", "workers": ["w1", "w2"], "kind": "same_profile_pair"},
    "B": {"task": "validation-serial", "workers": ["w1", "w2"], "kind": "serial_dependency"},
    "C": {"task": "validation-parallel", "workers": ["w1", "w2", "w3"], "kind": "join"},
}

_ARM_SEQ = [0]
CAPTURED: dict[str, list[dict[str, Any]]] = {}


# -- provider -----------------------------------------------------------------------------------


class _RecordingFake(FakeAdapter):
    """The provider. It answers, and records what it was asked; evidence is the wrapper's job."""

    def __init__(self, reply: str, sink: list[dict[str, Any]]) -> None:
        super().__init__(responses=[FakeResponse(text=reply)])
        self._sink = sink

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions):  # type: ignore[override]
        self._sink.append(
            {
                "messages": list(messages),
                "system_prompt": options.system_prompt or "",
                "options": options,
            }
        )
        return await super().chat(messages, options)


def _register(provider: str, reply: str, evidence: _RequestEvidence, models: list[str]) -> None:
    async def factory(**_: Any) -> Any:
        # The real wrapper: it observes at issue time, marks completion and attaches usage, so the
        # arm is measured on the same evidence path a live run would use.
        inner = _RecordingFake(reply, CAPTURED.setdefault(provider, []))
        return ModelRecordingAdapter(inner, models, evidence)

    register_provider_factory(provider, factory)


# -- one arm ------------------------------------------------------------------------------------


def _config(layout: str) -> AtlasConfig:
    return AtlasConfig(
        context=ContextConfig(
            policy="task_scoped", renderer="stable_deterministic", prompt_layout=layout
        )
    )


def _tool_runtime(run_dir: Path) -> AtlasToolRuntime:
    return AtlasToolRuntime(
        "closure1a",
        state_path=run_dir / "tool_state.json",
        conversation_path=run_dir / "tool_conversations.json",
        action_policy=ActionPolicy(name="closure1a", default="DENY", rules=()),
        action_catalog=ActionCatalog(),
    )


async def run_arm(workload: str, layout: str, *, workdir: Path) -> dict[str, Any]:
    spec = WORKLOADS[workload]
    task = next(t for t in VALIDATION_TASKS if t.task_id == spec["task"])
    entry = runner_entry.build_task(task, multi=True)
    arm_dir = workdir / workload / layout
    arm_dir.mkdir(parents=True, exist_ok=True)

    factory = ScopedWorkerFactory(
        model="fake",
        provider="fake",
        store=WorkerStore(arm_dir / "workers.json"),
    )
    parent = ExecutionContext(
        principal="user:evaluation",
        tenant_scope="atlas-evaluation",
        classification="internal",
        attributes={"run_id": f"closure1a-{workload}-{layout}", "sample_id": entry.sample_id},
    )
    workers: dict[str, Any] = {}
    for step in entry.workers:
        assignment = (
            step.prompt
            if layout == PROMPT_LAYOUT_SHARED_PREFIX
            else f"{ENVIRONMENT_NOTE}\n\n{step.prompt}"
        )
        workers[step.task_id] = factory.create(
            parent_context=parent,
            parent_agent="atlas-evaluation",
            assignment=WorkerAssignment(profile_id="coding_worker", task=assignment),
            worker_id=f"run-{step.task_id}",
            task_id=f"run-{step.task_id}",
            prompt_layout=layout,
        )

    evidence = _RequestEvidence()
    models: list[str] = []
    _ARM_SEQ[0] += 1
    prefix = f"c1a-{_ARM_SEQ[0]}"
    for step in entry.workers:
        provider = f"{prefix}-{step.task_id}"
        _register(provider, f"RESULT-OF-{step.task_id}", evidence, models)
        workers[step.task_id] = workers[step.task_id].model_copy(
            update={
                "agent_config": workers[step.task_id].agent_config.model_copy(
                    update={"provider": provider}
                )
            }
        )

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
    if plane is None:  # pragma: no cover - the config above always selects the plane
        raise SystemExit("the context plane was not constructed")
    builder, store = plane
    evidence.bind_store(store)

    tool_runtime = _tool_runtime(arm_dir)
    engine = AtlasContextEngine(
        OrchestratorConfig(),
        prompt_builder=builder,
        tool_runtime=tool_runtime,
    )
    team = engine.create_team(
        "closure1a",
        TeamConfig(
            name="closure1a", agents=[workers[s.task_id].agent_config for s in entry.workers]
        ),
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
    if not result.success:  # pragma: no cover - deterministic fakes always succeed
        raise SystemExit(f"arm {workload}/{layout} did not complete")

    requests: dict[str, dict[str, Any]] = {}
    responses_observed = 0
    for step in entry.workers:
        captured = CAPTURED[f"{prefix}-{step.task_id}"]
        requests[step.task_id] = captured[0]
        # The fake provider answers once per call, so this is an independent response-side count of
        # the same calls the request wrapper observed at issue time.
        responses_observed += len(captured)

    return {
        "workload": workload,
        "layout": layout,
        "entry": entry,
        "source": task,
        "workers": workers,
        "requests": requests,
        "tool_runtime_tools": list(tool_runtime.agent_tools),
        "evidence_requests": evidence.records,
        "context_evidence": _context_evidence(
            builder=builder,
            store=store,
            task=entry,
            task_texts=task_texts,
            observations=[],
            request_records=evidence.records,
            workers=workers,
            responses_observed=responses_observed,
        ),
        "profile_system_prompt": workers[
            entry.workers[0].task_id
        ].record.effective_system_prompt_hash,
    }


# -- serialisation of one request ---------------------------------------------------------------


class _StubCompletions:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return SimpleNamespace(
            id="stub",
            model="deepseek-chat",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="", tool_calls=None), finish_reason="stop"
                )
            ],
        )


async def _mapped_kwargs(request: dict[str, Any]) -> dict[str, Any]:
    """Run the real adapter over a captured request and return the kwargs it would send.

    The captured ``options`` object is passed through untouched, so the tool definitions measured
    here are the ones the runtime actually handed to the provider, not a rebuild from another
    registry that could silently differ.
    """

    completions = _StubCompletions()
    adapter = DeepSeekAdapter(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)), model="deepseek-chat"
    )
    await adapter.chat(request["messages"], request["options"])
    assert completions.kwargs is not None
    return completions.kwargs


def _message_texts(kwargs: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for message in kwargs["messages"]:
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.append(
                "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            )
        else:
            texts.append("")
    return texts


def _common_prefix(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    index = 0
    while index < limit and a[index] == b[index]:
        index += 1
    return index


def _assignment_of(source: Task, step_id: str) -> str:
    """The worker's own assignment text: what the legacy system prompt embeds as ``objective``."""

    worker = next(item for item in source.workers if str(item["task_id"]) == step_id)
    return str(worker["assignment"])


# -- measurement --------------------------------------------------------------------------------


async def measure(workload: str, layout: str, *, workdir: Path, encoder: Any) -> dict[str, Any]:
    arm = await run_arm(workload, layout, workdir=workdir)
    entry = arm["entry"]
    source: Task = arm["source"]
    # The DAG always runs whole; the *measurement scope* is the workload's declared workers. That
    # is what separates workload A (the independent pair) from workload C (the pair plus its join).
    scope = set(WORKLOADS[workload]["workers"])
    steps = [step for step in entry.workers if step.task_id in scope]

    per_worker: dict[str, Any] = {}
    rendered_prompts: dict[str, str] = {}
    for step in steps:
        request = arm["requests"][step.task_id]
        kwargs = await _mapped_kwargs(request)
        texts = _message_texts(kwargs)
        system_text = texts[0] if texts else ""
        initial_user = texts[1] if len(texts) > 1 else ""
        serialized = json.dumps(
            {"messages": kwargs["messages"], "tools": kwargs.get("tools")}, sort_keys=True
        )
        objective = _assignment_of(source, step.task_id)
        shared = source.shared_context
        dependencies = {
            str(dependency): {
                "structured_block_present": (
                    render_dependency_block(
                        task_id=str(dependency),
                        version=1,
                        content=f"RESULT-OF-{dependency}",
                    )
                    in initial_user
                ),
                "count": initial_user.count(
                    render_dependency_block(
                        task_id=str(dependency),
                        version=1,
                        content=f"RESULT-OF-{dependency}",
                    )
                ),
            }
            for dependency in step.depends_on
        }
        per_worker[step.task_id] = {
            "system_prompt_chars": len(system_text),
            "initial_user_turn_chars": len(initial_user),
            "tool_schema_chars": len(json.dumps(kwargs.get("tools") or [], sort_keys=True)),
            "request_chars": len(serialized),
            "estimated_request_tokens": len(encoder.encode(serialized)),
            "estimated_system_tokens": len(encoder.encode(system_text)),
            "estimated_user_tokens": len(encoder.encode(initial_user)),
            # The legacy system prompt embeds the assignment as a JSON string value, so the copy
            # there is the *escaped* form; the user turn carries it raw. Both are counted, and the
            # escaped one is the duplicate under test.
            "objective_occurrences": {
                "system": system_text.count(json.dumps(objective, ensure_ascii=True)[1:-1]),
                "system_raw": system_text.count(objective),
                "user": initial_user.count(objective),
            },
            "shared_public_block_chars": len(shared),
            "shared_public_block_retained_verbatim": shared in initial_user,
            "environment_note_in": [
                where
                for where, text in (("system", system_text), ("user", initial_user))
                if ENVIRONMENT_NOTE in text
            ],
            "declared_dependencies": list(step.depends_on),
            "dependencies": dependencies,
            "all_declared_dependencies_present_once": all(
                item["structured_block_present"] and item["count"] == 1
                for item in dependencies.values()
            ),
            "unrelated_dependency_leaked": any(
                f"RESULT-OF-{other}" in initial_user
                for other in (s.task_id for s in entry.workers)
                if other != step.task_id and other not in step.depends_on
            ),
            "system_prompt_is_shared_with_same_profile": None,
        }
        rendered_prompts[step.task_id] = system_text + "\n" + initial_user

    profile_hashes = {
        step.task_id: arm["workers"][step.task_id].record.effective_system_prompt_hash
        for step in steps
    }
    shared_system = len(set(profile_hashes.values())) == 1
    for step_id in per_worker:
        per_worker[step_id]["system_prompt_is_shared_with_same_profile"] = shared_system

    ids = [s.task_id for s in steps]
    pairs = [(a, b) for index, a in enumerate(ids) for b in ids[index + 1 :]]
    common = {
        f"{a}|{b}": _common_prefix(rendered_prompts[a], rendered_prompts[b]) for a, b in pairs
    }

    linkage = arm["context_evidence"]["request_linkage"]
    captured_options = arm["requests"][steps[0].task_id]["options"]
    return {
        "workload": workload,
        "layout": layout,
        "task_id": entry.sample_id,
        "measurement_scope": sorted(scope),
        "tool_count": len(getattr(captured_options, "tools", ()) or ()),
        "tool_definitions_source": "options.tools as captured at the adapter boundary",
        "system_prompt_identical_across_workers": shared_system,
        "system_prompt_hashes": profile_hashes,
        "common_prefix_chars": common,
        "common_prefix_min_chars": min(common.values()) if common else None,
        "totals": {
            "system_prompt_chars": sum(w["system_prompt_chars"] for w in per_worker.values()),
            "initial_user_turn_chars": sum(
                w["initial_user_turn_chars"] for w in per_worker.values()
            ),
            "tool_schema_chars": sum(w["tool_schema_chars"] for w in per_worker.values()),
            "request_chars": sum(w["request_chars"] for w in per_worker.values()),
            "estimated_request_tokens": sum(
                w["estimated_request_tokens"] for w in per_worker.values()
            ),
            "objective_occurrences_total": sum(
                w["objective_occurrences"]["system"] + w["objective_occurrences"]["user"]
                for w in per_worker.values()
            ),
            "objective_occurrences_raw_in_system_total": sum(
                w["objective_occurrences"]["system_raw"] for w in per_worker.values()
            ),
            "objective_occurrences_in_system_total": sum(
                w["objective_occurrences"]["system"] for w in per_worker.values()
            ),
        },
        "per_worker": per_worker,
        "request_linkage_status": linkage["status"],
        "request_linkage_reason_codes": linkage.get("reason_codes"),
        "request_coverage": linkage.get("coverage"),
        "dependency_failures": linkage.get("dependency_failures"),
    }


# -- report -------------------------------------------------------------------------------------


def _compare(arms: dict[str, dict[str, Any]]) -> dict[str, Any]:
    legacy = arms[PROMPT_LAYOUT_LEGACY]
    shared = arms[PROMPT_LAYOUT_SHARED_PREFIX]
    workers = list(legacy["per_worker"])
    retention = all(
        arms[layout]["per_worker"][worker]["shared_public_block_retained_verbatim"]
        for layout in arms
        for worker in workers
    )
    return {
        "duplicate_objective_in_system_removed": (
            legacy["totals"]["objective_occurrences_in_system_total"] > 0
            and shared["totals"]["objective_occurrences_in_system_total"] == 0
        ),
        "objective_still_present_in_request": all(
            arms[layout]["per_worker"][worker]["objective_occurrences"]["system"]
            + arms[layout]["per_worker"][worker]["objective_occurrences"]["user"]
            >= 1
            for layout in arms
            for worker in workers
        ),
        "system_prompt_identical_across_workers": {
            layout: arms[layout]["system_prompt_identical_across_workers"] for layout in arms
        },
        "common_prefix_min_chars": {
            layout: arms[layout]["common_prefix_min_chars"] for layout in arms
        },
        "common_prefix_delta_chars": (
            shared["common_prefix_min_chars"] - legacy["common_prefix_min_chars"]
            if shared["common_prefix_min_chars"] is not None
            and legacy["common_prefix_min_chars"] is not None
            else None
        ),
        "public_block_retained_verbatim_in_every_arm": retention,
        "dependency_delivered_once_in_every_arm": all(
            arms[layout]["request_linkage_status"] == "REQUEST_CONTENT_VERIFIED" for layout in arms
        ),
        "request_linkage": {
            layout: {
                "status": arms[layout]["request_linkage_status"],
                "reason_codes": arms[layout]["request_linkage_reason_codes"],
                "coverage": arms[layout]["request_coverage"],
            }
            for layout in arms
        },
        "system_prompt_chars": {
            layout: arms[layout]["totals"]["system_prompt_chars"] for layout in arms
        },
        "initial_user_turn_chars": {
            layout: arms[layout]["totals"]["initial_user_turn_chars"] for layout in arms
        },
        "request_chars": {layout: arms[layout]["totals"]["request_chars"] for layout in arms},
        "estimated_request_tokens": {
            layout: arms[layout]["totals"]["estimated_request_tokens"] for layout in arms
        },
        "objective_occurrences_in_system": {
            layout: arms[layout]["totals"]["objective_occurrences_in_system_total"]
            for layout in arms
        },
    }


async def _main() -> dict[str, Any]:
    import tiktoken

    encoder = tiktoken.get_encoding("cl100k_base")
    workdir = Path(tempfile.mkdtemp(prefix="closure1a-"))
    try:
        results: dict[str, Any] = {
            "stage": "closure-1a",
            "kind": "initial_request_layout_comparison",
            "scope_note": (
                "compares the FIRST request of each worker in one DAG run: system prompt, first "
                "user turn and the tool definitions handed to the adapter. It is not a "
                "multi-turn run measurement and it is not a provider cache measurement."
            ),
            "model_calls": 0,
            "tokenizer": {
                "name": TOKENIZER_NAME,
                "version": TOKENIZER_VERSION,
                "scope": (
                    "JSON serialisation of the mapped API messages plus tool definitions; "
                    "character-level estimate of input content, not provider-billed tokens"
                ),
                "includes_chat_template": False,
            },
            "common_prefix_definition": (
                "character-level longest common prefix of system_prompt + '\\n' + first user turn "
                "across two workers; a serialisation property, NOT a provider KV-cache prefix and "
                "NOT a whole-run token reduction"
            ),
            "usage_note": (
                "AnyCode TokenUsage is a normalized usage object. When the provider does not report "
                "the cache split, its cache-miss field is AnyCode's fallback, not an observed zero; "
                "no cache conclusion may be drawn from it here"
            ),
            "workloads": {},
            "claims_not_made": [
                "provider prompt-cache hit tokens",
                "prompt-cache miss / prefill reduction",
                "end-to-end latency",
                "monetary cost",
                "model task quality",
                "whole-run token reduction across all turns",
            ],
        }
        for workload in WORKLOADS:
            arms: dict[str, Any] = {}
            for layout in (PROMPT_LAYOUT_LEGACY, PROMPT_LAYOUT_SHARED_PREFIX):
                arms[layout] = await measure(workload, layout, workdir=workdir, encoder=encoder)
            results["workloads"][workload] = {
                "definition": WORKLOADS[workload],
                "arms": arms,
                "comparison": _compare(arms),
            }
        return results
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    results = asyncio.run(_main())
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({k: v["comparison"] for k, v in results["workloads"].items()}, indent=2))
    print(f"\nwritten: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
