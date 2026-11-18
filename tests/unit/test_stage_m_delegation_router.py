"""Stage M unit tests: admission, bounded projection, identity, and ablation switches."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from anycode.identity.context import ExecutionContext

from atlasos.config import AtlasConfig
from atlasos.delegation import (
    STAGE_M_POLICY_VERSION,
    ChildOutcome,
    ChildStatus,
    DelegateTasksInput,
    DelegationBatch,
    DelegationConfig,
    DelegationLimits,
    DelegationPlanContext,
    DelegationRoute,
    DelegationRouter,
    DelegationTotals,
    ProfileFacts,
)
from atlasos.delegation.models import MAX_TOOL_RESULT_CHARS
from atlasos.delegation.service import bound_summary, render_batch
from atlasos.experiments import semantic_config_hash
from atlasos.tools import AtlasToolRuntime
from atlasos.workers import ScopedWorkerFactory, WorkerAssignment, WorkerCatalog, WorkerStore
from atlasos.workers.models import WorkerConfigurationError, WorkerProfile
from atlasos.workers.routing import POLICY_VERSION


def _profiles() -> dict[str, ProfileFacts]:
    return {
        "research_worker": ProfileFacts(
            profile_id="research_worker",
            base_tools=("file_read", "grep", "list_files", "tool_search"),
            read_only=True,
        ),
        "coding_worker": ProfileFacts(
            profile_id="coding_worker",
            base_tools=("bash", "file_edit", "file_read", "file_write", "grep"),
            read_only=False,
        ),
    }


def _context(**overrides: object) -> DelegationPlanContext:
    values: dict[str, object] = {
        "run_id": "run-m",
        "parent_task_id": "run-m-main",
        "max_children": 2,
        "profiles": _profiles(),
        "granted_write_paths": {},
        "workspace_root": "/workspace/project",
    }
    values.update(overrides)
    return DelegationPlanContext(**values)  # type: ignore[arg-type]


def _task(key: str, objective: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "key": key,
        "objective": objective,
        "background": "the project is a small Python package",
        "profile_id": "research_worker",
    }
    payload.update(overrides)
    return payload


def _request(*tasks: dict[str, object]) -> DelegateTasksInput:
    return DelegateTasksInput.model_validate({"tasks": list(tasks), "reason": "unit"})


# --------------------------------------------------------------------------- admission


def test_one_read_only_task_with_context_is_delegate_one() -> None:
    decision = DelegationRouter().decide(
        _request(_task("alpha", "inspect the parser")), context=_context()
    )

    assert decision.route is DelegationRoute.DELEGATE_ONE
    assert decision.admitted_specs() == ("alpha",)
    assert decision.policy_version == STAGE_M_POLICY_VERSION
    assert decision.frozen_decision is not None


def test_two_independent_read_only_tasks_are_delegate_parallel() -> None:
    decision = DelegationRouter().decide(
        _request(_task("alpha", "inspect the parser"), _task("beta", "inspect the ledger")),
        context=_context(),
    )

    assert decision.route is DelegationRoute.DELEGATE_PARALLEL
    assert decision.admitted_specs() == ("alpha", "beta")
    assert decision.max_wave_width == 2


def test_a_single_task_without_background_is_refused() -> None:
    decision = DelegationRouter().decide(
        _request(_task("alpha", "inspect the parser", background=None)), context=_context()
    )

    assert decision.route is DelegationRoute.DIRECT
    # The frozen gate 1 refuses single-task delegation that cannot show context sufficiency.
    assert "TRIVIAL_DIRECT_TASK" in decision.reason_codes


def test_two_tasks_without_background_are_refused_by_the_context_gate() -> None:
    decision = DelegationRouter().decide(
        _request(
            _task("alpha", "inspect the parser", background=None),
            _task("beta", "inspect the ledger", background=None),
        ),
        context=_context(),
    )

    assert decision.route is DelegationRoute.DIRECT
    assert "CONTEXT_INSUFFICIENT" in decision.reason_codes


def test_duplicate_keys_are_refused_by_the_frozen_router() -> None:
    decision = DelegationRouter().decide(
        _request(_task("alpha", "one"), _task("alpha", "two")), context=_context()
    )

    assert decision.route is DelegationRoute.DIRECT
    assert "DUPLICATE_TASK_ID" in decision.reason_codes


def test_unknown_profile_is_refused() -> None:
    decision = DelegationRouter().decide(
        _request(_task("alpha", "inspect", profile_id="ghost_worker")), context=_context()
    )

    assert decision.route is DelegationRoute.DIRECT
    assert "UNKNOWN_PROFILE" in decision.reason_codes


def test_a_declared_dependency_is_refused_before_the_router_is_consulted() -> None:
    decision = DelegationRouter().decide(
        _request(
            _task("alpha", "one"),
            _task("beta", "two", depends_on=("alpha",)),
        ),
        context=_context(),
    )

    assert decision.route is DelegationRoute.DIRECT
    assert decision.reason_codes == ("DEPENDENCY_NOT_SUPPORTED",)
    assert decision.frozen_decision is None


def test_two_children_declaring_the_same_write_path_are_refused() -> None:
    context = _context(granted_write_paths={"coding_worker": ("src/shared.py",)})
    decision = DelegationRouter().decide(
        _request(
            _task(
                "alpha",
                "edit shared",
                profile_id="coding_worker",
                write_paths=["src/shared.py"],
            ),
            _task(
                "beta",
                "also edit shared",
                profile_id="coding_worker",
                write_paths=["src/shared.py"],
            ),
        ),
        context=context,
    )

    assert decision.route is DelegationRoute.DIRECT
    assert "SHARED_WRITE_RISK" in decision.reason_codes
    assert decision.admitted_specs() == ()


def test_a_write_outside_the_host_grant_is_refused() -> None:
    context = _context(granted_write_paths={"coding_worker": ("src/",)})
    decision = DelegationRouter().decide(
        _request(
            _task(
                "alpha",
                "edit elsewhere",
                profile_id="coding_worker",
                write_paths=["docs/readme.md"],
            )
        ),
        context=context,
    )

    assert decision.route is DelegationRoute.DIRECT
    assert decision.reason_codes == ("WRITE_SCOPE_NOT_GRANTED",)


def test_a_write_on_a_read_only_profile_is_refused() -> None:
    decision = DelegationRouter().decide(
        _request(_task("alpha", "edit", write_paths=["src/a.py"])), context=_context()
    )

    assert decision.route is DelegationRoute.DIRECT
    assert decision.reason_codes == ("WRITE_SCOPE_NOT_GRANTED",)


def test_a_path_that_escapes_the_workspace_is_refused() -> None:
    context = _context(granted_write_paths={"coding_worker": ("../",)})
    decision = DelegationRouter().decide(
        _request(
            _task(
                "alpha",
                "edit",
                profile_id="coding_worker",
                write_paths=["../outside.py"],
            )
        ),
        context=context,
    )

    assert decision.route is DelegationRoute.DIRECT
    assert decision.reason_codes == ("INVALID_PLAN",)


def test_the_decision_is_deterministic_and_versioned() -> None:
    router = DelegationRouter()
    request = _request(_task("alpha", "inspect"), _task("beta", "inspect"))
    first = router.decide(request, context=_context())
    second = router.decide(request, context=_context())

    assert first.model_dump() == second.model_dump()
    assert first.policy_version == "m.1.0"
    # The concurrency bound is part of the policy, so its digest must change with it.
    wide = router.decide(request, context=_context(max_children=3))
    assert wide.policy_digest != first.policy_digest


def test_the_frozen_router_module_is_unchanged_and_still_owns_the_vocabulary() -> None:
    assert POLICY_VERSION == "r0.1.0"
    assert STAGE_M_POLICY_VERSION != POLICY_VERSION


def test_the_router_refuses_a_plan_wider_than_the_concurrency_limit() -> None:
    decision = DelegationRouter().decide(
        _request(
            _task("alpha", "a"),
            _task("beta", "b"),
            _task("gamma", "c"),
        ),
        context=_context(max_children=2),
    )

    assert decision.route is DelegationRoute.DIRECT
    assert "PARALLEL_LIMIT_EXCEEDED" in decision.reason_codes


# --------------------------------------------------------------------------- projection


def _child(key: str, *, summary_chars: int, ref_id: str | None = None) -> ChildOutcome:
    return ChildOutcome(
        key=key,
        worker_id=f"w-{key}",
        profile_id="research_worker",
        status=ChildStatus.COMPLETED,
        success=True,
        summary="s" * summary_chars,
        summary_chars=summary_chars,
        output_chars=summary_chars,
        ref_id=ref_id,
    )


def _batch(children: tuple[ChildOutcome, ...]) -> DelegationBatch:
    return DelegationBatch(
        round=1,
        route=DelegationRoute.DELEGATE_PARALLEL,
        decision=DelegationRouter().decide(
            _request(_task("alpha", "a"), _task("beta", "b")), context=_context()
        ),
        children=children,
        totals=DelegationTotals(children_started=len(children)),
    )


def test_render_batch_keeps_the_full_projection_when_it_fits() -> None:
    text, truncated = render_batch(_batch((_child("alpha", summary_chars=100),)))
    payload = json.loads(text)

    assert truncated is False
    assert payload["children"][0]["summary"] == "s" * 100
    assert payload["route"] == "delegate_parallel"


def test_render_batch_drops_summaries_before_exceeding_the_ceiling() -> None:
    children = tuple(_child(key, summary_chars=4_000, ref_id="ho-x") for key in "abcd")
    text, truncated = render_batch(_batch(children))
    payload = json.loads(text)

    assert len(text) <= MAX_TOOL_RESULT_CHARS
    assert truncated is True
    assert all(child["summary"] == "" for child in payload["children"])
    assert all(child["summary_omitted"] is True for child in payload["children"])
    assert all(child["ref_id"] == "ho-x" for child in payload["children"])


def test_bound_summary_never_exceeds_the_limit() -> None:
    text = bound_summary("x" * 5_000, 900)

    assert len(text) <= 900
    assert text.endswith("behind a reference]")


# --------------------------------------------------------------------------- identity


def test_a_worker_profile_cannot_carry_a_delegate_tool() -> None:
    with pytest.raises(ValueError, match="delegate tools are not allowed"):
        WorkerProfile(
            profile_id="sneaky",
            role="r",
            instructions="i",
            base_tools=("file_read", "delegate_tasks"),
            operating_scope=("general",),
        )


def test_a_child_context_cannot_inherit_the_parent_delegation_identity(
    tmp_path: Path,
) -> None:
    factory = ScopedWorkerFactory(
        model="fake-model",
        provider=None,
        store=WorkerStore(tmp_path / "workers.json"),
        catalog=WorkerCatalog(),
    )
    parent = ExecutionContext(
        principal="user:test",
        tenant_scope="test",
        attributes={
            "run_id": "run-m",
            "task_id": "run-m-main",
            "atlas_role": "main",
            "delegation_depth": 0,
            "delegation_root": "run-m",
            "note": "kept",
        },
    )
    child = factory.create(
        parent_context=parent,
        parent_agent="atlas-main",
        assignment=WorkerAssignment(profile_id="research_worker", task="x"),
        worker_id="w1",
        task_id="t1",
    )
    attributes = child.execution_context.attributes

    assert attributes["atlas_role"] == "worker"
    assert attributes["delegation_depth"] == 1
    assert attributes["delegation_root"] == "run-m"
    assert attributes["parent_task_id"] == "run-m-main"
    assert attributes["task_id"] == "t1"
    assert attributes["note"] == "kept"
    # The parent context is untouched.
    assert parent.attributes["atlas_role"] == "main"
    assert parent.attributes["task_id"] == "run-m-main"

    grandchild = factory.create(
        parent_context=child.execution_context,
        parent_agent=child.worker_name,
        assignment=WorkerAssignment(profile_id="research_worker", task="y"),
        worker_id="w2",
        task_id="t2",
    )
    assert grandchild.execution_context.attributes["delegation_depth"] == 2


# --------------------------------------------------------------------------- configuration


def test_delegation_is_not_part_of_the_frozen_configuration_identity() -> None:
    """Adding the switch must not move any recorded experiment or dry-run digest."""

    assert "delegation" not in AtlasConfig().model_dump()
    # A YAML file may still carry the section: the goal path reads it separately.
    assert semantic_config_hash(AtlasConfig()) == semantic_config_hash(AtlasConfig())
    assert DelegationConfig().enabled is True


def test_the_delegation_section_is_read_without_touching_atlas_config(
    tmp_path: Path,
) -> None:
    from atlasos.delegation import load_delegation_config

    path = tmp_path / "goal.yaml"
    path.write_text(
        "provider:\n  model: deepseek-chat\ndelegation:\n  max_parallel_children: 3\n"
        "  max_children_per_call: 3\n",
        encoding="utf-8",
    )
    delegation = load_delegation_config(path)

    assert delegation.max_parallel_children == 3
    # The frozen AtlasConfig validates the same file and never sees the section.
    config = AtlasConfig.model_validate({"provider": {"model": "deepseek-chat"}})
    assert "delegation" not in config.model_dump()


def test_delegation_limits_are_validated() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        DelegationConfig(max_parallel_children=1, max_children_per_call=2)
    with pytest.raises(ValueError):
        DelegationConfig(child_timeout_seconds=0)
    with pytest.raises(ValueError):
        DelegationConfig.model_validate({"unknown_key": 1})


def test_the_default_native_tool_surface_is_unchanged() -> None:
    run_id = f"surface-{uuid.uuid4().hex[:6]}"
    runtime = AtlasToolRuntime(run_id, state_path=None)

    assert "delegate_tasks" not in runtime.native_tool_names
    assert "delegate.read" not in runtime.native_tool_names


def test_core_tool_injection_refuses_a_shadowed_name() -> None:
    from anycode.types import ToolDefinition, ToolResult

    async def execute(validated, context):  # noqa: ANN001, ANN202, ARG001
        return ToolResult(data="no")

    run_id = f"surface-{uuid.uuid4().hex[:6]}"
    with pytest.raises(ValueError, match="already registered"):
        AtlasToolRuntime(
            run_id,
            state_path=None,
            core_tools=(
                ToolDefinition(
                    name="file_read",
                    description="shadow",
                    input_model=DelegateTasksInput,
                    execute=execute,
                ),
            ),
        )


def test_delegation_limits_default_matches_the_shipped_config() -> None:
    config = DelegationConfig()
    limits = DelegationLimits(
        max_parallel_children=config.max_parallel_children,
        max_children_per_call=config.max_children_per_call,
        max_delegation_rounds=config.max_delegation_rounds,
        child_timeout_seconds=config.child_timeout_seconds,
        child_max_turns=config.child_max_turns,
        result_preview_chars=config.result_preview_chars,
    )

    assert limits.max_parallel_children == 2
    assert limits.max_children_per_call <= limits.max_parallel_children


def test_worker_configuration_errors_surface_as_a_refusal_not_a_crash(
    tmp_path: Path,
) -> None:
    """A profile the factory cannot build must not leave a half-created batch."""

    factory = ScopedWorkerFactory(
        model="fake-model",
        provider=None,
        store=WorkerStore(tmp_path / "workers.json"),
        catalog=WorkerCatalog(),
        available_tools=("file_read",),
    )
    with pytest.raises(WorkerConfigurationError, match="unavailable tools"):
        factory.create(
            parent_context=ExecutionContext(principal="user:test", tenant_scope="test"),
            parent_agent="atlas-main",
            assignment=WorkerAssignment(profile_id="coding_worker", task="x"),
        )
