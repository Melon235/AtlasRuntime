"""Thin evaluation adapter over the existing AtlasOS V1/V2 runtime.

This module composes the same AnyCode engine, Atlas tool runtime, M3 policy, M4 ledger,
M5 sandbox manager, M6 suspension stores, and M7 recorder used by the acceptance harnesses.
It does not implement a second runtime, tool executor, policy, approval, idempotency, or lock
system. Each sample gets a fresh run directory, sandbox, stores, and worker state.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import platform
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode import OrchestratorConfig, TaskSpec, TeamConfig
from anycode.identity.context import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.runstore.store import FilesystemRunStore
from anycode.tools.idempotency import SQLiteToolIdempotencyStore
from anycode.types import (
    MCPServerConfig,
    ProviderResilienceConfig,
    RetryPolicy,
    ToolSecurityPolicy,
)
from dotenv import load_dotenv
from experiments.evaluation.fixtures import (
    materialize_fixture,
    stage_git_repo,
    workspace_digest,
    workspace_hashes,
)
from experiments.evaluation.models import (
    EFFECTIVE_PUBLIC_TASK_DIGEST_VERSION,
    BenchmarkTask,
    EvalRunResult,
    FailureClass,
    OracleOutcome,
    RunIdentity,
    RuntimeMetrics,
    canonical_digest,
    effective_agent_max_turns,
    effective_public_task_digest,
)
from experiments.evaluation.oracle import evaluate_oracle
from experiments.evaluation.sandboxed_oracle import (
    ORACLE_MODE_SANDBOX,
    ORACLE_MODES,
    OracleSandboxConfig,
    evaluate_oracle_isolated,
    oracle_failure_class,
)
from experiments.evaluation.stage_g3_4a_integrity import (
    FIXTURE_COMMIT_LABEL,
    enforce_evaluation_isolation,
)
from experiments.evaluation.stage_g3_worker_contracts import (
    WORKER_CONTRACT_LEGACY,
    WORKER_CONTRACT_MODES,
    contract_overhead,
    worker_task_text,
)
from experiments.evaluation.stage_r0_routing import (
    DELEGATION_ROUTE_MODES,
    DELEGATION_ROUTE_OFF,
    DELEGATION_ROUTE_SELECTIVE,
    a0_steps,
    decision_digest,
    plan_route,
    routing_policy,
)
from experiments.evaluation.stage_r1_1_ownership import (
    manifest_for_task,
    manifest_public_summary,
)
from experiments.evaluation.stage_r1_1_trace import compare_with_result, summarize_trace

from atlasos.actions import ActionCatalog, ActionPolicy, ActionRule
from atlasos.actions.catalog import DEFAULT_MAPPINGS, ActionMapping
from atlasos.budget import BUDGET_MODE_GUARDED, RunBudgetState
from atlasos.config import AtlasConfig, Pricing, load_config
from atlasos.context import (
    InMemoryContextStore,
    NaiveExecutionOrderRenderer,
    StablePromptRenderer,
    TaskScopedSelector,
)
from atlasos.context.renderer import render_dependency_block
from atlasos.context.task_prompt import AtlasTaskPromptBuilder
from atlasos.context.transform import ContextPilotDedupTransform, IdentityContextTransform
from atlasos.experiments import git_commit, git_dirty
from atlasos.handoff import HANDOFF_READ_ACTION_MAPPING, HandoffNode, HandoffRuntime
from atlasos.history_compaction import (
    COMPACTION_MODES,
    COMPACTION_OFF,
    HISTORY_READ_ACTION_MAPPING,
    HistoryCompactionRuntime,
)
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.metrics.models import InferenceObservation
from atlasos.ownership import (
    OWNERSHIP_MODE_OFF,
    OWNERSHIP_MODES,
    OwnershipManifestError,
    build_ownership_enforcer,
)
from atlasos.sandbox import (
    OpenSandboxOptions,
    ProjectSandboxManager,
    SandboxProjectConfig,
    SandboxWorkspaceTools,
    SQLiteSandboxBindingStore,
    build_sandbox_provider,
    export_patch,
    export_workspace,
    is_real_sandbox_provider,
    require_capabilities,
)
from atlasos.sandbox.models import SandboxLostError
from atlasos.tool_result_offload import (
    TOOL_RESULT_MODES,
    TOOL_RESULT_OFF,
    ToolResultIngressRuntime,
)
from atlasos.tools import AtlasToolRuntime
from atlasos.tools.conversation import set_current_task_id
from atlasos.trajectory.payloads import (
    EXECUTION_INTEGRITY_MODES,
    EXECUTION_INTEGRITY_OFF,
    EXECUTION_INTEGRITY_RECORD,
    ToolPayloadCorpus,
)
from atlasos.trajectory.recorder import TrajectoryRecorder
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerCatalog, WorkerStore
from atlasos.workers.routing import capabilities_from_catalog
from atlasos.workflow import AtlasTaskSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "experiments" / "configs" / "eval-benchmark.yaml"
MCP_SERVER_DIR = REPO_ROOT / "experiments" / "evaluation" / "mcp_servers"
#: Stage R1-2A verification modes. ``observe`` adds findings; it never feeds back into the run.
VERIFICATION_MODES = ("off", "observe")

#: Final-4: what a unit claims about the Atlas Context Plane. ``unspecified`` is the default so no
#: existing caller changes behaviour; the other values are enforced before the first model call.
CONTEXT_PLANE_UNSPECIFIED = "unspecified"
CONTEXT_PLANE_TASK_SCOPED = "task_scoped"
CONTEXT_PLANE_UPSTREAM = "upstream_default"
CONTEXT_PLANE_EXPECTATIONS = (
    CONTEXT_PLANE_UNSPECIFIED,
    CONTEXT_PLANE_TASK_SCOPED,
    CONTEXT_PLANE_UPSTREAM,
)

#: Closure-1A: the effective request layout a unit declares. It must agree with the YAML's
#: ``context.prompt_layout``; a unit that declares one layout and resolves another is refused before
#: any I/O, for the same reason the Context Plane claim is enforced (G-1).
PROMPT_LAYOUT_LEGACY = "legacy"
PROMPT_LAYOUT_SHARED_PREFIX = "shared_prefix"
PROMPT_LAYOUTS = (PROMPT_LAYOUT_LEGACY, PROMPT_LAYOUT_SHARED_PREFIX)

_SANDBOX_TOOLS = frozenset({"file_read", "file_write", "file_edit", "grep", "list_files", "bash"})
_CONTROL_TOOLS = frozenset({"tool.search", "tool_search", "dispatch_action"})
_EVAL_MCP_TOOLS = (
    "lookup_fare",
    "employee_clearance",
    "convert_currency",
    "isbn_check_digit",
    "shipping_quote",
    "policy_lookup",
)
_EVAL_CATALOG = ActionCatalog(
    mappings=(
        *DEFAULT_MAPPINGS,
        *(ActionMapping(f"mcp_evaltools_{name}", "external.read") for name in _EVAL_MCP_TOOLS),
        # Stage G1 dual-channel handoff reader: read-only access to a stored dependency result.
        HANDOFF_READ_ACTION_MAPPING,
        # Stage G2b-1 history reader: read-only access to a worker's own archived tool results.
        HISTORY_READ_ACTION_MAPPING,
    )
)


class EvaluationEnvironmentError(RuntimeError):
    """Infrastructure is unavailable; this is not a model-quality failure."""


@dataclass(frozen=True)
class RunnerSettings:
    benchmark_id: str = "atlasos-agentbench-v1"
    dataset_version: str = "1.0.0"
    scorer_version: str = "1.0.0"
    config_path: Path = DEFAULT_CONFIG
    runs_root: Path = REPO_ROOT / "experiments" / "runs" / "evaluation"
    timeout_seconds: int = 900
    temperature: float = 0.0
    max_turns: int = 14
    #: Run-level override of the per-agent turn cap. ``None`` keeps the historical rule
    #: (``step.max_turns or max_turns``); a value wins over a per-step declaration, which is what
    #: lets a candidate budget raise the cap without editing the frozen task data.
    worker_max_turns: int | None = None
    max_tokens: int = 4096
    token_budget: int = 120_000
    budget_mode: str = "legacy"
    max_tool_calls: int = 40
    progressive_tools: bool = True
    model_override: str | None = None
    retain_sandbox_on_failure: bool = False
    variant: str = "default"
    capture_evidence: bool = False
    handoff_mode: str = "legacy"
    compaction_mode: str = "off"
    tool_error_mode: str = "legacy"
    tool_result_mode: str = "off"
    worker_contract_mode: str = "legacy"
    #: Stage R1-1: Host-owned write authorization for the shared workspace.
    ownership_mode: str = "off"
    #: Stage R1-1: durable tool-event evidence (private payload corpus + worker stop events).
    execution_integrity_mode: str = "off"
    #: Stage R1-2A: deterministic deliverable verification (``off`` or observation only).
    verification_mode: str = "off"
    #: Stage R1-2H: hidden tests run in an isolated sandbox. ``host`` only reproduces old numbers.
    oracle_mode: str = "sandbox"
    delegation_route_mode: str = "off"
    #: Stage R0.5: which frozen routing policy the selective arm must use.
    delegation_policy_version: str = "r0.1.0"
    #: Final-4: the Context Plane state this unit claims. The resolved configuration is compared to
    #: the claim before any model request; a mismatch aborts the run instead of silently
    #: falling back to ``upstream_default``.
    context_plane_expectation: str = CONTEXT_PLANE_UNSPECIFIED
    #: Final-4 Phase 2: probe the outgoing request body for the selected dependency content. Pure
    #: observation (digests and containment flags); no request byte is altered or persisted.
    request_evidence: bool = True
    #: Closure-1A: the effective request layout this unit claims. Compared with the resolved YAML
    #: before the first request; part of the effective configuration digest.
    prompt_layout: str = PROMPT_LAYOUT_LEGACY

    def digest(self) -> str:
        payload: dict[str, Any] = {
            "benchmark_id": self.benchmark_id,
            "dataset_version": self.dataset_version,
            "scorer_version": self.scorer_version,
            "temperature": self.temperature,
            "max_turns": self.max_turns,
            "max_tokens": self.max_tokens,
            "token_budget": self.token_budget,
            "budget_mode": self.budget_mode,
            "max_tool_calls": self.max_tool_calls,
            "progressive_tools": self.progressive_tools,
            "model_override": self.model_override,
            "variant": self.variant,
            "capture_evidence": self.capture_evidence,
            "handoff_mode": self.handoff_mode,
            "compaction_mode": self.compaction_mode,
            "tool_error_mode": self.tool_error_mode,
            "tool_result_mode": self.tool_result_mode,
            "worker_contract_mode": self.worker_contract_mode,
            "ownership_mode": self.ownership_mode,
            "execution_integrity_mode": self.execution_integrity_mode,
            "verification_mode": self.verification_mode,
            "oracle_mode": self.oracle_mode,
            "delegation_route_mode": self.delegation_route_mode,
            "delegation_policy_version": self.delegation_policy_version,
            "context_plane_expectation": self.context_plane_expectation,
            "prompt_layout": self.prompt_layout,
            "config": _config_digest(self.config_path),
        }
        # Only present when set: ``None`` *is* the historical semantics, and hashing it differently
        # would move the settings digest of every run that already executed.
        if self.worker_max_turns is not None:
            payload["worker_max_turns"] = self.worker_max_turns
        return canonical_digest(payload)


#: A request that was observed at issue time and has not finished yet.
REQUEST_OBSERVED = "OBSERVED"
#: The provider answered and the response reached the caller.
REQUEST_COMPLETED = "COMPLETED"
#: The call raised. It was issued, so it stays in the observed set.
REQUEST_FAILED = "FAILED"
#: The stream was abandoned before it produced a response (closed, cancelled or never drained
#: after the first item). It was issued, so it stays in the observed set.
REQUEST_CANCELLED = "CANCELLED"
_TERMINAL_REQUEST_STATES = frozenset({REQUEST_COMPLETED, REQUEST_FAILED, REQUEST_CANCELLED})


class _RequestEvidence:
    """Observation-only probe of the *outgoing* request body.

    It answers one question that fingerprints cannot: did the dependency content that the Context
    Plane selected actually appear in the message list the provider received, and how many times.
    Only digests and booleans are kept — never the prompt, never a transcript, never a completion.

    Three things a shared system prompt breaks are handled here:

    * **Identity** comes from ``options.execution_context``, the trusted per-agent context AnyCode
      passes through unchanged, never from a content hash. Two workers may legitimately carry
      byte-identical system prompts, so ``sha256(system_prompt)`` can no longer name a worker.
    * **Completeness** is tracked per request: observed at issue time, then completed, failed or
      cancelled. A stream that is never consumed never reaches :meth:`observe`, so an unconsumed
      stream is not counted as a sent request.
    * **Presence** is probed against the *complete structured* dependency block on the first message
      of the request, separately from any later mention the assistant history may contain.
    """

    def __init__(self, store: Any = None) -> None:
        self._store = store
        self.records: list[dict[str, Any]] = []
        self._request_index: dict[str, int] = {}

    def bind_store(self, store: Any) -> None:
        self._store = store

    def _blocks(self) -> list[Any]:
        if self._store is None:
            return []
        return [
            block for block in self._store.all_blocks() if block.block_id.startswith("task-output:")
        ]

    @staticmethod
    def _tool_schema_chars(tools: Any) -> int:
        if not tools:
            return 0
        payload = [
            tool.model_dump(mode="json") if hasattr(tool, "model_dump") else str(tool)
            for tool in tools
        ]
        return len(json.dumps(payload, sort_keys=True))

    @staticmethod
    def _trusted_identity(options: Any) -> dict[str, Any]:
        """Trusted ids of the agent that issued this request, or ``None``s when none is carried."""

        context = getattr(options, "execution_context", None)
        attributes = getattr(context, "attributes", None) if context is not None else None
        if not isinstance(attributes, dict):
            attributes = {}
        return {
            "trusted_task_id": attributes.get("task_id"),
            "trusted_worker_id": attributes.get("worker_id"),
            "trusted_profile_id": attributes.get("profile_id"),
            "trusted_run_id": attributes.get("run_id"),
        }

    def observe(self, messages: Any, options: Any) -> int:
        tools = tuple(getattr(options, "tools", ()) or ())
        texts = [
            "\n".join(str(getattr(block, "text", "")) for block in getattr(message, "content", ()))
            for message in messages
        ]
        joined = "\n".join(texts)
        initial = texts[0] if texts else ""
        identity = self._trusted_identity(options)
        trusted_task_id = identity.get("trusted_task_id")
        request_index = (
            self._request_index.get(str(trusted_task_id), 0)
            if isinstance(trusted_task_id, str)
            else None
        )
        if isinstance(trusted_task_id, str):
            self._request_index[str(trusted_task_id)] = (request_index or 0) + 1
        probes: dict[str, Any] = {}
        for block in self._blocks():
            content = str(getattr(block, "content", ""))
            # An empty dependency body is still a real block: probe the delimiters, and record that
            # the body itself carries nothing to match instead of dropping the evidence.
            structured = render_dependency_block(
                task_id=str(block.task_id or "unknown"),
                version=block.version,
                content=content,
            )
            in_initial = initial.count(structured)
            in_request = joined.count(structured)
            probes[block.block_id] = {
                # Full structured block in the initial task prompt: the injection proof.
                "present": in_initial > 0,
                "count": in_initial,
                # A later assistant/tool message may quote the same block. That is redundancy in the
                # history, never a second injection into the initial prompt.
                "later_history_mentions": max(in_request - in_initial, 0),
                "request_count": in_request,
                # Raw content, so a body that reached the turn without its delimiters is visible.
                "content_present": (content in initial) if content else None,
                "content_count": initial.count(content) if content else 0,
                "content_empty": not content,
                "block_version": block.version,
                "block_chars": len(content),
                "probe_kind": "FULL_STRUCTURED_BLOCK",
            }
        self.records.append(
            {
                "call_index": len(self.records),
                "request_index": request_index,
                **identity,
                "status": REQUEST_OBSERVED,
                "finished": False,
                "error_type": None,
                "system_prompt_digest": hashlib.sha256(
                    str(getattr(options, "system_prompt", "") or "").encode("utf-8")
                ).hexdigest(),
                "first_message_digest": hashlib.sha256(initial.encode("utf-8")).hexdigest()
                if texts
                else None,
                "message_count": len(texts),
                "text_chars": len(joined),
                "tool_count": len(tools),
                "tool_schema_chars": self._tool_schema_chars(tools),
                "dependency_probes": probes,
                "usage": None,
                "content_persisted": False,
            }
        )
        return len(self.records) - 1

    def _finish(self, call_index: int, status: str, *, error_type: str | None = None) -> None:
        if not 0 <= call_index < len(self.records):
            return
        record = self.records[call_index]
        if record["status"] in _TERMINAL_REQUEST_STATES:
            # First terminal state wins: a failure is not later relabelled as a cancellation just
            # because the generator was closed on the way out.
            return
        record["status"] = status
        record["finished"] = True
        record["error_type"] = error_type

    def mark_completed(self, call_index: int, usage: Any = None) -> None:
        self.attach_usage(call_index, usage)
        self._finish(call_index, REQUEST_COMPLETED)

    def mark_failed(self, call_index: int, error_type: str | None = None) -> None:
        self._finish(call_index, REQUEST_FAILED, error_type=error_type)

    def mark_cancelled(self, call_index: int) -> None:
        self._finish(call_index, REQUEST_CANCELLED)

    def attach_usage(self, call_index: int, usage: Any) -> None:
        """Record the *normalized* usage of one already-observed request.

        AnyCode's ``TokenUsage`` is a normalized object, and it cannot express "the provider did not
        report a cache split": both a provider-reported 0-hit split and no split at all arrive here
        as ``cache_read_input_tokens == 0``. So the split is recorded as **unknown** at this layer
        rather than being inferred from a zero.

        The provider's own split lives one layer up, in ``InferenceObservation.usage``, where
        :func:`_aggregate_observations` reads it. Nothing here reconstructs it from ordering,
        content similarity or process state — a wrong number would be worse than a missing one.
        """

        if not 0 <= call_index < len(self.records) or usage is None:
            return
        miss = int(getattr(usage, "input_tokens", 0) or 0)
        hit = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        self.records[call_index]["usage"] = {
            # Normalized totals: the adapter adds cache-read back into the prompt, so this is the
            # provider's prompt total in both the "split reported" and "split absent" cases.
            "prompt_tokens": miss + hit,
            "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "normalized_input_tokens": miss,
            "normalized_cache_read_tokens": hit,
            # Never inferred. ``None`` means the provider's split is not known here.
            "cache_hit_tokens": None,
            "cache_miss_tokens": None,
            "cache_split_known": False,
            "source": "anycode.TokenUsage (normalized by the provider adapter)",
            "cache_split_note": (
                "TokenUsage cannot distinguish a reported 0-hit split from an unreported split; "
                "the provider split is recorded at run level from InferenceObservation.usage"
            ),
        }

    def as_dict(self) -> dict[str, Any]:
        probes = [
            {
                "block_id": block_id,
                "call_index": record["call_index"],
                "present": result["present"],
                "count": result["count"],
            }
            for record in self.records
            for block_id, result in (record.get("dependency_probes") or {}).items()
        ]
        by_status = {
            state: sum(1 for record in self.records if record["status"] == state)
            for state in (REQUEST_OBSERVED, REQUEST_COMPLETED, REQUEST_FAILED, REQUEST_CANCELLED)
        }
        return {
            "enabled": True,
            "method": (
                "observed on the outgoing message list inside the provider wrapper; only digests, "
                "character counts and containment flags are recorded"
            ),
            "identity_source": "options.execution_context.attributes (trusted ExecutionContext)",
            "dependency_probe_basis": "complete structured dependency block, initial task prompt",
            "requests_observed": len(self.records),
            "requests_by_status": by_status,
            "requests_completed": by_status[REQUEST_COMPLETED],
            "requests_failed": by_status[REQUEST_FAILED],
            "requests_cancelled": by_status[REQUEST_CANCELLED],
            "requests_unfinished": by_status[REQUEST_OBSERVED],
            "requests_with_trusted_identity": sum(
                1 for record in self.records if record.get("trusted_task_id")
            ),
            "requests_with_usage": sum(1 for record in self.records if record.get("usage")),
            "requests_with_a_dependency_probe": len(
                {record["call_index"] for record in self.records if record["dependency_probes"]}
            ),
            "dependency_probe_hits": sum(1 for probe in probes if probe["present"]),
            "duplicate_dependency_injections": sorted(
                {probe["block_id"] for probe in probes if probe["present"] and probe["count"] > 1}
            ),
            "requests": self.records,
        }


class ModelRecordingAdapter:
    """Observation-only wrapper recording the provider-resolved model and per-request usage."""

    def __init__(
        self, inner: Any, models: list[str], evidence: _RequestEvidence | None = None
    ) -> None:
        # ``inner`` is whatever adapter the provider factory built. Production is the DeepSeek
        # adapter; the deterministic harnesses wrap a fake. The wrapper only needs ``name``,
        # ``chat`` and ``stream``, so it does not narrow to a concrete class.
        self._inner = inner
        self._models = models
        self._evidence = evidence

    @property
    def name(self) -> str:
        return self._inner.name

    async def chat(self, messages: Any, options: Any) -> Any:
        call_index = self._evidence.observe(messages, options) if self._evidence else None
        try:
            response = await self._inner.chat(messages, options)
        except asyncio.CancelledError:
            # A cancelled request is not a failed one: the caller went away, the provider did not
            # reject anything.
            if self._evidence is not None and call_index is not None:
                self._evidence.mark_cancelled(call_index)
            raise
        except BaseException as error:  # noqa: BLE001 - status only, re-raised unchanged
            if self._evidence is not None and call_index is not None:
                self._evidence.mark_failed(call_index, type(error).__name__)
            raise
        if self._evidence is not None and call_index is not None:
            self._evidence.mark_completed(call_index, getattr(response, "usage", None))
        self._models.append(str(response.model))
        return response

    def stream(self, messages: Any, options: Any) -> Any:
        # AnyCode's agent loop streams. Observation happens *inside* the generator, i.e. when the
        # request is actually issued: a stream that is created and never consumed never reaches
        # the first line below, so it is never counted as a sent request.
        # ``_inner`` is the real adapter: its own ``stream`` delegates to its own ``chat``, so this
        # request is counted once, not twice.
        async def _generate() -> Any:
            evidence = self._evidence
            call_index = evidence.observe(messages, options) if evidence else None
            completed = False

            async def _events() -> Any:
                nonlocal completed
                async for event in self._inner.stream(messages, options):
                    if getattr(event, "type", None) == "done":
                        response = getattr(event, "data", None)
                        model_name = getattr(response, "model", None)
                        if model_name is not None:
                            self._models.append(str(model_name))
                        completed = True
                        if evidence is not None and call_index is not None:
                            evidence.mark_completed(call_index, getattr(response, "usage", None))
                    yield event

            try:
                async for event in _events():
                    yield event
            except GeneratorExit:
                raise
            except asyncio.CancelledError:
                # Task cancellation, not a provider failure.
                if evidence is not None and call_index is not None:
                    evidence.mark_cancelled(call_index)
                raise
            except BaseException as error:  # noqa: BLE001 - status only, re-raised unchanged
                if evidence is not None and call_index is not None:
                    evidence.mark_failed(call_index, type(error).__name__)
                raise
            finally:
                if evidence is not None and call_index is not None and not completed:
                    # Abandoned before a response: it was issued, so it stays observed, but it is
                    # not a completed request and never counts as usage coverage.
                    evidence.mark_cancelled(call_index)

        return _generate()


class AllToolsAtlasRuntime(AtlasToolRuntime):
    """Evaluation-only ablation: expose every core tool natively, without tool.search."""

    @property
    def agent_tools(self) -> tuple[str, ...]:
        names = set(self.catalog.core_tool_names) | set(self.catalog.searchable_names)
        names.discard("tool.search")
        names.discard("dispatch_action")
        return tuple(sorted(names))


def _config_digest(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return canonical_digest(load_config(path).model_dump(mode="json"))


def evaluation_resilience() -> ProviderResilienceConfig:
    """One bounded attempt per model call; the outer per-sample timeout is the hard stop."""

    return ProviderResilienceConfig(
        enabled=True,
        retry=RetryPolicy(
            max_attempts=1,
            base_delay_seconds=0.1,
            max_delay_seconds=1.0,
            jitter=False,
            call_timeout_seconds=60.0,
        ),
        max_concurrency=1,
    )


def _coding_policy() -> ActionPolicy:
    allow = (
        "control.search",
        "control.dispatch",
        "filesystem.read",
        "filesystem.write",
        "shell.execute",
        "external.read",
    )
    return ActionPolicy(
        name="evaluation-coding",
        default="DENY",
        rules=tuple(
            ActionRule(id=f"allow-{action}", decision="ALLOW", action=action) for action in allow
        ),
    )


def _environment_note(workspace_root: str) -> str:
    return (
        f"The repository is at {workspace_root} inside a remote Linux sandbox. "
        "Use Python 3 and portable POSIX shell commands. Do not assume access to the Host."
    )


def _build_provider(config: AtlasConfig):
    sandbox = config.sandbox
    provider = build_sandbox_provider(
        sandbox.provider,
        opensandbox_options=OpenSandboxOptions(
            endpoint=sandbox.endpoint,
            protocol=sandbox.protocol,
            timeout_minutes=sandbox.timeout_minutes,
            ready_timeout_seconds=sandbox.ready_timeout_seconds,
            cpu=sandbox.cpu,
            memory=sandbox.memory,
            use_server_proxy=sandbox.use_server_proxy,
        ),
    )
    return provider


def _context_prompt_builder(
    *,
    config: AtlasConfig,
    task: Any,
    title_by_id: dict[str, str],
    task_texts: dict[str, str],
    worker_by_task: dict[str, Any],
    prompt_layout: str = PROMPT_LAYOUT_LEGACY,
    task_bodies: dict[str, str] | None = None,
    shared_context_by_task: dict[str, str] | None = None,
) -> tuple[AtlasTaskPromptBuilder, InMemoryContextStore] | None:
    """Wire the existing Context Plane into a multi-worker coding DAG (opt-in via ``config``).

    ``context.policy="upstream_default"`` returns ``None`` and the handoff prompt builder keeps
    today's behaviour. Nothing here schedules, calls a model, or stores anything new. The store is
    returned as well because it *is* the run's logical context: reading it afterwards is
    observation, not a second implementation.

    ``prompt_layout`` decides what the *builder* renders, never what it selects: the selector sees
    the same declared dependencies in both layouts. In the shared-prefix layout the structured
    public block travels as ``AtlasTaskSpec.shared_context`` and the per-task body is the worker's
    own assignment, so the renderer can put the public text first without slicing a concatenation.
    """

    if config.context.policy != "task_scoped":
        return None
    layout = prompt_layout if prompt_layout in PROMPT_LAYOUTS else PROMPT_LAYOUT_LEGACY
    renderer = (
        NaiveExecutionOrderRenderer()
        if config.context.renderer == "naive_execution_order"
        else StablePromptRenderer(preserve_input_order=True)
    )
    transform = (
        IdentityContextTransform()
        if config.context.transform.name == "identity"
        else ContextPilotDedupTransform()
    )
    bodies = task_bodies or {}
    shared_by_task = shared_context_by_task or {}
    specs = {
        title_by_id[step.task_id]: AtlasTaskSpec(
            task_id=step.task_id,
            title=title_by_id[step.task_id],
            description=(
                bodies.get(step.task_id, task_texts[step.task_id])
                if layout == PROMPT_LAYOUT_SHARED_PREFIX
                else task_texts[step.task_id]
            ),
            assignee=worker_by_task[step.task_id].worker_name,
            depends_on=step.depends_on,
            shared_context=(
                shared_by_task.get(step.task_id, "")
                if layout == PROMPT_LAYOUT_SHARED_PREFIX
                else ""
            ),
        )
        for step in task.workers
    }
    agents = {
        worker_by_task[step.task_id].worker_name: (
            worker_by_task[step.task_id].agent_config.system_prompt,
            "coding_worker",
        )
        for step in task.workers
    }
    store = InMemoryContextStore()
    builder = AtlasTaskPromptBuilder(
        store=store,
        selector=TaskScopedSelector(),
        renderer=renderer,
        transform=transform,
        specs_by_title=specs,
        agents_by_name=agents,
        prompt_layout=layout,
    )
    return builder, store


def _task_turn_parts(
    step: Any, *, steps: tuple[Any, ...], sample_id: str, mode: str
) -> tuple[str, str]:
    """``(shared public context, worker's own task body)`` — the two halves, unmerged."""

    return (
        getattr(step, "shared_context", "") or "",
        worker_task_text(step, steps=steps, sample_id=sample_id, mode=mode),
    )


def _task_turn(step: Any, *, steps: tuple[Any, ...], sample_id: str, mode: str) -> str:
    """The model-visible task turn: shared public context, then the worker's own assignment."""

    shared, text = _task_turn_parts(step, steps=steps, sample_id=sample_id, mode=mode)
    return f"{shared}\n\n{text}" if shared else text


def _contract_overhead_chars(step: Any, task_text: str) -> int:
    """Characters the worker contract added, excluding the shared public context."""

    shared = getattr(step, "shared_context", "") or ""
    own_text = task_text[len(shared) :].lstrip("\n") if shared else task_text
    return contract_overhead(step.prompt, own_text)


def _normalized_expectation(value: str) -> str:
    return value if value in CONTEXT_PLANE_EXPECTATIONS else CONTEXT_PLANE_UNSPECIFIED


def _normalized_prompt_layout(value: str) -> str:
    """Accept a known layout or fail. Never turn a typo into the default."""

    if value not in PROMPT_LAYOUTS:
        raise EvaluationEnvironmentError(
            f"evaluation configuration error: unknown prompt_layout {value!r}; "
            f"expected one of {sorted(PROMPT_LAYOUTS)}"
        )
    return value


def _enforce_prompt_layout(declared_layout: str, *, config: AtlasConfig, config_file: Path) -> str:
    """Resolve the request layout and refuse any unit whose claim the YAML cannot satisfy.

    Two arms whose requests differ in layout must never share one effective configuration record,
    and a hand-written claim must never survive a YAML that resolves something else.
    """

    declared = _normalized_prompt_layout(declared_layout)
    resolved = config.context.prompt_layout
    if resolved != declared:
        raise EvaluationEnvironmentError(
            "evaluation configuration error: this unit declares prompt_layout="
            f"{declared!r} but {config_file.name} resolves {resolved!r}. Refusing to run: the "
            "recorded layout would not describe the request that was sent."
        )
    if declared != PROMPT_LAYOUT_SHARED_PREFIX:
        return declared
    problems: list[str] = []
    if config.context.policy != CONTEXT_PLANE_TASK_SCOPED:
        problems.append(f"context.policy={config.context.policy!r}")
    if config.context.renderer != "stable_deterministic":
        problems.append(f"context.renderer={config.context.renderer!r}")
    if config.context.transform.name != "identity":
        problems.append(f"context.transform={config.context.transform.name!r}")
    if problems:
        raise EvaluationEnvironmentError(
            "evaluation configuration error: prompt_layout='shared_prefix' is validated only with "
            f"policy={CONTEXT_PLANE_TASK_SCOPED!r}, renderer='stable_deterministic' and "
            f"transform='identity'; {config_file.name} resolves {', '.join(problems)}"
        )
    return declared


def _enforce_context_plane_config(
    expectation: str, *, config: AtlasConfig, config_file: Path, handoff_mode: str
) -> None:
    """Pre-I/O guard: the resolved YAML already has to be able to satisfy the unit's claim.

    Runs before a run directory, a sandbox or a provider exists, so a declared-but-unloaded Context
    Plane can never again reach a model request.
    """

    if expectation == CONTEXT_PLANE_UNSPECIFIED:
        return
    problems: list[str] = []
    if expectation == CONTEXT_PLANE_TASK_SCOPED:
        if config.context.policy != CONTEXT_PLANE_TASK_SCOPED:
            problems.append(f"context.policy={config.context.policy!r}")
        if config.context.renderer != "stable_deterministic":
            problems.append(f"context.renderer={config.context.renderer!r}")
        if config.context.transform.name != "identity":
            problems.append(f"context.transform={config.context.transform.name!r}")
        if handoff_mode == "dual":
            problems.append("handoff_mode='dual' (both project dependency outputs)")
    elif config.context.policy != CONTEXT_PLANE_UPSTREAM:
        problems.append(f"context.policy={config.context.policy!r}")
    elif config.context.renderer != CONTEXT_PLANE_UPSTREAM:
        problems.append(f"context.renderer={config.context.renderer!r}")
    if problems:
        raise EvaluationEnvironmentError(
            "evaluation configuration error: this unit declares context plane "
            f"{expectation!r} but {config_file.name} resolves {', '.join(problems)}. "
            "Refusing to run: the declared mechanism would not execute."
        )


def _enforce_context_plane(expectation: str, resolved: dict[str, Any]) -> None:
    """Fail closed when the resolved Context Plane contradicts the unit's claim.

    This is an evaluation-entry constraint, not a change to the runtime default: a unit that
    declares ``task_scoped`` must actually reach ``AtlasTaskPromptBuilder``, and a unit that
    declares ``upstream_default`` must not. A silent fallback is exactly the defect this guards.
    """

    builder = resolved.get("builder")
    policy = resolved.get("policy")
    if expectation == CONTEXT_PLANE_TASK_SCOPED:
        if builder is None or policy != CONTEXT_PLANE_TASK_SCOPED:
            raise EvaluationEnvironmentError(
                "evaluation configuration error: this unit declares "
                f"context.policy={CONTEXT_PLANE_TASK_SCOPED!r} but resolved policy={policy!r} "
                f"with builder={builder!r} from {resolved.get('config_path')!r}. "
                "The Atlas Context Plane would not run; refusing to call the model."
            )
        if resolved.get("handoff_mode") != "legacy":
            raise EvaluationEnvironmentError(
                "evaluation configuration error: task_scoped context projection and "
                f"handoff_mode={resolved.get('handoff_mode')!r} both project dependency outputs "
                "into the task prompt; enable exactly one"
            )
    elif expectation == CONTEXT_PLANE_UPSTREAM and builder is not None:
        raise EvaluationEnvironmentError(
            "evaluation configuration error: this unit declares upstream_default context but "
            f"resolved a {builder!r} builder from {resolved.get('config_path')!r}"
        )


def _context_evidence(
    *,
    builder: AtlasTaskPromptBuilder,
    store: InMemoryContextStore,
    task: Any,
    task_texts: dict[str, str],
    observations: list[InferenceObservation],
    request_records: list[dict[str, Any]] | None = None,
    workers: dict[str, Any] | None = None,
    responses_observed: int | None = None,
) -> dict[str, Any]:
    """Read-only evidence for every task-prompt render the run performed.

    ``task_prompt_hash`` is the renderer's hash of the user turn it produced.
    ``provider_request_hash`` is the adapter's hash of the messages it actually sent. They are
    different functions over different objects and are never reported as one value.
    """

    blocks = {block.block_id: block for block in store.all_blocks()}
    dependencies = {step.task_id: tuple(step.depends_on) for step in task.workers}
    per_task: list[dict[str, Any]] = []
    for task_id in sorted(builder.renders()):
        render = builder.last(task_id)
        if render is None:  # pragma: no cover - renders() and last() share one mapping
            continue
        declared = dependencies.get(task_id, ())
        dependency_facts: list[dict[str, Any]] = []
        for dependency in declared:
            block = blocks.get(f"task-output:{dependency}")
            content = block.content if block is not None else ""
            occurrences = render.prompt.count(content) if content else 0
            dependency_facts.append(
                {
                    "task_id": dependency,
                    "store_block_id": block.block_id if block is not None else None,
                    "block_version": block.version if block is not None else None,
                    "block_chars": len(content),
                    "in_rendered_prompt": occurrences > 0,
                    "injection_count": occurrences,
                    "duplicate_injection": occurrences > 1,
                }
            )
        per_task.append(
            {
                "task_id": task_id,
                "declared_dependencies": list(declared),
                "builder_invoked": True,
                "rendered_dependency_order": list(render.rendered_dependency_order),
                "dependency_completion_order": list(render.dependency_completion_order),
                "selected_block_ids": [
                    f"task-output:{dependency}" for dependency in render.rendered_dependency_order
                ],
                "selected_block_versions": [
                    blocks[f"task-output:{dependency}"].version
                    for dependency in render.rendered_dependency_order
                    if f"task-output:{dependency}" in blocks
                ],
                "store_block_count": len(blocks),
                "transform_name": render.transform_name,
                "deduplicated": render.deduplicated,
                "context_layout_hash": render.context_layout_hash,
                "pre_transform_layout_hash": render.pre_transform_layout_hash,
                "stable_prefix_hash": render.stable_prefix_hash,
                "task_prompt_hash": render.full_prompt_hash,
                # Content identity of the exact user turn handed back to AnyCode. A provider request
                # whose first message digests to this value provably carried this render.
                "task_prompt_sha256": hashlib.sha256(render.prompt.encode("utf-8")).hexdigest(),
                "task_prompt_chars": len(render.prompt),
                "task_text_chars": len(task_texts.get(task_id, "")),
                "prompt_layout": render.prompt_layout,
                "effective_system_prompt_sha256": render.system_prompt_sha256,
                "shared_context_chars": render.shared_context_chars,
                "dependencies": dependency_facts,
            }
        )
    return {
        "enabled": True,
        "builder": type(builder).__name__,
        "store": type(store).__name__,
        "store_block_ids": sorted(blocks),
        "builder_invocations": len(per_task),
        "tasks": per_task,
        "provider_requests": [
            {
                "request_index": index,
                "full_prompt_hash": observation.full_prompt_hash,
                "stable_prefix_hash": observation.stable_prefix_hash,
                "prompt_tokens": observation.usage.prompt_tokens,
                "cache_hit_tokens": observation.usage.cache_hit_tokens,
                "cache_miss_tokens": observation.usage.cache_miss_tokens,
                "completion_tokens": observation.usage.completion_tokens,
            }
            for index, observation in enumerate(observations)
        ],
        "correlation": {
            "task_prompt_hash_vs_provider_request_hash": (
                "DIFFERENT_FUNCTIONS; never compare or substitute one for the other"
            ),
            "per_request_task_attribution": (
                "trusted: options.execution_context.attributes.task_id, mapped to the Atlas "
                "logical task by the host-owned worker record. Never inferred from prompt "
                "content, because a shared-prefix layout gives several workers the same "
                "system prompt"
            ),
            "dependency_delivery_basis": (
                "the builder's return value is the AnyCode task prompt; dependency presence is "
                "probed on the complete structured block in the first message of the request, and "
                "the provider-side hash is recorded separately"
            ),
        },
        # ``observations`` is the inner adapter's per-response record, a counter independent of the
        # wrapper's per-request observation. Passing its length is what lets the linkage say whether
        # the request set is reconciled instead of merely non-empty.
        "request_linkage": _request_linkage(
            per_task,
            request_records,
            workers,
            responses_observed=(
                len(observations) if responses_observed is None else responses_observed
            ),
        ),
    }


def _request_index_integrity(linked: list[dict[str, Any]]) -> dict[str, Any]:
    """Structural checks on the observed request set.

    A count reconciliation can still pass while the identifiers are duplicated or missing — one
    request counted twice and another never recorded. These checks are cheap and catch that class.

    ``call_index`` is the wrapper's global observation order, so it must be unique and contiguous.
    ``request_index`` is per trusted task, so it must be unique and contiguous within that task.
    """

    call_indices = [item["call_index"] for item in linked]
    duplicates = sorted({index for index in call_indices if call_indices.count(index) > 1})
    missing = sorted(set(range(len(linked))) - set(call_indices))

    per_task: dict[str, list[Any]] = {}
    for item in linked:
        task_id = item["matched_task_id"]
        if task_id is None:
            continue
        per_task.setdefault(str(task_id), []).append(item["request_index"])
    task_problems: dict[str, Any] = {}
    for task_id, indices in per_task.items():
        duplicated = sorted(
            {index for index in indices if index is not None and indices.count(index) > 1}
        )
        missing_index = sum(1 for index in indices if index is None)
        present = sorted(index for index in indices if index is not None)
        contiguous = present == list(range(len(present)))
        if duplicated or missing_index or not contiguous:
            task_problems[task_id] = {
                "duplicate_request_indices": duplicated,
                "requests_without_index": missing_index,
                "contiguous": contiguous,
                "indices": indices,
            }
    state_ok = all(
        item["status"] in _TERMINAL_REQUEST_STATES or item["status"] == REQUEST_OBSERVED
        for item in linked
    )
    return {
        "passed": not duplicates and not missing and not task_problems and state_ok,
        "call_index_unique": not duplicates,
        "duplicate_call_indices": duplicates,
        "call_index_contiguous": not missing,
        "missing_call_indices": missing,
        "request_index_contiguous_per_task": not task_problems,
        "request_index_problems": task_problems,
        "single_terminal_state": state_ok,
    }


def _request_linkage(
    per_task: list[dict[str, Any]],
    request_records: list[dict[str, Any]] | None,
    workers: dict[str, Any] | None,
    *,
    responses_observed: int | None = None,
) -> dict[str, Any]:
    """Tie an observed provider request to a task and a worker, and decide how complete that is.

    Identity is trusted first: the request carries the agent's ``ExecutionContext``, and the
    host-owned worker record maps its runtime task id to the Atlas logical task. All four trusted
    ids (task, worker, profile, run) must agree with **the same** record; a request that only
    happens to carry a task id that looks like a logical task id is not trusted, because that is
    exactly the inference a shared system prompt makes unsafe.

    Content is then checked separately, per request:

    * ``sha256(first message)`` must equal the ``task_prompt_sha256`` of the render that request
      claims to belong to;
    * ``sha256(system prompt)`` must equal the effective system prompt hash of that same worker.

    ``sha256(first message)`` is still used as a diagnostic fallback when the request carries no
    trusted context, but a fallback match never earns ``REQUEST_CONTENT_VERIFIED``.

    ``responses_observed`` is an independent count of responses produced by the inner adapter. It
    reconciles the observed request set: a request that was issued either got a response, failed, or
    was abandoned. Without that count the request set is not independently bounded, and this
    function says so instead of claiming full coverage.
    """

    renders = {item["task_id"]: item for item in per_task}
    expected_task_ids = sorted(renders)
    if not request_records:
        return {
            "status": "REQUEST_CONTENT_NOT_VERIFIED",
            "reason": (
                "no request-level observation was recorded for this run (request_evidence disabled "
                "or the run predates it)"
            ),
            "reason_codes": ["NO_REQUEST_OBSERVED"],
            "expected_task_ids": expected_task_ids,
            "observed_task_ids": [],
            "coverage": {"status": "NO_OBSERVATION"},
        }

    # -- what a trusted worker of this run looks like -------------------------------------------
    worker_facts: dict[str, dict[str, Any]] = {}
    logical_by_record_task: dict[str, str] = {}
    digest_by_system: dict[str, list[str]] = {}
    for logical_id, worker in (workers or {}).items():
        record = getattr(worker, "record", None)
        agent_config = getattr(worker, "agent_config", None)
        system_prompt = str(getattr(agent_config, "system_prompt", "") or "")
        agent_digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        attributes = getattr(getattr(record, "execution_context", None), "attributes", None)
        record_run_id = attributes.get("run_id") if isinstance(attributes, dict) else None
        record_digest = getattr(record, "effective_system_prompt_hash", None)
        worker_facts[logical_id] = {
            "record_task_id": getattr(record, "task_id", None),
            "record_worker_id": getattr(record, "worker_id", None),
            "record_profile_id": getattr(record, "profile_id", None),
            "record_run_id": record_run_id,
            "record_system_digest": record_digest,
            "agent_config_system_digest": agent_digest,
            # The record hash is the persisted truth of what ran; the live config is the fallback
            # for a record written before that field existed, and disagreement is reported
            # separately.
            "expected_system_digest": record_digest or agent_digest,
        }
        record_task_id = getattr(record, "task_id", None)
        if isinstance(record_task_id, str) and record_task_id:
            logical_by_record_task[record_task_id] = logical_id
        digest_by_system.setdefault(agent_digest, []).append(logical_id)

    # -- per request: identity, then content ----------------------------------------------------
    linked: list[dict[str, Any]] = []
    for record in request_records:
        observed_ids = {
            "task_id": record.get("trusted_task_id"),
            "worker_id": record.get("trusted_worker_id"),
            "profile_id": record.get("trusted_profile_id"),
            "run_id": record.get("trusted_run_id"),
        }
        logical_id: str | None = None
        identity_status: str
        identity_mismatches: list[str] = []
        trusted_record = observed_ids["task_id"]
        if not isinstance(trusted_record, str) or not trusted_record:
            identity_status = "NO_TRUSTED_IDENTITY"
        elif trusted_record not in logical_by_record_task:
            identity_status = "NO_HOST_WORKER_RECORD"
        else:
            logical_id = logical_by_record_task[trusted_record]
            facts = worker_facts[logical_id]
            for field, expected_key in (
                ("task_id", "record_task_id"),
                ("worker_id", "record_worker_id"),
                ("profile_id", "record_profile_id"),
                ("run_id", "record_run_id"),
            ):
                expected = facts[expected_key]
                if expected is None or observed_ids[field] != expected:
                    identity_mismatches.append(field)
            identity_status = "TRUSTED" if not identity_mismatches else "IDENTITY_MISMATCH"

        identity_source = "trusted_execution_context" if identity_status == "TRUSTED" else None
        task_id: str | None = logical_id if identity_status == "TRUSTED" else None
        if task_id is None:
            # Diagnostic only: a content match can show what a request probably was, but it can
            # never promote an untrusted request, so the status stays PARTIAL.
            fallback = {
                item["task_prompt_sha256"]: item["task_id"]
                for item in per_task
                if item.get("task_prompt_sha256")
            }.get(str(record.get("first_message_digest")))
            if fallback is not None:
                identity_source = "prompt_digest_fallback"
                if logical_id is None:
                    logical_id = fallback

        render = renders.get(logical_id) if logical_id is not None else None
        facts = worker_facts.get(logical_id) if logical_id is not None else None
        task_prompt_match = (
            render is not None
            and bool(render.get("task_prompt_sha256"))
            and render.get("task_prompt_sha256") == record.get("first_message_digest")
        )
        expected_system = facts["expected_system_digest"] if facts else None
        system_prompt_match = expected_system is not None and expected_system == record.get(
            "system_prompt_digest"
        )
        if render is None:
            content_status = "NO_RENDER_TO_COMPARE"
        elif not task_prompt_match:
            content_status = "TASK_PROMPT_MISMATCH"
        elif not system_prompt_match:
            content_status = "SYSTEM_PROMPT_MISMATCH"
        else:
            content_status = "MATCH"

        linked.append(
            {
                "call_index": record["call_index"],
                "request_index": record.get("request_index"),
                "status": record.get("status"),
                "matched_task_id": render["task_id"] if render is not None else None,
                "identity_source": identity_source,
                "identity_status": identity_status,
                "identity_mismatches": identity_mismatches,
                "trusted_task_id": record.get("trusted_task_id"),
                "trusted_worker_id": record.get("trusted_worker_id"),
                "trusted_profile_id": record.get("trusted_profile_id"),
                "trusted_run_id": record.get("trusted_run_id"),
                "matched_worker_task_id": (logical_id if identity_status == "TRUSTED" else None),
                "task_prompt_hash_match": task_prompt_match,
                "system_prompt_hash_match": system_prompt_match,
                "content_status": content_status,
                "context_layout_digest": render.get("context_layout_hash") if render else None,
                "system_prompt_digest": record.get("system_prompt_digest"),
                "usage": record.get("usage"),
                "usage_present": record.get("usage") is not None,
                "dependency_probes": record.get("dependency_probes") or {},
                "message_count": record["message_count"],
                "text_chars": record["text_chars"],
                "content_persisted": record.get("content_persisted", False),
            }
        )

    observed_task_ids = sorted(
        {item["matched_task_id"] for item in linked if item["matched_task_id"]}
    )
    missing_task_ids = sorted(set(expected_task_ids) - set(observed_task_ids))
    unlinked = [item["call_index"] for item in linked if item["matched_task_id"] is None]
    untrusted = [item["call_index"] for item in linked if item["identity_status"] != "TRUSTED"]
    identity_mismatch_requests = [
        {"call_index": item["call_index"], "fields": item["identity_mismatches"]}
        for item in linked
        if item["identity_mismatches"]
    ]
    task_prompt_mismatches = [
        item["call_index"] for item in linked if not item["task_prompt_hash_match"]
    ]
    system_prompt_mismatches = [
        item["call_index"] for item in linked if not item["system_prompt_hash_match"]
    ]

    # -- dependency delivery, per consumer -------------------------------------------------------
    dependency_failures: list[dict[str, Any]] = []
    for task_id in expected_task_ids:
        declared = renders[task_id].get("declared_dependencies") or []
        if not declared:
            continue
        task_requests = [item for item in linked if item["matched_task_id"] == task_id]
        if not task_requests:
            dependency_failures.append({"task_id": task_id, "reason": "NO_REQUEST_OBSERVED"})
            continue
        ordered = sorted(
            task_requests,
            key=lambda item: item["request_index"] if item["request_index"] is not None else 0,
        )
        for dependency in declared:
            block_id = f"task-output:{dependency}"
            # Every observed request of the consumer is checked, not just the ones that happen to
            # carry a probe. Skipping a request without a probe would let a run with a correct
            # first and third request and a missing middle one still pass as fully verified.
            missing_probe: list[int] = []
            absent: list[int] = []
            wrong_count: list[dict[str, Any]] = []
            content_missing: list[int] = []
            for item in ordered:
                probe = (item["dependency_probes"] or {}).get(block_id)
                call_index = item["call_index"]
                if probe is None:
                    missing_probe.append(call_index)
                    continue
                if not probe.get("present"):
                    absent.append(call_index)
                    continue
                if probe.get("count") != 1:
                    wrong_count.append({"call_index": call_index, "count": probe.get("count")})
                    continue
                # An empty dependency body is legal: the structured block is the evidence, and the
                # content probe reports "nothing to compare" for it.
                if not probe.get("content_empty") and probe.get("content_present") is False:
                    content_missing.append(call_index)
            if missing_probe:
                dependency_failures.append(
                    {
                        "task_id": task_id,
                        "block_id": block_id,
                        "reason": "PROBE_MISSING_ON_CONSUMER_REQUEST",
                        "calls_without_probe": missing_probe,
                        "observed_calls": [item["call_index"] for item in ordered],
                    }
                )
            if absent:
                dependency_failures.append(
                    {
                        "task_id": task_id,
                        "block_id": block_id,
                        "reason": "STRUCTURED_BLOCK_ABSENT",
                        "calls": absent,
                    }
                )
            if wrong_count:
                dependency_failures.append(
                    {
                        "task_id": task_id,
                        "block_id": block_id,
                        "reason": "STRUCTURED_BLOCK_COUNT",
                        "counts": wrong_count,
                    }
                )
            if content_missing:
                dependency_failures.append(
                    {
                        "task_id": task_id,
                        "block_id": block_id,
                        "reason": "CONTENT_ABSENT",
                        "calls": content_missing,
                    }
                )

    # -- coverage --------------------------------------------------------------------------------
    completed = [item for item in linked if item["status"] == REQUEST_COMPLETED]
    failed = [item for item in linked if item["status"] == REQUEST_FAILED]
    cancelled = [item for item in linked if item["status"] == REQUEST_CANCELLED]
    unfinished = [item for item in linked if item["status"] not in _TERMINAL_REQUEST_STATES]
    integrity = _request_index_integrity(linked)
    if responses_observed is None:
        coverage_status = "NO_INDEPENDENT_COUNTER"
        reconciled: bool | None = None
    else:
        reconciled = (
            len(linked) == responses_observed + len(failed) + len(cancelled)
            and not unfinished
            and integrity["passed"]
        )
        coverage_status = "RECONCILED" if reconciled else "UNRECONCILED"
    coverage = {
        "status": coverage_status,
        "requests_observed": len(linked),
        "requests_completed": len(completed),
        "requests_failed": len(failed),
        "requests_cancelled": len(cancelled),
        "requests_unfinished": len(unfinished),
        "responses_observed_by_adapter": responses_observed,
        "reconciled": reconciled,
        "index_integrity": integrity,
        "task_coverage": "COMPLETE" if not missing_task_ids else "INCOMPLETE",
        "usage_coverage": (
            f"{sum(1 for item in linked if item['usage_present'])}/{len(completed)} completed "
            "requests carry usage"
            if completed
            else "no completed request"
        ),
        "basis": (
            "requests are counted when the wrapper issues them; responses are counted by the inner "
            "adapter per returned response. A request either produced a response, failed, or was "
            "abandoned, so observed == responses + failed + cancelled must hold, and the indices "
            "must be unique and contiguous. This is an adapter-level reconciliation, NOT a network "
            "trace: it bounds what this wrapper saw, not every packet the provider handled"
        ),
    }

    matched = sum(1 for item in linked if item["matched_task_id"] is not None)
    reason_codes: list[str] = []
    if missing_task_ids:
        reason_codes.append("TASK_NEVER_OBSERVED")
    if unlinked:
        reason_codes.append("REQUEST_WITHOUT_A_TASK_IDENTITY")
    if untrusted:
        reason_codes.append("IDENTITY_NOT_TRUSTED")
    if identity_mismatch_requests:
        reason_codes.append("IDENTITY_MISMATCH")
    if task_prompt_mismatches:
        reason_codes.append("REQUEST_CONTENT_MISMATCH")
    if system_prompt_mismatches:
        reason_codes.append("SYSTEM_PROMPT_MISMATCH")
    if dependency_failures:
        reason_codes.append("DEPENDENCY_NOT_FULLY_DELIVERED")
    if failed or cancelled or unfinished:
        reason_codes.append("REQUEST_NOT_COMPLETED")
    if not integrity["passed"]:
        reason_codes.append("REQUEST_INDEX_INTEGRITY_FAILED")
    if coverage_status != "RECONCILED":
        reason_codes.append("REQUEST_COVERAGE_UNVERIFIED")

    if matched == 0:
        status = "REQUEST_CONTENT_NOT_VERIFIED"
        reported_reasons = ["NO_REQUEST_LINKED_TO_A_RENDER"]
    elif reason_codes:
        status = "REQUEST_CONTENT_PARTIAL"
        reported_reasons = reason_codes
    else:
        status = "REQUEST_CONTENT_VERIFIED"
        reported_reasons = []
    return {
        "status": status,
        "reason_codes": reported_reasons,
        "requests_observed": len(linked),
        "requests_linked_to_a_render": matched,
        "requests_unlinked": len(linked) - matched,
        "requests_with_trusted_identity": sum(
            1 for item in linked if item["identity_status"] == "TRUSTED"
        ),
        "requests_with_matching_task_prompt": sum(
            1 for item in linked if item["task_prompt_hash_match"]
        ),
        "requests_with_matching_system_prompt": sum(
            1 for item in linked if item["system_prompt_hash_match"]
        ),
        "expected_task_ids": expected_task_ids,
        "observed_task_ids": observed_task_ids,
        "missing_task_ids": missing_task_ids,
        "identity_failures": [
            {
                "call_index": item["call_index"],
                "status": item["identity_status"],
                "mismatches": item["identity_mismatches"],
            }
            for item in linked
            if item["identity_status"] != "TRUSTED"
        ],
        "identity_mismatch_requests": identity_mismatch_requests,
        "task_prompt_mismatch_requests": task_prompt_mismatches,
        "system_prompt_mismatch_requests": system_prompt_mismatches,
        "coverage": coverage,
        "linkage_basis": (
            "primary: all four options.execution_context.attributes ids (task, worker, profile, "
            "run) resolved through the host-owned worker record; content: sha256(first message) == "
            "task_prompt_sha256 and sha256(system prompt) == effective_system_prompt_hash; digest "
            "identity is a diagnostic fallback only"
        ),
        "dependency_failures": dependency_failures,
        "dependency_delivery_in_request": [
            {
                "call_index": item["call_index"],
                "task_id": item["matched_task_id"],
                "block_id": block_id,
                "present": probe["present"],
                "count": probe["count"],
                "content_empty": probe.get("content_empty"),
                "later_history_mentions": probe.get("later_history_mentions"),
                "probe_kind": probe.get("probe_kind"),
            }
            for item in linked
            for block_id, probe in item["dependency_probes"].items()
        ],
        "requests": linked,
    }


def _project_config(config: AtlasConfig) -> SandboxProjectConfig:
    sandbox = config.sandbox
    return SandboxProjectConfig(
        provider_name=sandbox.provider,
        image=sandbox.image,
        snapshot=sandbox.snapshot,
        workspace_root=sandbox.workspace_root,
        max_bundle_bytes=sandbox.max_bundle_bytes,
        network=sandbox.network,
    )


def build_deepseek_adapter(
    model: str,
    on_call: Any,
    *,
    timeout_seconds: float = 45.0,
) -> DeepSeekAdapter:
    """Build the existing AtlasOS adapter with a bounded HTTP timeout.

    The evaluation harness must fail a hung provider call instead of blocking an entire round.
    This still uses the AtlasOS DeepSeek adapter and the same inference path.
    """

    from openai import AsyncOpenAI

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise EvaluationEnvironmentError("DEEPSEEK_API_KEY is not configured")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_seconds,
        max_retries=0,
    )
    return DeepSeekAdapter(client=client, model=model, on_call=on_call)


