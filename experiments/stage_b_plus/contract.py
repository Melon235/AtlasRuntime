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
from collections.abc import Mapping
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
PROTOCOL_VERSION = "bplus-2.2.0"
#: What this contract is *not*: it supersedes no frozen Stage B document and changes none of them.
SUPERSEDES: None = None
DOES_NOT_MODIFY = (
    "experiments/stage_benchmark/execution/freeze-1.6.json",
    "experiments/stage_benchmark/protocol.json",
    "experiments/evaluation/closure2/*",
)

#: The one execution this stage has actually performed, kept as a record rather than as a note.
#:
#: v1 was authorized at ``a4e1836`` and dispatched once. It stopped on the first cell, before any
#: model call, because the runtime's admission demanded a dynamic acceptance command. The claim is
#: consumed and stays consumed; the authorization does not migrate to v2.
SUPERSEDED_CANARY: dict[str, Any] = {
    "plan_id": "stage-b-plus-diagnostic-canary-v1",
    "plan_digest": ("sha256:7ae71273eea29f489edc657ada9a26c834e178289ac6744807350d674dc53575"),
    "execution_head": "a4e1836b3953207db262d28dc4d58e32b66d2f35",
    "executor_version": "bplus-executor-1.1",
    "executor_digest": ("sha256:4826d81acf1df64fa9754ba8f85fdf34600ff9ea4096fec8dc08b7f0ee138c73"),
    "claim": "CONSUMED",
    "attempt_limit": 1,
    "cells_planned": 6,
    "cells_started": 1,
    "cells_valid": 0,
    "cells_not_run": 5,
    "model_requests": 0,
    "sandbox_creations": 0,
    "oracle_executions": 0,
    "process_exit": 4,
    "raw_stop_reason": "PROVIDER_FAILURE",
    "classification": "LIVE_PREFLIGHT_CONTRACT_MISMATCH",
    "authorization_migrates": False,
}

#: v2 ran for real and stopped on an executor evidence defect, not on the model.
SUPERSEDED_CANARY_V2: dict[str, Any] = {
    "plan_id": "stage-b-plus-diagnostic-canary-v2",
    "plan_digest": ("sha256:41fc36da78feb51a4fa8462b7901bd3f41c6573105ad0d93486bafde4ddd5046"),
    "execution_head": "106939ed470e84339cc53ae98b5a62800db8ab23",
    "protocol_version": "bplus-2.1.0",
    "protocol_digest": ("sha256:5ab5d4787753429b95f096ff187175a7fa38bdaeac08f164808ba8ca39880154"),
    "executor_version": "bplus-executor-1.2",
    "executor_digest": ("sha256:1c2f1338fa02f2de961301193dcdb7df43ee54f2393e3f7383c70c0687146f71"),
    "claim": "CONSUMED",
    "attempt_limit": 1,
    "cells_planned": 6,
    "cells_started": 1,
    "cells_valid": 0,
    "cells_not_run": 5,
    "model_requests": 12,
    "sandbox_creations": 2,
    "oracle_executions": 1,
    "process_exit": 4,
    "raw_stop_reason": "EXPORT_INVALID",
    "classification": "EXECUTOR_EVIDENCE_CONTRACT_MISMATCH",
    "observed": {
        "agent_success": True,
        "public_tests_final": "PASS",
        "independent_oracle": "PASS",
        "delegation": "NO_PROPOSAL",
        "system_tokens": 92_097,
        "estimated_cost_usd": 0.006837,
    },
    "defects": [
        "the export audit compared the producer's archive digest with a directory hash",
        "the audit read a top-level public_tests.status the producer never writes",
    ],
    "authorization_migrates": False,
}

#: Every execution this stage has performed, newest last. A claim is consumed for ever.
SUPERSEDED_CANARIES: tuple[dict[str, Any], ...] = (SUPERSEDED_CANARY, SUPERSEDED_CANARY_V2)

