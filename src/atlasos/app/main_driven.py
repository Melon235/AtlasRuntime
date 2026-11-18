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
from dataclasses import dataclass, field
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
from atlasos.delegation.handoff import handoffs_root, index_path
from atlasos.delegation.tool import STAGE_M_DELEGATE_TOOL
from atlasos.delegation.usage import UsageAttributionHook
from atlasos.handoff import HandoffStore
from atlasos.integrations.anycode_engine import AtlasContextEngine
from atlasos.integrations.deepseek import DeepSeekAdapter
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.tools import AtlasToolRuntime
from atlasos.tools.conversation import reset_current_task_id, set_current_task_id
from atlasos.trajectory.recorder import TrajectoryRecorder

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO_ROOT / "experiments" / "configs" / "v2-uat.yaml"
MAIN_AGENT_NAME = "atlas-main"
MAIN_PROFILE_ID = "coding_worker"

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
)


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
    #: Deterministic test seam. The default builds the real DeepSeek adapter from the environment.
    provider_factory: Callable[[], Any] | None = None
    run_id: str | None = None


def _goal_policy() -> ActionPolicy:
    """Action classes the goal path may run. Path limits come from the ownership policy."""

    allow = (
        "control.search",
        "control.dispatch",
        "delegation.dispatch",
        "delegation.read",
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
        )
    )


def _register_provider(name: str, factory: Callable[[], Any]) -> None:
    async def build(**_: Any) -> Any:
        return factory()

    register_provider_factory(name, build)


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


async def run_goal(options: GoalRunOptions) -> dict[str, Any]:
    """Run one natural-language goal through the Main-driven delegation plane."""

    run_id = options.run_id or f"goal-{uuid.uuid4().hex}"
    runs_root = options.runs_root.resolve()
    workspace_root = options.workspace_root.resolve()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    if not workspace_root.is_dir():
        raise SystemExit(f"workspace does not exist: {workspace_root}")

    problem = _config_error(options.config)
    if problem is not None:
        raise ValueError(problem)
    delegation_config = options.delegation

    load_dotenv(REPO_ROOT / ".env", override=False)
    main_task_id = f"{run_id}-main"
    main_worker_id = f"{run_id}-main-worker"
    main_agent_name = MAIN_AGENT_NAME

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

    tool_security = ToolSecurityPolicy(workspace_root=str(workspace_root))
    enforcer, ownership = build_delegation_ownership(
        run_id=run_id, workspace_root=str(workspace_root)
    )
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
    recorder = TrajectoryRecorder(
        store, run_id, agent_name=MAIN_AGENT_NAME, model=options.model
    )
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

    delegate_tool = DelegateTasksTool()
    # Ablation: with delegation disabled neither tool exists, so the Main cannot delegate at all
    # and the run is a plain single-agent baseline.
    core_tools = (
        [delegate_tool.definition(), reader.tool_definition()]
        if delegation_config.enabled
        else []
    )

    runtime: AtlasToolRuntime | None = None
    service: DelegationService | None = None
    engine: AtlasContextEngine | None = None
    main_result: Any | None = None
    failure: str | None = None
    main_usage = UsageAttributionHook()

    try:
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
            trajectory_recorder=recorder,
            runtime_instance_id=f"goal-{run_id}",
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
            system_prompt=MAIN_SYSTEM_PROMPT,
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
        with contextlib.suppress(Exception):
            await idempotency.teardown()
        recorder_status = (
            "completed"
            if failure is None and main_result is not None and main_result.success
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
    succeeded = (
        failure is None and main_result is not None and bool(main_result.success)
    )
    session: dict[str, Any] = {
        "run_id": run_id,
        "mode": "main_driven",
        "goal": options.goal,
        "run_dir": str(run_dir),
        "workspace_root": str(workspace_root),
        "provider": options.provider,
        "model": options.model,
        "status": "succeeded" if succeeded else "failed",
        "failure": failure,
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
        "child_tokens": child_usage,
        "budget": budget_state.as_dict() if budget_state is not None else None,
        "ownership": ownership.evidence_snapshot(),
        "handoff_reads": service.reader_metrics() if service is not None else None,
        "tool_surface": list(runtime.native_tool_names) if runtime is not None else [],
    }
    (run_dir / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
    return session


def _options_from_args(args: argparse.Namespace) -> GoalRunOptions:
    config = load_config(args.config) if args.config is not None else AtlasConfig()
    delegation = (
        load_delegation_config(args.config) if args.config is not None else DelegationConfig()
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
        child_write_grants={
            profile: tuple(paths) for profile, paths in (args.child_grant or ())
        },
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
        "--child-grant",
        action="append",
        nargs=2,
        metavar=("PROFILE", "PATH"),
        default=None,
    )
    args = parser.parse_args()
    session = asyncio.run(run_goal(_options_from_args(args)))
    print(json.dumps({k: session[k] for k in ("run_id", "status", "failure")}, indent=2))
    raise SystemExit(0 if session["status"] == "succeeded" else 1)


if __name__ == "__main__":
    main()