def _estimate_cost(
    observations: list[InferenceObservation], pricing: Pricing | None
) -> float | None:
    if not observations or pricing is None or not pricing.is_complete:
        return None
    hit_rate = pricing.cache_hit_input_per_million
    miss_rate = pricing.cache_miss_input_per_million
    output_rate = pricing.output_per_million
    if hit_rate is None or miss_rate is None or output_rate is None:
        return None
    total = 0.0
    for observation in observations:
        usage = observation.usage
        if usage.cache_hit_tokens is None or usage.cache_miss_tokens is None:
            return None
        total += usage.cache_hit_tokens * hit_rate / 1_000_000
        total += usage.cache_miss_tokens * miss_rate / 1_000_000
        total += usage.completion_tokens * output_rate / 1_000_000
    return round(total, 8)


def _aggregate_observations(
    observations: list[InferenceObservation],
) -> dict[str, Any]:
    """Sum the provider's own numbers without inventing the ones it did not report.

    ``cache_split_status`` makes the difference between "the provider said 0 hits" and "the provider
    said nothing" explicit instead of letting both collapse into a zero:

    * ``COMPLETE`` — every call reported both hit and miss tokens;
    * ``PARTIAL``  — some did, some did not (the total is still unknown, not a partial sum);
    * ``NONE``     — no call reported a split;
    * ``NO_OBSERVATIONS`` — nothing was measured.
    """

    if not observations:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_hit_tokens": None,
            "cache_miss_tokens": None,
            "cache_hit_ratio": None,
            "cache_metrics_complete": False,
            "cache_split_status": "NO_OBSERVATIONS",
            "prompt_token_consistency": None,
            "inference_latency_ms": 0.0,
        }
    prompt = sum(item.usage.prompt_tokens for item in observations)
    completion = sum(item.usage.completion_tokens for item in observations)
    with_split = [
        item
        for item in observations
        if item.usage.cache_hit_tokens is not None and item.usage.cache_miss_tokens is not None
    ]
    cache_complete = len(with_split) == len(observations)
    if cache_complete:
        status = "COMPLETE"
    elif with_split:
        status = "PARTIAL"
    else:
        status = "NONE"
    hit = sum(item.usage.cache_hit_tokens or 0 for item in with_split) if cache_complete else None
    miss = sum(item.usage.cache_miss_tokens or 0 for item in with_split) if cache_complete else None
    consistency: dict[str, Any] | None = None
    if hit is not None and miss is not None:
        # Reported, never corrected: a provider whose split does not add up is a fact to record.
        consistency = {
            "consistent": hit + miss == prompt,
            "hit_plus_miss": hit + miss,
            "prompt_tokens": prompt,
            "delta": prompt - (hit + miss),
        }
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "cache_hit_ratio": (hit / prompt) if hit is not None and prompt else None,
        "cache_metrics_complete": cache_complete,
        "cache_split_status": status,
        "prompt_token_consistency": consistency,
        "inference_latency_ms": round(sum(item.usage.latency_ms for item in observations), 3),
    }


