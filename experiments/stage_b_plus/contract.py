"""Stage B+ frozen candidate contract: Worker Capability x Capability Exposure.

The document this module builds is the *candidate* identity of the ablation. It is deliberately a
new object rather than an edit of Stage B Freeze 1.6: the tasks, the prompt, the worker turn cap and
the cost ceiling all differ, so reusing ``3080f313…`` or ``eb14ea4e…`` would misdescribe the run.

What the ablation varies
------------------------
Only two things, and they are not independent in the implementation (§D of the report says why):

``capability``  which trusted profile the run offers and grants write capacity to.
``exposure``    what the Main is told about that profile, appended to ``delegate_tasks``.

Everything else — Main prompt bytes, model, tasks, fixtures, oracle, budgets, router policy, worker
contract, ownership rules, concurrency — is held at one value across the four arms.

The prompt
----------
The primary matrix uses ``main-v2.1-rc1-bplus``: Stage S2's P3 text with the
``## WORKER CAPABILITY CONTRACT`` section removed. That section is a hand-written enumeration of one
profile's tools, so keeping it while varying the profile asks the Main to use the profile the arm is
replacing. The removal is mechanical, both digests are recorded, and ``P3_LITERAL`` remains
available so the Owner can run the literal-S06 variant and observe the degeneracy it produces.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2 import catalog as closure2
from experiments.evaluation.models import canonical_digest
from experiments.stage_benchmark import matrix
from experiments.stage_benchmark.reconstruct import LEVELS as DELEGATION_LEVELS
from experiments.stage_benchmark.reconstruct import PROPOSAL_STATES

STAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = STAGE_DIR.parents[1]

STAGE_ID = "B+"
PROTOCOL_VERSION = "bplus-1.0.0"
#: What this contract is *not*: it supersedes no frozen Stage B document and changes none of them.
SUPERSEDES: None = None
DOES_NOT_MODIFY = (
    "experiments/stage_benchmark/execution/freeze-1.6.json",
    "experiments/stage_benchmark/protocol.json",
    "experiments/evaluation/closure2/*",
)

# -- prompts ---------------------------------------------------------------------------------

P3_PATH = REPO_ROOT / "experiments" / "stage_s2_prompt_v21" / "prompts" / "main_v21.md"
P3_DIGEST = "sha256:4147f19cab445241f79dde4a03db69a1a96961d775ec6e0da35dc24cb7cdd489"
P3_VERSION = "main-v2.1-rc1"

_SECTION_START = "## WORKER CAPABILITY CONTRACT"
_SECTION_END = "## HANDOFF-FIRST INTEGRATION"

BPLUS_VERSION = "main-v2.1-rc1-bplus"
P3_LITERAL = "p3-literal"
PROMPT_VARIANTS: tuple[str, ...] = ("p3-bplus", "p3-literal")


def p3_text() -> str:
    return P3_PATH.read_text(encoding="utf-8")


def remove_capability_section(text: str) -> tuple[str, str]:
    """Return ``(without_section, removed_section)``.

    The boundaries are exact heading strings and each must occur exactly once, so a prompt edit
    cannot silently widen or shrink what the ablation removes.
    """

    if text.count(_SECTION_START) != 1 or text.count(_SECTION_END) != 1:
        raise ValueError("the P3 prompt no longer has the two section markers B+ removes")
    start = text.index(_SECTION_START)
    end = text.index(_SECTION_END)
    if end <= start:
        raise ValueError("the capability section is not before the handoff section")
    return text[:start] + text[end:], text[start:end]


def prompt_text(variant: str) -> str:
    if variant == "p3-literal":
        return p3_text()
    if variant == "p3-bplus":
        return remove_capability_section(p3_text())[0]
    raise ValueError(f"unknown prompt variant {variant!r}; expected one of {list(PROMPT_VARIANTS)}")


def prompt_version(variant: str) -> str:
    return P3_VERSION if variant == "p3-literal" else BPLUS_VERSION


# -- workers ---------------------------------------------------------------------------------

RESEARCH = "research_worker"
CODING = "coding_worker"

#: Exposure levels. ``none`` is the shipped description; ``native`` is a derived prose enumeration
#: of role and tools (the same disclosure class P3 hand-wrote); ``card`` adds the contracts.
EXPOSURES: tuple[str, ...] = ("none", "native", "card")

#: Write capacity the Host grants, expressed as the directory the task owns.
TASK_WRITE_SCOPE: dict[str, tuple[str, ...]] = {
    task.task_id: tuple(task.allowed_paths) for task in closure2.TASKS
}


@dataclass(frozen=True)
class Arm:
    arm_id: str
    capability: str
    profile_id: str
    exposure: str
    description: str

    def grant_for(self, task_id: str) -> dict[str, tuple[str, ...]]:
        """Host-owned write grant for this arm on this task.

        The research arms grant nothing: the profile carries no write tool, so a grant would state
        something the runtime could never enforce. The coding arms grant the task's own write scope,
        which is the frozen ``allowed_paths`` of the Closure-2 task, not a model-supplied list.
        """

        if self.profile_id == CODING:
            return {CODING: TASK_WRITE_SCOPE[task_id]}
        return {}


ARMS: tuple[Arm, ...] = (
    Arm(
        arm_id="C0",
        capability="research",
        profile_id=RESEARCH,
        exposure="native",
        description="research_worker with the existing prose disclosure",
    ),
    Arm(
        arm_id="C1",
        capability="research",
        profile_id=RESEARCH,
        exposure="card",
        description="research_worker with the derived capability card",
    ),
    Arm(
        arm_id="C2",
        capability="coding",
        profile_id=CODING,
        exposure="native",
        description="coding_worker with the existing prose disclosure",
    ),
    Arm(
        arm_id="C3",
        capability="coding",
        profile_id=CODING,
        exposure="card",
        description="coding_worker with the derived capability card",
    ),
)

ARMS_BY_ID: dict[str, Arm] = {arm.arm_id: arm for arm in ARMS}
ARM_IDS: tuple[str, ...] = tuple(arm.arm_id for arm in ARMS)

# -- tasks -----------------------------------------------------------------------------------

#: The three Closure-2 tasks, in the order the matrix uses. ``family`` is the frozen P1/P2/N1 label.
TASKS: tuple[closure2.Task, ...] = closure2.TASKS
TASK_IDS: tuple[str, ...] = tuple(task.task_id for task in TASKS)
TASKS_BY_ID: dict[str, closure2.Task] = {task.task_id: task for task in TASKS}
STRUCTURE_OF: dict[str, str] = {task.task_id: task.family for task in TASKS}

#: The goal the Main receives. It is the task's own public requirement: no decomposition, no worker
#: count, no hint that anything is being measured. The frozen interface contract is a workspace file
#: (``docs/INTERFACE.md``), so the goal does not have to restate it.
GOAL_OF: dict[str, str] = {task.task_id: task.prompt for task in TASKS}

FORBIDDEN_GOAL_PHRASES: tuple[str, ...] = (
    "you must delegate",
    "call delegate_tasks",
    "create two workers",
    "use subworkers",
    "delegate this",
    "this run evaluates",
    "worker profile",
    "coding_worker",
    "research_worker",
    "benchmark",
)

# -- configuration ---------------------------------------------------------------------------

BUDGET: dict[str, Any] = {
    "provider": "deepseek",
    "model": "deepseek-chat",
    "main_max_turns": 20,
    "child_max_turns": 20,
    "worker_max_turns": 20,
    "max_concurrent_children": 2,
    "max_children_per_call": 2,
    "max_delegation_rounds": 3,
    "token_budget": 500_000,
    "result_preview_chars": 900,
    "public_test_max_calls": 4,
    "tool_error_mode": "corrected",
    "network": "none",
    "cpu": 1,
    "memory_gib": 1.0,
    "sandbox_provider": "opensandbox",
    "image": "atlas-uat-python:3.12",
    "router_policy": "m.1.0",
    "sandbox_timeout_minutes": 30,
    "matrix_concurrency": 1,
    "repetitions": 2,
    "seed": matrix.DEFAULT_SEED,
    #: ``deepseek-chat`` accepts no sampling seed; repetitions are the only control over sampling.
    "sampling_seed_supported": False,
}

#: S06 ran with ``child_max_turns=6`` and every profile's own cap below it, so the requested worker
#: turns were never the effective ones. B+ states the effective value and checks it.
S06_REFERENCE: dict[str, Any] = {
    "execution_head": "e0bc2ccd0e7e2ea0e5f8682f03e1cf47dde446b1",
    "execution_freeze": "1.6",
    "executor": "1.2.1",
    "cells": 36,
    "executor_passed": 30,
    "goal_passed": 31,
    "provider_requests": 432,
    "system_tokens": 3_032_172,
    "cost_usd": 0.18207,
    "main_max_turns": 20,
    "child_max_turns": 6,
    "worker_profiles": [RESEARCH],
    "child_write_grants": {},
    "proposals": 0,
    "admissions": 0,
}

PRIMARY_METRICS: tuple[dict[str, str], ...] = (
    {
        "id": "proposal_rate",
        "numerator": "cells with at least one delegate_tasks call",
        "denominator": "cells",
        "missing": "0 only when delegate_tasks was on the surface; otherwise UNKNOWN",
    },
    {
        "id": "valid_proposal_rate",
        "numerator": "cells whose first proposal was admitted",
        "denominator": "cells with a proposal",
        "missing": "UNKNOWN when no proposal exists; never counted as a rejection",
    },
    {
        "id": "admission_rate",
        "numerator": "proposals the router admitted at least one child for",
        "denominator": "proposals",
        "missing": "N/A when no proposal exists; NO_PROPOSAL and PROPOSAL_REJECTED are separate",
    },
    {
        "id": "worker_completion_rate",
        "numerator": "admitted children that ended successfully",
        "denominator": "admitted children",
        "missing": "UNKNOWN when no child was admitted",
    },
    {
        "id": "handoff_delivery_rate",
        "numerator": "cells where a usable handoff reached the Main",
        "denominator": "cells with a completed child",
        "missing": "UNKNOWN when no child completed",
    },
    {
        "id": "main_continuation_rate",
        "numerator": "cells where the Main edited or tested after the handoff",
        "denominator": "cells with a delivered handoff",
        "missing": "UNKNOWN when no handoff was delivered",
    },
    {
        "id": "goal_correctness_rate",
        "numerator": "cells whose public suite and host oracle both passed",
        "denominator": "cells",
        "missing": "counted as FAIL, never dropped",
    },
)

CONTEXT_METRICS: tuple[str, ...] = (
    "main_peak_context_tokens",
    "main_cumulative_input_tokens",
    "main_cache_hit_tokens",
    "main_cache_miss_tokens",
    "main_completion_tokens",
    "child_total_prompt_tokens",
    "child_completion_tokens",
    "system_total_tokens",
    "worker_tool_calls",
    "main_tool_calls",
    "handoff_payload_chars",
    "handoff_reference_count",
    "context_projection_chars",
    "first_delegation_turn",
    "first_implementation_turn",
    "main_continuation_after_handoff",
    "wall_clock_seconds",
    "cost_estimate_usd",
)

#: Pre-registered interpretation. Fixed before any cell runs, so the reading cannot be chosen after
#: the numbers are known.
INTERPRETATION: tuple[dict[str, str], ...] = (
    {
        "id": "I1",
        "observation": "C1 > C0 on proposal_rate",
        "reading": "disclosure alone can change whether a research worker is used",
        "limit": "12 cells per arm; a direction, not a significance claim",
    },
    {
        "id": "I2",
        "observation": "C2 > C0 on proposal_rate",
        "reading": "write-capable workers plus their native disclosure can change delegation",
        "limit": "capability and disclosure are coupled: C2 discloses coding_worker, C0 discloses "
        "research_worker",
    },
    {
        "id": "I3",
        "observation": "C3 > C2 on proposal_rate",
        "reading": "the derived card adds value beyond the prose enumeration on a coding worker",
        "limit": "the two disclosures differ in contract detail, not in the presence of capability "
        "information",
    },
    {
        "id": "I4",
        "observation": "only C3 improves",
        "reading": "possible interaction between capability and disclosure",
        "limit": "an interaction cannot be separated from arm order at 24 cells",
    },
    {
        "id": "I5",
        "observation": "proposals rise but admissions do not",
        "reading": "the bottleneck is proposal quality or the router, not disclosure",
        "limit": "read the per-proposal reason codes before concluding",
    },
    {
        "id": "I6",
        "observation": "admissions rise but worker completion does not",
        "reading": "the bottleneck is the worker contract, its tools or its budget",
        "limit": "check child stop reasons and ownership denials",
    },
    {
        "id": "I7",
        "observation": "completion rises but handoff or continuation does not",
        "reading": "the bottleneck is how results are projected into Main context",
        "limit": "check handoff payload size and the preview bound",
    },
    {
        "id": "I8",
        "observation": "L1-L5 improve but L6 does not",
        "reading": "no claim about final task performance may be made",
        "limit": "L6 is correctness of the run and is never attributed to delegation",
    },
    {
        "id": "I9",
        "observation": "all four arms stay at NO_PROPOSAL",
        "reading": "the capability hypothesis does not explain S06; task, model or decision policy "
        "is the next place to look",
        "limit": "no arm may be declared best",
    },
)


def cells() -> list[dict[str, Any]]:
    """The 24-cell matrix, in execution order. Same generator Stage B used, larger arm set."""

    return matrix.build_plan(
        TASK_IDS,
        ARM_IDS,
        repetitions=int(BUDGET["repetitions"]),
        seed=int(BUDGET["seed"]),
    )


def balance(cell_list: list[dict[str, Any]]) -> dict[str, Any]:
    """The properties this design promises, measured on the plan itself.

    ``each_arm_in_each_round`` is deliberately **not** promised: with four arms and three tasks a
    round can hold only three arms. What is promised is that every task meets every arm once per
    repetition and that no arm is systematically earlier.
    """

    per_task_arm: dict[tuple[str, str], int] = {}
    for cell in cell_list:
        key = (cell["task_id"], cell["arm_id"])
        per_task_arm[key] = per_task_arm.get(key, 0) + 1
    counts = {arm: sum(1 for cell in cell_list if cell["arm_id"] == arm) for arm in ARM_IDS}
    first_arm: dict[int, str] = {}
    for cell in cell_list:
        first_arm.setdefault(cell["repetition"], cell["arm_id"])
    distinct = len(set(first_arm.values())) == len(first_arm) if len(first_arm) > 1 else True
    return {
        "cells": len(cell_list),
        "each_task_meets_each_arm": all(
            per_task_arm.get((task, arm)) == int(BUDGET["repetitions"])
            for task in TASK_IDS
            for arm in ARM_IDS
        ),
        "every_arm_run_equally_often": len(set(counts.values())) == 1,
        "arm_cell_counts": counts,
        "first_arm_by_repetition": first_arm,
        "first_arm_differs_across_repetitions": distinct,
        "each_arm_in_each_round": None,
    }


def task_record(task: closure2.Task) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "family": task.family,
        "title": task.title,
        "goal": GOAL_OF[task.task_id],
        "goal_digest": "sha256:" + hashlib.sha256(GOAL_OF[task.task_id].encode()).hexdigest(),
        "write_scope": list(task.allowed_paths),
        "deliverables": list(task.deliverables),
        "forbidden_paths": list(task.forbidden_paths),
        "seed_files": sorted(task.seed),
        "seed_digest": canonical_digest(dict(task.seed)),
        "reference_files": sorted(task.reference),
        "reference_digest": canonical_digest(dict(task.reference)),
        "oracle_files": sorted(task.oracle_tests),
        "oracle_digest": canonical_digest(dict(task.oracle_tests)),
        "public_tests": sorted(task.public_tests),
        # The DAG is recorded here for audit only. It is never given to the Main.
        "frozen_workers": [
            {"task_id": worker["task_id"], "owned_paths": list(worker["owned_paths"])}
            for worker in task.workers
        ],
        "main_visible_inputs": {
            "goal": True,
            "workspace": True,
            "frozen_dag": False,
            "worker_assignment": False,
            "placement": False,
            "oracle": False,
            "reference": False,
        },
    }


def arm_record(arm: Arm) -> dict[str, Any]:
    return {
        "arm_id": arm.arm_id,
        "capability": arm.capability,
        "profile_id": arm.profile_id,
        "worker_profiles": [arm.profile_id],
        "exposure": arm.exposure,
        "description": arm.description,
        "child_write_grants": "per task write_scope" if arm.profile_id == CODING else "none",
        "budget": dict(BUDGET),
    }


def protocol_document() -> dict[str, Any]:
    """The candidate protocol, built from files and declared constants."""

    p3 = p3_text()
    bplus, removed = remove_capability_section(p3)
    cell_list = cells()
    return {
        "stage": STAGE_ID,
        "protocol_version": PROTOCOL_VERSION,
        "supersedes": SUPERSEDES,
        "does_not_modify": list(DOES_NOT_MODIFY),
        "prompts": {
            "p3-literal": {
                "version": P3_VERSION,
                "path": "experiments/stage_s2_prompt_v21/prompts/main_v21.md",
                "chars": len(p3),
                "digest": "sha256:" + hashlib.sha256(p3.encode("utf-8")).hexdigest(),
                "declared_digest": P3_DIGEST,
            },
            "p3-bplus": {
                "version": BPLUS_VERSION,
                "derived_from": "p3-literal",
                "chars": len(bplus),
                "digest": "sha256:" + hashlib.sha256(bplus.encode("utf-8")).hexdigest(),
                "removed_section": {
                    "heading": _SECTION_START,
                    "chars": len(removed),
                    "digest": "sha256:" + hashlib.sha256(removed.encode("utf-8")).hexdigest(),
                },
            },
            "selected": "p3-bplus",
        },
        "arms": [arm_record(arm) for arm in ARMS],
        "tasks": [task_record(task) for task in TASKS],
        "structures": [task.family for task in TASKS],
        "run_config": dict(BUDGET),
        "s06_reference": dict(S06_REFERENCE),
        "delegation_ladder": {name: text for name, text in DELEGATION_LEVELS},
        "proposal_states": list(PROPOSAL_STATES),
        "primary_metrics": [dict(item) for item in PRIMARY_METRICS],
        "context_metrics": list(CONTEXT_METRICS),
        "interpretation": [dict(item) for item in INTERPRETATION],
        "plan": {
            "seed": BUDGET["seed"],
            "repetitions": BUDGET["repetitions"],
            "cells": cell_list,
            "digest": matrix.plan_digest(cell_list),
            "balance": balance(cell_list),
        },
        "authorization": {
            "state": "PENDING_OWNER_APPROVAL",
            "live_execution_authorized": False,
            "cells_allowed": 0,
            "deepseek_calls_allowed": 0,
            "sandboxes_allowed": 0,
            "reuses_s06_or_s08_balance": False,
        },
    }


def validate(document: dict[str, Any]) -> list[str]:
    """Every invariant the candidate document promises, checked on the document."""

    problems: list[str] = []
    if document.get("stage") != STAGE_ID:
        problems.append("stage is not B+")
    if document.get("protocol_version") != PROTOCOL_VERSION:
        problems.append("protocol_version differs from the code")
    if document.get("supersedes") is not None:
        problems.append("a candidate contract must not claim to supersede a frozen document")

    prompts = document.get("prompts") or {}
    literal = prompts.get("p3-literal") or {}
    if literal.get("digest") != P3_DIGEST:
        problems.append("the P3 literal digest moved; the S2/S06 identity would change")
    bplus = prompts.get("p3-bplus") or {}
    if not bplus.get("digest") or bplus.get("digest") == literal.get("digest"):
        problems.append("the B+ prompt is not a distinct object from P3")
    if int(bplus.get("chars") or 0) >= int(literal.get("chars") or 0):
        problems.append("the B+ prompt is not smaller than P3; the removed section grew")

    arms = document.get("arms") or []
    if [item.get("arm_id") for item in arms] != list(ARM_IDS):
        problems.append("arms are not C0..C3")
    for arm in arms:
        if arm.get("exposure") not in EXPOSURES:
            problems.append(f"{arm.get('arm_id')} has an unknown exposure level")
        if arm.get("profile_id") not in (RESEARCH, CODING):
            problems.append(f"{arm.get('arm_id')} offers an unregistered profile")
        if arm.get("budget") != dict(BUDGET):
            problems.append(f"{arm.get('arm_id')} does not share the frozen budget")
    pairs = {(arm.get("capability"), arm.get("exposure")) for arm in arms}
    if len(pairs) != len(arms):
        problems.append("the four arms do not cross capability with exposure exactly once")

    tasks = document.get("tasks") or []
    if [item.get("task_id") for item in tasks] != list(TASK_IDS):
        problems.append("tasks are not the three frozen Closure-2 tasks")
    for task in tasks:
        goal = str(task.get("goal") or "").lower()
        for phrase in FORBIDDEN_GOAL_PHRASES:
            if phrase in goal:
                problems.append(f"{task.get('task_id')} goal mentions {phrase!r}")
        visible = task.get("main_visible_inputs") or {}
        for hidden in ("frozen_dag", "worker_assignment", "placement", "oracle", "reference"):
            if visible.get(hidden) is not False:
                problems.append(f"{task.get('task_id')} exposes {hidden} to the Main")
        scope = [str(path).rstrip("*").rstrip("/") for path in task.get("write_scope") or []]
        for deliverable in task.get("deliverables") or []:
            if not any(str(deliverable).startswith(item + "/") for item in scope):
                problems.append(f"{deliverable} is outside the declared write scope")

    plan = document.get("plan") or {}
    cell_list = plan.get("cells") or []
    expected = len(ARM_IDS) * len(TASK_IDS) * int(BUDGET["repetitions"])
    if len(cell_list) != expected:
        problems.append(f"plan has {len(cell_list)} cells, expected {expected}")
    if len({cell["cell_id"] for cell in cell_list}) != len(cell_list):
        problems.append("cell ids are not unique")
    if plan.get("digest") != matrix.plan_digest(cell_list):
        problems.append("plan digest does not match its cells")
    plan_balance = plan.get("balance") or {}
    for key in ("each_task_meets_each_arm", "every_arm_run_equally_often"):
        if not plan_balance.get(key):
            problems.append(f"plan is not balanced: {key}")
    if not plan_balance.get("first_arm_differs_across_repetitions"):
        problems.append("the same arm opens both repetitions")

    config = document.get("run_config") or {}
    if config.get("token_budget") != 500_000:
        problems.append("token budget is not the candidate 500000")
    if config.get("worker_max_turns") != config.get("child_max_turns"):
        problems.append("the worker turn cap would be clipped by the delegation limit")
    if config.get("network") != "none":
        problems.append("network is not none")
    if config.get("max_children_per_call") > config.get("max_concurrent_children"):
        problems.append("a proposal wave can be wider than the concurrency limit")

    auth = document.get("authorization") or {}
    for key in (
        "live_execution_authorized",
        "deepseek_calls_allowed",
        "sandboxes_allowed",
        "cells_allowed",
        "reuses_s06_or_s08_balance",
    ):
        value = auth.get(key)
        if value not in (False, 0):
            problems.append(
                f"authorization.{key} is {value!r}; a preparation stage authorizes none"
            )
    return problems


def write_document(document: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


__all__ = [
    "ARM_IDS",
    "ARMS",
    "ARMS_BY_ID",
    "Arm",
    "BPLUS_VERSION",
    "BUDGET",
    "CODING",
    "CONTEXT_METRICS",
    "DELEGATION_LEVELS",
    "EXPOSURES",
    "FORBIDDEN_GOAL_PHRASES",
    "GOAL_OF",
    "INTERPRETATION",
    "P3_DIGEST",
    "P3_LITERAL",
    "PRIMARY_METRICS",
    "PROMPT_VARIANTS",
    "PROPOSAL_STATES",
    "PROTOCOL_VERSION",
    "RESEARCH",
    "S06_REFERENCE",
    "STAGE_ID",
    "STRUCTURE_OF",
    "TASKS",
    "TASKS_BY_ID",
    "TASK_IDS",
    "TASK_WRITE_SCOPE",
    "balance",
    "cells",
    "p3_text",
    "prompt_text",
    "prompt_version",
    "protocol_document",
    "remove_capability_section",
    "task_record",
    "validate",
    "write_document",
]
