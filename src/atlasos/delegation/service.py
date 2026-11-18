"""Main-driven delegation service: run admitted children and return a bounded projection.

What this module is responsible for
-----------------------------------
* **Identity**: only the delegation authority may delegate. The check reads the trusted
  ``ExecutionContext`` (``atlas_role``, ``delegation_depth``, ``atlas_worker``, ``task_id``), so a
  child that somehow reaches the tool is refused by the runtime, not by a prompt.
* **Bounded concurrency**: children run behind their own semaphore, never through the AnyCode agent
  pool. The Main's tool call already owns a pool slot, so waiting for a pool slot here would
  deadlock at ``max_concurrency=1``.
* **Isolation**: a child receives its assignment text only. Its conversation is stored under its
  own task id, and the Main receives a bounded summary plus an opaque reference.
* **Failure semantics**: timeout, cancellation, refusal and model failure are distinct terminal
  states. A failed child never fails the batch and never raises into the Main's loop.

What it is *not* responsible for: admission (``DelegationRouter``), authorization of individual
tool calls (M3 gate), write scope (R1-1 ownership), or prompt construction (worker renderer).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode.security.redaction import safe_exception_message
from anycode.types import (
    AgentConfig,
    ExecutionContext,
    ToolResult,
    ToolSecurityPolicy,
)

from atlasos.budget import RunBudgetHook, RunBudgetState
from atlasos.delegation.handoff import (
    DELEGATE_READ_TOOL,
    DelegatedResultIndex,
    DelegatedResultReader,
    caller_from_context,
)
from atlasos.delegation.models import (
    MAX_TOOL_RESULT_CHARS,
    REASON_INVALID_PLAN,
    REASON_NESTED_DELEGATION,
    REASON_ROUND_LIMIT_EXCEEDED,
    REASON_RUNTIME_UNAVAILABLE,
    ChildOutcome,
    ChildStatus,
    DelegatedTaskSpec,
    DelegateTasksInput,
    DelegationBatch,
    DelegationCaller,
    DelegationDecision,
    DelegationEvidence,
    DelegationRoute,
    DelegationTotals,
)
from atlasos.delegation.ownership import DelegatedOwnershipPolicy
from atlasos.delegation.router import (
    DelegationPlanContext,
    DelegationRouter,
    ProfileFacts,
)
from atlasos.delegation.usage import UsageAttributionHook
from atlasos.handoff import HandoffStore
from atlasos.tools.conversation import AtlasToolAgent, reset_current_task_id, set_current_task_id
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerCatalog, WorkerInstance
from atlasos.workers.models import WorkerConfigurationError
from atlasos.workers.store import WorkerStore

#: Tools that make a profile write-capable. Mirrors the R1-1 classifier vocabulary.
_WRITE_TOOLS = frozenset({"file_write", "file_edit", "bash"})


@dataclass(frozen=True)
class DelegationLimits:
    """Effective limits for one run. Values come from ``config.delegation``."""

    max_parallel_children: int = 2
    max_children_per_call: int = 2
    max_delegation_rounds: int = 4
    child_timeout_seconds: float = 300.0
    child_max_turns: int = 6
    result_preview_chars: int = 900

    def as_dict(self) -> dict[str, object]:
        return {
            "max_parallel_children": self.max_parallel_children,
            "max_children_per_call": self.max_children_per_call,
            "max_delegation_rounds": self.max_delegation_rounds,
            "child_timeout_seconds": self.child_timeout_seconds,
            "child_max_turns": self.child_max_turns,
            "result_preview_chars": self.result_preview_chars,
        }


class DelegationService:
    """Run admitted subworkers for one delegation authority."""

    def __init__(
        self,
        *,
        run_id: str,
        run_dir: str | Path,
        model: str,
        provider: str | None,
        parent_context: ExecutionContext,
        parent_task_id: str,
        parent_agent: str,
        tool_runtime: Any,
        worker_catalog: WorkerCatalog | None = None,
        worker_store: WorkerStore | None = None,
        handoff_store: HandoffStore | None = None,
        handoff_index: DelegatedResultIndex | None = None,
        reader: DelegatedResultReader | None = None,
        limits: DelegationLimits | None = None,
        budget_state: RunBudgetState | None = None,
        tool_security: ToolSecurityPolicy | None = None,
        ownership: DelegatedOwnershipPolicy | None = None,
        granted_write_paths: dict[str, tuple[str, ...]] | None = None,
        available_tools: Iterable[str] | None = None,
        router: DelegationRouter | None = None,
        trajectory_recorder: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self.model = model
        self.provider = provider
        self.parent_context = parent_context
        self.parent_task_id = parent_task_id
        self.parent_agent = parent_agent
        self.tool_runtime = tool_runtime
        self.catalog = worker_catalog or WorkerCatalog()
        self.limits = limits or DelegationLimits()
        self.budget_state = budget_state
        self.tool_security = tool_security
        self.ownership = ownership
        self.granted_write_paths = dict(granted_write_paths or {})
        self.router = router or DelegationRouter()
        self._clock = clock or (lambda: datetime.now(UTC))
        self.worker_store = worker_store or WorkerStore(self.run_dir / "workers.json")
        self.handoff_store = handoff_store or HandoffStore(
            self.run_dir / "handoffs", run_id=run_id
        )
        self.handoff_index = handoff_index or DelegatedResultIndex(
            run_id=run_id, path=self.run_dir / "delegation" / "handoffs.json"
        )
        self.reader = reader or DelegatedResultReader(
            store=self.handoff_store,
            index=self.handoff_index,
            parent_task_id=parent_task_id,
            parent_agent=parent_agent,
        )
        self.factory = ScopedWorkerFactory(
            model=model,
            provider=provider,
            store=self.worker_store,
            catalog=self.catalog,
            available_tools=available_tools,
            trajectory_recorder=trajectory_recorder,
            clock=self._clock,
        )
        self._rounds = 0
        self._identity_denials = 0
        self._batches: list[DelegationBatch] = []
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._active_children = 0
        self._max_active_children = 0

    # -- public entry ---------------------------------------------------------

    async def delegate(self, request: DelegateTasksInput, context: Any) -> ToolResult:
        """Tool entry point. Never raises; every outcome becomes a ``ToolResult``."""

        caller = caller_from_context(context)
        denial = self._identity_denial(caller)
        if denial is not None:
            self._identity_denials += 1
            return ToolResult(data=_refusal_payload(denial, None), is_error=True)
        if self._rounds >= self.limits.max_delegation_rounds:
            decision = self._static_decision(
                request, REASON_ROUND_LIMIT_EXCEEDED, "GATE_M_ROUND_LIMIT"
            )
            return ToolResult(data=_refusal_payload(REASON_ROUND_LIMIT_EXCEEDED, decision))
        batch = await self.run_batch(request)
        data, truncated = self.render(batch)
        rendered = batch.model_copy(
            update={"result_chars": len(data), "result_truncated": truncated}
        )
        if self._batches and self._batches[-1] is batch:
            self._batches[-1] = rendered
        return ToolResult(data=data, is_error=False)

    async def run_batch(self, request: DelegateTasksInput) -> DelegationBatch:
        """Admission, worker creation, concurrent execution, and evidence assembly."""

        self._rounds += 1
        if self._started_at is None:
            self._started_at = self._clock()
        decision = self.router.decide(request, context=self.plan_context())
        # ``perf_counter`` rather than ``monotonic``: on Windows the latter is ~15.6 ms coarse,
        # which is too coarse to observe that two children actually overlapped.
        batch_start = time.perf_counter()
        if not decision.admitted:
            batch = DelegationBatch(round=self._rounds, route=decision.route, decision=decision)
            self._batches.append(batch)
            return batch

        admitted_keys = decision.admitted_specs()
        specs = [spec for spec in request.tasks if spec.key in admitted_keys]
        try:
            workers = [self._create_worker(spec) for spec in specs]
        except (WorkerConfigurationError, ValueError) as error:
            refusal = self._static_decision(
                request,
                REASON_INVALID_PLAN,
                "GATE_M_PROPOSAL",
                detail=safe_exception_message(error),
            )
            batch = DelegationBatch(round=self._rounds, route=refusal.route, decision=refusal)
            self._batches.append(batch)
            return batch

        outcomes = await self._execute(workers, specs, batch_start=batch_start)
        batch = DelegationBatch(
            round=self._rounds,
            route=decision.route,
            decision=decision,
            children=tuple(outcomes),
            totals=_totals(outcomes),
            batch_span_ms=max(
                (outcome.finished_offset_ms for outcome in outcomes), default=0.0
            ),
            max_observed_concurrency=self._max_active_children,
        )
        self._batches.append(batch)
        return batch

    # -- plan context ---------------------------------------------------------

    def plan_context(self) -> DelegationPlanContext:
        profiles = {
            profile_id: ProfileFacts(
                profile_id=profile_id,
                base_tools=tuple(profile.base_tools),
                read_only=not (_WRITE_TOOLS & set(profile.base_tools)),
                can_delegate=bool(profile.can_delegate),
            )
            for profile_id, profile in self.catalog.profiles.items()
        }
        return DelegationPlanContext(
            run_id=self.run_id,
            parent_task_id=self.parent_task_id,
            max_children=self.limits.max_children_per_call,
            profiles=profiles,
            granted_write_paths={
                profile_id: tuple(paths)
                for profile_id, paths in self.granted_write_paths.items()
            },
            workspace_root=self._workspace_root(),
        )

    def evidence(self) -> DelegationEvidence:
        return DelegationEvidence(
            run_id=self.run_id,
            enabled=True,
            limits=self.limits.as_dict(),
            batches=tuple(self._batches),
            totals=_totals(
                [child for batch in self._batches for child in batch.children]
            ),
            started_at=self._started_at,
            finished_at=self._finished_at or self._clock(),
        )

    def reader_metrics(self) -> dict[str, int]:
        return self.reader.metrics()

    async def aclose(self) -> None:
        """Freeze the evidence clock. Children are reaped by :meth:`_execute` on cancellation."""

        self._finished_at = self._clock()

    @property
    def rounds(self) -> int:
        return self._rounds

    @property
    def identity_denials(self) -> int:
        return self._identity_denials

    # -- identity -------------------------------------------------------------

    def _identity_denial(self, caller: DelegationCaller) -> str | None:
        if not caller.is_main:
            return REASON_NESTED_DELEGATION
        if caller.task_id != self.parent_task_id:
            return REASON_NESTED_DELEGATION
        if caller.root is not None and caller.root != self.run_id:
            return REASON_NESTED_DELEGATION
        if self.tool_runtime is None:
            return REASON_RUNTIME_UNAVAILABLE
        return None

    # -- workers --------------------------------------------------------------

    def _create_worker(self, spec: DelegatedTaskSpec) -> WorkerInstance:
        worker_id = f"{self.parent_task_id}:r{self._rounds}:{spec.key}"
        worker = self.factory.create(
            parent_context=self.parent_context,
            parent_agent=self.parent_agent,
            assignment=WorkerAssignment(
                profile_id=spec.profile_id,
                task=spec.objective,
                context=spec.background,
            ),
            worker_id=worker_id,
            task_id=worker_id,
        )
        if self.ownership is not None:
            # Exactly the declared paths, which the router already proved are inside the Host
            # grant. A child that declares nothing owns nothing and is read-only by construction.
            self.ownership.declare(
                worker_id=worker.worker_id,
                task_id=worker.record.task_id,
                paths=tuple(spec.write_paths),
            )
        return worker

    def _build_agent(
        self, worker: WorkerInstance, usage: UsageAttributionHook
    ) -> AtlasToolAgent:
        config = worker.agent_config.model_copy(
            update={
                "max_turns": min(worker.profile.max_turns, self.limits.child_max_turns),
                "tool_security": self.tool_security,
            }
        )
        budget_hook: RunBudgetHook | None = None
        hooks: list[Any] = []
        if self.budget_state is not None:
            budget_hook = RunBudgetHook(
                self.budget_state,
                worker=worker.worker_name,
                model=config.model,
                system_prompt=config.system_prompt,
                tool_definitions=_child_tool_definitions_text(config, self.tool_runtime),
                completion_reserve=config.max_tokens,
            )
            hooks.append(budget_hook)
        hooks.append(self.tool_runtime.conversation_hook())
        # Last, so the estimate-based admission and the conversation snapshot stay first; this
        # hook only observes the provider-reported usage.
        hooks.append(usage)
        return AtlasToolAgent(
            config,
            self.tool_runtime.registry,
            self.tool_runtime.executor,
            hooks=hooks,
            conversation_store=self.tool_runtime.conversation_store,
            tool_runtime=self.tool_runtime,
            budget_hook=budget_hook,
        )

    # -- execution ------------------------------------------------------------

    async def _execute(
        self,
        workers: list[WorkerInstance],
        specs: list[DelegatedTaskSpec],
        *,
        batch_start: float,
    ) -> list[ChildOutcome]:
        gate = asyncio.Semaphore(self.limits.max_parallel_children)
        tasks = [
            asyncio.create_task(
                self._one_child(worker, spec, gate=gate, batch_start=batch_start),
                name=f"delegate:{worker.worker_id}",
            )
            for worker, spec in zip(workers, specs, strict=True)
        ]
        try:
            return list(await asyncio.gather(*tasks))
        except asyncio.CancelledError:
            # The Main's loop is being cancelled: reap every child before propagating so no
            # subworker outlives the run.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _one_child(
        self,
        worker: WorkerInstance,
        spec: DelegatedTaskSpec,
        *,
        gate: asyncio.Semaphore,
        batch_start: float,
    ) -> ChildOutcome:
        async with gate:
            started = time.perf_counter() - batch_start
            self._active_children += 1
            self._max_active_children = max(self._max_active_children, self._active_children)
            token = set_current_task_id(worker.record.task_id)
            status = ChildStatus.FAILED
            success = False
            stop_reason: str | None = None
            turns = 0
            tool_calls = 0
            output = ""
            error_code: str | None = None
            error_message: str | None = None
            usage = UsageAttributionHook()
            try:
                agent = self._build_agent(worker, usage)
                result = await asyncio.wait_for(
                    agent.run(spec.objective), timeout=self.limits.child_timeout_seconds
                )
                success = bool(result.success)
                stop_reason = result.stop_reason.code if result.stop_reason is not None else None
                turns = sum(1 for message in result.messages if message.role == "assistant")
                tool_calls = len(result.tool_calls)
                output = result.output or ""
                if success and not result.messages and not output:
                    # ``AgentRunner.stream`` converts a provider failure into an ``error`` event and
                    # returns without a ``done`` event; ``Agent`` then reports an empty run whose
                    # ``stop_reason`` is ``None``, which reads as success. No turn completed, so
                    # this is a failure and must not be reported as an empty answer.
                    success = False
                    error_code = "child_no_result"
                    error_message = "the model loop ended without completing a turn"
                status = ChildStatus.COMPLETED if success else ChildStatus.FAILED
                if not success:
                    error_code = error_code or stop_reason or "child_unsuccessful"
            except TimeoutError:
                status = ChildStatus.TIMEOUT
                error_code = "child_timeout"
                error_message = (
                    f"child exceeded {self.limits.child_timeout_seconds:g}s and was cancelled"
                )
            except asyncio.CancelledError:
                status = ChildStatus.CANCELLED
                error_code = "child_cancelled"
                error_message = "child was cancelled with its parent run"
                raise
            except Exception as error:  # noqa: BLE001 - a child failure is data, not control flow
                status = ChildStatus.FAILED
                error_code = "child_error"
                error_message = safe_exception_message(error)
            finally:
                reset_current_task_id(token)
                self._active_children -= 1
                finished = time.perf_counter() - batch_start

            summary = bound_summary(output, self.limits.result_preview_chars)
            ref_id = self._store_result(spec, worker, output)
            # Attribution comes from the hook because AnyCode's usage merge discards cache
            # counters; a child that made no completed request reports zeros, never a guess.
            tokens = {
                "prompt_tokens": usage.cache_miss_tokens,
                "total_prompt_tokens": usage.prompt_tokens,
                "cache_hit_tokens": usage.cache_hit_tokens,
                "cache_miss_tokens": usage.cache_miss_tokens,
                "completion_tokens": usage.completion_tokens,
            }
            return ChildOutcome(
                key=spec.key,
                worker_id=worker.worker_id,
                profile_id=worker.profile.profile_id,
                status=status,
                success=success,
                stop_reason=stop_reason,
                turns=turns,
                tool_calls=tool_calls,
                started_offset_ms=round(started * 1000.0, 3),
                finished_offset_ms=round(finished * 1000.0, 3),
                output_chars=len(output),
                summary=summary,
                summary_chars=len(summary),
                ref_id=ref_id,
                error_code=error_code,
                error_message=error_message,
                **tokens,
            )

    def _store_result(
        self, spec: DelegatedTaskSpec, worker: WorkerInstance, output: str
    ) -> str | None:
        if not output.strip():
            return None
        ref = self.handoff_store.put(
            producer_key=f"delegate:{worker.worker_id}",
            producer_title=f"{spec.key} ({worker.profile.profile_id})",
            content=output,
        )
        self.handoff_index.record(
            ref_id=ref.ref_id,
            parent_task_id=self.parent_task_id,
            parent_agent=self.parent_agent,
            worker_id=worker.worker_id,
            key=spec.key,
        )
        return ref.ref_id

    # -- rendering ------------------------------------------------------------

    def render(self, batch: DelegationBatch) -> tuple[str, bool]:
        return render_batch(batch)

    def _static_decision(
        self,
        request: DelegateTasksInput,
        reason: str,
        gate: str,
        *,
        detail: str | None = None,
    ) -> DelegationDecision:
        from atlasos.delegation.models import ChildAdmission

        policy = self.router.policy_for(self.limits.max_children_per_call)
        return DelegationDecision(
            route=DelegationRoute.DIRECT,
            reason_codes=(reason,),
            blocked_gates=(gate,),
            children=tuple(
                ChildAdmission(
                    key=spec.key,
                    profile_id=spec.profile_id,
                    admitted=False,
                    reason_codes=(reason,),
                )
                for spec in request.tasks
            ),
            policy_version=policy.version,
            policy_digest=policy.digest(),
            frozen_decision={"detail": detail} if detail is not None else None,
        )

    def _workspace_root(self) -> str:
        policy = self.tool_security
        if policy is not None and policy.workspace_root:
            return policy.workspace_root
        return ""


def _child_tool_definitions_text(config: AgentConfig, tool_runtime: Any) -> str | None:
    """Serialize the child's real tool surface for the budget estimate.

    ``AgentRunner`` sends exactly the registry definitions allowed by ``config.tools``; the same
    filter is applied here so the prompt estimate includes the schemas the provider bills.
    """

    allowed = set(config.tools or ())
    definitions = [
        definition
        for definition in tool_runtime.registry.to_tool_defs()
        if definition.name in allowed
    ]
    if not definitions:
        return None
    return json.dumps(
        [
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_schema,
            }
            for definition in definitions
        ],
        default=str,
        sort_keys=True,
    )


def bound_summary(output: str, limit: int) -> str:
    """Bounded, deterministic projection of a child result for the Main's conversation.

    The returned string never exceeds ``limit`` characters: the truncation notice is part of the
    budget, so a caller can treat ``limit`` as a hard cap rather than an approximation.
    """

    text = output.strip()
    if len(text) <= limit:
        return text
    marker = "\n[... more characters behind a reference]"
    keep = max(0, limit - len(marker))
    return text[:keep].rstrip() + marker


def _totals(outcomes: Iterable[ChildOutcome]) -> DelegationTotals:
    items = list(outcomes)
    return DelegationTotals(
        children_started=len(items),
        children_completed=sum(1 for item in items if item.status is ChildStatus.COMPLETED),
        children_failed=sum(1 for item in items if item.status is ChildStatus.FAILED),
        children_timed_out=sum(1 for item in items if item.status is ChildStatus.TIMEOUT),
        children_cancelled=sum(1 for item in items if item.status is ChildStatus.CANCELLED),
        prompt_tokens=sum(item.prompt_tokens for item in items),
        total_prompt_tokens=sum(item.total_prompt_tokens for item in items),
        cache_hit_tokens=sum(item.cache_hit_tokens for item in items),
        cache_miss_tokens=sum(item.cache_miss_tokens for item in items),
        completion_tokens=sum(item.completion_tokens for item in items),
    )


def render_batch(batch: DelegationBatch) -> tuple[str, bool]:
    """Render one batch as a bounded JSON tool result.

    Three deterministic tiers, so the Main never receives a child transcript by accident:

    1. full projection (summaries included);
    2. summaries dropped, references kept;
    3. children reduced to ``key/status/ref_id``.
    """

    tiers = (
        _payload(batch, tier=1),
        _payload(batch, tier=2),
        _payload(batch, tier=3),
    )
    for index, payload in enumerate(tiers):
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if len(text) <= MAX_TOOL_RESULT_CHARS:
            return text, index > 0
    return text[:MAX_TOOL_RESULT_CHARS], True


def _payload(batch: DelegationBatch, *, tier: int) -> dict[str, Any]:
    children: list[dict[str, Any]] = []
    for child in batch.children:
        if tier == 3:
            children.append(
                {"key": child.key, "status": child.status.value, "ref_id": child.ref_id}
            )
            continue
        entry: dict[str, Any] = {
            "key": child.key,
            "worker_id": child.worker_id,
            "profile_id": child.profile_id,
            "status": child.status.value,
            "success": child.success,
            "stop_reason": child.stop_reason,
            "turns": child.turns,
            "tool_calls": child.tool_calls,
            "started_offset_ms": child.started_offset_ms,
            "finished_offset_ms": child.finished_offset_ms,
            "prompt_tokens": child.prompt_tokens,
            "total_prompt_tokens": child.total_prompt_tokens,
            "cache_hit_tokens": child.cache_hit_tokens,
            "cache_miss_tokens": child.cache_miss_tokens,
            "completion_tokens": child.completion_tokens,
            "output_chars": child.output_chars,
            "ref_id": child.ref_id,
            "error_code": child.error_code,
            "error_message": child.error_message,
        }
        entry["summary"] = child.summary if tier == 1 else ""
        if tier == 2:
            entry["summary_omitted"] = True
        children.append(entry)
    payload: dict[str, Any] = {
        "route": batch.route.value,
        "reason_codes": list(batch.decision.reason_codes),
        "blocked_gates": list(batch.decision.blocked_gates),
        "policy_version": batch.decision.policy_version,
        "max_wave_width": batch.decision.max_wave_width,
        "max_observed_concurrency": batch.max_observed_concurrency,
        "children": children,
    }
    refs = [child.ref_id for child in batch.children if child.ref_id]
    if refs:
        payload["handoff"] = {
            "tool": DELEGATE_READ_TOOL,
            "refs": refs,
            "note": (
                "Each summary above is a bounded projection. Call delegate.read with a ref_id and "
                "an offset to page through the complete child output."
            ),
        }
    if batch.route is DelegationRoute.DIRECT:
        payload["next"] = (
            "No subworker was created. Complete this work yourself; if the plan was refused, "
            "change the plan (independent, read-only, context-supplied subtasks) or proceed."
        )
    return payload


def _refusal_payload(reason: str, decision: DelegationDecision | None) -> str:
    payload: dict[str, Any] = {
        "route": DelegationRoute.DIRECT.value,
        "reason_codes": [reason],
        "children": [],
    }
    if decision is not None:
        payload["blocked_gates"] = list(decision.blocked_gates)
        payload["policy_version"] = decision.policy_version
    payload["next"] = "No subworker was created. Continue in this agent loop."
    return json.dumps(payload, sort_keys=True)


__all__ = [
    "DelegationLimits",
    "DelegationService",
    "bound_summary",
    "render_batch",
]