UNAVAILABLE = "UNAVAILABLE"


def _provider_call_records(
    observations: list[InferenceObservation],
    *,
    run_id: str,
    model: str,
    pricing: Pricing | None,
) -> list[dict[str, Any]]:
    """One record per provider call (Stage G3-2 observability contract).

    ``on_call`` fires inside the adapter, which has no agent identity, so ``worker_id`` and
    ``task_id`` are reported as ``UNAVAILABLE`` rather than guessed from a process-global value.
    Token, cache and latency fields are exactly what the provider returned for that call.
    """

    records: list[dict[str, Any]] = []
    for index, observation in enumerate(observations):
        usage = observation.usage
        hit = usage.cache_hit_tokens
        miss = usage.cache_miss_tokens
        cost = _estimate_cost([observation], pricing)
        records.append(
            {
                "run_id": run_id,
                "request_index": index,
                "worker_id": UNAVAILABLE,
                "task_id": UNAVAILABLE,
                "attribution": UNAVAILABLE,
                "model": model,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "cache_hit_tokens": hit if hit is not None else UNAVAILABLE,
                "cache_miss_tokens": miss if miss is not None else UNAVAILABLE,
                "cache_hit_ratio": (
                    round(usage.cache_hit_ratio, 6)
                    if usage.cache_hit_ratio is not None
                    else UNAVAILABLE
                ),
                "cost_usd": cost if cost is not None else UNAVAILABLE,
                "latency_ms": round(usage.latency_ms, 3),
                "stable_prefix_hash": observation.stable_prefix_hash,
                "full_prompt_hash": observation.full_prompt_hash,
            }
        )
    return records