#: The approved revision. ``bplus-1.0.0`` was a candidate that was never authorized and never ran.
#: ``bplus-2.0.0`` *was* authorized and dispatched once: the record below says what happened to it,
#: because a revision that supersedes an executed identity has to say so.
REVISION: dict[str, Any] = {
    "version": "bplus-2.2.0",
    "previous_version": "bplus-2.1.0",
    "previous_protocol_digest": (
        "sha256:5ab5d4787753429b95f096ff187175a7fa38bdaeac08f164808ba8ca39880154"
    ),
    "previous_canary_plan_digest": (
        "sha256:41fc36da78feb51a4fa8462b7901bd3f41c6573105ad0d93486bafde4ddd5046"
    ),
    "previous_formal_plan_digest": (
        "sha256:ab4d0e509962885c9a24d981446c4697f00e794b6b03e5a5d9bf42cfe91aee72"
    ),
    "previous_protocol_version": "bplus-2.1.0",
    "previous_execution": dict(SUPERSEDED_CANARY_V2),
    "approval": "OWNER_DESIGN_DECISIONS_CONFIRMED on 2026-11-25",
    "changes": [
        "R-B+1 accepted: main-v2.1-rc1-bplus becomes the matrix prompt",
        "G-B+2 accepted: the Main verifies; no worker test tool is added",
        "G-B+3 accepted: the router rules are unchanged",
        "two control arms C4 (research/none) and C5 (coding/none) added for a diagnostic canary",
        "a six-cell canary plan with its own identity, claim namespace and authorization",
        "an executable entry point with an authority contract, a one-shot dispatch claim and a "
        "post-cell gate",
        "the formal non-delegation scenario is no longer described as a mathematical lower bound",
        "the acceptance contract declares its oracle mode: IN_RUN_ORACLE or "
        "EXTERNAL_SANDBOX_ORACLE",
        "a live dispatch admits the six production configurations before the claim is written",
        "an admission refusal is classified as LIVE_PREFLIGHT_BLOCKED, not as a provider failure",
        "the executor identity covers the two files that decide what admission allows",
        "the export audit keeps the producer's archive digest and the host's directory digest "
        "apart, and never compares them",
        "the export audit recomputes the host directory identity and the file count at audit time",
        "the public-test verdict is read from the recorded runs, last run wins, NOT_RUN is not a "
        "pass",
        "the public-test verdict refuses an incomplete observation, a foreign suite identity, a "
        "non-contiguous call index and a status that contradicts its exit code",
        "the export identity requires the same scoring path and the same archive digest on both "
        "the runner and the session, and a directory identity the runner actually took",
        "the observed cost floor sums per-cell costs instead of running totals",
        "a READY admission report is written to the evidence root, not a null",
    ],
    "unchanged": [
        "the four-arm formal matrix (same cells, same order, same digest)",
        "the three Closure-2 tasks, the prompts, the model and every budget field",
        "Stage B Freeze 1.6, the Closure-2 tree and the S06/S08 authorization remain untouched",
        "the six canary cells: same arms, same order, same task, same prompt, same turns",
    ],
}

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

#: What each exposure level *is*. ``native`` is new in this stage, not a reproduction of S06's
#: exposure: S06 had no derived disclosure at all, and its only capability text was the hand-written
#: prompt section this revision removes. Calling ``native`` "the existing mechanism" would be wrong.
EXPOSURE_TERMS: dict[str, dict[str, Any]] = {
    "none": {
        "label": "shipped",
        "definition": "the shipped delegate_tasks description, byte for byte, no derived "
        "information",
        "new_in_this_stage": False,
        "provides_role": False,
        "provides_tools": False,
        "provides_contract": False,
        "arms": ["C4", "C5"],
    },
    "native": {
        "label": "basic derived disclosure",
        "definition": "a derived prose enumeration of the role, the tools the profile has, "
        "the tools it does not have, its limits and how a result comes back",
        "new_in_this_stage": True,
        "provides_role": True,
        "provides_tools": True,
        "provides_contract": False,
        "arms": ["C0", "C2"],
    },
    "card": {
        "label": "full capability contract",
        "definition": "the basic disclosure plus structured capability, ownership, task-input, "
        "deliverable, verification and handoff contracts",
        "new_in_this_stage": True,
        "provides_role": True,
        "provides_tools": True,
        "provides_contract": True,
        "arms": ["C1", "C3"],
    },
}

#: Limitations that must travel with the result. Each one is a place where a naive reading of the
#: numbers would be wrong, so they are part of the contract rather than a footnote in a report.
COUPLING_DISCLOSURE: tuple[dict[str, str], ...] = (
    {
        "id": "X1",
        "statement": "the offered worker profile and the Main-facing description are coupled",
        "detail": "an arm cannot offer coding_worker while disclosing research_worker; the "
        "is derived from the profile that will actually run",
    },
    {
        "id": "X2",
        "statement": "native and card differ in content and length, not only in structure",
        "detail": "card adds six contract fields, roughly 2.2-2.5k characters over native",
    },
    {
        "id": "X3",
        "statement": "the delegate_tasks schema still defaults profile_id to research_worker",
        "detail": "the frozen schema has no enum and cannot be edited in this stage, so a proposal "
        "that omits profile_id in a coding-only arm is refused UNKNOWN_PROFILE",
    },
    {
        "id": "X4",
        "statement": "C5 may lose proposals to the schema default rather than to disclosure",
        "detail": "UNKNOWN_PROFILE is counted separately and is not read as a disclosure effect",
    },
    {
        "id": "X5",
        "statement": "NO_PROPOSAL is not ROUTER_REJECTED",
        "detail": "a cell that never called the tool has no admission denominator; N/A, not 0",
    },
    {
        "id": "X6",
        "statement": "C2 - C0 is not a pure permission effect",
        "detail": "it changes both which worker exists and what the Main is told about it",
    },
    {
        "id": "X7",
        "statement": "C4 and C5 send byte-identical requests",
        "detail": "with no disclosure the two control arms differ only in which profile the run "
        "accepts, so a C5 - C4 difference can come from the schema default or from the router, "
        "never from what the Main was told",
    },
)


