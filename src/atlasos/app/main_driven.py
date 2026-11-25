"""Main-driven goal session: one Main agent, one delegation plane, one run directory.

This is the public entry point for stage M. It turns a natural-language goal into:

```text
Main Agent (AnyCode AgentRunner, one task)
  └─ delegate_tasks            ← a real tool call inside the Main's own loop
      ├─ DelegationRouter      ← frozen R0.1.0 admission over runtime-authored evidence
      ├─ child subworkers      ← ScopedWorkerFactory + own semaphore + own conversation
      └─ bounded summary + ref ← HandoffStore, read back with delegate.read
  └─ loop continues            ← Main integrates, verifies, answers
```

Nothing here is a second orchestrator: the Main is executed by `AtlasContextEngine.run_tasks`, the
same wave path the static-DAG entry point uses, and every tool call passes the same M3 gate, M4
ledger and R1-1 ownership policy.

Deliberate composition choices, stated as facts rather than aspirations:

* ``OrchestratorConfig(max_concurrency=1)``: the Main occupies the only agent-pool slot. Children
  never request one, so child concurrency cannot deadlock against the pool. This is the property
  stage M is required to prove, so it is the default here.
* ``tools.mode`` must be ``progressive`` and ``context.policy`` must be the upstream default: the
  goal path has no dependency edges, so a task-scoped context plane would be configured but unused.
  It refuses instead of reporting a policy it does not apply.
* Children are read-only unless the Host grants a profile writable paths. The grant is Host-owned;
  the Main cannot widen it by asking.
* ``worker_profiles``, ``worker_max_turns`` and ``worker_exposure`` are Host-owned ablation controls
  and every one of them defaults to a no-op. The first restricts what this run may construct, the
  second replaces a profile turn cap that a run-level limit cannot raise, and the third appends a
  derived capability disclosure to the ``delegate_tasks`` description. None of them is an
  authorization source: a disclosed capability is still refused by the router and the ownership
  policy when it is not granted.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anycode import AgentConfig, OrchestratorConfig, TaskSpec, TeamConfig
from anycode.identity.context import ExecutionContext
from anycode.plugins.registry import register_provider_factory
from anycode.runstore.store import FilesystemRunStore
from anycode.security.redaction import safe_exception_message
from anycode.tools.idempotency import SQLiteToolIdempotencyStore
from anycode.types import ToolSecurityPolicy
from dotenv import load_dotenv

from atlasos.actions import ActionCatalog, ActionPolicy, ActionRule
from atlasos.actions.catalog import ActionMapping
from atlasos.app.goal_sandbox import GoalSandbox, GoalSandboxOptions
from atlasos.app.goal_verification import GoalAcceptance, verify_goal
from atlasos.app.live_preflight import READY, run_live_preflight
from atlasos.app.public_tests import (
    PUBLIC_TEST_ACTION,
    PUBLIC_TEST_TOOL,
    PublicTestContract,
    PublicTestRunner,
)
from atlasos.budget import RunBudgetState
from atlasos.config import AtlasConfig, load_config
from atlasos.delegation import (
    DELEGATE_READ_TOOL,
    DelegatedResultIndex,
    DelegatedResultReader,
    DelegateTasksTool,
    DelegationConfig,
    DelegationLimits,
    DelegationService,
    build_delegation_ownership,
    load_delegation_config,
)
from atlasos.delegation.capability_card import (
    REFERENCE_SURFACE,
    WorkerCapabilityCard,
    build_card,
)
from atlasos.delegation.exposure import (
    LEVELS as EXPOSURE_LEVELS,
)
from atlasos.delegation.exposure import (
    exposure_evidence,
    render_exposure,
)
from atlasos.delegation.handoff import handoffs_root, index_path
from atlasos.delegation.tool import DEFAULT_DESCRIPTION as DELEGATE_TASK_DESCRIPTION
from atlasos.delegation.tool import STAGE_M_DELEGATE_TOOL
from atlasos.delegation.usage import UsageAttributionHook
from atlasos.handoff import HandoffStore
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.sandbox.tools import TOOL_ERROR_MODE_CORRECTED, TOOL_ERROR_MODES
from atlasos.tools import AtlasToolRuntime
from atlasos.tools.conversation import reset_current_task_id, set_current_task_id
from atlasos.trajectory.payloads import (
    EXECUTION_INTEGRITY_OFF,
    EXECUTION_INTEGRITY_RECORD,
    PRIVATE_PAYLOAD_FILE,
    PRIVATE_PAYLOAD_ROOT,
    ToolPayloadCorpus,
    read_corpus,
)
from atlasos.trajectory.recorder import TrajectoryRecorder
from atlasos.workers import WorkerCatalog
from atlasos.workers.factory import DEFAULT_AVAILABLE_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO_ROOT / "experiments" / "configs" / "v2-uat.yaml"
MAIN_AGENT_NAME = "atlas-main"
MAIN_PROFILE_ID = "coding_worker"
#: The workspace root inside a project sandbox (AnyCode's M5 contract fixes this value).
SANDBOX_WORKSPACE_ROOT = "/workspace/project"

#: Stable Main prompt. No timestamp, run id, path or metric may enter it (stable-prefix invariant).
MAIN_SYSTEM_PROMPT = (
    "ATLAS MAIN AGENT\n"
    "You own the goal end to end: plan, execute, integrate, and verify the result yourself.\n"
    "\n"
    "DELEGATION CONTRACT\n"
    "- Use delegate_tasks to offload independent subtasks. The runtime, not you, decides\n"
    "  whether a request is admitted.\n"
    "- A subworker sees only the objective and the background you pass. It never sees this\n"
    "  conversation, your file reads, or your tool logs.\n"
    "- Subworkers are read-only unless the host granted their profile writable paths. They\n"
    "  cannot delegate further and cannot read each other's results.\n"
    "- Every subworker returns a bounded summary plus a ref_id. Use delegate.read to page\n"
    "  through the full result when the summary is not enough.\n"
    "- If a request is refused (route='direct'), do the work yourself; the reason says why.\n"
    "- Integrate and verify subworker output yourself. Never report work you have not checked.\n"
    "\n"
    "WORKSPACE\n"
    "- All file tools operate inside the workspace root. Use paths relative to it.\n"
    "- A shell command that runs an interpreter (python, pytest, node, a script) is refused by\n"
    "  the ownership policy. Read the refusal, then choose another action; a refusal is data.\n"
    "- If a public test tool is available, it runs the project's authorized suite inside the\n"
    "  sandbox with a host-fixed command. It is not the acceptance oracle: the harness verifies\n"
    "  the deliverable independently after you stop. Never claim a test result you did not see.\n"
)


#: Version label of :data:`MAIN_SYSTEM_PROMPT`. A run's prompt is part of its identity: the same
#: goal executed under a different prompt is a different experiment, so the label is recorded.
MAIN_PROMPT_V1_VERSION = "main-v1.0"


@dataclass(frozen=True)
class MainPromptSpec:
    """One selectable Main system prompt: a version label plus the exact bytes it injects.

    The bytes are the whole contract. A caller that wants a different prompt passes a spec through
    ``GoalRunOptions``; no module-level constant is reassigned, so one run cannot change the prompt
    another run is already using, and the shipped prompt keeps its bytes and its digest.
    """

    version: str
    text: str

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("a main prompt spec requires a non-empty version label")
        if not self.text.strip():
            raise ValueError("a main prompt spec requires non-empty prompt text")

    @property
    def digest(self) -> str:
        """``sha256:``-prefixed digest of exactly the text that becomes ``AgentConfig``."""
        return "sha256:" + hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def evidence(self, *, source: str) -> dict[str, Any]:
        return {
            "version": self.version,
            "digest": self.digest,
            "chars": len(self.text),
            # ``default`` when the run used the shipped prompt, ``override`` when the Host selected
            # another one. Never inferred: the caller that built the options is the only source.
            "source": source,
        }


#: The shipped Main prompt, unchanged since stage P0.1. Its digest is frozen by every earlier stage.
MAIN_PROMPT_V1 = MainPromptSpec(version=MAIN_PROMPT_V1_VERSION, text=MAIN_SYSTEM_PROMPT)


@dataclass(frozen=True)
class GoalRunOptions:
    """Everything one main-driven goal session needs."""

    goal: str
    runs_root: Path
    workspace_root: Path
    config: AtlasConfig
    #: Delegation limits and the ablation switch. Kept out of ``AtlasConfig`` so the frozen
    #: experiment configuration identity cannot move.
    delegation: DelegationConfig = field(default_factory=DelegationConfig)
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    max_turns: int = 16
    #: Optional run-scoped token budget shared by the Main and every child.
    token_budget: int | None = None
    #: Host-owned write scope for the Main. Empty means the Main cannot write either.
    main_write_paths: tuple[str, ...] = ()
    #: Host-owned write grants per worker profile, for delegated children.
    child_write_grants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Host-owned worker profiles this run may construct. ``None`` keeps the shipped catalog, which
    #: is what every earlier stage ran: the field is documentation-free, so an unspecified run
    #: resolves exactly as before. Restricting it makes a proposal for another profile a router
    #: refusal (``UNKNOWN_PROFILE``) instead of a worker that quietly runs without a grant.
    worker_profiles: tuple[str, ...] | None = None
    #: Host-owned turn cap applied to the selected profiles. ``None`` keeps each profile's own
    #: ``max_turns``. The child agent is built with ``min(profile.max_turns, child_max_turns)``, so
    #: only a value at or below the delegation limit has an effect, and a value above it is refused:
    #: the effective number is recorded in the run's capability evidence rather than assumed.
    worker_max_turns: int | None = None
    #: What the Main is told about the workers it may request. ``none`` keeps the shipped
    #: ``delegate_tasks`` bytes; ``native`` and ``card`` append a derived disclosure (see
    #: :mod:`atlasos.delegation.exposure`). Disclosure only: it never widens a grant.
    worker_exposure: str = "none"
    #: Deterministic test seam. The default builds the real DeepSeek adapter from the environment.
    provider_factory: Callable[[], Any] | None = None
    run_id: str | None = None
    #: Stage P: run every file and shell tool inside one project sandbox shared by the Main and all
    #: children. ``None`` keeps the stage-M host mode, which has no isolation and says so.
    sandbox: GoalSandboxOptions | None = None
    #: Opt-in Main system prompt. ``None`` means the shipped :data:`MAIN_PROMPT_V1`, byte for byte:
    #: an ablation that does not ask for another prompt reads and writes nothing. A spec here is
    #: only the Main's prompt; children are rendered from their trusted worker profile either way.
    main_prompt: MainPromptSpec | None = None
    #: Host-owned acceptance contract, evaluated after the run by the harness (never by the Main).
    acceptance: GoalAcceptance | None = None
    #: Host-fixed public suite the Main may run on itself. Requires ``sandbox``: the suite must
    #: execute inside the sandbox, and the command is never model-supplied.
    public_tests: PublicTestContract | None = None
    #: Stage G3-1 semantics for the goal path. ``corrected`` makes a command whose exit status was
    #: observed a *known* error the Main may read and recover from; an unobserved outcome (transport
    #: failure, lost sandbox) still stops the run in both modes. Overrides the value on
    #: ``sandbox`` so a run has exactly one mode, and is recorded in ``session.json``.
    tool_error_mode: str = TOOL_ERROR_MODE_CORRECTED
    #: Stage R1-1 evidence: record the exact input of every proposed tool call in the run's private
    #: payload corpus before it executes. This is what makes the call that killed a run recoverable
    #: after ``side_effect_unknown`` (stage P1b, DEV-4). The corpus lives under the run directory,
    #: is never copied into a report, and carries no runtime authority.
    tool_payload_recording: bool = True


def _goal_policy() -> ActionPolicy:
    """Action classes the goal path may run. Path limits come from the ownership policy."""

    allow = (
        "control.search",
        "control.dispatch",
        "delegation.dispatch",
        "delegation.read",
        # Stage P0.1: the Host-authorized public suite. The action is distinct from
        # ``shell.execute`` so an audit can tell a fixed harness command from model-chosen shell.
        PUBLIC_TEST_ACTION,
        "filesystem.read",
        "filesystem.write",
        "shell.execute",
    )
    return ActionPolicy(
        name="stage-m-goal",
        default="DENY",
        rules=tuple(
            ActionRule(id=f"allow-{action}", decision="ALLOW", action=action) for action in allow
        ),
    )


def _goal_catalog() -> ActionCatalog:
    from atlasos.actions import DEFAULT_MAPPINGS

    return ActionCatalog(
        DEFAULT_MAPPINGS
        + (
            ActionMapping(STAGE_M_DELEGATE_TOOL, "delegation.dispatch"),
            ActionMapping(DELEGATE_READ_TOOL, "delegation.read"),
            ActionMapping(PUBLIC_TEST_TOOL, PUBLIC_TEST_ACTION),
        )
    )


def _register_provider(name: str, factory: Callable[[], Any]) -> None:
    async def build(**_: Any) -> Any:
        return factory()

    register_provider_factory(name, build)


def _goal_sandbox_options(options: GoalRunOptions) -> GoalSandboxOptions | None:
    """The run's sandbox configuration, with the run-level tool error mode applied.

    One knob, one value: ``GoalRunOptions.tool_error_mode`` wins over whatever the caller put on
    ``GoalSandboxOptions``, so the mode is stated once and recorded once.
    """

    if options.sandbox is None:
        return None
    if options.tool_error_mode not in TOOL_ERROR_MODES:
        raise ValueError(
            f"unknown tool error mode {options.tool_error_mode!r}; expected one of "
            f"{list(TOOL_ERROR_MODES)}"
        )
    return replace(options.sandbox, tool_error_mode=options.tool_error_mode)


def _config_error(config: AtlasConfig) -> str | None:
    if config.tools.mode != "progressive":
        return (
            "the goal path requires tools.mode='progressive': the Main needs the shared registry, "
            f"action gate and ledger, but resolved {config.tools.mode!r}"
        )
    if config.context.policy != "upstream_default":
        return (
            "the goal path has no dependency edges, so context.policy="
            f"{config.context.policy!r} would be configured but unused; it refuses rather than "
            "reporting a context plane it does not apply"
        )
    return None


def _worker_catalog(options: GoalRunOptions) -> WorkerCatalog:
    """The profiles this run may construct.

    ``None`` means the shipped catalog, resolved exactly as before. A selection is Host-owned and
    makes an unlisted profile id a router refusal rather than a silently different worker.
    """

    shipped = WorkerCatalog()
    profiles = dict(shipped.profiles)
    if options.worker_profiles is None:
        selected = tuple(profiles[name] for name in sorted(profiles))
    else:
        if not options.worker_profiles:
            raise ValueError("worker_profiles was given but is empty")
        unknown = [name for name in options.worker_profiles if name not in profiles]
        if unknown:
            raise ValueError(f"unknown worker profile(s): {sorted(unknown)}")
        selected = tuple(profiles[name] for name in options.worker_profiles)
    if options.worker_max_turns is not None:
        cap = int(options.worker_max_turns)
        if cap > options.delegation.child_max_turns:
            # ``DelegationService`` builds each child with min(profile.max_turns, child_max_turns),
            # and the child cap is checked before the model call. A value above it would be reported
            # by a config file and never reach the agent, which is the failure S07 documented.
            raise ValueError(
                "worker_max_turns exceeds delegation.child_max_turns "
                f"({cap} > {options.delegation.child_max_turns}); the effective cap would be the "
                "delegation limit, not this value"
            )
        selected = tuple(profile.model_copy(update={"max_turns": cap}) for profile in selected)
    return WorkerCatalog(selected)


def _worker_cards(
    catalog: WorkerCatalog, options: GoalRunOptions
) -> tuple[WorkerCapabilityCard, ...]:
    """Derived capability cards for the offered profiles, or nothing when nothing is disclosed."""

    if options.worker_exposure == "none":
        return ()
    return tuple(
        build_card(
            profile,
            # The factory's own gate: a profile naming a tool outside it is refused at build time.
            registered_tools=DEFAULT_AVAILABLE_TOOLS,
            surface_tools=REFERENCE_SURFACE,
            granted_write_paths=tuple(options.child_write_grants.get(profile_id, ())),
            child_max_turns=options.delegation.child_max_turns,
            result_preview_chars=options.delegation.result_preview_chars,
        )
        for profile_id, profile in sorted(catalog.profiles.items())
    )


def _usage_evidence(usage: Any | None) -> dict[str, int]:
    if usage is None:
        return {
            "prompt_tokens": 0,
            "total_prompt_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "completion_tokens": 0,
        }
    miss = int(getattr(usage, "input_tokens", 0) or 0)
    hit = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    created = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    return {
        "prompt_tokens": miss,
        "total_prompt_tokens": miss + hit + created,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
    }


#: Kinds this producer is allowed to name. They are exactly the ones the runtime recorded a
#: structured stop for; anything else stays unclassified so the consuming audit fails closed.
CLASSIFIED_FAILURE_KINDS: tuple[str, ...] = (
    "budget_exhaustion",
    "main_max_turns",
    "acceptance_failed",
)


def classify_failure(
    *,
    failure_text: str | None,
    main_result: Any | None,
    acceptance_verdict: str,
    budget_state: RunBudgetState | None,
    succeeded: bool,
) -> dict[str, Any] | None:
    """Turn the recorded stop evidence into the structured failure the audit classifies.

    Stage C1 stopped with ``UNKNOWN_FAILURE_KIND`` although the runtime had recorded
    ``main.stop_reason = "budget_exceeded"`` and the budget's own message: the goal path emitted a
    bare string (or nothing), so ``failure.kind`` was always missing and every failed Main run would
    stop a dispatch. This maps only what the runtime actually recorded and leaves everything else
    unclassified, so an unknown failure still fails closed.
    """

    if succeeded:
        return None
    stop = getattr(main_result, "stop_reason", None) if main_result is not None else None
    stop_code = getattr(stop, "code", None)
    stop_message = getattr(stop, "message", None)
    budget = budget_state.as_dict() if budget_state is not None else {}
    kind: str | None = None
    if failure_text is None:
        if stop_code == "budget_exceeded":
            kind = "budget_exhaustion"
        elif stop_code == "max_turns":
            kind = "main_max_turns"
        elif acceptance_verdict == "FAIL":
            kind = "acceptance_failed"
    # For a budget rejection the budget's own message is the evidence, not the stop code's label.
    evidence_message = budget.get("stop_reason") if kind == "budget_exhaustion" else None
    return {
        "kind": kind,
        "classified_from": "recorded_stop_evidence" if kind else "unclassified",
        "message": failure_text
        or (str(evidence_message) if evidence_message else None)
        or (str(stop_message) if stop_message else None)
        or (
            f"main stopped: {stop_code}" if stop_code else "the run failed without a recorded stop"
        ),
        "main_stop_reason": stop_code,
        "main_stop_message": stop_message,
        "budget_stop_reason": budget.get("stop_reason"),
        "acceptance_verdict": acceptance_verdict,
    }


def _context_metrics(
    main_tokens: Mapping[str, Any],
    *,
    main_requests: int,
    main_context_tokens: list[int],
    delegation_evidence: Any | None,
    rounds: int,
) -> dict[str, Any]:
    """Per-request Main context plus the system totals, stated with their accounting.

    Accounting, so a reader never has to guess:

    * a request's *context* is the full prompt: cache-miss + cache reads (+ cache creation when the
      provider reports it). It is what the model read, not what the provider billed.
    * ``main_cumulative_input_tokens`` sums those context sizes over the Main's own requests only.
    * ``child_total_prompt_tokens`` is the same quantity summed over every delegated child.
    * ``system_total_tokens`` = Main + children, prompt + completion. Cache-hit tokens are part of
      the prompt totals and are never added again.

    Fields that were not observed are ``None`` and named in ``unavailable_fields``; nothing is
    estimated to fill a gap.
    """

    totals = getattr(delegation_evidence, "totals", None)
    batches = getattr(delegation_evidence, "batches", ()) or ()
    child_prompt = int(getattr(totals, "total_prompt_tokens", 0) or 0)
    child_completion = int(getattr(totals, "completion_tokens", 0) or 0)
    handoff_chars = sum(int(getattr(batch, "result_chars", 0) or 0) for batch in batches)
    concurrency = max(
        (int(getattr(batch, "max_observed_concurrency", 0) or 0) for batch in batches),
        default=0,
    )

    if main_requests <= 0:
        unavailable = [
            "main_peak_context_tokens",
            "main_cumulative_input_tokens",
            "main_context_tokens_by_request",
            "main_cache_hit_tokens",
            "main_cache_miss_tokens",
            "main_completion_tokens",
        ]
        return {
            "main_requests": 0,
            "main_peak_context_tokens": None,
            "main_cumulative_input_tokens": None,
            "main_context_tokens_by_request": None,
            "main_cache_hit_tokens": None,
            "main_cache_miss_tokens": None,
            "main_completion_tokens": None,
            "child_total_prompt_tokens": child_prompt,
            "child_completion_tokens": child_completion,
            "system_total_tokens": child_prompt + child_completion,
            "handoff_payload_chars": handoff_chars,
            "delegation_rounds": rounds,
            "max_observed_child_concurrency": concurrency,
            "unavailable_fields": unavailable,
        }

    main_prompt = int(main_tokens.get("total_prompt_tokens", 0) or 0)
    main_completion = int(main_tokens.get("completion_tokens", 0) or 0)
    return {
        "main_requests": main_requests,
        "main_peak_context_tokens": max(main_context_tokens, default=0),
        "main_cumulative_input_tokens": main_prompt,
        "main_context_tokens_by_request": list(main_context_tokens),
        "main_cache_hit_tokens": int(main_tokens.get("cache_hit_tokens", 0) or 0),
        "main_cache_miss_tokens": int(main_tokens.get("cache_miss_tokens", 0) or 0),
        "main_completion_tokens": main_completion,
        "child_total_prompt_tokens": child_prompt,
        "child_completion_tokens": child_completion,
        "system_total_tokens": main_prompt + main_completion + child_prompt + child_completion,
        "handoff_payload_chars": handoff_chars,
        "delegation_rounds": rounds,
        "max_observed_child_concurrency": concurrency,
        "unavailable_fields": [],
    }


async def run_goal(options: GoalRunOptions) -> dict[str, Any]:
    """Run one natural-language goal through the Main-driven delegation plane."""

    run_id = options.run_id or f"goal-{uuid.uuid4().hex}"
    runs_root = options.runs_root.resolve()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    # In sandbox mode the workspace is the sandbox's, and the Host path is only an evidence label:
    # what gets provisioned is the source repository, never the Host working directory.
    sandboxed = options.sandbox is not None
    workspace_root = options.workspace_root.resolve()
    if sandboxed:
        assert options.sandbox is not None
        source_repo = options.sandbox.source_repo.resolve()
        if not source_repo.is_dir():
            raise SystemExit(f"sandbox source repository does not exist: {source_repo}")
    elif not workspace_root.is_dir():
        raise SystemExit(f"workspace does not exist: {workspace_root}")
    #: The workspace path every tool, ownership check and child sees.
    tool_workspace = SANDBOX_WORKSPACE_ROOT if sandboxed else str(workspace_root)

    problem = _config_error(options.config)
    if problem is not None:
        raise ValueError(problem)
    delegation_config = options.delegation
    # Resolved once: the prompt that reaches ``AgentConfig``, the one recorded in ``session.json``
    # and the one a freeze check can hash are the same object.
    main_prompt = options.main_prompt or MAIN_PROMPT_V1

    load_dotenv(REPO_ROOT / ".env", override=False)
    main_task_id = f"{run_id}-main"
    main_worker_id = f"{run_id}-main-worker"
    main_agent_name = MAIN_AGENT_NAME

    # A live run must pass admission before any provider is built or any sandbox is started. A
    # deterministic test injects a factory and skips it: the report describes the live
    # configuration, not the seam.
    preflight: dict[str, Any] | None = None
    if options.provider_factory is None:
        preflight = run_live_preflight(options)
        (run_dir / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
        if preflight["status"] != READY:
            raise SystemExit(
                "live preflight is BLOCKED: "
                + ", ".join(preflight["blocking_failures"])
                + f"; report: {run_dir / 'preflight.json'}"
            )

    # Registry names are immutable for the process lifetime, so the name is derived from the run
    # id instead of being reused across runs.
    suffix = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:10]
    provider_name = f"{options.provider}-m{suffix}"
    if options.provider_factory is None:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise SystemExit(
                "DEEPSEEK_API_KEY is not configured; the goal path never falls back to a fake "
                "provider"
            )
        _register_provider(provider_name, lambda: DeepSeekAdapter.from_env())
    else:
        factory = options.provider_factory
        _register_provider(provider_name, factory)

    tool_security = ToolSecurityPolicy(workspace_root=tool_workspace)
    enforcer, ownership = build_delegation_ownership(run_id=run_id, workspace_root=tool_workspace)
    ownership.declare(
        worker_id=main_worker_id,
        task_id=main_task_id,
        paths=tuple(options.main_write_paths),
    )

    main_context = ExecutionContext(
        principal="user:goal",
        tenant_scope="goal",
        classification="internal",
        attributes={
            "run_id": run_id,
            "task_id": main_task_id,
            "worker_id": main_worker_id,
            "worker_profile": MAIN_PROFILE_ID,
            "operating_scope": "coding",
            "atlas_role": "main",
            "delegation_depth": 0,
            "delegation_root": run_id,
        },
    )

    store = FilesystemRunStore(run_dir / "trajectory")
    recorder = TrajectoryRecorder(store, run_id, agent_name=MAIN_AGENT_NAME, model=options.model)
    ledger = SQLiteActionLedgerStore(run_dir / "ledger.db")
    idempotency = SQLiteToolIdempotencyStore(run_dir / "idempotency.db")
    budget_state = (
        RunBudgetState(limit_tokens=max(1, int(options.token_budget)))
        if options.token_budget is not None
        else None
    )

    handoff_store = HandoffStore(handoffs_root(run_dir), run_id=run_id)
    handoff_index = DelegatedResultIndex(run_id=run_id, path=index_path(run_dir))
    reader = DelegatedResultReader(
        store=handoff_store,
        index=handoff_index,
        parent_task_id=main_task_id,
        parent_agent=main_agent_name,
    )

    if options.worker_exposure not in EXPOSURE_LEVELS:
        raise ValueError(
            f"unknown worker exposure {options.worker_exposure!r}; expected one of "
            f"{list(EXPOSURE_LEVELS)}"
        )
    worker_catalog = _worker_catalog(options)
    worker_cards = _worker_cards(worker_catalog, options)
    delegate_description = render_exposure(
        options.worker_exposure,
        worker_cards,
        base_description=DELEGATE_TASK_DESCRIPTION,
    )
    delegate_tool = DelegateTasksTool(description=delegate_description)
    # Ablation: with delegation disabled neither tool exists, so the Main cannot delegate at all
    # and the run is a plain single-agent baseline.
    core_tools = (
        [delegate_tool.definition(), reader.tool_definition()] if delegation_config.enabled else []
    )
    public_tests: PublicTestRunner | None = None

    runtime: AtlasToolRuntime | None = None
    service: DelegationService | None = None
    engine: AtlasContextEngine | None = None
    sandbox: GoalSandbox | None = None
    payload_corpus: ToolPayloadCorpus | None = None
    main_result: Any | None = None
    failure: str | None = None
    sandbox_failure: str | None = None
    exported: dict[str, Any] | None = None
    acceptance_result: dict[str, Any] | None = None
    main_usage = UsageAttributionHook()

    try:
        sandbox_options = _goal_sandbox_options(options)
        if options.tool_payload_recording:
            payload_corpus = ToolPayloadCorpus.for_run(run_dir)
        if sandbox_options is not None:
            sandbox = GoalSandbox(
                sandbox_options,
                config=options.config,
                run_id=run_id,
                run_dir=run_dir,
                recorder=recorder,
            )
            await sandbox.start(
                task_id=main_task_id,
                context=main_context,
                worker_id=main_worker_id,
            )
        if options.public_tests is not None:
            if sandbox is None:
                raise SystemExit(
                    "public_tests require sandbox mode: the suite must run inside the sandbox, "
                    "never on the host"
                )
            public_tests = PublicTestRunner(
                sandbox,
                options.public_tests,
                task_id=main_task_id,
                agent_name=main_agent_name,
            )
            core_tools = [*core_tools, public_tests.tool_definition()]
        runtime = AtlasToolRuntime(
            run_id,
            state_path=run_dir / "tool_state.json",
            conversation_path=run_dir / "tool_conversations.json",
            external_tools=(),
            core_tools=tuple(core_tools),
            action_policy=_goal_policy(),
            action_catalog=_goal_catalog(),
            action_ledger=ledger,
            action_external_policy=enforcer,
            idempotency_store=idempotency,
            # Same six tools, physical backend = the sandbox. Empty when there is no sandbox, so the
            # Host configuration is unchanged for the stage-M host mode.
            sandbox_tools=sandbox.sandbox_tools if sandbox is not None else None,
            trajectory_recorder=recorder,
            runtime_instance_id=f"goal-{run_id}",
            # DEV-4: every proposed tool input is written to the private corpus before it runs, so
            # the call that ended a failed run is still recoverable afterwards.
            execution_integrity_mode=(
                EXECUTION_INTEGRITY_RECORD
                if payload_corpus is not None
                else EXECUTION_INTEGRITY_OFF
            ),
            tool_payload_corpus=payload_corpus,
            # P0.1: every refusal this gate returns happens before the tool body runs, so the Main
            # may read the reason and choose another action instead of losing the run.
            pre_execution_denials_retry_safe=True,
        )
        if delegation_config.enabled:
            service = DelegationService(
                run_id=run_id,
                run_dir=run_dir,
                model=options.model,
                provider=provider_name,
                parent_context=main_context,
                parent_task_id=main_task_id,
                parent_agent=main_agent_name,
                tool_runtime=runtime,
                worker_catalog=worker_catalog,
                handoff_store=handoff_store,
                handoff_index=handoff_index,
                reader=reader,
                limits=DelegationLimits(
                    max_parallel_children=delegation_config.max_parallel_children,
                    max_children_per_call=delegation_config.max_children_per_call,
                    max_delegation_rounds=delegation_config.max_delegation_rounds,
                    child_timeout_seconds=delegation_config.child_timeout_seconds,
                    child_max_turns=delegation_config.child_max_turns,
                    result_preview_chars=delegation_config.result_preview_chars,
                ),
                budget_state=budget_state,
                tool_security=tool_security,
                ownership=ownership,
                granted_write_paths=dict(options.child_write_grants),
            )
            delegate_tool.bind(service)

        main_config = AgentConfig(
            name=main_agent_name,
            model=options.model,
            provider=provider_name,
            system_prompt=main_prompt.text,
            tools=list(runtime.agent_tools),
            max_turns=options.max_turns,
            tool_security=tool_security,
            execution_context=main_context,
        )
        engine = AtlasContextEngine(
            # One pool slot for the Main. Children never request one, so they cannot deadlock
            # against a pool the Main already occupies.
            OrchestratorConfig(max_concurrency=1),
            tool_runtime=runtime,
            budget_state=budget_state,
        )
        # Provider-reported usage of the Main, including the cache split that AnyCode's usage
        # merge discards.
        engine.configure(hooks=[main_usage])
        team = engine.create_team("goal", TeamConfig(name="goal", agents=[main_config]))
        task = TaskSpec(
            title="main-driven goal",
            description=options.goal,
            assignee=main_agent_name,
        )
        token = set_current_task_id(main_task_id)
        try:
            team_result = await engine.run_tasks(team, [task])
        finally:
            reset_current_task_id(token)
        main_result = team_result.agent_results.get(main_agent_name)
        if sandbox is not None:
            # Export before teardown: the patch and the workspace copy are the run's artifacts, and
            # they can only be read while the binding is alive.
            exported = await sandbox.export()
            if options.acceptance is not None:
                acceptance_result = await verify_goal(
                    options.acceptance,
                    snapshot=Path(exported["workspace_path"]),
                    sandbox=sandbox,
                    sandbox_failed=failure is not None,
                )
    except BaseException as error:  # noqa: BLE001 - recorded, then classified below
        failure = safe_exception_message(error)
        if not isinstance(error, Exception):
            raise
    finally:
        if engine is not None:
            try:
                await engine.close()
            except Exception as error:  # noqa: BLE001
                failure = failure or safe_exception_message(error)
        if service is not None:
            await service.aclose()
        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.close()
        if sandbox is not None:
            try:
                await sandbox.close(failed=failure is not None)
            except Exception as error:  # noqa: BLE001 - a leaked sandbox is a recorded failure
                sandbox_failure = safe_exception_message(error)
                failure = failure or sandbox_failure
        with contextlib.suppress(Exception):
            await idempotency.teardown()
        acceptance_failed = (
            acceptance_result is not None and acceptance_result.get("verdict") == "FAIL"
        )
        recorder_status = (
            "completed"
            if failure is None
            and main_result is not None
            and main_result.success
            and not acceptance_failed
            else "failed"
        )
        try:  # noqa: SIM105 - evidence is optional
            recorder.record_run_terminal(
                status=recorder_status,
                task_count=1,
                final_output_digest=None,
                final_output_size=len((main_result.output if main_result else "") or ""),
                occurred_at=datetime.now(UTC),
            )
        except Exception:  # noqa: BLE001
            pass

    main_output = (main_result.output if main_result is not None else "") or ""
    main_tokens = (
        main_usage.as_evidence()
        if main_usage.requests
        else _usage_evidence(main_result.token_usage if main_result is not None else None)
    )
    delegation_evidence = service.evidence() if service is not None else None
    child_usage = (
        delegation_evidence.totals.model_dump(mode="json") if delegation_evidence else None
    )
    metrics = _context_metrics(
        main_tokens,
        main_requests=main_usage.requests,
        main_context_tokens=list(main_usage.context_tokens),
        delegation_evidence=delegation_evidence,
        rounds=service.rounds if service is not None else 0,
    )
    # AnyCode turns a provider failure into an ``error`` event and returns without a ``done`` one,
    # so ``Agent`` reports an empty run whose ``success`` is True. A Main that completed no turn
    # and produced no output did not succeed, and must not be reported as if it had (the same
    # guard the delegation service applies to every child).
    main_silent_failure = (
        main_result is not None
        and bool(main_result.success)
        and not main_result.messages
        and not main_output
    )
    if main_silent_failure:
        failure = failure or "main_no_result: the model loop ended without completing a turn"
    execution_succeeded = failure is None and main_result is not None and bool(main_result.success)
    acceptance_verdict = (
        acceptance_result["verdict"] if acceptance_result is not None else "NOT_CONFIGURED"
    )
    succeeded = execution_succeeded and acceptance_verdict in ("PASS", "NOT_CONFIGURED")
    failure_record = classify_failure(
        failure_text=failure,
        main_result=main_result,
        acceptance_verdict=acceptance_verdict,
        budget_state=budget_state,
        succeeded=succeeded,
    )
    session: dict[str, Any] = {
        "run_id": run_id,
        "mode": "main_driven",
        "goal": options.goal,
        "run_dir": str(run_dir),
        "workspace_root": str(workspace_root),
        "workspace_tool_root": tool_workspace,
        "isolation": "sandbox" if sandboxed else "host",
        "provider": options.provider,
        "model": options.model,
        # Prompt identity of this run. ``source`` says whether the Host selected a prompt or the
        # shipped default was used, so a reader never has to infer it from a digest.
        "main_prompt": main_prompt.evidence(
            source="default" if options.main_prompt is None else "override"
        ),
        "status": "succeeded" if succeeded else "failed",
        "execution_succeeded": execution_succeeded,
        # A structured record, not a bare string: the audit classifies a cell by ``failure.kind``,
        # and a run that produced no kind used to stop every dispatch as UNKNOWN_FAILURE_KIND even
        # when the runtime had recorded exactly why it stopped (Stage C1-B).
        "failure": failure_record,
        "main": {
            "agent": main_agent_name,
            "worker_id": main_worker_id,
            "task_id": main_task_id,
            "success": bool(main_result.success) if main_result is not None else False,
            "stop_reason": (
                main_result.stop_reason.code
                if main_result is not None and main_result.stop_reason is not None
                else None
            ),
            "stop_message": (
                main_result.stop_reason.message
                if main_result is not None and main_result.stop_reason is not None
                else None
            ),
            "turns": (
                sum(1 for message in main_result.messages if message.role == "assistant")
                if main_result is not None
                else 0
            ),
            "tool_calls": (
                [record.tool_name for record in main_result.tool_calls]
                if main_result is not None
                else []
            ),
            "output": main_output,
            **main_tokens,
        },
        "delegation": delegation_evidence.as_dict() if delegation_evidence else None,
        "delegation_enabled": delegation_config.enabled,
        "delegation_controls": _delegation_controls_evidence(
            options, worker_catalog, worker_cards, delegate_description
        ),
        "child_tokens": child_usage,
        "metrics": metrics,
        "sandbox": sandbox.evidence.as_dict() if sandbox is not None else None,
        "artifacts": exported,
        "verification": acceptance_result,
        "acceptance": acceptance_verdict,
        "public_tests": _public_test_evidence(options, public_tests),
        "preflight": preflight,
        "budget": budget_state.as_dict() if budget_state is not None else None,
        "ownership": ownership.evidence_snapshot(),
        "handoff_reads": service.reader_metrics() if service is not None else None,
        "tool_surface": list(runtime.native_tool_names) if runtime is not None else [],
        # Reliability closure: the two semantics a reader would otherwise have to infer.
        "tool_error_mode": options.tool_error_mode,
        "execution_integrity": _integrity_evidence(payload_corpus, run_dir),
    }
    (run_dir / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
    return session


def _delegation_controls_evidence(
    options: GoalRunOptions,
    catalog: WorkerCatalog,
    cards: tuple[WorkerCapabilityCard, ...],
    description: str,
) -> dict[str, Any]:
    """What this run offered the Main, stated with the effective numbers rather than the asked ones.

    ``effective_max_turns`` is read back from the catalog the service was built with, because
    ``min(profile.max_turns, child_max_turns)`` is what the child agent actually receives.
    """

    profiles = {
        profile_id: {
            "role": profile.role,
            "tools": list(profile.base_tools),
            "operating_scope": list(profile.operating_scope),
            "profile_max_turns": profile.max_turns,
            "effective_max_turns": min(profile.max_turns, options.delegation.child_max_turns),
            "can_delegate": profile.can_delegate,
            "granted_write_paths": list(options.child_write_grants.get(profile_id, ())),
        }
        for profile_id, profile in sorted(catalog.profiles.items())
    }
    return {
        "worker_profiles": sorted(catalog.profiles),
        "worker_profiles_requested": (
            list(options.worker_profiles) if options.worker_profiles is not None else None
        ),
        "worker_max_turns_requested": options.worker_max_turns,
        "child_max_turns_limit": options.delegation.child_max_turns,
        "profiles": profiles,
        "exposure": exposure_evidence(
            options.worker_exposure,
            description,
            cards,
            base_description=DELEGATE_TASK_DESCRIPTION,
        ),
        # The shipped bytes, so a reader can tell an unchanged surface from a changed one.
        "delegate_description_shipped_digest": "sha256:"
        + hashlib.sha256(DELEGATE_TASK_DESCRIPTION.encode("utf-8")).hexdigest(),
        "authority": "disclosure_only",
    }


def _integrity_evidence(corpus: ToolPayloadCorpus | None, run_dir: Path) -> dict[str, Any]:
    """Whether the exact input of a failed call survived the run, and where it lives.

    ``records`` counts payloads, not calls: a call is recoverable when its ``input_ref`` in the
    transcript resolves here. A write failure is reported, never hidden — an unrecoverable call is
    an evidence gap, not an empty corpus.
    """

    if corpus is None:
        return {
            "mode": EXECUTION_INTEGRITY_OFF,
            "records": 0,
            "write_failures": 0,
            "payload_file": None,
            "authority": "none",
        }
    path = Path(run_dir) / PRIVATE_PAYLOAD_ROOT / PRIVATE_PAYLOAD_FILE
    return {
        "mode": EXECUTION_INTEGRITY_RECORD,
        "payload_file": f"{PRIVATE_PAYLOAD_ROOT}/{PRIVATE_PAYLOAD_FILE}",
        "records": len(read_corpus(path)),
        "write_failures": corpus.write_failures,
        "last_error": corpus.last_error,
        # The corpus is host-local forensic evidence. It confers no permission and is never
        # published: see docs/stage-reliability-closure-b0.md.
        "authority": "evidence_only",
    }


def _public_test_evidence(
    options: GoalRunOptions, runner: PublicTestRunner | None
) -> dict[str, Any]:
    """The public channel's evidence, kept apart from the oracle's verdict."""

    if options.public_tests is None:
        return {"status": "NOT_CONFIGURED", "oracle_is_separate": True, "calls": 0, "runs": []}
    return {
        "contract": options.public_tests.payload(),
        "command_digest": options.public_tests.command_digest(),
        "calls": runner.calls if runner is not None else 0,
        "runs": runner.evidence() if runner is not None else [],
        "oracle_is_separate": True,
    }


def _load_acceptance(path: Path | None) -> GoalAcceptance | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"acceptance contract must be a JSON object: {path}")
    return GoalAcceptance.from_payload(payload)


def _options_from_args(args: argparse.Namespace) -> GoalRunOptions:
    config = load_config(args.config) if args.config is not None else AtlasConfig()
    delegation = (
        load_delegation_config(args.config) if args.config is not None else DelegationConfig()
    )
    public_tests = (
        PublicTestContract(
            test_id=args.public_test_id,
            command=args.public_test_command,
            timeout_seconds=args.public_test_timeout,
        )
        if args.public_test_command
        else None
    )
    return GoalRunOptions(
        goal=args.goal,
        runs_root=args.runs_root,
        workspace_root=args.workspace_root,
        config=config,
        delegation=delegation,
        provider=args.provider,
        model=args.model,
        max_turns=args.max_turns,
        token_budget=args.token_budget,
        main_write_paths=tuple(args.main_write_path or ()),
        child_write_grants={profile: tuple(paths) for profile, paths in (args.child_grant or ())},
        sandbox=(
            GoalSandboxOptions(
                provider=args.sandbox_provider,
                source_repo=args.sandbox_source,
                image=args.sandbox_image,
                retain_on_failure=args.retain_sandbox_on_failure,
            )
            if args.sandbox_source is not None
            else None
        ),
        public_tests=public_tests,
        acceptance=_load_acceptance(args.acceptance_spec),
    )


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="atlas-goal-session")
    parser.add_argument("goal", help="natural-language goal for the Main agent")
    parser.add_argument("--runs-root", type=Path, default=REPO_ROOT / "experiments" / "runs")
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--max-turns", type=int, default=16)
    parser.add_argument("--token-budget", type=int, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--main-write-path", action="append", default=None)
    parser.add_argument(
        "--sandbox-source",
        type=Path,
        default=None,
        help=(
            "clean git repository to provision into a project sandbox; omitting it runs on the Host"
        ),
    )
    parser.add_argument("--sandbox-provider", default="opensandbox")
    parser.add_argument("--sandbox-image", default=None)
    parser.add_argument("--retain-sandbox-on-failure", action="store_true")
    parser.add_argument(
        "--public-test-command",
        default=None,
        help="host-fixed public suite the Main may run itself (requires --sandbox-source)",
    )
    parser.add_argument("--public-test-id", default="public")
    parser.add_argument("--public-test-timeout", type=int, default=120)
    parser.add_argument(
        "--acceptance-spec",
        type=Path,
        default=None,
        help="JSON file with the host-owned acceptance contract (spec + dynamic_command)",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="print the Stage P1 admission report and exit; never calls a provider",
    )
    parser.add_argument(
        "--child-grant",
        action="append",
        nargs=2,
        metavar=("PROFILE", "PATH"),
        default=None,
    )
    args = parser.parse_args()
    options = _options_from_args(args)
    if args.preflight:
        report = run_live_preflight(options)
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["status"] == READY else 2)
    session = asyncio.run(run_goal(options))
    print(json.dumps({k: session[k] for k in ("run_id", "status", "failure")}, indent=2))
    raise SystemExit(0 if session["status"] == "succeeded" else 1)


if __name__ == "__main__":
    main()
