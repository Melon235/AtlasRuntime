# ruff: noqa: E501
"""Closure-2A: the frozen arm matrix, the resource contract and the preregistration payload.

This module is data plus pure functions. It starts no run, loads no model and creates no sandbox.
`docs/closure-2-preregistration.md` is the human-readable copy of what is frozen here; when the two
disagree, the *document* is wrong and must be corrected, because the JSON written by
``preflight.py`` is generated from this module.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from experiments.evaluation.closure2.catalog import (
    BENCHMARK_ID,
    BENCHMARK_VERSION,
    DATASET_VERSION,
    TASKS,
    Task,
    build_task,
    task_digests,
)
from experiments.evaluation.models import canonical_digest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Repository-relative POSIX paths are the frozen identity. An absolute path would put the checkout
#: location into the digest, so the same definition would fingerprint differently on another machine.
LEGACY_CONTEXT_CONFIG = "experiments/configs/final4-context.yaml"
SHARED_PREFIX_CONFIG = "experiments/configs/closure2-shared-prefix.yaml"

#: Protocol revision. The task set does not change in R1; the registration structure does.
PROTOCOL_REVISION = "closure-2a-r1"
BENCHMARK_DATASET_VERSION = DATASET_VERSION

#: The frozen router policy. ``r0.1.0`` is never redefined in place.
ROUTING_POLICY_VERSION = "r0.1.0"


def resolve_repo_path(relative: str, *, root: Path | None = None) -> Path:
    """Resolve a frozen repository-relative POSIX path against ``root``.

    Refuses absolute input and any ``..`` segment: the frozen identity is a path *inside* the
    checkout, and a caller must not be able to point the protocol at an arbitrary file.
    """

    text = str(relative)
    if not text or text.startswith("/") or Path(text).is_absolute() or ":" in text:
        raise ValueError(f"frozen protocol paths must be repository-relative: {relative!r}")
    parts = PurePosixPath(text).parts
    if any(part == ".." for part in parts):
        raise ValueError(f"frozen protocol paths must not escape the repository: {relative!r}")
    base = Path(root) if root is not None else REPO_ROOT
    return base.joinpath(*parts)


#: One arm per row. ``topology`` selects the BenchmarkTask the arm runs; ``runner`` selects which
#: entry point does it. Nothing else is allowed to differ inside a comparison pair.
ARMS: dict[str, dict[str, Any]] = {
    "N0": {
        "runner": "native",
        "topology": "single",
        "engine": "anycode-0.9.0-native",
        "context": "native",
        "prompt_layout": "native",
        "route": "off",
        "config": None,
    },
    "N1": {
        "runner": "native",
        "topology": "multi",
        "engine": "anycode-0.9.0-native",
        "context": "native prerequisite context",
        "prompt_layout": "native",
        "route": "off",
        "config": None,
    },
    "A0": {
        "runner": "atlas",
        "topology": "single",
        "engine": "atlas",
        "context": "task_scoped",
        "prompt_layout": "legacy",
        "route": "off",
        "config": LEGACY_CONTEXT_CONFIG,
    },
    "A1-L": {
        "runner": "atlas",
        "topology": "multi",
        "engine": "atlas",
        "context": "task_scoped",
        "prompt_layout": "legacy",
        "route": "off",
        "config": LEGACY_CONTEXT_CONFIG,
    },
    "A1-P": {
        "runner": "atlas",
        "topology": "multi",
        "engine": "atlas",
        "context": "task_scoped",
        "prompt_layout": "shared_prefix",
        "route": "off",
        "config": SHARED_PREFIX_CONFIG,
    },
    "A2-P": {
        "runner": "atlas",
        "topology": "multi",
        "engine": "atlas",
        "context": "task_scoped",
        "prompt_layout": "shared_prefix",
        "route": "selective",
        "config": SHARED_PREFIX_CONFIG,
    },
}

#: Every comparison the preregistration commits to, and nothing else.
COMPARISONS: tuple[dict[str, str], ...] = (
    {
        "id": "native-vs-atlas-single",
        "left": "N0",
        "right": "A0",
        "question": "does the Atlas runtime beat a plain AnyCode agent on the same single-worker task",
        "attribution": "whole system: execution control, tool organisation, context management",
    },
    {
        "id": "native-vs-atlas-multi",
        "left": "N1",
        "right": "A1-L",
        "question": "does the Atlas runtime beat a plain AnyCode DAG on the same multi-worker task",
        "attribution": "whole system, not the Context Plane alone",
    },
    {
        "id": "layout-ablation",
        "left": "A1-L",
        "right": "A1-P",
        "question": "does the shared-prefix layout change tokens or quality with everything else fixed",
        "attribution": "prompt layout only: same config file apart from context.prompt_layout",
    },
    {
        "id": "topology-ablation-atlas",
        "left": "A0",
        "right": "A1-P",
        "question": "does decomposing the same task help or hurt when the context plane is on",
        "attribution": "topology plus the context-plane projection it entails",
    },
    {
        "id": "topology-ablation-native",
        "left": "N0",
        "right": "N1",
        "question": "does AnyCode's own DAG help or hurt on the same task",
        "attribution": "topology only, inside one engine",
    },
    {
        "id": "router-admission",
        "left": "A1-P",
        "right": "A2-P",
        "question": "does the frozen router admit or refuse the candidate DAG, and why",
        "attribution": "router decision, audited offline; not a quality comparison",
    },
)

#: Pairs that share one candidate DAG and therefore one BenchmarkTask.
SAME_DAG_GROUP: tuple[tuple[str, ...], ...] = (("N1", "A1-L", "A1-P"), ("A2-P",))
#: Pairs that must produce the same ``effective_public_task_digest``.
SAME_DIGEST_GROUP: tuple[tuple[str, ...], ...] = (("N0", "A0"), ("N1", "A1-L", "A1-P"))

#: Resource contract — **frozen parameters only**. The approval state is deliberately *not* here:
#: approving a budget must not change the identity of the thing that was approved, or the approval
#: would invalidate itself. See ``APPROVAL_RECORD`` and ``approval_gate``.
BUDGET: dict[str, Any] = {
    "single": {
        "max_turns": 36,
        "token_budget": 260_000,
        "max_tool_calls": 90,
        "timeout_seconds": 1800,
    },
    "multi": {
        "max_turns_per_worker": 12,
        "workers": 3,
        "theoretical_worker_turns": 36,
        "token_budget": 260_000,
        "max_tool_calls": 90,
        "timeout_seconds": 1800,
    },
    "shared": {
        "temperature": 0.0,
        "max_tokens_per_request": 8000,
        "sandbox_image": "atlas-uat-python:3.12",
        "cpu": "1",
        "memory": "1Gi",
        "network": "none",
        "workspace_root": "/workspace/project",
        "oracle": "isolated sandbox, same image, hidden tests",
        "initial_fixture": "the task seed, staged into a fresh git repository per run",
    },
    "rationale": (
        "260k prompt tokens sits between the historical multi-worker mean (~139k/run on E1 A1) and "
        "the 300k ceiling: roughly 1.9x the observed multi-worker load for a larger codebase, "
        "identical in both topologies so neither is handicapped. 36 turns and 90 tool calls keep "
        "the same 1:2.5 turn-to-call ratio the Final-4 units used (14 turns / 40 calls). 1800 s is "
        "twice the Final-4 timeout because each run now provisions, migrates and grades three "
        "modules. No value was tuned against a model run; nothing was executed to choose them."
    ),
}

#: Mutable approval state. It is *not* part of ``preregistration_payload()``, so moving it from
#: PENDING to APPROVED changes nothing about the frozen identity. An approval is only usable when it
#: names the exact digest currently frozen.
APPROVAL_RECORD: dict[str, Any] = {
    "status": "PENDING_OWNER_APPROVAL",
    "approved_digest": None,
    "approved_by": None,
    "approval_commit": None,
    "approved_at": None,
}


#: The C2-A registration identities, copied from the ac7af16 preregistration table. They are the
#: *original* task identities: the preflight compares the live task set against them rather than
#: against a digest it just computed from the same objects.
C2A_FROZEN_TASK_DIGESTS: dict[str, dict[str, str]] = {
    "closure2-pipeline-migration": {
        "seed_digest": "f9378c44a0ee564962e0a56ac7172ad616904d098fbc8dabb0806c4cdbbef515",
        "reference_digest": "80706d098281e6d9f2cb9374aa6f449b29ce1665b88b77c2ffa7e7fa3b4dd3fe",
        "public_tests_digest": "2977f44a55619abcdefcbc35de36cbe5eec769abd1762428c298865174bf66d8",
        "oracle_digest": "95174a0d6f42036ed52d98b5b7c8929f860ae55911646323f75886a8e7499808",
        "public_requirement_digest": "060640018bfc5b35080cc2d36de89a67d459287dc17f2a77a6b05039102ad703",
        "candidate_dag_digest": "5795f3906e06219061dc5f7ddd7c1e38a141fb691522c096646398de8260df02",
        "effective_public_task_digest_single": (
            "b89987bdbee4ca79f5728c00df6ed13e979464c0173744397d1f1ff4c73db18d"
        ),
        "effective_public_task_digest_multi": (
            "c4b5373c162e0082cc90104afb8c0dee916b2cee1f678ab5198a6f39cb1d57b6"
        ),
    },
    "closure2-plugin-migration": {
        "seed_digest": "cdc114a995318c7021016a08fff1cfb50b1d14d26f9d8f30ffa0ff12b53f8076",
        "reference_digest": "6e907eb96c35bb76c0e4bf091f0b6119cb1af1341d96580fd76557ade524ab97",
        "public_tests_digest": "95561cd58a8478703c1cb3e3416d6657703203c8042bbf9cd38f1305e1bc1ab2",
        "oracle_digest": "a82b6add0cb721db78008ba2361820b1e7768cdd4bdad4cd09affb547ac4983a",
        "public_requirement_digest": "4886c52b92472f74c8aa3333019f5b1b8c1379627c6596ab973c0645f761c5ee",
        "candidate_dag_digest": "5828146832b92dc0d7f13470ba01113eb4b83ac33b715705b49f09a4a459ab02",
        "effective_public_task_digest_single": (
            "4e97b92d94c6f8fdeb624d5252c7c576ef922d9879768412399d6d1ceededd61"
        ),
        "effective_public_task_digest_multi": (
            "8bf208d30d6c8f2f543174a612a742adc7059d92457e1ef0e4bbb0edbeb89e31"
        ),
    },
    "closure2-state-machine-refactor": {
        "seed_digest": "400f56d38c1425757835701e73db4feb8ee3beea5764f008006058bd6a9e4012",
        "reference_digest": "5a04ee0c48152b9f18cdd0b8902d32660404c28d72ba345a06114f7ec1cd494c",
        "public_tests_digest": "181a5358a104a9e8af794903f37ef4094e9cd0790d09e3a63a1af72e0d55b671",
        "oracle_digest": "a6f05cfc0deee5add17131c1f0e7ffc1f15081600507ba7639ae235dd664094e",
        "public_requirement_digest": "fcc12d5d1f96e42e37bfe6b258f8f8c1c234b7413784d91f2b4f7607c0e4733c",
        "candidate_dag_digest": "66eb757768eb4e41ea478c765f06fd7cfa52da01a21b08dedc0361d132eec2fe",
        "effective_public_task_digest_single": (
            "7014163b05f184239e62168c30fe36f3a007323152f80feaa9ce5615130b8cfd"
        ),
        "effective_public_task_digest_multi": (
            "104306bf95e4d6e731a4d5279960344a45fc31d74a472d58ace6b7d11bb1bae6"
        ),
    },
}

#: The C2-A preregistration digest at ac7af16. Kept as history, never reused as the current one.
C2A_PREREGISTRATION_DIGEST = "f7bbdfb8350589a959d78be00a9dc150c3b12fd6d277cf0fe2cee8a8b536b058"
#: The C2-A commits this revision supersedes. Recorded so the lineage is machine-readable.
C2A_TASK_COMMIT = "7f5c1eae12e466e6a37ce2a7d77608ba65a009a5"
C2A_HEAD_COMMIT = "ac7af16b4506afafe6e932e2cc351b04354b9f57"

#: The R1 registration identity, pinned **before** any registration-dependent check runs. Pinning it
#: in source is what makes "the payload still hashes to the same value" a real comparison instead of
#: hashing the current code against itself. See ``docs/closure-2a-r1-review-repair.md``.
C2A_R1_PREREGISTRATION_DIGEST = "3080f3137a408856337681c14ff4e10fc67b50a15c602f54628b117159b1a765"


def approval_gate(
    *, record: dict[str, Any] | None = None, digest: str | None = None
) -> dict[str, Any]:
    """Decide whether Closure-2B may start.

    ``execution_ready`` is true only when the record is approved, names the *current* frozen digest,
    and carries a non-empty approver, approval commit and timestamp. A pending record is the expected
    state for this stage: the offline preflight passes, the live pilot stays closed. An approval that
    names a different digest is a *wrong* approval, not a usable one: it fails closed.
    """

    active = APPROVAL_RECORD if record is None else record
    target = preregistration_digest() if digest is None else digest
    approved_digest = active.get("approved_digest")
    status = active.get("status")
    problems: list[str] = []
    if status != "APPROVED":
        problems.append(f"approval status is {status!r}, not 'APPROVED'")
    if approved_digest != target:
        problems.append(
            f"approval names digest {approved_digest!r} but the frozen digest is {target!r}"
        )
    if not active.get("approved_by"):
        problems.append("approval has no approver")
    if not active.get("approval_commit"):
        problems.append("approval has no approval commit")
    if not active.get("approved_at"):
        problems.append("approval has no approval timestamp")
    return {
        "status": status,
        "execution_ready": not problems,
        "target_digest": target,
        "approved_digest": approved_digest,
        "problems": problems,
        "record": dict(active),
    }


def require_execution_approval(*, record: dict[str, Any] | None = None) -> dict[str, Any]:
    """The only sanctioned entry point for a live Closure-2B run.

    Raises instead of returning a boolean: a caller cannot accidentally treat a pending or
    mismatched approval as permission. Offline preflight never calls this.
    """

    gate = approval_gate(record=record)
    if not gate["execution_ready"]:
        raise PermissionError("Closure-2B is not approved: " + "; ".join(gate["problems"]))
    return gate


#: Two different enforcement mechanisms. Recorded separately, never presented as equivalent.
BUDGET_SEMANTICS: dict[str, str] = {
    "atlas": (
        "shared run budget guarded before send: a request that would breach the token budget is "
        "refused, so the arm can end with a budget rejection and an incomplete deliverable"
    ),
    "native": (
        "no in-run guard: the same public thresholds are applied after the run, and the wall clock "
        "is the only enforced limit, so an over-budget native run is reported, not stopped"
    ),
    "consequence": (
        "a TASK_SUCCESS comparison alone cannot separate budget effects; results must report oracle "
        "pass, budget overrun, rejected requests and 'deliverable passed but budget failed' "
        "separately"
    ),
}

#: Execution order rule for Closure-2B: interleave, never block-by-arm.
EXECUTION_ORDER: dict[str, Any] = {
    "rule": (
        "tasks x repetitions are the outer loop and arms the inner loop, with the arm order rotated "
        "per repetition; no arm is ever run to completion before the next one starts"
    ),
    "repetitions": 3,
    "pairing": "each repetition of a task is a paired block across all arms of that task",
    "rationale": (
        "provider latency, cache warmth and time-of-day drift are shared inside a block instead of "
        "being aliased onto one arm"
    ),
}

#: Primary and secondary metrics, in the order they must be reported.
METRICS: dict[str, Any] = {
    "primary": "independent oracle correctness (OracleOutcome.passed)",
    "secondary": [
        "TASK_SUCCESS",
        "provider prompt tokens",
        "provider cache hit / miss (Unknown when the provider did not report a split)",
        "estimated cost (None when the split is incomplete)",
        "agent latency",
        "end-to-end latency",
        "model calls",
        "tool calls",
        "budget violations",
        "parallel execution overlap",
        "context delivery correctness",
        "safety and environment failures",
    ],
    "reporting_rules": [
        "a success rate never hides budget overruns",
        "a cost is labelled provider-usage or estimate, never both",
        "cache hit/miss stays Unknown without a provider-reported split",
        "cache hit rate never substitutes for total cost",
    ],
}

#: Data that may be excluded, and nothing else.
EXCLUSIONS: dict[str, Any] = {
    "allowed": [
        "a run whose sandbox never became healthy (ENVIRONMENT_FAILURE before any model call)",
        "a run killed by the operator (recorded with its reason)",
        "a run whose oracle environment failed (ORACLE_TIMEOUT / SANDBOX_* on the oracle side)",
    ],
    "never_allowed": [
        "a run whose deliverable failed the oracle",
        "a run that exceeded the budget",
        "a run from one arm only, chosen after seeing the numbers",
    ],
    "rule": (
        "exclusions are decided by mechanism class, applied identically to every arm, and every "
        "excluded run stays in the ledger with its reason"
    ),
}

#: Comparison cells the design does **not** support.
UNSUPPORTED: list[str] = [
    "Router-assisted vs static multi for a task whose DAG the router refuses: A2-P collapses to the "
    "single worker, so it is not a multi-worker cell and must not be reported as one",
    "single vs multi across different tasks: topologies are compared only inside one task",
    "a Native vs Atlas difference attributed to the Context Plane alone: the two engines differ in "
    "execution control, tool organisation and budget enforcement as well",
    "any cost claim from a run whose provider cache split is Unknown",
    "any statistical claim from a single repetition",
]


def _json_safe(node: Any) -> Any:
    """YAML may hand back dates and other scalars canonical JSON cannot hash."""

    if isinstance(node, dict):
        return {str(key): _json_safe(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [_json_safe(value) for value in node]
    if node is None or isinstance(node, (str, bool, int)):
        return node
    if isinstance(node, float):
        # ``0`` and ``0.0`` are one effective value: without this, an editor normalising a float
        # would change the frozen identity of a config whose meaning did not move.
        return int(node) if node.is_integer() else node
    if hasattr(node, "isoformat"):
        return node.isoformat()
    return str(node)


def config_semantics(relative: str, *, root: Path | None = None) -> dict[str, Any]:
    """Load a frozen config file and return its parsed content as canonical-JSON-safe data."""

    path = resolve_repo_path(relative, root=root)
    if not path.is_file():
        raise FileNotFoundError(
            f"frozen config file is missing: {relative} (looked in {path.parent})"
        )
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"frozen config file {relative} is not a YAML mapping")
    return _json_safe(loaded)


def config_semantic_digest(relative: str, *, root: Path | None = None) -> str:
    """Digest of the config's **effective semantics**, not its bytes.

    Re-indenting or commenting a file keeps this digest; changing ``provider.model``,
    ``tools.search_backend``, ``context.prompt_layout`` or any ``sandbox`` field changes it. A digest
    that only used the file name would not notice any of those.
    """

    return canonical_digest({"path": relative, "content": config_semantics(relative, root=root)})


def arm_config_digest(arm: str, *, root: Path | None = None) -> str:
    """Digest of everything an arm is allowed to change, including its config semantics.

    ``root`` exists only so a test can prove the digest is identical across two checkouts; the
    production call uses the repository root.
    """

    entry = ARMS[arm]
    config = entry["config"]
    return canonical_digest(
        {
            "protocol_revision": PROTOCOL_REVISION,
            "benchmark": BENCHMARK_ID,
            "arm": arm,
            "runner": entry["runner"],
            "topology": entry["topology"],
            "engine": entry["engine"],
            "context": entry["context"],
            "prompt_layout": entry["prompt_layout"],
            "route": entry["route"],
            # Repository-relative POSIX path: the checkout location must never enter the identity.
            "config_path": config,
            "config_semantic_digest": (
                config_semantic_digest(config, root=root) if config is not None else None
            ),
            "budget": BUDGET["single"] if entry["topology"] == "single" else BUDGET["multi"],
            "shared": BUDGET["shared"],
        }
    )


def layout_ablation_delta(*, root: Path | None = None) -> dict[str, Any]:
    """The exact effective-config difference between A1-L and A1-P.

    The ablation only means something if ``context.prompt_layout`` is the single variable. This
    flattens both files and returns the differing dotted keys, so a stray edit anywhere else fails
    the fairness gate instead of quietly turning the pair into two variables.
    """

    left = config_semantics(LEGACY_CONTEXT_CONFIG, root=root)
    right = config_semantics(SHARED_PREFIX_CONFIG, root=root)

    def flatten(node: Any, prefix: str = "") -> dict[str, Any]:
        flat: dict[str, Any] = {}
        if isinstance(node, dict):
            for key, value in node.items():
                flat.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        else:
            flat[prefix] = node
        return flat

    left_flat, right_flat = flatten(left), flatten(right)
    keys = set(left_flat) | set(right_flat)
    differing = sorted(key for key in keys if left_flat.get(key) != right_flat.get(key))
    return {
        "left": LEGACY_CONTEXT_CONFIG,
        "right": SHARED_PREFIX_CONFIG,
        "differing_keys": differing,
        "only_layout_differs": differing == ["context.prompt_layout"],
        "left_layout": left_flat.get("context.prompt_layout"),
        "right_layout": right_flat.get("context.prompt_layout"),
    }


def task_record(task: Task) -> dict[str, Any]:
    digests = task_digests(task)
    return {
        "task_id": task.task_id,
        "family": task.family,
        "title": task.title,
        "notes": task.notes,
        "research_question": task.research_question,
        "workers": [
            {
                "task_id": str(worker["task_id"]),
                "objective": str(worker["objective"]),
                "depends_on": list(worker["depends_on"]),
                "owned_paths": list(worker["owned_paths"]),
            }
            for worker in task.workers
        ],
        "deliverables": list(task.deliverables),
        "allowed_paths": list(task.allowed_paths),
        "forbidden_paths": list(task.forbidden_paths),
        "seed_files": sorted(task.seed),
        "oracle_files": sorted(task.oracle_tests),
        "digests": digests,
    }


def preregistration_payload(*, root: Path | None = None) -> dict[str, Any]:
    """The frozen definition Closure-2B must verify before it runs anything.

    Contains no absolute path and no approval state: both would make the identity of the
    registration depend on where it was checked out or whether it had been signed.
    """

    return {
        "protocol_revision": PROTOCOL_REVISION,
        # Lineage, not a re-freeze: the tasks are byte-identical to C2-A, only the registration
        # structure changed. The superseded digest is a constant, never recomputed.
        "supersedes": {
            "protocol_revision": "closure-2a",
            "preregistration_digest": C2A_PREREGISTRATION_DIGEST,
            "task_commit": C2A_TASK_COMMIT,
            "head_commit": C2A_HEAD_COMMIT,
        },
        "benchmark_id": BENCHMARK_ID,
        "benchmark_version": BENCHMARK_VERSION,
        "dataset_version": DATASET_VERSION,
        "routing_policy_version": ROUTING_POLICY_VERSION,
        "tasks": [task_record(task) for task in TASKS],
        "arms": {
            arm: {
                **entry,
                # Repository-relative path plus a content digest: the frozen identity protects what
                # the config *means*, not only which file it is.
                "config_semantic_digest": (
                    config_semantic_digest(entry["config"], root=root)
                    if entry["config"] is not None
                    else None
                ),
                "config_digest": arm_config_digest(arm, root=root),
            }
            for arm, entry in ARMS.items()
        },
        "layout_ablation": layout_ablation_delta(root=root),
        "comparisons": list(COMPARISONS),
        "digest_groups": [list(group) for group in SAME_DIGEST_GROUP],
        "same_dag_groups": [list(group) for group in SAME_DAG_GROUP],
        "budget": BUDGET,
        "budget_semantics": BUDGET_SEMANTICS,
        "execution_order": EXECUTION_ORDER,
        "metrics": METRICS,
        "exclusions": EXCLUSIONS,
        "unsupported": list(UNSUPPORTED),
        "release_boundary": {
            "agent_workspace_contains": "seed files only (source, docs, public tests)",
            "repository_contains": (
                "hidden oracle tests and reference implementations — these tasks are therefore NOT "
                "secret held-out data; only the agent workspace is held out"
            ),
            "rule": (
                "every published result must carry this sentence; the tasks must not be described "
                "as fully unpublished"
            ),
        },
        "model_calls_to_author_this": 0,
        "sandbox_runs_to_author_this": 0,
    }


def preregistration_digest(*, root: Path | None = None) -> str:
    return canonical_digest(preregistration_payload(root=root))


def task_taskspec_parity() -> dict[str, dict[str, Any]]:
    """Per task, the facts that must be identical across single and multi.

    The public requirement, the shared block, the visible fixture, the hidden oracle and the
    deliverables are task data; only the worker decomposition may differ.
    """

    out: dict[str, dict[str, Any]] = {}
    for task in TASKS:
        single = build_task(task, topology="single")
        multi = build_task(task, topology="multi")
        out[task.task_id] = {
            "same_visible_files": single.visible_files == multi.visible_files,
            "same_hidden_files": single.hidden_files == multi.hidden_files,
            "same_required_artifacts": single.required_artifacts == multi.required_artifacts,
            "same_allowed_paths": single.allowed_paths == multi.allowed_paths,
            "same_forbidden_paths": single.forbidden_paths == multi.forbidden_paths,
            "single_prompt_contains_shared_context": task.shared_context.splitlines()[0]
            in single.prompt,
            "multi_steps_carry_shared_context": all(
                step.shared_context == task.shared_context for step in multi.workers
            ),
            "single_digest": single.effective_public_task_digest,
            "multi_digest": multi.effective_public_task_digest,
        }
    return out