@dataclass(frozen=True)
class Arm:
    arm_id: str
    capability: str
    profile_id: str
    exposure: str
    description: str
    #: A control arm is diagnostic only. It is never part of the formal matrix and its cells are
    #: marked ``is_benchmark_sample = false``.
    is_control: bool = False

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
        description="research_worker with the basic derived disclosure",
    ),
    Arm(
        arm_id="C1",
        capability="research",
        profile_id=RESEARCH,
        exposure="card",
        description="research_worker with the full capability card",
    ),
    Arm(
        arm_id="C2",
        capability="coding",
        profile_id=CODING,
        exposure="native",
        description="coding_worker with the basic derived disclosure",
    ),
    Arm(
        arm_id="C3",
        capability="coding",
        profile_id=CODING,
        exposure="card",
        description="coding_worker with the full capability card",
    ),
    Arm(
        arm_id="C4",
        capability="research",
        profile_id=RESEARCH,
        exposure="none",
        description="research_worker with the shipped description and no derived disclosure",
        is_control=True,
    ),
    Arm(
        arm_id="C5",
        capability="coding",
        profile_id=CODING,
        exposure="none",
        description="coding_worker with the shipped description and no derived disclosure",
        is_control=True,
    ),
)

ARMS_BY_ID: dict[str, Arm] = {arm.arm_id: arm for arm in ARMS}
ARM_IDS: tuple[str, ...] = tuple(arm.arm_id for arm in ARMS)

#: The formal matrix is the four-arm crossed design. The two control arms are diagnostic only and
#: stay out of it until the canary is reviewed.
FORMAL_ARM_IDS: tuple[str, ...] = tuple(arm.arm_id for arm in ARMS if not arm.is_control)
CONTROL_ARM_IDS: tuple[str, ...] = tuple(arm.arm_id for arm in ARMS if arm.is_control)

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


