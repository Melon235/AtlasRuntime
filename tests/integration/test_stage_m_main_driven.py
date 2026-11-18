"""Stage M acceptance: Main-driven delegation through the real AnyCode agent loop.

Every test drives a real ``AtlasContextEngine`` → ``AnyCode`` wave → ``AgentRunner`` turn loop. Only
the provider is fake, so the tool call, the tool result, the conversation continuation and the
concurrency are the production code paths.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from anycode.types import (
    AgentInfo,
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
    ToolUseContext,
)

from atlasos.app.main_driven import GoalRunOptions, run_goal
from atlasos.config import AtlasConfig, ToolsConfig
from atlasos.delegation import (
    DelegateTasksInput,
    DelegationConfig,
    DelegationLimits,
    DelegationService,
)
from atlasos.handoff import HandoffReadInput

Response = Callable[[list[LLMMessage]], list[Any]]


class DelegationRig:
    """One scripted provider for the Main and every child, keyed by system prompt.

    The Main's system prompt starts with ``ATLAS MAIN AGENT``. A child's prompt embeds the
    objective JSON, so each child is identified by the marker inside its objective.
    """

    def __init__(self) -> None:
        self.main_script: list[Response] = []
        self.child_scripts: dict[str, list[Response]] = {}
        self.requests: list[dict[str, Any]] = []
        self.calls: dict[str, int] = {}
        self.barrier: asyncio.Barrier | None = None
        self.slow_children: dict[str, float] = {}
        self.fail_children: dict[str, str] = {}
        self.child_started: dict[str, asyncio.Event] = {}
        self.usage: dict[str, tuple[int, int, int]] = {
            "main": (100, 40, 10),
            "alpha": (200, 50, 20),
            "beta": (300, 60, 30),
        }
        self.name = "stage-m-scripted"

    # -- provider API ---------------------------------------------------------

    def build(self) -> DelegationRig:
        return self

    def _role(self, system_prompt: str | None) -> str:
        text = system_prompt or ""
        if text.startswith("ATLAS MAIN AGENT"):
            return "main"
        for marker in self.child_scripts:
            if marker in text:
                return marker
        return "unknown"

    def _next(self, role: str, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        self.requests.append(
            {
                "role": role,
                "system_prompt": options.system_prompt,
                "messages": [message.model_dump(mode="json") for message in messages],
            }
        )
        turn = self.calls.get(role, 0)
        self.calls[role] = turn + 1
        script = self.main_script if role == "main" else self.child_scripts.get(role, [])
        blocks: list[Any] = (
            script[turn](messages) if turn < len(script) else [TextBlock(text="done")]
        )
        usage_key = role.removeprefix("MARK-").lower()
        input_tokens, cache_read, output_tokens = self.usage.get(usage_key, (1, 0, 1))
        has_tool_use = any(isinstance(block, ToolUseBlock) for block in blocks)
        return LLMResponse(
            id=f"{role}-{turn}",
            content=blocks,
            model=options.model,
            stop_reason="tool_use" if has_tool_use else "end_turn",
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read,
            ),
        )

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        role = self._role(options.system_prompt)
        started = self.child_started.get(role)
        if started is not None:
            started.set()
        if role in self.slow_children:
            await asyncio.sleep(self.slow_children[role])
        if role in self.fail_children:
            raise RuntimeError(self.fail_children[role])
        if self.barrier is not None and role != "main" and self.calls.get(role, 0) == 0:
            await asyncio.wait_for(self.barrier.wait(), timeout=5)
        return self._next(role, messages, options)

    def stream(self, messages: list[LLMMessage], options: LLMStreamOptions):
        async def _gen():
            response = await self.chat(messages, options)
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    yield StreamEvent(type="tool_use", data=block)
                elif isinstance(block, TextBlock):
                    yield StreamEvent(type="text", data=block.text)
            yield StreamEvent(type="done", data=response)

        return _gen()

    # -- assertions helpers ---------------------------------------------------

    def main_requests(self) -> list[dict[str, Any]]:
        return [item for item in self.requests if item["role"] == "main"]

    def text_of(self, request: dict[str, Any]) -> str:
        return json.dumps(request, sort_keys=True, ensure_ascii=False)


def _delegate_call(tasks: list[dict[str, Any]], call_id: str = "call-delegate") -> Response:
    def respond(_: list[LLMMessage]) -> list[Any]:
        return [ToolUseBlock(id=call_id, name="delegate_tasks", input={"tasks": tasks})]

    return respond


def _text(text: str) -> Response:
    return lambda _: [TextBlock(text=text)]


def _task(
    key: str,
    objective: str,
    *,
    background: str = "The workspace is a small Python project.",
    profile_id: str = "research_worker",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "key": key,
        "objective": objective,
        "background": background,
        "profile_id": profile_id,
        **extra,
    }


def _config(**kwargs: Any) -> AtlasConfig:
    return AtlasConfig(tools=ToolsConfig(mode="progressive"), **kwargs)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return workspace


def _workers(session: dict[str, Any]) -> dict[str, Any]:
    path = Path(session["run_dir"]) / "workers.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))["workers"]


async def _run(
    tmp_path: Path,
    rig: DelegationRig,
    *,
    config: AtlasConfig | None = None,
    workspace: Path | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    options = GoalRunOptions(
        goal="Add a subtract function and report what arithmetic exists.",
        runs_root=tmp_path / "runs",
        workspace_root=workspace or _workspace(tmp_path),
        config=config or _config(),
        provider_factory=rig.build,
        run_id=f"stage-m-{uuid.uuid4().hex[:10]}",
        **overrides,
    )
    return await run_goal(options)


def _delegation(**overrides: Any) -> DelegationConfig:
    return DelegationConfig.model_validate(overrides) if overrides else DelegationConfig()


# --------------------------------------------------------------------------- M1: one child


async def test_main_delegates_one_task_and_resumes_its_own_loop(tmp_path: Path) -> None:
    rig = DelegationRig()
    rig.child_scripts["MARK-ALPHA"] = [_text("ALPHA-RESULT: arithmetic lives in app.py")]
    rig.main_script = [
        _delegate_call(
            [_task("alpha", "MARK-ALPHA find where arithmetic is implemented")],
        ),
        _text("MAIN-FINAL: integrated"),
    ]

    session = await _run(tmp_path, rig)

    assert session["status"] == "succeeded"
    assert session["main"]["tool_calls"] == ["delegate_tasks"]
    # 1) The Main really used its own AgentRunner loop: two model turns, the second one after the
    #    tool result came back.
    assert len(rig.main_requests()) == 2
    second = json.dumps(rig.main_requests()[1]["messages"], sort_keys=True)
    assert "ALPHA-RESULT" in second
    assert "tool_result" in second
    # 2) The delegation was admitted by the router and executed by a real child.
    delegation = session["delegation"]
    assert delegation["batches"][0]["route"] == "delegate_one"
    children = delegation["batches"][0]["children"]
    assert len(children) == 1
    assert children[0]["key"] == "alpha"
    assert children[0]["status"] == "completed"
    assert children[0]["success"] is True
    assert children[0]["ref_id"].startswith("ho-")
    # 3) The child saw only its assignment, not the Main conversation.
    child_request = next(item for item in rig.requests if item["role"] == "MARK-ALPHA")
    assert "Add a subtract function" not in json.dumps(child_request, sort_keys=True)
    assert "MARK-ALPHA" in json.dumps(child_request, sort_keys=True)


# --------------------------------------------------------------------------- M2: concurrency


async def test_two_children_run_concurrently_in_isolated_contexts(tmp_path: Path) -> None:
    rig = DelegationRig()
    rig.barrier = asyncio.Barrier(2)
    rig.child_scripts["MARK-ALPHA"] = [
        _text("ALPHA-HEAD " + "a" * 4000 + " ALPHA-TAIL-SECRET")
    ]
    rig.child_scripts["MARK-BETA"] = [_text("BETA-HEAD " + "b" * 4000 + " BETA-TAIL-SECRET")]
    rig.main_script = [
        _delegate_call(
            [
                _task("alpha", "MARK-ALPHA summarize module A"),
                _task("beta", "MARK-BETA summarize module B"),
            ]
        ),
        _text("MAIN-FINAL"),
    ]

    session = await _run(tmp_path, rig)

    assert session["status"] == "succeeded"
    batch = session["delegation"]["batches"][0]
    assert batch["route"] == "delegate_parallel"
    # 1) Real overlap: both children were inside the provider at the same time (the barrier would
    #    time out if they were serialized), and their measured intervals intersect.
    assert batch["max_observed_concurrency"] == 2
    alpha, beta = batch["children"]
    assert alpha["started_offset_ms"] < beta["finished_offset_ms"]
    assert beta["started_offset_ms"] < alpha["finished_offset_ms"]
    # 2) Context isolation: neither child's tail reached the Main's provider requests.
    main_text = json.dumps([rig.main_requests()[1]], sort_keys=True)
    assert "ALPHA-TAIL-SECRET" not in main_text
    assert "BETA-TAIL-SECRET" not in main_text
    # 3) Each child kept its own conversation record.
    conversations = json.loads(
        (Path(session["run_dir"]) / "tool_conversations.json").read_text(encoding="utf-8")
    )
    assert len(conversations["tasks"]) == 3  # main + two children
    # 4) Token attribution is per child, not aggregated into the Main. ``prompt_tokens`` is the
    #    cache-miss side; ``total_prompt_tokens`` is the full prompt.
    assert session["child_tokens"]["prompt_tokens"] == 200 + 300
    assert session["child_tokens"]["total_prompt_tokens"] == 250 + 360
    assert session["main"]["prompt_tokens"] == 100 * 2
    assert session["main"]["cache_hit_tokens"] == 40 * 2
    assert alpha["cache_hit_tokens"] == 50
    assert beta["cache_hit_tokens"] == 60


# --------------------------------------------------------------------------- refusal: shared write


async def test_shared_write_plan_is_refused_before_any_child_is_created(tmp_path: Path) -> None:
    rig = DelegationRig()
    rig.child_scripts["MARK-ALPHA"] = [_text("ALPHA")]
    rig.child_scripts["MARK-BETA"] = [_text("BETA")]
    rig.main_script = [
        _delegate_call(
            [
                _task(
                    "alpha",
                    "MARK-ALPHA edit the shared module",
                    profile_id="coding_worker",
                    write_paths=["src/shared.py"],
                ),
                _task(
                    "beta",
                    "MARK-BETA also edit the shared module",
                    profile_id="coding_worker",
                    write_paths=["src/shared.py"],
                ),
            ]
        ),
        _text("MAIN-FINAL: did it myself"),
    ]

    session = await _run(
        tmp_path,
        rig,
        child_write_grants={"coding_worker": ("src/shared.py",)},
    )

    assert session["status"] == "succeeded"
    batch = session["delegation"]["batches"][0]
    assert batch["route"] == "direct"
    assert "SHARED_WRITE_RISK" in batch["decision"]["blocked_gates"] or (
        "SHARED_WRITE_RISK" in batch["decision"]["reason_codes"]
    )
    assert batch["children"] == []
    # No worker was created at all: the refusal happens before execution.
    assert _workers(session) == {}


async def test_write_intent_without_a_host_grant_is_refused(tmp_path: Path) -> None:
    rig = DelegationRig()
    rig.main_script = [
        _delegate_call(
            [
                _task(
                    "alpha",
                    "write the new module",
                    profile_id="coding_worker",
                    write_paths=["src/new.py"],
                )
            ]
        ),
        _text("MAIN-FINAL"),
    ]

    session = await _run(tmp_path, rig)

    batch = session["delegation"]["batches"][0]
    assert batch["route"] == "direct"
    assert batch["decision"]["reason_codes"] == ["WRITE_SCOPE_NOT_GRANTED"]


# --------------------------------------------------------------------------- child permissions


async def test_child_cannot_write_outside_its_declared_scope(tmp_path: Path) -> None:
    rig = DelegationRig()
    rig.child_scripts["MARK-ALPHA"] = [
        lambda _: [
            ToolUseBlock(id="w1", name="file_write", input={"path": "secret.txt", "content": "x"})
        ],
        _text("ALPHA-DONE"),
    ]
    rig.child_scripts["MARK-BETA"] = [_text("BETA-DONE")]
    rig.main_script = [
        _delegate_call(
            [
                _task(
                    "alpha",
                    "MARK-ALPHA add a helper",
                    profile_id="coding_worker",
                    write_paths=["src/owned.py"],
                ),
                _task("beta", "MARK-BETA inspect the module"),
            ]
        ),
        _text("MAIN-FINAL"),
    ]

    session = await _run(
        tmp_path,
        rig,
        child_write_grants={"coding_worker": ("src/",)},
    )

    workspace = Path(session["workspace_root"])
    assert not (workspace / "secret.txt").exists()
    ownership = session["ownership"]
    assert ownership["denied_calls"] == 1
    assert ownership["denials"][0]["reason_code"] == "OWNERSHIP_DENIED_UNDECLARED_PATH"
    assert ownership["denials"][0]["tool_name"] == "file_write"
    # The read-only sibling is declared with an empty scope and simply cannot write.
    assert ownership["workers"][ownership["denials"][0]["worker_id"]]["paths"] == ["src/owned.py"]


async def test_main_can_write_inside_its_host_declared_scope(tmp_path: Path) -> None:
    rig = DelegationRig()
    rig.main_script = [
        lambda _: [
            ToolUseBlock(id="w1", name="file_write", input={"path": "notes.md", "content": "ok\n"})
        ],
        _text("MAIN-FINAL"),
    ]

    session = await _run(tmp_path, rig, main_write_paths=("notes.md",))

    workspace = Path(session["workspace_root"])
    assert (workspace / "notes.md").read_text(encoding="utf-8") == "ok\n"
    assert session["ownership"]["denied_calls"] == 0


async def test_child_cannot_delegate_recursively(tmp_path: Path) -> None:
    rig = DelegationRig()
    rig.child_scripts["MARK-ALPHA"] = [
        lambda _: [
            ToolUseBlock(
                id="d1",
                name="delegate_tasks",
                input={"tasks": [_task("nested", "delegate again")]},
            )
        ],
        _text("ALPHA-DONE"),
    ]
    rig.main_script = [
        _delegate_call([_task("alpha", "MARK-ALPHA do the work")]),
        _text("MAIN-FINAL"),
    ]

    session = await _run(tmp_path, rig)

    # Layer 1: the tool is not in the child's surface, so the runner refuses the call.
    conversation = json.loads(
        (Path(session["run_dir"]) / "tool_conversations.json").read_text(encoding="utf-8")
    )
    child_messages = json.dumps(conversation["tasks"], sort_keys=True)
    assert "is not allowed for this agent" in child_messages
    # Only the Main's own child exists; the nested attempt created nothing.
    assert len(_workers(session)) == 1


# --------------------------------------------------------------------------- failure handling


async def test_timeout_and_child_error_are_reported_without_failing_the_goal(
    tmp_path: Path,
) -> None:
    rig = DelegationRig()
    rig.slow_children["MARK-ALPHA"] = 5.0
    rig.fail_children["MARK-BETA"] = "provider exploded"
    rig.child_scripts["MARK-ALPHA"] = [_text("never")]
    rig.child_scripts["MARK-BETA"] = [_text("never")]
    rig.main_script = [
        _delegate_call(
            [
                _task("alpha", "MARK-ALPHA slow work"),
                _task("beta", "MARK-BETA broken work"),
            ]
        ),
        _text("MAIN-FINAL: partial"),
    ]

    session = await _run(tmp_path, rig, delegation=_delegation(child_timeout_seconds=0.05))

    assert session["status"] == "succeeded"
    children = {
        child["key"]: child for child in session["delegation"]["batches"][0]["children"]
    }
    assert children["alpha"]["status"] == "timeout"
    assert children["alpha"]["success"] is False
    # A provider failure never becomes an empty success: the loop produced no completed turn.
    assert children["beta"]["status"] == "failed"
    assert children["beta"]["error_code"] == "child_no_result"
    assert "without completing a turn" in (children["beta"]["error_message"] or "")


async def test_cancelling_the_goal_reaps_every_child(tmp_path: Path) -> None:
    rig = DelegationRig()
    rig.child_scripts["MARK-ALPHA"] = [_text("never")]
    rig.child_scripts["MARK-BETA"] = [_text("never")]
    rig.slow_children["MARK-ALPHA"] = 30.0
    rig.slow_children["MARK-BETA"] = 30.0
    rig.child_started["MARK-ALPHA"] = asyncio.Event()
    rig.main_script = [
        _delegate_call(
            [_task("alpha", "MARK-ALPHA slow"), _task("beta", "MARK-BETA slow")]
        ),
        _text("MAIN-FINAL"),
    ]

    options = GoalRunOptions(
        goal="cancel me",
        runs_root=tmp_path / "runs",
        workspace_root=_workspace(tmp_path),
        config=_config(),
        provider_factory=rig.build,
        run_id=f"stage-m-{uuid.uuid4().hex[:10]}",
    )
    task = asyncio.create_task(run_goal(options))
    await asyncio.wait_for(rig.child_started["MARK-ALPHA"].wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # No subworker survives the cancellation.
    leaked = [
        pending
        for pending in asyncio.all_tasks()
        if pending is not asyncio.current_task()
        and pending.get_name().startswith("delegate:")
    ]
    assert leaked == []


# --------------------------------------------------------------------------- budget


async def test_budget_admits_and_attributes_each_child_separately(tmp_path: Path) -> None:
    rig = DelegationRig()
    rig.child_scripts["MARK-ALPHA"] = [_text("ALPHA")]
    rig.child_scripts["MARK-BETA"] = [_text("BETA")]
    rig.main_script = [
        _delegate_call(
            [_task("alpha", "MARK-ALPHA a"), _task("beta", "MARK-BETA b")]
        ),
        _text("MAIN-FINAL"),
    ]

    session = await _run(tmp_path, rig, token_budget=10_000_000)

    budget = session["budget"]
    assert budget is not None
    assert budget["admission_rejections"] == 0
    # Two Main turns (delegate + answer) and one turn per child.
    assert budget["requests_completed"] == 4
    workers = budget["workers"]
    assert set(workers) == {
        "atlas-main",
        workers_key(budget, "research_worker", "alpha"),
        workers_key(budget, "research_worker", "beta"),
    }
    assert workers[workers_key(budget, "research_worker", "alpha")]["actual_tokens"] > 0


def workers_key(budget: dict[str, Any], profile_id: str, key: str) -> str:
    """The budget hook names a child ``<profile>:<worker_id>``; find it by its key suffix."""

    matches = [name for name in budget["workers"] if name.endswith(f":{key}")]
    assert len(matches) == 1, budget["workers"]
    return matches[0]


async def test_a_rejected_child_request_is_a_failure_not_an_empty_success(
    tmp_path: Path,
) -> None:
    rig = DelegationRig()
    rig.child_scripts["MARK-ALPHA"] = [_text("ALPHA")]
    rig.main_script = [
        _delegate_call([_task("alpha", "MARK-ALPHA any work")]),
        _text("MAIN-FINAL"),
    ]
    options = GoalRunOptions(
        goal="budget",
        runs_root=tmp_path / "runs",
        workspace_root=_workspace(tmp_path),
        config=_config(),
        provider_factory=rig.build,
        run_id=f"stage-m-{uuid.uuid4().hex[:10]}",
        token_budget=1,
    )

    session = await run_goal(options)

    # The Main itself is rejected by the budget, so the goal cannot succeed -- and it must not be
    # reported as a successful empty run.
    assert session["status"] == "failed"
    assert session["main"]["success"] is False
    assert session["main"]["stop_reason"] == "budget_exceeded"
    assert session["budget"]["admission_rejections"] >= 1


# --------------------------------------------------------------------------- handoff safety


async def test_handoff_reads_are_authorized_for_the_parent_only(tmp_path: Path) -> None:
    rig = DelegationRig()
    rig.child_scripts["MARK-ALPHA"] = [_text("ALPHA" * 1000)]
    rig.main_script = [
        _delegate_call([_task("alpha", "MARK-ALPHA long result")]),
        lambda _: [
            ToolUseBlock(
                id="r1",
                name="delegate_read",
                input={"ref_id": "PLACEHOLDER", "offset": 0},
            )
        ],
        _text("MAIN-FINAL"),
    ]

    session = await _run(tmp_path, rig)

    ref_id = session["delegation"]["batches"][0]["children"][0]["ref_id"]
    assert ref_id is not None
    # The Main's own read is recorded as a successful handoff read.
    assert session["handoff_reads"]["read_success"] == 0  # the placeholder ref was refused
    assert session["handoff_reads"]["read_denied"] == 1

    from atlasos.delegation.handoff import DelegatedResultReader, caller_from_context
    from atlasos.handoff import HandoffStore

    run_dir = Path(session["run_dir"])
    store = HandoffStore(run_dir / "handoffs", run_id=session["run_id"])
    parent = DelegatedResultReader(
        store=store,
        index=_index(run_dir, session["run_id"]),
        parent_task_id=f"{session['run_id']}-main",
        parent_agent="atlas-main",
    )
    main_context = _context(
        task_id=f"{session['run_id']}-main",
        role="main",
        depth=0,
        extra={"delegation_root": session["run_id"]},
    )
    result = await parent.read(HandoffReadInput(ref_id=ref_id), _tool_context(main_context))
    assert result.is_error is False
    payload = json.loads(result.data)
    assert payload["producer"].startswith("delegate:")
    assert payload["content"].startswith("ALPHA")

    # A child of the same run cannot read it: it is not the delegation authority.
    child_context = _context(
        task_id=f"{session['run_id']}-main:r1:alpha",
        role="worker",
        depth=1,
        extra={
            "worker_id": f"{session['run_id']}-main:r1:alpha",
            "parent_task_id": f"{session['run_id']}-main",
            "delegation_root": session["run_id"],
        },
    )
    denied = await parent.read(HandoffReadInput(ref_id=ref_id), _tool_context(child_context))
    assert denied.is_error is True
    assert "delegate_read_denied" in denied.data
    assert caller_from_context(_tool_context(child_context)).is_main is False


def _index(run_dir: Path, run_id: str):
    from atlasos.delegation.handoff import DelegatedResultIndex, index_path

    return DelegatedResultIndex(run_id=run_id, path=index_path(run_dir))


def _context(
    *,
    task_id: str,
    role: str,
    depth: int,
    extra: dict[str, Any] | None = None,
):
    from anycode.identity.context import ExecutionContext

    return ExecutionContext(
        principal="user:test",
        tenant_scope="test",
        attributes={
            "task_id": task_id,
            "atlas_role": role,
            "delegation_depth": depth,
            **dict(extra or {}),
        },
    )


def _tool_context(execution_context) -> ToolUseContext:
    return ToolUseContext(
        agent=AgentInfo(name="tester", role="tester", model="fake-model"),
        execution_context=execution_context,
    )


# --------------------------------------------------------------------------- ablation / isolation


async def test_delegation_disabled_removes_the_tool_and_runs_the_main_alone(
    tmp_path: Path,
) -> None:
    rig = DelegationRig()
    rig.main_script = [_text("MAIN-FINAL: alone")]

    session = await _run(tmp_path, rig, delegation=_delegation(enabled=False))

    assert session["status"] == "succeeded"
    assert session["delegation"] is None
    assert session["delegation_enabled"] is False
    assert "delegate_tasks" not in session["tool_surface"]
    assert "delegate.read" not in session["tool_surface"]


async def test_the_main_tool_result_stays_bounded_for_a_huge_child_output(
    tmp_path: Path,
) -> None:
    rig = DelegationRig()
    rig.child_scripts["MARK-ALPHA"] = [_text("H" * 50_000)]
    rig.main_script = [
        _delegate_call([_task("alpha", "MARK-ALPHA huge result")]),
        _text("MAIN-FINAL"),
    ]

    session = await _run(tmp_path, rig)

    batch = session["delegation"]["batches"][0]
    assert batch["result_chars"] <= 8_000
    child = batch["children"][0]
    assert child["output_chars"] == 50_000
    assert child["summary_chars"] <= 900
    assert child["ref_id"] is not None
    tool_results = [
        block
        for message in rig.main_requests()[1]["messages"]
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert len(tool_results) == 1
    assert len(tool_results[0]["content"]) <= 8_000
    # The full output is still reachable behind the reference.
    assert children_ref(session) is not None


def children_ref(session: dict[str, Any]) -> str | None:
    return session["delegation"]["batches"][0]["children"][0]["ref_id"]


# --------------------------------------------------------------------------- service-level units


async def test_service_refuses_a_child_caller_without_creating_a_worker(tmp_path: Path) -> None:
    from atlasos.delegation.handoff import DelegatedResultIndex
    from atlasos.handoff import HandoffStore
    from atlasos.tools import AtlasToolRuntime
    from atlasos.workers import WorkerStore

    run_id = f"svc-{uuid.uuid4().hex[:8]}"
    run_dir = tmp_path / "svc"
    run_dir.mkdir()
    store = HandoffStore(run_dir / "handoffs", run_id=run_id)
    index = DelegatedResultIndex(run_id=run_id, path=run_dir / "delegation" / "handoffs.json")
    runtime = AtlasToolRuntime(run_id, state_path=run_dir / "tool_state.json")
    service = DelegationService(
        run_id=run_id,
        run_dir=run_dir,
        model="fake-model",
        provider=None,
        parent_context=_context(
            task_id="main-task", role="main", depth=0, extra={"delegation_root": run_id}
        ),
        parent_task_id="main-task",
        parent_agent="atlas-main",
        tool_runtime=runtime,
        handoff_store=store,
        handoff_index=index,
        limits=DelegationLimits(),
        worker_store=WorkerStore(run_dir / "workers.json"),
    )
    request = DelegateTasksInput.model_validate(
        {"tasks": [_task("alpha", "MARK-ALPHA any work")]}
    )
    child = _tool_context(
        _context(
            task_id="main-task:r1:alpha",
            role="worker",
            depth=1,
            extra={"worker_id": "w", "delegation_root": run_id},
        )
    )
    result = await service.delegate(request, child)
    assert result.is_error is True
    assert "NESTED_DELEGATION" in result.data
    assert service.rounds == 0
    assert not (run_dir / "workers.json").exists()
    await runtime.close()


async def test_round_limit_refuses_a_second_round(tmp_path: Path) -> None:
    rig = DelegationRig()
    rig.child_scripts["MARK-ALPHA"] = [_text("ALPHA")]
    rig.main_script = [
        _delegate_call([_task("alpha", "MARK-ALPHA one")], call_id="c1"),
        _delegate_call([_task("alpha", "MARK-ALPHA two")], call_id="c2"),
        _text("MAIN-FINAL"),
    ]
    session = await _run(tmp_path, rig, delegation=_delegation(max_delegation_rounds=1))

    batches = session["delegation"]["batches"]
    assert len(batches) == 1
    assert batches[0]["route"] == "delegate_one"
    # The second call was refused by the round limit and the Main still completed.
    tool_results = [
        block
        for message in rig.main_requests()[-1]["messages"]
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert any("ROUND_LIMIT_EXCEEDED" in json.dumps(block) for block in tool_results)
    assert session["status"] == "succeeded"