def _classify_exception(error: BaseException) -> FailureClass:
    if isinstance(error, EvaluationEnvironmentError):
        return "ENVIRONMENT_FAILURE"
    if isinstance(error, SandboxLostError):
        return "SANDBOX_FAILURE"
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    name = type(error).__name__
    module = type(error).__module__
    if "openai" in module or "API" in name or "RateLimit" in name or "Timeout" in name:
        return "MODEL_FAILURE"
    return "RUNTIME_FAILURE"


async def _observe_verification(
    *,
    task: Any,
    workspace: Path | None,
    baseline_hashes: dict[str, str],
    run_dir: Path,
) -> dict[str, Any] | None:
    """Verify the exported workspace against the trusted contract, without executing its code."""

    from experiments.evaluation.stage_r1_2_contracts import spec_for_sample

    from atlasos.verification.engine import VerificationEngine

    if workspace is None:
        return None
    spec = spec_for_sample(task.sample_id)
    if spec is None:
        return None
    try:
        result = await VerificationEngine(None).verify_async(
            spec, Path(workspace), baseline_digests=baseline_hashes
        )
    except Exception as error:  # noqa: BLE001 - verification must never break a run
        payload = {
            "spec_id": spec.spec_id,
            "spec_digest": spec.declared_digest,
            "verification_overall": "UNKNOWN",
            "mode": "observe",
            "error": f"{type(error).__name__}: {error}",
        }
        _write_json(Path(run_dir) / "verification_summary.json", payload)
        return payload
    payload = result.as_dict()
    payload["mode"] = "observe"
    payload["dynamic_mode"] = "unavailable"
    summary = {
        "spec_id": result.spec_id,
        "spec_version": result.spec_version,
        "spec_digest": result.spec_digest,
        "artifact_digest": result.artifact_digest,
        "mode": "observe",
        "dynamic_mode": "unavailable",
        "verification_wall_ms": result.verification_wall_ms,
        "verification_cpu_ms": result.verification_cpu_ms,
        **result.category_statuses(),
        "verification_overall": result.overall,
        "failed_checks": [item.check_id for item in result.failures()],
        "unknown_checks": [item.check_id for item in result.unknowns()],
    }
    _persist_verification(Path(run_dir), payload, summary)
    return payload