def child_grants_for(cell: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """The Host write grants a cell executes with.

    A Stage B+ cell carries none of its own, so the arm's grant for the task applies unchanged. A
    diagnostic plan may bind its own grants into the cell — and therefore into the plan digest —
    when it needs a representation the arm cannot express (Stage C1 normalises ``plugins/**`` to
    ``plugins/`` so the frozen router's own matcher accepts it). Nothing else about the cell
    changes, and the profile that receives the grant is still the arm's profile.
    """

    override = cell.get("child_write_grants")
    if isinstance(override, Mapping) and override:
        return {
            str(profile_id): tuple(str(path) for path in paths)
            for profile_id, paths in override.items()
        }
    return ARMS_BY_ID[cell["arm_id"]].grant_for(cell["task_id"])


def goal_for(cell: Mapping[str, Any]) -> str:
    """The goal a cell executes.

    A Stage B+ cell carries no ``goal`` of its own, so it resolves to the frozen task prompt: byte
    for byte what every B+ run executed. A diagnostic plan may bind its own goal into the cell (and
    therefore into the plan digest); that text wins, and nothing else about the cell changes.
    """

    goal = cell.get("goal")
    if isinstance(goal, str) and goal:
        return goal
    return GOAL_OF[cell["task_id"]]


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
    """The 24-cell formal matrix, in execution order.

    Deliberately *unchanged* from ``bplus-1.0.0``: the same tasks, the same four arms, the same
    seed and the same repetition count, so ``plan_digest`` still returns ``ab4d0e50…``. A reader can
    therefore verify that approving this revision does not silently move the formal matrix.
    """

    return matrix.build_plan(
        TASK_IDS,
        FORMAL_ARM_IDS,
        repetitions=int(BUDGET["repetitions"]),
        seed=int(BUDGET["seed"]),
    )


CANARY_PLAN_ID = "stage-b-plus-diagnostic-canary-v3"
#: v1 and v2 were each dispatched once and both claims are consumed. The ids are kept here so a
#: reader can find them, and ``scope_plan`` refuses them: neither can be rebuilt into a dispatchable
#: record.
CANARY_PLAN_ID_V1 = "stage-b-plus-diagnostic-canary-v1"
CANARY_PLAN_ID_V2 = "stage-b-plus-diagnostic-canary-v2"
SUPERSEDED_CANARY_PLAN_IDS: tuple[str, ...] = (CANARY_PLAN_ID_V1, CANARY_PLAN_ID_V2)
FORMAL_PLAN_ID = "stage-b-plus-formal-matrix-v1"
SCOPE_CANARY = "CANARY_ONLY"
SCOPE_FORMAL = "FORMAL_MATRIX_ONLY"
SCOPES: tuple[str, ...] = (SCOPE_CANARY, SCOPE_FORMAL)

#: The execution contract, as a value.
#:
#: v1 was refused by ``run_goal``'s live admission because the B+ acceptance is static and the
#: oracle is external, while the admission contract demanded an in-run dynamic command. That is a
#: change to *what it means to run a cell*, so it belongs in the plan identity: the digest below is
#: stamped into every canary cell, which is why the v2 plan digest cannot be produced by renaming.
EXECUTION_CONTRACT: dict[str, Any] = {
    "revision": "bplus-exec-3.1",
    "supersedes": "bplus-exec-3.0",
    "oracle_mode": "EXTERNAL_SANDBOX_ORACLE",
    "acceptance": "static presence checks only; correctness is never self-graded",
    "oracle_runner": "experiments.evaluation.sandboxed_oracle::run_hidden_tests_in_sandbox",
    "scored_snapshot": "the exported workspace (artifacts.workspace_path), never the fixture",
    "public_suite_separation": "the public suite is a different artifact from the hidden oracle",
    "pre_claim_preflight": "run_live_preflight over the six production GoalRunOptions",
    # The two evidence contracts v2 got wrong, stated where the plan digest can bind them.
    "export_identity": {
        "archive": "artifacts.workspace_digest — the producer's sha256 of the transferred archive; "
        "recorded, not recomputable, never compared with a directory hash",
        "directory": "directory_digest — computed by the host after export and before the oracle, "
        "recomputed at audit time",
        "files": "workspace_files must equal the number of files the directory holds",
        "same_object": "the runner's workspace_path and the session's must name the same directory",
        "agreement": "the runner's and the session's archive digest must be the same value",
        "provenance": "the directory identity must come from the runner; a value that exists only "
        "in the session has no source and is refused",
        "fail_closed": "a missing directory identity, a changed directory, a count mismatch, a "
        "diverged path or a diverged archive digest stops the matrix",
    },
    "public_test_evidence": {
        "source": "session.public_tests.runs — one entry per call, no top-level status",
        "rule": "the last recorded run decides; a final record without a status is not the earlier "
        "PASS",
        "not_run": "a suite that never ran is NOT_RUN and is never a pass",
        "error": "an ERROR run leaves correctness unknown, it is not a failure and not a pass",
        "completeness": "calls must equal the number of recorded runs; a call that returned no "
        "record leaves the last observation unknown and is INCOMPLETE",
        "identity": "every run must name the frozen test id and command digest for its cell",
        "order": "call_index must be the contiguous position of the record",
        "outcome": "PASS must exit 0 and FAIL must exit non-zero; a contradiction is INVALID",
        "required": "B+ always configures the public suite; NOT_CONFIGURED is refused",
        "classification": "INCOMPLETE and INVALID are evidence-integrity stops; FAIL continues "
        "the matrix and is scored as a measured failure",
    },
    "v1_refusal": (
        "live_preflight required a dynamic acceptance command; the B+ acceptance has none, and the "
        "oracle is a Host sandbox that runs after the run, so every cell was refused before the "
        "first model call"
    ),
    "v2_refusal": (
        "the audit compared the producer's archive digest with a directory hash and read a "
        "top-level public_tests.status that the producer never writes, so a cell whose independent "
        "oracle passed was recorded as an integrity failure"
    ),
}
EXECUTION_CONTRACT_DIGEST = "sha256:" + canonical_digest(EXECUTION_CONTRACT)

#: The canary runs one P1 task, and stays on P1: its plan digest is consumed and must not move.
#:
#: The reason first written here — "P1 is the only family with two genuinely disjoint module write
#: sets, so it is the only one where a coding pair can be admitted at all" — is **wrong**, and Stage
#: C1 measured it rather than assuming: P2's ``csv`` and ``json`` steps declare disjoint owned paths
#: (``plugins/csv_*.py`` versus ``plugins/json_*.py``) and the frozen router admits both, with
#: ``DELEGATION_ADMITTED`` and route ``delegate_parallel``
#: (``experiments/stage_c1/checks.py``, check C05). P1 remains the canary's task because that
#: identity is frozen, not because P2 is inadmissible.
CANARY_TASK_IDS: tuple[str, ...] = ("closure2-pipeline-migration",)


def canary_cells() -> list[dict[str, Any]]:
    """Six cells, one per arm, on one task, one repetition, in fixed arm order.

    The order is not randomized: six cells are a mechanism diagnostic, and a fixed order makes the
    report readable. ``cell_id`` carries the ``canary.`` prefix so it cannot collide with a formal
    cell id, and every cell is marked ``is_benchmark_sample = False`` because these cells are not
    evidence for the formal matrix.
    """

    base = matrix.build_plan(
        CANARY_TASK_IDS,
        ARM_IDS,
        repetitions=1,
        seed=int(BUDGET["seed"]),
    )
    cells_out: list[dict[str, Any]] = []
    for order, cell in enumerate(base, start=1):
        cells_out.append(
            {
                **cell,
                "order": order,
                "cell_id": f"canary.{cell['cell_id']}",
                "scope": SCOPE_CANARY,
                "plan_id": CANARY_PLAN_ID,
                "is_benchmark_sample": False,
                # The contract revision is part of the cell, so it is part of the plan digest. A
                # rename alone cannot produce a new identity.
                "execution_contract": EXECUTION_CONTRACT_DIGEST,
            }
        )
    return cells_out


def scope_plan(scope_or_id: str) -> dict[str, Any]:
    """The plan record for one scope. ``scope_or_id`` accepts a scope type or a plan id."""

    # A Stage C1 / C1-B diagnostic plan declares itself in its own module; this is the one seam, so
    # a new single-cell plan reuses ``executor.dispatch`` instead of a second executor.
    from experiments.stage_c1 import plan as c1_plan
    from experiments.stage_c1b import plan as c1b_plan

    if scope_or_id in (c1_plan.SCOPE, c1_plan.PLAN_ID):
        return c1_plan.plan_record()
    if scope_or_id in (c1b_plan.SCOPE, c1b_plan.PLAN_ID):
        return c1b_plan.plan_record()
    if scope_or_id in SUPERSEDED_CANARY_PLAN_IDS:
        raise ValueError(
            f"{scope_or_id} was dispatched once and its claim is consumed; it is superseded by "
            f"{CANARY_PLAN_ID} and cannot be rebuilt into a dispatchable plan"
        )
    if scope_or_id in (SCOPE_CANARY, CANARY_PLAN_ID):
        scope, plan_id = SCOPE_CANARY, CANARY_PLAN_ID
        plan_cells = canary_cells()
        task_ids = list(CANARY_TASK_IDS)
        arm_ids = list(ARM_IDS)
        repetitions = 1
    elif scope_or_id in (SCOPE_FORMAL, FORMAL_PLAN_ID):
        scope, plan_id = SCOPE_FORMAL, FORMAL_PLAN_ID
        plan_cells = cells()
        task_ids = list(TASK_IDS)
        arm_ids = list(FORMAL_ARM_IDS)
        repetitions = int(BUDGET["repetitions"])
    else:
        raise ValueError(f"unknown scope {scope_or_id!r}; expected one of {list(SCOPES)}")
    return {
        "plan_id": plan_id,
        "scope": scope,
        "tasks": task_ids,
        "arms": arm_ids,
        "repetitions": repetitions,
        "cells_count": len(plan_cells),
        "cells": plan_cells,
        "digest": matrix.plan_digest(plan_cells),
        "balance": balance(plan_cells),
        "claim_namespace": f"bplus-claim/{plan_id}",
        "runs_subdir": f"runs/stage-b-plus/{plan_id}",
        "evidence_subdir": f"evidence/stage-b-plus/{plan_id}",
        "is_benchmark_sample": scope == SCOPE_FORMAL,
        "nominal_token_capacity": len(plan_cells) * int(BUDGET["token_budget"]),
        "execution_contract": dict(EXECUTION_CONTRACT),
        "execution_contract_digest": EXECUTION_CONTRACT_DIGEST,
        "supersedes": CANARY_PLAN_ID_V2 if scope == SCOPE_CANARY else None,
        "supersedes_record": dict(SUPERSEDED_CANARY_V2) if scope == SCOPE_CANARY else None,
        "execution_history": [dict(item) for item in SUPERSEDED_CANARIES]
        if scope == SCOPE_CANARY
        else [],
        # An approval for one plan never carries over to another: the Owner signs a digest.
        "authorization_migrates": False,
    }


def balance(
    cell_list: list[dict[str, Any]], arm_ids: tuple[str, ...] | None = None
) -> dict[str, Any]:
    """The properties this design promises, measured on the plan itself.

    ``each_arm_in_each_round`` is deliberately **not** promised: with four arms and three tasks a
    round can hold only three arms. What is promised is that every task meets every arm once per
    repetition and that no arm is systematically earlier.
    """

    arm_ids = (
        tuple(arm_ids)
        if arm_ids is not None
        else tuple(dict.fromkeys(cell["arm_id"] for cell in cell_list))
    )
    task_ids = tuple(dict.fromkeys(cell["task_id"] for cell in cell_list))
    per_task_arm: dict[tuple[str, str], int] = {}
    for cell in cell_list:
        key = (cell["task_id"], cell["arm_id"])
        per_task_arm[key] = per_task_arm.get(key, 0) + 1
    counts = {arm: sum(1 for cell in cell_list if cell["arm_id"] == arm) for arm in arm_ids}
    per_repetition = len(cell_list) // max(len(arm_ids) * len(task_ids), 1)
    first_arm: dict[int, str] = {}
    for cell in cell_list:
        first_arm.setdefault(cell["repetition"], cell["arm_id"])
    distinct = len(set(first_arm.values())) == len(first_arm) if len(first_arm) > 1 else True
    return {
        "cells": len(cell_list),
        "arms": list(arm_ids),
        "tasks": list(task_ids),
        "each_task_meets_each_arm": all(
            per_task_arm.get((task, arm)) == per_repetition for task in task_ids for arm in arm_ids
        ),
        "each_arm_exactly_once_per_task": len(cell_list)
        == len(arm_ids) * len(task_ids) * per_repetition,
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
        "exposure_term": EXPOSURE_TERMS[arm.exposure]["label"],
        "description": arm.description,
        "is_control": arm.is_control,
        "in_formal_matrix": arm.arm_id in FORMAL_ARM_IDS,
        "child_write_grants": "per task write_scope" if arm.profile_id == CODING else "none",
        "budget": dict(BUDGET),
    }


def protocol_document() -> dict[str, Any]:
    """The candidate protocol, built from files and declared constants."""

    p3 = p3_text()
    bplus, removed = remove_capability_section(p3)
    return {
        "stage": STAGE_ID,
        "protocol_version": PROTOCOL_VERSION,
        "supersedes": SUPERSEDES,
        "revision": dict(REVISION),
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
        "exposure_terms": {key: dict(value) for key, value in EXPOSURE_TERMS.items()},
        "coupling_disclosure": [dict(item) for item in COUPLING_DISCLOSURE],
        "tasks": [task_record(task) for task in TASKS],
        "structures": [task.family for task in TASKS],
        "run_config": dict(BUDGET),
        "execution_contract": dict(EXECUTION_CONTRACT),
        "execution_contract_digest": EXECUTION_CONTRACT_DIGEST,
        "superseded_executions": [dict(item) for item in SUPERSEDED_CANARIES],
        "s06_reference": dict(S06_REFERENCE),
        "delegation_ladder": {name: text for name, text in DELEGATION_LEVELS},
        "proposal_states": list(PROPOSAL_STATES),
        "primary_metrics": [dict(item) for item in PRIMARY_METRICS],
        "context_metrics": list(CONTEXT_METRICS),
        "interpretation": [dict(item) for item in INTERPRETATION],
        "plans": {
            plan["plan_id"]: plan for plan in (scope_plan(SCOPE_CANARY), scope_plan(SCOPE_FORMAL))
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
        problems.append(f"arms are not {list(ARM_IDS)}")
    for arm in arms:
        if arm.get("exposure") not in EXPOSURES:
            problems.append(f"{arm.get('arm_id')} has an unknown exposure level")
        if arm.get("profile_id") not in (RESEARCH, CODING):
            problems.append(f"{arm.get('arm_id')} offers an unregistered profile")
        if arm.get("budget") != dict(BUDGET):
            problems.append(f"{arm.get('arm_id')} does not share the frozen budget")
        if arm.get("is_control") and arm.get("in_formal_matrix"):
            problems.append(f"{arm.get('arm_id')} is a control arm inside the formal matrix")
    pairs = {(arm.get("capability"), arm.get("exposure")) for arm in arms}
    if len(pairs) != len(arms):
        problems.append("the six arms do not cross capability with exposure exactly once")
    if {arm.get("exposure") for arm in arms if not arm.get("is_control")} != {
        "native",
        "card",
    }:
        problems.append("the four formal arms do not cross native with card")

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

    plans = document.get("plans") or {}
    if set(plans) != {CANARY_PLAN_ID, FORMAL_PLAN_ID}:
        problems.append("the document does not carry exactly the canary and formal plans")

    canary = plans.get(CANARY_PLAN_ID) or {}
    canary_cells = canary.get("cells") or []
    if canary.get("scope") != SCOPE_CANARY or canary.get("plan_id") != CANARY_PLAN_ID:
        problems.append("the canary plan is not marked CANARY_ONLY")
    if len(canary_cells) != len(ARM_IDS):
        problems.append(f"the canary has {len(canary_cells)} cells, expected {len(ARM_IDS)}")
    if sorted(cell.get("arm_id") for cell in canary_cells) != sorted(ARM_IDS):
        problems.append("the canary does not run each arm exactly once")
    if len({cell.get("cell_id") for cell in canary_cells}) != len(canary_cells):
        problems.append("canary cell ids are not unique")
    if any(cell.get("is_benchmark_sample") is not False for cell in canary_cells):
        problems.append("a canary cell is marked as a benchmark sample")
    if len({cell.get("task_id") for cell in canary_cells}) != 1:
        problems.append("the canary uses more than one task")
    if {cell.get("repetition") for cell in canary_cells} != {1}:
        problems.append("the canary is not a single repetition")
    if canary.get("digest") != matrix.plan_digest(canary_cells):
        problems.append("canary plan digest does not match its cells")
    if any(cell.get("execution_contract") != EXECUTION_CONTRACT_DIGEST for cell in canary_cells):
        problems.append("a canary cell is not bound to the current execution contract")
    if canary.get("execution_contract_digest") != EXECUTION_CONTRACT_DIGEST:
        problems.append("the canary plan does not declare the current execution contract")
    if canary.get("supersedes") != CANARY_PLAN_ID_V2:
        problems.append("the canary plan does not say which plan it supersedes")
    if (canary.get("supersedes_record") or {}).get("claim") != "CONSUMED":
        problems.append("the superseded plan is not recorded as having consumed its claim")
    if len(canary.get("execution_history") or []) != 2:
        problems.append("the canary plan does not carry both superseded executions")
    if canary.get("authorization_migrates") is not False:
        problems.append("the canary plan claims an authorization migrates to it")

    formal = plans.get(FORMAL_PLAN_ID) or {}
    cell_list = formal.get("cells") or []
    expected = len(FORMAL_ARM_IDS) * len(TASK_IDS) * int(BUDGET["repetitions"])
    if formal.get("scope") != SCOPE_FORMAL or formal.get("plan_id") != FORMAL_PLAN_ID:
        problems.append("the formal plan is not marked FORMAL_MATRIX_ONLY")
    if len(cell_list) != expected:
        problems.append(f"formal plan has {len(cell_list)} cells, expected {expected}")
    if len({cell["cell_id"] for cell in cell_list}) != len(cell_list):
        problems.append("cell ids are not unique")
    if {cell.get("arm_id") for cell in cell_list} != set(FORMAL_ARM_IDS):
        problems.append("the formal plan is not the four-arm design")
    if formal.get("digest") != matrix.plan_digest(cell_list):
        problems.append("plan digest does not match its cells")
    plan_balance = formal.get("balance") or {}
    for key in ("each_task_meets_each_arm", "every_arm_run_equally_often"):
        if not plan_balance.get(key):
            problems.append(f"plan is not balanced: {key}")
    if not plan_balance.get("first_arm_differs_across_repetitions"):
        problems.append("the same arm opens both repetitions")
    canary_ids = {cell.get("cell_id") for cell in canary_cells}
    if canary_ids & {cell.get("cell_id") for cell in cell_list}:
        problems.append("a canary cell id collides with a formal cell id")

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


def delegation_config() -> Any:
    """The one delegation configuration every B+ cell uses.

    Centralised here so a capture, a probe and a dispatch cannot drift apart on a single limit.
    """

    from atlasos.delegation.config import DelegationConfig

    return DelegationConfig(
        enabled=True,
        max_parallel_children=int(BUDGET["max_concurrent_children"]),
        max_children_per_call=int(BUDGET["max_children_per_call"]),
        max_delegation_rounds=int(BUDGET["max_delegation_rounds"]),
        child_timeout_seconds=180.0,
        child_max_turns=int(BUDGET["child_max_turns"]),
        result_preview_chars=int(BUDGET["result_preview_chars"]),
    )


def main_prompt_spec() -> Any:
    from atlasos.app.main_driven import MainPromptSpec

    return MainPromptSpec(version=prompt_version("p3-bplus"), text=prompt_text("p3-bplus"))


def main_prompt_for(cell: Mapping[str, Any]) -> Any:
    """The Main prompt a cell executes with.

    A Stage B+ cell carries none of its own, so the frozen B+ prompt applies. A diagnostic plan may
    bind its own prompt into the cell — and therefore into the plan digest — when its intervention
    is a prompt change; the text travels with the cell so the digest covers the bytes that ran.
    """

    from atlasos.app.main_driven import MainPromptSpec

    override = cell.get("main_prompt")
    if isinstance(override, Mapping) and str(override.get("text") or "").strip():
        return MainPromptSpec(version=str(override["version"]), text=str(override["text"]))
    return main_prompt_spec()


def public_test_spec(task: closure2.Task) -> Any:
    """The frozen public suite for one cell.

    One function, two callers: the runner that fixes the command and the audit that checks the run
    recorded *that* command. Separate copies is how a test id or a command digest drifts.
    """

    from atlasos.app.public_tests import PublicTestContract

    return PublicTestContract(
        test_id="bplus-public",
        command="python3 -B -m pytest -q " + " ".join(sorted(task.public_tests)),
        timeout_seconds=240,
        max_output_chars=6000,
        max_calls=int(BUDGET["public_test_max_calls"]),
    )


def acceptance_spec(task: closure2.Task) -> Any:
    """The Host contract for one cell: the deliverables must exist, and the Host checks them.

    Static only. The hidden oracle is *not* the acceptance command: it is run by the Host after the
    run, on the exported workspace, and its verdict is recorded separately so a self-graded Main
    cannot be mistaken for a verified one.

    The contract says so explicitly (``oracle_mode``), instead of leaving admission to infer it from
    a missing command. v1 left it implicit and every cell was refused by the in-run admission rule.
    """

    from atlasos.app.goal_verification import (
        ORACLE_MODE_EXTERNAL,
        ExternalOracleContract,
        GoalAcceptance,
    )
    from atlasos.verification.models import CheckKind, CheckSpec, VerificationSpec

    checks = tuple(
        CheckSpec(
            check_id=f"present-{index}",
            category="PRESENCE",
            kind=CheckKind.FILE_PRESENT,
            artifact=artifact,
        )
        for index, artifact in enumerate(sorted(task.deliverables), start=1)
    )
    spec = VerificationSpec(
        spec_id=f"bplus-{task.task_id}",
        version=PROTOCOL_VERSION,
        deliverable_type="python-source",
        required_artifacts=tuple(sorted(task.deliverables)),
        checks=checks,
        notes="Stage B+ acceptance: the deliverables exist. Correctness is the Host oracle's job.",
    ).with_declared_digest()

    hidden = tuple(sorted(task.oracle_tests))
    public = tuple(sorted(task.public_tests))
    body = {
        "oracle_id": f"bplus-external-{task.task_id}",
        "task_id": task.task_id,
        "suite": "experiments.evaluation.closure2 (hidden tests, Host-owned)",
        "runner": "experiments.evaluation.sandboxed_oracle::run_hidden_tests_in_sandbox",
        "hidden_paths": list(hidden),
        "public_paths": list(public),
        "artifact_root": "artifacts.workspace_path",
        "scored_snapshot": "the exported workspace, never the fixture",
        "sandbox": "a second throwaway sandbox, created after the agent sandbox is destroyed",
        "verdict_field": "oracle.status",
        "host_only": True,
    }
    external = ExternalOracleContract(
        oracle_id=f"bplus-external-{task.task_id}",
        task_id=task.task_id,
        artifact_root="artifacts.workspace_path",
        body=body,
        hidden_paths=hidden,
        public_paths=public,
        sandbox_required=True,
        independent_sandbox=True,
        declared_digest="sha256:" + canonical_digest(body),
    )
    return GoalAcceptance(
        spec=spec,
        dynamic_command=None,
        require_dynamic=False,
        oracle_mode=ORACLE_MODE_EXTERNAL,
        external_oracle=external,
    )


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
    "CANARY_PLAN_ID",
    "CANARY_PLAN_ID_V1",
    "CANARY_PLAN_ID_V2",
    "CANARY_TASK_IDS",
    "CODING",
    "CONTEXT_METRICS",
    "CONTROL_ARM_IDS",
    "COUPLING_DISCLOSURE",
    "DELEGATION_LEVELS",
    "EXECUTION_CONTRACT",
    "EXECUTION_CONTRACT_DIGEST",
    "EXPOSURES",
    "EXPOSURE_TERMS",
    "FORBIDDEN_GOAL_PHRASES",
    "FORMAL_ARM_IDS",
    "FORMAL_PLAN_ID",
    "GOAL_OF",
    "INTERPRETATION",
    "child_grants_for",
    "goal_for",
    "P3_DIGEST",
    "P3_LITERAL",
    "PRIMARY_METRICS",
    "PROMPT_VARIANTS",
    "PROPOSAL_STATES",
    "PROTOCOL_VERSION",
    "RESEARCH",
    "REVISION",
    "S06_REFERENCE",
    "SCOPES",
    "SCOPE_CANARY",
    "SCOPE_FORMAL",
    "STAGE_ID",
    "STRUCTURE_OF",
    "SUPERSEDED_CANARIES",
    "SUPERSEDED_CANARY",
    "SUPERSEDED_CANARY_PLAN_IDS",
    "SUPERSEDED_CANARY_V2",
    "TASKS",
    "TASKS_BY_ID",
    "TASK_IDS",
    "TASK_WRITE_SCOPE",
    "acceptance_spec",
    "balance",
    "canary_cells",
    "cells",
    "delegation_config",
    "main_prompt_for",
    "main_prompt_spec",
    "p3_text",
    "prompt_text",
    "prompt_version",
    "protocol_document",
    "remove_capability_section",
    "scope_plan",
    "task_record",
    "validate",
    "write_document",
]
