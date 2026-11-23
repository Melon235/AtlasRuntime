"""Host-side provisioning for one Stage B cell.

Everything a model can see is built here from the frozen protocol: the goal, the prompt, the fresh
fixture, the public-suite command and the write scope. The oracle source stays on the Host and is
never present in the fixture. This module composes ``run_goal``; it contains no agent loop.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from atlasos.app.goal_sandbox import GoalSandboxOptions
from atlasos.app.goal_verification import GoalAcceptance
from atlasos.app.main_driven import GoalRunOptions
from atlasos.app.public_tests import PublicTestContract
from atlasos.config import AtlasConfig, SandboxConfig, ToolsConfig
from atlasos.delegation.config import DelegationConfig
from atlasos.delegation.router import STAGE_M_POLICY_VERSION
from atlasos.sandbox.tools import TOOL_ERROR_MODE_CORRECTED
from atlasos.verification.models import CheckKind, CheckSpec, VerificationSpec

from .. import protocol
from ..tasks import BY_ID
from ..tasks import base as task_base
from ..tasks.base import TaskSpec
from . import network
from .models import CellIdentity

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_ROOT = "/workspace/project"
LEAK_MARKERS = (
    "HIDDEN_OK",
    "HIDDEN_FAIL",
    "oracle_program",
    "oracle_source",
    "reference implementation",
)

CALIBRATION = {
    "task": "workgrid",
    "source": "experiments/fixtures/stage_s2_workgrid",
    "in_matrix": False,
    "arm_id": "P1",
    #: The first attempt (c1) is consumed and preserved. A retry gets a new identity.
    "attempt": 2,
    "status": "PREPARED_NOT_RUN",
    "reason_not_run": "this stage may not call the provider",
    "previous_attempt": "CALIBRATION.workgrid.c1",
}


def arm_entry(document: Mapping[str, Any], arm_id: str) -> dict[str, Any]:
    for item in document["arms"]:
        if item["arm_id"] == arm_id:
            return dict(item)
    raise ValueError(f"arm {arm_id!r} is not in the frozen protocol")


def task_entry(document: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    for item in document["tasks"]:
        if item["task_id"] == task_id:
            return dict(item)
    raise ValueError(f"task {task_id!r} is not in the frozen protocol")


def cell_from_plan(document: dict[str, Any], cell_id: str) -> CellIdentity:
    """Recover the full identity from the frozen plan, never from a caller-supplied arm string."""

    for entry in document["plan"]["cells"]:
        if entry["cell_id"] == cell_id:
            return CellIdentity(
                task_id=entry["task_id"],
                arm_id=entry["arm_id"],
                repetition=int(entry["repetition"]),
                cell_id=entry["cell_id"],
                round=int(entry["round"]),
                order=int(entry["order"]),
            )
    raise ValueError(f"cell {cell_id!r} is not in the frozen 36-cell plan")


def calibration_cell() -> CellIdentity:
    return CellIdentity(
        task_id=CALIBRATION["task"],
        arm_id=CALIBRATION["arm_id"],
        repetition=0,
        cell_id=f"CALIBRATION.workgrid.c{CALIBRATION['attempt']}",
        in_matrix=False,
    )


def memory_quantity(gib: float) -> str:
    """Render a memory limit the way OpenSandbox accepts it: ``1Gi``, never ``1.0Gi``.

    The frozen experiment parameter is a float (1.0 GiB). The server parses resource quantities as
    integers, so the decimal suffix must not leak from the Python type into the wire value.
    """

    value = float(gib)
    if value <= 0:
        raise ValueError(f"memory_gib must be positive, got {gib!r}")
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{text}Gi"


def _hexdigest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def s2_frozen_identity() -> dict[str, Any]:
    """The frozen S2 calibration source of truth. Read-only, never regenerated here."""

    path = REPO_ROOT / "experiments/stage_s2_prompt_v21/freeze.json"
    return json.loads(path.read_text(encoding="utf-8"))


def verify_calibration_identity() -> list[str]:
    """Byte-level checks of WorkGrid goal, fixture, public tests and Host oracle against S2."""

    from experiments.stage_s2_prompt_v21 import s2_contract

    frozen = s2_frozen_identity()
    identity = frozen["identity"]
    problems: list[str] = []

    if s2_contract.goal_digest() != identity["goal_digest"] or identity["goal"] != s2_contract.GOAL:
        problems.append("calibration goal drifted from S2")
    root = REPO_ROOT / CALIBRATION["source"]
    if not root.is_dir():
        problems.append("calibration fixture source is missing")
    else:
        for relative, expected in (frozen["fixture"]["files"]).items():
            path = root / relative
            if not path.is_file():
                problems.append(f"calibration fixture missing: {relative}")
                continue
            digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected:
                problems.append(f"calibration fixture drifted: {relative}")

    tests = s2_contract.PUBLIC_TESTS
    declared = identity["public_tests"]
    if any(
        getattr(tests, field) != declared[field]
        for field in ("command", "max_calls", "timeout_seconds", "test_id")
    ):
        problems.append("calibration public tests drifted from S2")
    if tests.command_digest() != declared["command_digest"]:
        problems.append("calibration public-test command digest drifted")

    oracle = identity["oracle"]
    if s2_contract.digest(s2_contract.HIDDEN_PROGRAM) != oracle["program_digest"]:
        problems.append("calibration oracle program drifted from S2")
    if s2_contract.digest(s2_contract.ORACLE_COMMAND) != oracle["command_digest"]:
        problems.append("calibration oracle command drifted from S2")
    if list(s2_contract.MAIN_WRITE_PATHS) != list(identity["main_write_paths"]):
        problems.append("calibration write scope drifted from S2")
    return problems


def verify_identity(
    cell: CellIdentity, *, document: dict[str, Any], runtime_digest: str, plan_digest: str
) -> list[str]:
    """Every identity claim on a cell must survive a byte-level re-derivation."""

    problems: list[str] = []
    if cell.task_id not in BY_ID:
        problems.extend(verify_calibration_identity())
        if cell.arm_id != CALIBRATION["arm_id"]:
            problems.append("calibration arm changed")
        if cell.cell_id != calibration_cell().cell_id:
            problems.append("calibration cell id changed")
        return problems
    spec = BY_ID[cell.task_id]
    task = task_entry(document, cell.task_id)
    if task_base.goal_digest(spec) != task["goal_digest"] or spec.goal != task["goal"]:
        problems.append("goal changed")
    if task_base.fixture_digest(spec) != task["fixture_digest"]:
        problems.append("fixture changed")
    if task_base.oracle_digest(spec) != task["oracle_digest"]:
        problems.append("oracle changed")
    prompt = protocol.prompt_spec(cell.arm_id)
    arm = arm_entry(document, cell.arm_id)
    if prompt.version != arm["label"] or prompt.digest != arm["declared_digest"]:
        problems.append("prompt version or digest changed")
    if runtime_digest != "sha256:4eebcf1e56e7bf786320fda4252b2c584e040a702ae8642b4336cf21432d6dae":
        problems.append("runtime tree changed")
    if plan_digest != "sha256:c69144744b14745677fdad9d9cfce69cdc9f777499aa7dabcc804e4b6fe76648":
        problems.append("plan digest changed")
    config = document["run_config"]
    if config["router_policy"] != STAGE_M_POLICY_VERSION:
        problems.append("router policy changed")
    if config["worker_profiles"] != ["research_worker"]:
        problems.append("worker profile changed")
    if int(config["token_budget"]) != 500_000 or int(config["main_max_turns"]) != 20:
        problems.append("budget or main turns changed")
    if config["model"] != "deepseek-chat" or config["provider"] != "deepseek":
        problems.append("model changed")
    if config["tool_error_mode"] != TOOL_ERROR_MODE_CORRECTED:
        problems.append("tool error mode changed")
    if cell.in_matrix:
        matching = [
            entry for entry in document["plan"]["cells"] if entry["cell_id"] == cell.cell_id
        ]
        if len(matching) != 1 or matching[0]["arm_id"] != cell.arm_id:
            problems.append("cell identity does not match the plan")
    elif cell.cell_id in {entry["cell_id"] for entry in document["plan"]["cells"]}:
        problems.append("calibration cell collides with the matrix")
    return problems


def isolation_checks(document: dict[str, Any]) -> dict[str, Any]:
    """Prove the oracle is Host-owned and no agent-visible surface contains it."""

    problems: list[str] = []
    for entry in document["tasks"]:
        task_id = entry["task_id"]
        task = dict(entry)
        spec = BY_ID[task_id]
        visible = "\n".join([spec.goal, *spec.files.values()]).lower()
        if any(marker.lower() in visible for marker in LEAK_MARKERS):
            problems.append(f"{task_id}: oracle marker in an agent-visible surface")
        leaked = [
            path
            for path, text in spec.reference.items()
            if any(text and text in visible_file for visible_file in spec.files.values())
        ]
        if leaked:
            problems.append(
                f"{task_id}: reference implementation is in the fixture: {sorted(leaked)}"
            )
        if task["oracle_digest"] != task_base.oracle_digest(spec):
            problems.append(f"{task_id}: oracle digest changed")

    from experiments.stage_s2_prompt_v21 import s2_contract

    visible_calibration = s2_contract.GOAL.lower()
    if any(marker.lower() in visible_calibration for marker in LEAK_MARKERS):
        problems.append("workgrid: oracle marker in the calibration goal")
    return {
        "ok": not problems,
        "problems": problems,
        "host_only": ("oracle_source", "reference", "oracle.json"),
        "executed_after_main_stops": True,
    }


def _git_init(root: Path) -> None:
    """Create a clean local repo. Identity is set per command, never in global config."""

    commands = (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=stage-b@example.invalid",
            "-c",
            "user.name=Stage B",
            "commit",
            "-q",
            "-m",
            "stage b fixture",
        ],
    )
    for command in commands:
        subprocess.run(command, cwd=root, check=True, capture_output=True)


def materialize_fixture(root: Path, spec: TaskSpec) -> Path:
    """Fresh, committed fixture per cell. Refuses to touch an existing directory."""

    task_base.build(root, spec)
    _git_init(root)
    return root


def materialize_calibration_fixture(root: Path) -> Path:
    source = REPO_ROOT / CALIBRATION["source"]
    if not source.is_dir():
        raise RuntimeError(f"calibration fixture is missing: {source}")
    if root.exists():
        raise RuntimeError(f"calibration fixture destination already exists: {root}")
    copied = task_base.copy_tree(source, root)
    _git_init(copied)
    return copied


def _acceptance(spec: TaskSpec) -> GoalAcceptance:
    """Static checks plus the frozen oracle, encoded so the model never sees its source."""

    checks = [
        CheckSpec(
            check_id=f"present-{index}",
            category="PRESENCE",
            kind=CheckKind.FILE_PRESENT,
            artifact=path,
        )
        for index, path in enumerate(sorted(spec.write_scope))
    ]
    checks.extend(
        CheckSpec(
            check_id=f"parses-{index}",
            category="STRUCTURE",
            kind=CheckKind.PYTHON_PARSES,
            artifact=path,
        )
        for index, path in enumerate(
            sorted(path for path in spec.write_scope if path.endswith(".py"))
        )
    )
    encoded = base64.b64encode(spec.oracle_source.encode("utf-8")).decode("ascii")
    command = (
        "python3 -B -m pytest -q tests && "
        f"python3 -c \"import base64;exec(base64.b64decode('{encoded}').decode())\""
    )
    contract = VerificationSpec(
        spec_id=f"stage-b-{spec.task_id}",
        version="1.0.0",
        deliverable_type="python-package",
        required_artifacts=tuple(sorted(spec.write_scope)),
        checks=tuple(checks),
    ).with_declared_digest()
    return GoalAcceptance(
        spec=contract,
        dynamic_command=command,
        dynamic_timeout_seconds=420,
        require_dynamic=True,
    )


def build_options(
    *,
    cell: CellIdentity,
    fixture_repo: Path,
    runs_root: Path,
    run_id: str,
    document: dict[str, Any] | None = None,
    provider_factory: Any = None,
    sandbox_factory: Any = None,
    offline: bool = False,
) -> GoalRunOptions:
    """Compose real ``GoalRunOptions`` from frozen inputs. No prompt text is hard-coded here."""

    document = document or protocol.read_protocol()
    config = document["run_config"]
    if cell.task_id in BY_ID:
        spec = BY_ID[cell.task_id]
        goal_text = spec.goal
        write_paths = tuple(spec.write_scope)
        acceptance_contract = _acceptance(spec)
        public_contract = PublicTestContract(
            test_id=f"stage-b-{spec.task_id}",
            command="python3 -B -m pytest -q " + " ".join(spec.target_command),
            timeout_seconds=240,
            max_output_chars=6000,
            max_calls=int(config["public_test_max_calls"]),
        )
    else:
        # WorkGrid is the matrix-external calibration task. Its goal and oracle come from the
        # already frozen S2 contract; they are never scored and never enter an arm.
        from experiments.stage_s2_prompt_v21 import s2_contract

        goal_text = s2_contract.GOAL
        write_paths = tuple(s2_contract.MAIN_WRITE_PATHS)
        acceptance_contract = s2_contract.acceptance()
        public_contract = s2_contract.PUBLIC_TESTS
    prompt = protocol.prompt_spec(cell.arm_id)
    provider_name = "fake" if offline else config["sandbox_provider"]
    sandbox_config = SandboxConfig(
        enabled=True,
        provider=provider_name,
        endpoint=network.sandbox_endpoint()[0],
        image=config["image"],
        workspace_root=WORKSPACE_ROOT,
        network=config["network"],
        cpu=str(config["cpu"]),
        memory=memory_quantity(config["memory_gib"]),
        use_server_proxy=True,
        retain_on_failure=False,
    )
    delegation = DelegationConfig(
        enabled=config["delegation"] == "enabled",
        max_parallel_children=int(config["max_concurrent_children"]),
        max_children_per_call=min(2, int(config["max_concurrent_children"])),
        max_delegation_rounds=int(config["max_delegation_rounds"]),
        child_timeout_seconds=300.0,
        child_max_turns=int(config["child_max_turns"]),
        result_preview_chars=900,
    )
    return GoalRunOptions(
        goal=goal_text,
        runs_root=runs_root,
        workspace_root=runs_root / "host-workspace",
        config=AtlasConfig(tools=ToolsConfig(mode="progressive"), sandbox=sandbox_config),
        delegation=delegation,
        provider="fake" if offline else config["provider"],
        model=config["model"],
        max_turns=int(config["main_max_turns"]),
        token_budget=int(config["token_budget"]),
        main_write_paths=write_paths,
        child_write_grants={},
        provider_factory=provider_factory,
        run_id=run_id,
        sandbox=GoalSandboxOptions(
            provider=provider_name,
            source_repo=fixture_repo,
            image=config["image"],
            workspace_root=WORKSPACE_ROOT,
            require_real=not offline,
            provider_factory=sandbox_factory,
            tool_error_mode=config["tool_error_mode"],
        ),
        main_prompt=prompt,
        acceptance=acceptance_contract,
        public_tests=public_contract,
        tool_error_mode=config["tool_error_mode"],
        tool_payload_recording=True,
    )


@dataclass(frozen=True)
class _OracleOnlySpec:
    oracle_source: str


def host_oracle(workspace: Path, task_id: str) -> dict[str, Any]:
    """Run the frozen oracle on the Host, after the model has stopped. Never in a fixture."""

    if task_id in BY_ID:
        result = task_base.run_oracle(workspace, BY_ID[task_id])
        source = "host:experiments/stage_benchmark/tasks"
    else:
        from experiments.stage_s2_prompt_v21 import s2_contract

        result = task_base.run_oracle(
            workspace, cast(TaskSpec, _OracleOnlySpec(s2_contract.HIDDEN_PROGRAM))
        )
        source = "host:experiments/stage_s2_prompt_v21/s2_contract"
    return {
        "task_id": task_id,
        "source": source,
        "executed_after_main_stops": True,
        **result,
    }


def evidence_contract() -> dict[str, Any]:
    return {
        "required": [
            "session.json",
            "attempt-1.json",
            "transcript.jsonl",
            "tool_conversations.json",
            "workspace.patch",
            "trajectory/",
            "router_decisions.json",
            "worker_outcomes.json",
            "handoff_references.json",
            "provider_usage.json",
            "public_tests.json",
            "oracle.json",
            "sandbox_lifecycle.json",
            "analysis.json",
            "evidence_manifest.json",
        ],
        "derived": ["analysis.json", "evidence_manifest.json"],
        "never": ["oracle source", "reference implementation"],
    }