def _persist_verification(run_dir: Path, payload: dict[str, Any], summary: dict[str, Any]) -> None:
    _write_json(run_dir / "verification.json", payload)
    _write_json(run_dir / "verification_summary.json", summary)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return _jsonable(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_jsonable(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _collect_tool_calls(
    agent_results: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    workers: dict[str, Any] = {}
    for agent, result in agent_results.items():
        result_calls = [
            {
                "agent": agent,
                "tool_name": record.tool_name,
                "input": _jsonable(record.input),
                "output": str(record.output)[:4000],
                "duration_ms": round(float(record.duration) * 1000.0, 3),
            }
            for record in result.tool_calls
        ]
        calls.extend(result_calls)
        workers[agent] = {
            "success": bool(result.success),
            "output": str(result.output or ""),
            "stop_reason": _jsonable(result.stop_reason),
            "tool_calls": len(result_calls),
            "token_usage": _jsonable(result.token_usage),
        }
    return calls, workers


async def _run_mcp_setup(runtime: AtlasToolRuntime, task: BenchmarkTask) -> list[str]:
    if task.mcp_server is None:
        return []
    server_path = MCP_SERVER_DIR / f"{task.mcp_server}.py"
    if not server_path.is_file():
        raise EvaluationEnvironmentError(f"MCP server fixture is missing: {server_path}")
    config = MCPServerConfig(
        name=task.mcp_server,
        transport="stdio",
        command=sys.executable,
        args=[str(server_path)],
        timeout=30.0,
    )
    names = await runtime.add_mcp_server(config)
    return list(names)


async def run_sample(
    task: BenchmarkTask,
    *,
    settings: RunnerSettings,
    repetition_id: int,
    config_path: Path | None = None,
) -> EvalRunResult:
    """Execute exactly one benchmark sample with one fresh sandbox and run identity."""

    load_dotenv(REPO_ROOT / ".env", override=False)
    config_file = Path(config_path or settings.config_path)
    config = load_config(config_file)
    # Fail closed before any I/O: a unit that declares a Context Plane must load a configuration
    # that actually enables it. This is an evaluation-entry constraint, not a runtime default.
    _enforce_context_plane_config(
        _normalized_expectation(settings.context_plane_expectation),
        config=config,
        config_file=config_file,
        handoff_mode=settings.handoff_mode,
    )
    # Same rule for the request layout: the declared and the resolved value must be the same one.
    prompt_layout = _enforce_prompt_layout(
        settings.prompt_layout, config=config, config_file=config_file
    )
    model = settings.model_override or config.provider.model
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    # OpenSandbox metadata labels are limited to 63 characters; the durable run identity is the
    # short label while sample/repetition/config/fixture digests remain in the persisted record.
    run_id = f"eval-{uuid.uuid4().hex[:12]}"
    run_dir = (
        settings.runs_root / settings.benchmark_id / task.sample_id / f"r{repetition_id}-{run_id}"
    ).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    fixture_dir = run_dir / "fixture"
    state_dir = run_dir / "state"
    state_dir.mkdir()
    materialize_fixture(task, fixture_dir)
    baseline_hashes = workspace_hashes(fixture_dir)
    # The commit message is part of the worker-visible workspace (``git log``), so it must not
    # advertise the benchmark sample id or that this is an evaluation fixture.
    stage_git_repo(fixture_dir, label=FIXTURE_COMMIT_LABEL)
    host_hashes_before = workspace_hashes(fixture_dir)

    identity = RunIdentity(
        benchmark_id=settings.benchmark_id,
        dataset_version=settings.dataset_version,
        scorer_version=settings.scorer_version,
        sample_id=task.sample_id,
        repetition_id=repetition_id,
        run_id=run_id,
        git_commit=git_commit(REPO_ROOT),
        git_dirty=git_dirty(REPO_ROOT),
        config_digest=settings.digest(),
        fixture_digest=task.fixture_digest,
        model=model,
        provider=config.provider.name,
        started_at=started_at,
    )

    observations: list[InferenceObservation] = []
    resolved_models: list[str] = []
    #: Bound to the run's context store once it exists. Observes requests, never edits them.
    request_evidence = _RequestEvidence()
    metrics = RuntimeMetrics()
    failure_class: FailureClass | None = None
    failure_reason: str | None = None
    final_output = ""
    agent_success = False
    tool_calls: list[dict[str, Any]] = []
    worker_results: dict[str, Any] = {}
    task_events: list[dict[str, Any]] = []
    team_lifecycle_events: list[Any] = []
    workspace_export: Path | None = None
    patch_digest: str | None = None
    oracle = None
    environment: dict[str, Any] = {
        "python": sys.version,
        "platform": platform.platform(),
        "config_path": str(config_file),
        "sandbox_image": config.sandbox.image,
        "sandbox_provider": config.sandbox.provider,
        "temperature": settings.temperature,
        "max_turns": settings.max_turns,
        "worker_max_turns": settings.worker_max_turns,
        "max_tokens": settings.max_tokens,
        "token_budget": settings.token_budget,
        "budget_mode": settings.budget_mode,
        "max_tool_calls": settings.max_tool_calls,
        "progressive_tools": settings.progressive_tools,
        "variant": settings.variant,
        "capture_evidence": settings.capture_evidence,
        "handoff_mode": settings.handoff_mode,
        "compaction_mode": settings.compaction_mode,
        "tool_error_mode": settings.tool_error_mode,
        "tool_result_mode": settings.tool_result_mode,
        "worker_contract_mode": settings.worker_contract_mode,
        "ownership_mode": settings.ownership_mode,
        "execution_integrity_mode": settings.execution_integrity_mode,
        "verification_mode": settings.verification_mode,
        "delegation_route_mode": settings.delegation_route_mode,
        "delegation_policy_version": settings.delegation_policy_version,
        # Closure-1C: a second, versioned digest over the *effective public task input*. The
        # historical ``identity.fixture_digest`` is unchanged and stays the run's fixture identity;
        # this one additionally covers ``WorkerStep.shared_context``, which is what the model is
        # actually told. Both are recorded so neither can stand in for the other.
        "task_identity": {
            "fixture_digest": identity.fixture_digest,
            "effective_public_task_digest": effective_public_task_digest(task),
            "effective_public_task_digest_version": EFFECTIVE_PUBLIC_TASK_DIGEST_VERSION,
        },
    }
    provider = None
    manager: ProjectSandboxManager | None = None
    engine: AtlasContextEngine | None = None
    idempotency: SQLiteToolIdempotencyStore | None = None
    runtime: AtlasToolRuntime | None = None
    handoff: HandoffRuntime | None = None
    compaction: HistoryCompactionRuntime | None = None
    ingress: ToolResultIngressRuntime | None = None
    recorder: TrajectoryRecorder | None = None
    budget_state: RunBudgetState | None = (
        RunBudgetState(limit_tokens=settings.token_budget)
        if settings.budget_mode == BUDGET_MODE_GUARDED and settings.token_budget > 0
        else None
    )
    binding = None
    workspace_task_id = f"{run_id}-workspace"
    failure_class: FailureClass | None = None
    failure_reason: str | None = None
    task_texts: dict[str, str] = {}
    #: Bound before the ``try`` so a failure before prompt construction still records evidence.
    context_builder: AtlasTaskPromptBuilder | None = None
    context_store: InMemoryContextStore | None = None
    contract_mode = WORKER_CONTRACT_LEGACY
    contract_overhead_chars = 0
    routing_record: dict[str, Any] | None = None
    cleanup_error: BaseException | None = None
    final_result: EvalRunResult | None = None
    try:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise EvaluationEnvironmentError("DEEPSEEK_API_KEY is not configured")
        if config.sandbox.provider == "fake":
            raise EvaluationEnvironmentError("evaluation requires a real Atlas sandbox provider")
        provider = _build_provider(config)
        capabilities = provider.capabilities()
        environment["provider_is_real"] = is_real_sandbox_provider(
            provider, expected_name=config.sandbox.provider
        )
        environment["capabilities_digest"] = canonical_digest(capabilities.model_dump(mode="json"))
        require_capabilities(capabilities)
        health = await provider.health()
        environment["sandbox_health"] = health.status
        environment["sandbox_health_message"] = health.message
        if health.status == "unavailable":
            raise EvaluationEnvironmentError(f"sandbox provider is unavailable: {health.message}")

        provider_name = f"atlas-eval-{uuid.uuid4().hex[:12]}"

        async def factory(**_: Any) -> ModelRecordingAdapter:
            inner = build_deepseek_adapter(model, observations.append)
            return ModelRecordingAdapter(
                inner,
                resolved_models,
                request_evidence if settings.request_evidence else None,
            )

        register_provider_factory(provider_name, factory)

        project_config = _project_config(config)
        recorder = TrajectoryRecorder(
            FilesystemRunStore(run_dir / config.trajectory.store_root),
            run_id,
            agent_name="atlas-evaluation",
            model=model,
        )
        manager = ProjectSandboxManager(
            provider,
            project_config,
            run_id=run_id,
            binding_store=SQLiteSandboxBindingStore(state_dir / "sandbox_bindings.db"),
            ownership_root=state_dir,
            trajectory_recorder=recorder,
        )
        idempotency = SQLiteToolIdempotencyStore(state_dir / "idempotency.db")
        await idempotency.setup()
        ledger = SQLiteActionLedgerStore(state_dir / "ledger.db")
        sandbox_tools = SandboxWorkspaceTools(
            manager,
            workspace_task_id,
            provider,
            tool_error_mode=settings.tool_error_mode,
        )
        # Stage R1-1: the ownership manifest is built from benchmark metadata before any worker
        # runs, and the enforcer is installed as M3's external policy. In ``off`` both stay None,
        # so the gate is byte-identical to the pre-R1-1 configuration.
        ownership_mode = (
            settings.ownership_mode
            if settings.ownership_mode in OWNERSHIP_MODES
            else OWNERSHIP_MODE_OFF
        )
        integrity_mode = (
            settings.execution_integrity_mode
            if settings.execution_integrity_mode in EXECUTION_INTEGRITY_MODES
            else EXECUTION_INTEGRITY_OFF
        )
        # Stage R1-2A: ``off`` runs no verifier at all; ``observe`` records findings only.
        verification_mode = (
            settings.verification_mode
            if settings.verification_mode in VERIFICATION_MODES
            else "off"
        )
        # Stage R1-2H: unknown values fall back to the isolated oracle, never to Host execution.
        oracle_mode = (
            settings.oracle_mode if settings.oracle_mode in ORACLE_MODES else ORACLE_MODE_SANDBOX
        )
        ownership_manifest = None
        ownership_policy = None
        external_policy = None
        payload_corpus = (
            ToolPayloadCorpus.for_run(run_dir)
            if integrity_mode == EXECUTION_INTEGRITY_RECORD
            else None
        )
        if ownership_mode != OWNERSHIP_MODE_OFF:
            try:
                ownership_manifest = manifest_for_task(
                    task,
                    run_id=run_id,
                    workspace_root=project_config.workspace_root,
                )
            except OwnershipManifestError as error:
                # An ambiguous Host declaration is a configuration defect, not a model failure.
                raise EvaluationEnvironmentError(f"ownership manifest: {error}") from error
            external_policy, ownership_policy = build_ownership_enforcer(
                ownership_manifest, mode=ownership_mode
            )
            environment["ownership"] = manifest_public_summary(ownership_manifest)
            environment["ownership"]["declarations"] = [
                {
                    "worker": worker,
                    "task": ownership_manifest.task_of(worker),
                    "paths": list(ownership_manifest.declared_paths(worker)),
                }
                for worker in ownership_manifest.workers()
            ]
        # Stage R0: the delegation route decides whether the existing A0 (single worker) or A1
        # (candidate DAG) path runs. The routing decision is recorded either way; only
        # ``selective`` changes execution, and it changes nothing else.
        route_mode = (
            settings.delegation_route_mode
            if settings.delegation_route_mode in DELEGATION_ROUTE_MODES
            else DELEGATION_ROUTE_OFF
        )
        if route_mode != DELEGATION_ROUTE_OFF:
            routed = plan_route(
                task,
                capabilities_from_catalog(WorkerCatalog()),
                routing_policy(settings.delegation_policy_version),
            )
            routing_record = routed.as_dict()
            routing_record["mode"] = route_mode
            routing_record["candidate_worker_count"] = len(task.workers)
            if route_mode == DELEGATION_ROUTE_SELECTIVE and routed.is_direct:
                # The executed plan is the A0 plan, so the task is graded exactly like an A0
                # sample (single-worker mode, same oracle checks on artifacts and hidden tests).
                task = replace(
                    task,
                    mode="coding",
                    workers=a0_steps(task),
                    tool_expectations={
                        **task.tool_expectations,
                        "distinct_workers": 1,
                    },
                )
                routing_record["executed_worker_count"] = 1
            else:
                routing_record["executed_worker_count"] = len(task.workers)
            routing_record["route_digest"] = decision_digest(routed.decision)
        title_by_id = {step.task_id: f"{task.sample_id}-{step.task_id}" for step in task.workers}
        # Stage G1: one handoff runtime per run. The trusted task identity of every consumer is
        # the runtime task id the worker was created with, never a process-global variable.
        handoff = HandoffRuntime.create(
            run_dir=run_dir,
            run_id=run_id,
            nodes=[
                HandoffNode(
                    key=step.task_id,
                    title=title_by_id[step.task_id],
                    depends_on=tuple(step.depends_on),
                )
                for step in task.workers
            ],
            consumer_keys={f"{run_id}-{step.task_id}": step.task_id for step in task.workers},
            mode="dual" if settings.handoff_mode == "dual" else "legacy",
        )
        # Stage G2b-1: one compaction runtime per run. Created in both arms so the tool registry
        # and therefore the model-visible tool schema are identical in C0 and C1.
        compaction_mode = (
            settings.compaction_mode
            if settings.compaction_mode in COMPACTION_MODES
            else COMPACTION_OFF
        )
        compaction = HistoryCompactionRuntime.create(
            run_dir=run_dir, run_id=run_id, mode=compaction_mode
        )
        # Stage G3-2: ingress offloading reuses the same HistoryStore and the same
        # ``history.read`` tool, so the model-visible tool surface is identical in both arms.
        tool_result_mode = (
            settings.tool_result_mode
            if settings.tool_result_mode in TOOL_RESULT_MODES
            else TOOL_RESULT_OFF
        )
        ingress = ToolResultIngressRuntime.wrap(
            store=compaction.store,
            mode=tool_result_mode,
        )
        runtime_type = AtlasToolRuntime if settings.progressive_tools else AllToolsAtlasRuntime
        runtime = runtime_type(
            run_id,
            state_path=run_dir / "tool_state.json",
            conversation_path=run_dir / "tool_conversations.json",
            external_tools=(*handoff.external_tools(), *compaction.external_tools()),
            search_backend=config.tools.search_backend,
            embedding_model=config.tools.embedding_model,
            max_results=config.tools.max_results,
            action_policy=_coding_policy(),
            action_catalog=_EVAL_CATALOG,
            action_external_policy=external_policy,
            action_ledger=ledger,
            idempotency_store=idempotency,
            sandbox_tools=sandbox_tools,
            trajectory_recorder=recorder,
            runtime_instance_id=f"eval-{run_id}",
            tool_result_offloader=ingress,
            execution_integrity_mode=integrity_mode,
            tool_payload_corpus=payload_corpus,
            external_denial_retry_safe=ownership_mode != OWNERSHIP_MODE_OFF,
        )
        await _run_mcp_setup(runtime, task)

        parent = ExecutionContext(
            principal="user:evaluation",
            tenant_scope="atlas-evaluation",
            classification="internal",
            attributes={
                "run_id": run_id,
                "sample_id": task.sample_id,
                "benchmark_id": settings.benchmark_id,
            },
        )
        worker_factory = ScopedWorkerFactory(
            model=model,
            provider=provider_name,
            store=WorkerStore(run_dir / "workers.json"),
            trajectory_recorder=recorder,
        )
        environment_note = _environment_note(project_config.workspace_root)
        worker_by_task: dict[str, Any] = {}
        for step in task.workers:
            # In the shared-prefix layout the assignment is the worker's own objective only: the
            # environment note moves to the user turn's public block, because a per-run constant in
            # a per-task system prompt is exactly what stops two workers from sharing a prefix. The
            # information is relocated, never removed.
            assignment_text = (
                step.prompt
                if prompt_layout == PROMPT_LAYOUT_SHARED_PREFIX
                else f"{environment_note}\n\n{step.prompt}"
            )
            worker = worker_factory.create(
                parent_context=parent,
                parent_agent="atlas-evaluation",
                assignment=WorkerAssignment(
                    profile_id="coding_worker",
                    task=assignment_text,
                ),
                worker_id=f"{run_id}-{step.task_id}",
                task_id=f"{run_id}-{step.task_id}",
                prompt_layout=prompt_layout,
            )
            worker_by_task[step.task_id] = worker

        provision_started = time.perf_counter()
        first_worker = worker_by_task[task.workers[0].task_id]
        binding = await manager.ensure_binding(
            task_id=workspace_task_id,
            source_repo=str(fixture_dir),
            context=first_worker.execution_context,
            worker_id=first_worker.worker_id,
        )
        metrics.provision_latency_ms = round((time.perf_counter() - provision_started) * 1000.0, 3)
        metrics.sandbox_created = manager.create_count > 0
        # Stage G3-4a: drop the provisioning staging bundle and prove that no private artifact
        # name is reachable from inside the worker sandbox.
        isolation = await enforce_evaluation_isolation(provider, binding.sandbox_handle)
        metrics.extra["integrity"] = isolation.as_dict()
        if not isolation.passed:
            # No worker turn may run in an environment that still exposes private artifacts.
            raise EvaluationEnvironmentError(
                "evaluation isolation check failed: " + "; ".join(isolation.failures)
            )

        def on_progress(event: Any) -> None:
            if settings.capture_evidence:
                # Stage R0.5: task/agent identity is recorded for *every* event so a start can be
                # paired with its own completion by runtime task id. This is measurement wiring
                # only: it observes the scheduler, it never influences it.
                recorded = {
                    "ts": datetime.now(UTC).isoformat(),
                    "type": str(event.type),
                    "task": str(getattr(event, "task", "") or ""),
                    "agent": str(getattr(event, "agent", "") or ""),
                }
                data = getattr(event, "data", None)
                if data is not None:
                    recorded["title"] = getattr(data, "title", None)
                    recorded["assignee"] = getattr(data, "assignee", None)
                if event.type == "task_complete":
                    recorded["task_status"] = str(getattr(event, "status", "") or "")
                task_events.append(recorded)
            if event.type == "task_start" and getattr(event, "data", None) is not None:
                title = getattr(event.data, "title", None)
                for task_id, expected_title in title_by_id.items():
                    if title == expected_title:
                        set_current_task_id(task_id)
                        break
            elif event.type == "task_complete":
                set_current_task_id(None)

        # Stage G3-3: only the worker's task turn changes. The assignment (system prompt), the
        # tool list, the DAG and the budget are identical in both contract modes.
        contract_mode = (
            settings.worker_contract_mode
            if settings.worker_contract_mode in WORKER_CONTRACT_MODES
            else WORKER_CONTRACT_LEGACY
        )
        task_parts = {
            step.task_id: _task_turn_parts(
                step, steps=task.workers, sample_id=task.sample_id, mode=contract_mode
            )
            for step in task.workers
        }
        # The AnyCode task spec carries the same effective facts in both layouts: identical public
        # block, identical objective, identical constraints and identical declared dependencies.
        task_texts = {
            task_id: (f"{shared}\n\n{body}" if shared else body)
            for task_id, (shared, body) in task_parts.items()
        }
        contract_overhead_chars = sum(
            _contract_overhead_chars(step, task_texts[step.task_id]) for step in task.workers
        )
        task_specs = [
            TaskSpec(
                title=title_by_id[step.task_id],
                description=task_texts[step.task_id],
                assignee=worker_by_task[step.task_id].worker_name,
                depends_on=[title_by_id[item] for item in step.depends_on],
            )
            for step in task.workers
        ]
        context_plane = _context_prompt_builder(
            config=config,
            task=task,
            title_by_id=title_by_id,
            task_texts=task_texts,
            worker_by_task=worker_by_task,
            prompt_layout=prompt_layout,
            task_bodies={task_id: body for task_id, (_, body) in task_parts.items()},
            shared_context_by_task={
                task_id: (f"{environment_note}\n\n{shared}" if shared else environment_note)
                for task_id, (shared, _) in task_parts.items()
            },
        )
        context_builder, context_store = (
            context_plane if context_plane is not None else (None, None)
        )
        if context_store is not None:
            request_evidence.bind_store(context_store)
        if context_builder is not None and settings.handoff_mode == "dual":
            raise EvaluationEnvironmentError(
                "context.policy='task_scoped' and handoff_mode='dual' both project dependency "
                "outputs into the task prompt; enable exactly one of them"
            )
        expectation = _normalized_expectation(settings.context_plane_expectation)
        resolved_context = {
            "config_path": str(config_file),
            "policy": config.context.policy,
            "renderer": config.context.renderer,
            "transform": config.context.transform.name,
            "placement_policy": config.placement.policy,
            "handoff_mode": settings.handoff_mode,
            "budget_mode": settings.budget_mode,
            "token_budget": settings.token_budget,
            "tool_error_mode": settings.tool_error_mode,
            "worker_contract_mode": settings.worker_contract_mode,
            "worker_count": len(task.workers),
            "worker_ids": [worker_by_task[step.task_id].worker_name for step in task.workers],
            "builder": None if context_builder is None else type(context_builder).__name__,
            "prompt_builder_source": (
                "atlas_context_plane" if context_builder is not None else "handoff_legacy"
            ),
            "expectation": expectation,
            "prompt_layout": prompt_layout,
            # The persisted worker hash is the legacy render and stays the resume contract; the
            # effective hash is the system prompt the agent actually ran with. They are recorded
            # side by side so a shared-prefix run cannot be read as a legacy one.
            "workers": {
                step.task_id: {
                    "prompt_layout": worker_by_task[step.task_id].record.prompt_layout,
                    "original_worker_context_hash": (
                        worker_by_task[step.task_id].record.rendered_context_hash
                    ),
                    "effective_system_prompt_hash": (
                        worker_by_task[step.task_id].record.effective_system_prompt_hash
                    ),
                }
                for step in task.workers
            },
        }
        environment["context_resolved"] = resolved_context
        environment["request_evidence"] = bool(settings.request_evidence)
        _enforce_context_plane(expectation, resolved_context)
        engine = AtlasContextEngine(
            OrchestratorConfig(on_progress=on_progress),
            tool_runtime=runtime,
            prompt_builder=context_builder or handoff.build_prompt,
            budget_state=budget_state,
            compaction=compaction,
        )
        agent_configs = [
            worker_by_task[step.task_id].agent_config.model_copy(
                update={
                    "provider": provider_name,
                    "max_turns": effective_agent_max_turns(
                        step_max_turns=step.max_turns,
                        settings_max_turns=settings.max_turns,
                        worker_max_turns=settings.worker_max_turns,
                    ),
                    "max_tokens": settings.max_tokens,
                    "temperature": settings.temperature,
                    "provider_resilience": evaluation_resilience(),
                    "tool_security": ToolSecurityPolicy(
                        workspace_root=project_config.workspace_root
                    ),
                }
            )
            for step in task.workers
        ]
        team = engine.create_team(
            "evaluation",
            TeamConfig(name="evaluation", agents=agent_configs),
        )
        # Stage G3-3: only the worker's task turn changes. The assignment (system prompt), the
        # tool list, the DAG and the budget are identical in both contract modes.
        contract_mode = (
            settings.worker_contract_mode
            if settings.worker_contract_mode in WORKER_CONTRACT_MODES
            else WORKER_CONTRACT_LEGACY
        )
        task_texts = {
            step.task_id: _task_turn(
                step, steps=task.workers, sample_id=task.sample_id, mode=contract_mode
            )
            for step in task.workers
        }
        contract_overhead_chars = sum(
            _contract_overhead_chars(step, task_texts[step.task_id]) for step in task.workers
        )
        agent_started = time.perf_counter()
        result = await asyncio.wait_for(
            engine.run_tasks(team, task_specs),
            timeout=settings.timeout_seconds,
        )
        environment["agent_latency_ms"] = round((time.perf_counter() - agent_started) * 1000.0, 3)
        agent_success = bool(result.success)
        team_lifecycle_events = _jsonable(getattr(result, "lifecycle_events", []))
        tool_calls, worker_results = _collect_tool_calls(result.agent_results)
        final_output = "\n".join(
            str(item.get("output", "")) for item in worker_results.values() if item.get("output")
        )

        patch = await export_patch(
            provider,
            binding.sandbox_handle,
            base_commit=binding.source_commit,
            workspace_root=binding.workspace_root,
        )
        patch_digest = patch.patch_digest
        (run_dir / "workspace.patch").write_text(patch.patch_text, encoding="utf-8")
        workspace_export = run_dir / "workspace"
        await export_workspace(
            provider,
            binding.sandbox_handle,
            destination=workspace_export,
            workspace_root=binding.workspace_root,
        )
    except TimeoutError as error:
        failure_class = "TIMEOUT"
        failure_reason = f"sample exceeded {settings.timeout_seconds}s"
        del error
    except BaseException as error:  # noqa: BLE001 - every failure is classified and persisted
        if not isinstance(error, Exception):
            raise
        failure_class = _classify_exception(error)
        failure_reason = f"{type(error).__name__}: {error}"
    finally:
        if runtime is not None:
            with contextlib.suppress(BaseException):
                await runtime.close()
        if engine is not None:
            with contextlib.suppress(Exception):
                await engine.close()
        if idempotency is not None:
            with contextlib.suppress(Exception):
                await idempotency.teardown()
        if manager is not None:
            try:
                if binding is not None:
                    if settings.retain_sandbox_on_failure and failure_class is not None:
                        metrics.sandbox_destroyed = False
                    else:
                        managed = await manager.close(workspace_task_id)
                        metrics.sandbox_destroyed = bool(managed)
                        if not managed:
                            cleanup_error = RuntimeError("provider did not confirm destroy")
                else:
                    metrics.sandbox_destroyed = True
            except BaseException as error:
                cleanup_error = error
                metrics.sandbox_destroyed = False
            if cleanup_error is not None and failure_class is None:
                failure_class = "CLEANUP_FAILURE"
                failure_reason = f"sandbox cleanup failed: {cleanup_error}"
            elif cleanup_error is not None and failure_reason is not None:
                failure_reason = f"{failure_reason}; sandbox cleanup also failed: {cleanup_error}"

        host_hashes_after = workspace_hashes(fixture_dir)
        host_unchanged = host_hashes_before == host_hashes_after
        if not host_unchanged and failure_class is None:
            failure_class = "HOST_ISOLATION_VIOLATION"
            failure_reason = "Host fixture changed during the run"
        if runtime is not None:
            with contextlib.suppress(Exception):
                tool_metrics = runtime.metrics()
                environment["tool_runtime_metrics"] = (
                    tool_metrics.model_dump(mode="json")
                    if hasattr(tool_metrics, "model_dump")
                    else str(tool_metrics)
                )
        aggregate = _aggregate_observations(observations)
        metrics.model_turns = len(observations)
        metrics.tool_calls = len(tool_calls)
        metrics.prompt_tokens = int(aggregate["prompt_tokens"]) if observations else None
        metrics.completion_tokens = int(aggregate["completion_tokens"]) if observations else None
        metrics.cache_hit_tokens = (
            int(aggregate["cache_hit_tokens"])
            if aggregate["cache_hit_tokens"] is not None
            else None
        )
        metrics.cache_miss_tokens = (
            int(aggregate["cache_miss_tokens"])
            if aggregate["cache_miss_tokens"] is not None
            else None
        )
        metrics.estimated_cost_usd = _estimate_cost(observations, config.pricing)
        metrics.extra["cache_metrics_complete"] = bool(aggregate["cache_metrics_complete"])
        # ``COMPLETE`` / ``PARTIAL`` / ``NONE`` / ``NO_OBSERVATIONS``. A zero the provider never
        # reported must never be readable as an observed 0 % hit rate.
        metrics.extra["cache_split_status"] = aggregate["cache_split_status"]
        metrics.extra["cache_hit_ratio"] = aggregate["cache_hit_ratio"]
        metrics.extra["prompt_token_consistency"] = aggregate["prompt_token_consistency"]
        metrics.extra["inference_latency_ms"] = aggregate["inference_latency_ms"]
        environment["resolved_models"] = sorted(set(resolved_models))
        environment["requested_model"] = model
        environment["pricing_basis"] = (
            "DeepSeek Flash peak rates snapshot 2026-11-06; deepseek-chat resolves to "
            "deepseek-flash; upper-bound estimate"
        )
        metrics.extra["provider_name"] = config.sandbox.provider
        metrics.extra["sandbox_health"] = environment.get("sandbox_health")
        if handoff is not None:
            metrics.extra["handoff"] = handoff.metrics.as_dict()
        if compaction is not None:
            metrics.extra["compaction"] = compaction.metrics_dict()
        if ingress is not None:
            metrics.extra["tool_result"] = ingress.metrics_dict()
        # Stage R1-1: ownership evidence is measurement only. It never feeds the oracle.
        if ownership_policy is not None:
            metrics.extra["ownership"] = ownership_policy.evidence_snapshot()
        if recorder is not None and integrity_mode == EXECUTION_INTEGRITY_RECORD:
            with contextlib.suppress(Exception):
                recorder.record_run_terminal(
                    status=failure_class or "SUCCESS",
                    task_count=len(task.workers),
                    final_output_digest=canonical_digest({"output": final_output}),
                    final_output_size=len(final_output.encode("utf-8")),
                )
        if integrity_mode == EXECUTION_INTEGRITY_RECORD or ownership_mode != OWNERSHIP_MODE_OFF:
            trace = summarize_trace(
                run_dir,
                recorder_failures=recorder.recording_failures if recorder is not None else 0,
            )
            trace["result_comparison"] = compare_with_result(trace, tool_calls)
            trace["integrity_mode"] = integrity_mode
            trace["ownership_mode"] = ownership_mode
            metrics.extra["trace"] = trace
            environment["trace_status"] = trace["status"]
        task_text_digest = hashlib.sha256(
            "\n".join(f"{step_id}:{text}" for step_id, text in sorted(task_texts.items())).encode(
                "utf-8"
            )
        ).hexdigest()
        if routing_record is not None:
            metrics.extra["routing"] = routing_record
        metrics.extra["worker_contract"] = {
            "mode": contract_mode,
            "overhead_chars": contract_overhead_chars,
            "task_text_digest": task_text_digest,
            "workers": [
                {
                    "task_id": step.task_id,
                    "task_text_chars": len(task_texts.get(step.task_id, step.prompt)),
                    "shared_context_chars": len(getattr(step, "shared_context", "") or ""),
                    "overhead_chars": _contract_overhead_chars(
                        step, task_texts.get(step.task_id, step.prompt)
                    ),
                }
                for step in task.workers
            ],
        }
        # Final-4: read-only evidence that the Context Plane really built each task prompt.
        if context_builder is not None and context_store is not None:
            metrics.extra["context"] = _context_evidence(
                builder=context_builder,
                store=context_store,
                task=task,
                task_texts=task_texts,
                observations=observations,
                request_records=request_evidence.records if settings.request_evidence else None,
                workers=worker_by_task,
            )
        else:
            metrics.extra["context"] = {
                "enabled": False,
                "prompt_builder_source": "handoff_legacy",
                "builder": None,
                "builder_invocations": 0,
            }
        metrics.extra["request_evidence"] = (
            request_evidence.as_dict()
            if settings.request_evidence
            else {"enabled": False, "reason": "request_evidence disabled for this unit"}
        )
        # ``RunMetrics.as_dict`` does not serialise ``extra``, so the Context Plane evidence needs
        # its own durable file next to ``result.json``. Writing it here keeps one artifact per run
        # that a later reader can audit without re-deriving anything.
        with contextlib.suppress(Exception):
            _write_json(
                run_dir / "context.json",
                {
                    "resolved": environment.get("context_resolved"),
                    "evidence": metrics.extra.get("context"),
                    "handoff": metrics.extra.get("handoff"),
                    "routing": metrics.extra.get("routing"),
                    "budget": metrics.extra.get("budget"),
                    "request_evidence": metrics.extra.get("request_evidence"),
                },
            )
        call_records = _provider_call_records(
            observations, run_id=run_id, model=model, pricing=config.pricing
        )
        (run_dir / "provider_calls.jsonl").write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in call_records),
            encoding="utf-8",
        )
        metrics.extra["provider_calls"] = len(call_records)
        metrics.tool_search_calls = sum(
            1 for item in tool_calls if item["tool_name"] in ("tool.search", "tool_search")
        )
        metrics.dispatch_calls = sum(
            1 for item in tool_calls if item["tool_name"] == "dispatch_action"
        )
        unknown_tools = [
            item
            for item in tool_calls
            if item["tool_name"] not in _SANDBOX_TOOLS
            and item["tool_name"] not in _CONTROL_TOOLS
            and not str(item["tool_name"]).startswith("mcp_")
        ]
        metrics.extra["unexpected_native_tools"] = len(unknown_tools)
        metrics.host_isolation_violations = 0 if host_unchanged else 1
        metrics.undisclosed_tool_rejections = sum(
            str(item.get("output", "")).count("tool_not_disclosed") for item in tool_calls
        )
        metrics.worker_selection = sorted(worker_results)
        budget_snapshot = budget_state.finalize() if budget_state is not None else None
        if budget_snapshot is not None:
            metrics.extra["budget"] = budget_snapshot
            if failure_class is None and budget_state is not None:
                rejections = int(budget_snapshot["admission_rejections"])
                if rejections:
                    failure_class = "BUDGET_EXCEEDED"
                    failure_reason = (
                        f"run budget guard rejected {rejections} LLM request(s): "
                        f"{budget_snapshot['last_rejection']}"
                    )
        if (
            failure_class is None
            and metrics.prompt_tokens is not None
            and metrics.completion_tokens is not None
            and settings.token_budget > 0
            and metrics.prompt_tokens + metrics.completion_tokens > settings.token_budget
        ):
            failure_class = "BUDGET_EXCEEDED"
            failure_reason = (
                f"token usage exceeded budget: "
                f"{metrics.prompt_tokens + metrics.completion_tokens} > {settings.token_budget}"
            )
        if failure_class is None and metrics.tool_calls > settings.max_tool_calls:
            failure_class = "BUDGET_EXCEEDED"
            failure_reason = (
                f"tool call budget exceeded: {metrics.tool_calls} > {settings.max_tool_calls}"
            )

        if workspace_export is not None and workspace_export.is_dir():
            # Stage R1-2H: hidden tests run in their own disposable sandbox by default. The Host
            # never imports the exported deliverable, and the worker sandbox is already destroyed.
            if oracle_mode == ORACLE_MODE_SANDBOX and provider is not None:
                oracle_outcome = await evaluate_oracle_isolated(
                    task,
                    workspace=workspace_export,
                    baseline_hashes=baseline_hashes,
                    tool_calls=tool_calls,
                    worker_results=worker_results,
                    run_dir=run_dir,
                    provider=provider,
                    config=OracleSandboxConfig(image=config.sandbox.image),
                    host_fixture_unchanged=host_unchanged,
                    run_id=run_id,
                )
            elif oracle_mode == ORACLE_MODE_SANDBOX:
                oracle_outcome = OracleOutcome(
                    passed=False,
                    checks={"oracle_sandbox_available": False},
                    failure_reasons=["no sandbox provider was available for the isolated oracle"],
                    execution_mode=ORACLE_MODE_SANDBOX,
                    failure_kind="SANDBOX_PROVISIONING_FAILURE",
                    environment_failure=True,
                )
            else:
                oracle_outcome = evaluate_oracle(
                    task,
                    workspace=workspace_export,
                    baseline_hashes=baseline_hashes,
                    tool_calls=tool_calls,
                    worker_results=worker_results,
                    run_dir=run_dir,
                    host_fixture_unchanged=host_unchanged,
                )
        else:
            oracle_outcome = OracleOutcome(
                passed=False,
                checks={"workspace_exported": False},
                failure_reasons=["sandbox workspace was not exported"],
                environment_failure=True,
                failure_kind="WORKSPACE_NOT_EXPORTED",
            )
        oracle = oracle_outcome
        metrics.extra["oracle"] = {
            "mode": oracle.execution_mode,
            "failure_kind": oracle.failure_kind,
            "environment_failure": oracle.environment_failure,
            "cleanup_failure": oracle.cleanup_failure,
            "timings_ms": oracle.timings_ms,
            "digests": oracle.digests,
        }
        # Stage R1-2A: verification is measurement only. It runs after the export, reads the
        # snapshot, and never touches the oracle verdict or the run's failure classification.
        if verification_mode == "observe":
            verification = await _observe_verification(
                task=task,
                workspace=workspace_export,
                baseline_hashes=baseline_hashes,
                run_dir=run_dir,
            )
            if verification is not None:
                metrics.extra["verification"] = {
                    key: value
                    for key, value in verification.items()
                    if key in {"spec_id", "spec_digest", "verification_overall", "artifact_digest"}
                    or key.endswith("_status")
                    or key.endswith("_validity")
                    or key == "deliverable_presence"
                }
        if failure_class is None and (oracle_class := oracle_failure_class(oracle)) is not None:
            # An oracle environment defect is not a model failure: a missing pytest session, a
            # timeout or a dead sandbox must never be scored as a wrong deliverable.
            failure_class = oracle_class
            failure_reason = "; ".join(oracle.failure_reasons)[:2000] or "oracle failed"
        if failure_class is None:
            failure_class = "TASK_SUCCESS"
        success = failure_class == "TASK_SUCCESS" and oracle.passed

        finished_at = datetime.now(UTC)
        metrics.latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        result = EvalRunResult(
            identity=identity,
            task=task,
            success=success,
            failure_class=failure_class,
            failure_reason=failure_reason,
            final_output=final_output,
            agent_success=agent_success,
            tool_calls=tool_calls,
            worker_results=worker_results,
            oracle=oracle,
            metrics=metrics,
            run_dir=str(run_dir),
            fixture_dir=str(fixture_dir),
            workspace_export=str(workspace_export) if workspace_export else None,
            patch_digest=patch_digest,
            started_at=started_at,
            finished_at=finished_at,
            environment=environment,
            safety={
                "host_fixture_unchanged": host_unchanged,
                "sandbox_destroyed": metrics.sandbox_destroyed,
                "cleanup_error": str(cleanup_error) if cleanup_error else None,
                "unexpected_native_tools": len(unknown_tools),
                "real_sandbox_provider": environment.get("provider_is_real", False),
            },
        )
        _write_json(run_dir / "result.json", result.as_dict())
        _write_json(
            run_dir / "run.json",
            {
                "identity": identity.as_dict(),
                "task": task.public_metadata(),
                "environment": environment,
                "config_digest": settings.digest(),
                "fixture_digest": task.fixture_digest,
                "workspace_digest": workspace_digest(fixture_dir),
            },
        )
        if settings.capture_evidence:
            _write_json(
                run_dir / "stage_e_evidence.json",
                {
                    "identity": identity.as_dict(),
                    "task": {
                        **task.public_metadata(),
                        "workers": [
                            {
                                "task_id": step.task_id,
                                "title": step.title,
                                "depends_on": list(step.depends_on),
                            }
                            for step in task.workers
                        ],
                    },
                    "task_events": task_events,
                    "anycode_lifecycle_events": team_lifecycle_events,
                    "worker_results": worker_results,
                    "tool_calls": tool_calls,
                    "handoff": metrics.extra.get("handoff"),
                },
            )
        ledger_path = settings.runs_root / settings.benchmark_id / "runs.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result.as_dict(), sort_keys=True) + "\n")
        final_result = result

    if final_result is None:
        raise RuntimeError("evaluation run finished without a persisted result")
    return final_result
