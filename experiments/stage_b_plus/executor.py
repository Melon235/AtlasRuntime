"""Stage B+ executable entry point: authority, global claim, dispatch, post-cell gate.

One entry point, ``dispatch``, and one injected dependency, the cell runner. Everything a live run
does is in this module, and the offline proofs go through the *same* code with a fake sandbox and a
scripted model, so a passing proof says something about the production path rather than about a
parallel one.

The live cell, in order
-----------------------
1. a **per-cell** fixture is materialised from the frozen seed and staged as a clean git repository
   (a dirty or missing repository is refused by the provisioner, and the seed digest is checked
   against the frozen one);
2. ``run_goal`` provisions the agent sandbox from that repository, runs the Main and its workers,
   exports ``workspace.patch`` and the workspace copy, and destroys the sandbox;
3. the **exported workspace** — never the fixture — is uploaded into a second, throwaway sandbox
   together with the hidden tests, and pytest runs there. No Host process ever imports agent code,
   and the Main can never read the hidden suite;
4. the cell is scored, and the first stop ends the matrix.

Two identities decide whether a dispatch may start: the *global* claim for the plan digest (not a
file under the evidence root, which a caller could move) and the real ``git HEAD`` (never a value a
caller supplies). ``mode="offline_proof"`` is the only way to inject a runner, a head or a claim
root, and it can never be reached by a production invocation because the mode is a required
argument with no default in the CLI wrapper.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2 import catalog as closure2_catalog
from experiments.evaluation.fixtures import (
    materialize_fixture,
    stage_git_repo,
    workspace_digest,
    workspace_hashes,
)
from experiments.evaluation.models import canonical_digest
from experiments.evaluation.sandboxed_oracle import (
    OracleSandboxConfig,
    run_hidden_tests_in_sandbox,
)
from experiments.stage_b_plus import authority as authority_module
from experiments.stage_b_plus import contract, cost

EXECUTOR_VERSION = "bplus-executor-1.9"

#: Files whose bytes *are* the executor identity. A dispatch is bound to their digests, so editing
#: any of them invalidates an existing signature rather than silently running different code.
#:
#: The two admission files were added after v1: the preflight decides whether a cell may start at
#: all, so leaving it outside the identity meant a signature could survive a change in what it
#: admits. The runtime's admission behaviour is part of what the Owner approves.
EXECUTOR_FILES: tuple[str, ...] = (
    "experiments/stage_b_plus/executor.py",
    "experiments/stage_b_plus/authority.py",
    "experiments/stage_b_plus/contract.py",
    "experiments/stage_b_plus/cost.py",
    # Stage C1 / C1-B / C1-C / C2 plans decide which goal, task, arm, prompt, budget and
    # intervention a cell executes, so their bytes belong in the identity the Owner signs. Stage B+
    # plans do not read these modules.
    "experiments/stage_c1/plan.py",
    "experiments/stage_c1b/plan.py",
    "experiments/stage_c1b/prompts.py",
    "experiments/stage_c1c/plan.py",
    # Stage C2 adds a Main decision prompt; the two prompt objects are what its cells vary.
    "experiments/stage_c2/plan.py",
    "experiments/stage_c2/prompts.py",
    # Stage F decides which of four execution architectures a cell runs, what its evidence schema is
    # and how the 96-cell matrix is ordered, so those bytes are part of the signed identity too.
    "experiments/stage_f/arms.py",
    "experiments/stage_f/plan.py",
    "experiments/stage_f/normalize.py",
    "experiments/stage_f/runners.py",
    "experiments/stage_f/cumulative.py",
    # The preparation gate decides whether the matrix is READY, and its record is what the Owner
    # reads before signing; a gate outside the signed identity could be edited after the fact.
    "experiments/stage_f/preflight.py",
    # The bytes a cell starts from are produced by these two modules: the task builder decides
    # the visible fixture and the materialiser writes it, so a change there changes every cell.
    "experiments/evaluation/closure2/catalog.py",
    "experiments/evaluation/fixtures.py",
    # Stage C1-B: the worker completion contract and the failure classification are part of what a
    # cell executes, so a signature has to cover them.
    "src/atlasos/workers/catalog.py",
    "src/atlasos/app/main_driven.py",
    "src/atlasos/app/live_preflight.py",
    "src/atlasos/app/goal_verification.py",
)

#: A live dispatch and an offline proof are different operations with different privileges.
MODE_LIVE = "live"
MODE_OFFLINE_PROOF = "offline_proof"
MODES: tuple[str, ...] = (MODE_LIVE, MODE_OFFLINE_PROOF)

# -- stop reasons -----------------------------------------------------------------------------

STOP_NONE = "NONE"
STOP_CLAIM_CONSUMED = "CLAIM_CONSUMED"
STOP_CLAIM_UNTRUSTED = "CLAIM_ROOT_NOT_TRUSTED"
STOP_LEGACY_EVIDENCE = "LEGACY_EVIDENCE_PRESENT"
STOP_DIRTY_WORKTREE = "DIRTY_WORKTREE"
STOP_PROVIDER_FAILURE = "PROVIDER_FAILURE"
STOP_SANDBOX_FAILURE = "SANDBOX_FAILURE"
STOP_SANDBOX_CONFIG = "SANDBOX_CONFIG_MISMATCH"
STOP_SANDBOX_CLEANUP = "SANDBOX_CLEANUP_FAILURE"
STOP_HOST_ISOLATION = "HOST_ISOLATION_VIOLATION"
STOP_FIXTURE = "FIXTURE_INVALID"
STOP_EXPORT = "EXPORT_INVALID"
STOP_ORACLE_ENVIRONMENT = "ORACLE_ENVIRONMENT_FAILURE"
STOP_ORACLE_CLEANUP = "ORACLE_CLEANUP_FAILURE"
STOP_UNKNOWN_USAGE = "UNKNOWN_OR_ILLEGAL_USAGE"
STOP_MISSING_EVIDENCE = "MISSING_OR_CORRUPT_EVIDENCE"
STOP_UNKNOWN_FAILURE = "UNKNOWN_FAILURE_KIND"
STOP_IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
STOP_EXECUTION_EXCEPTION = "EXECUTION_EXCEPTION"
STOP_LEDGER_INTEGRITY = "LEDGER_INTEGRITY_FAILURE"
STOP_COST_REACHED = "COST_STOP_REACHED"
STOP_TOKEN_REACHED = "TOKEN_STOP_REACHED"
STOP_NOT_AUTHORIZED = "NOT_AUTHORIZED"
STOP_MODE_VIOLATION = "MODE_VIOLATION"
#: A common integrity check failed or could not be evaluated. Distinct from a measured task
#: failure: the cell's own contract was not established, so the matrix must not continue.
STOP_INTEGRITY = "EXECUTION_INTEGRITY_FAILED"
#: An earlier Stage F batch did not settle: missing report, unknown consumption, a frozen stop, a
#: plan/executor identity mismatch, or no headroom left. Refused before any claim is written.
STOP_CUMULATIVE = "CUMULATIVE_SETTLEMENT_REFUSED"
#: The run never reached the model: admission refused the configuration. Distinct from a provider
#: failure, which means a request was attempted and failed.
STOP_LIVE_PREFLIGHT = "LIVE_PREFLIGHT_BLOCKED"

#: A failure the runtime measured. These continue: they are the experiment's subject matter, not an
#: infrastructure defect. Anything else stops, including an unclassified exception.
NORMAL_FAILURE_KINDS: tuple[str, ...] = (
    "oracle_failed",
    "public_tests_failed",
    "budget_exhaustion",
    "main_max_turns",
    "acceptance_failed",
    "child_failed",
)

REQUIRED_USAGE_FIELDS: tuple[str, ...] = (
    "main_cumulative_input_tokens",
    "main_completion_tokens",
    "child_total_prompt_tokens",
    "child_completion_tokens",
    "system_total_tokens",
)

_ISO = "%Y-%m-%dT%H:%M:%SZ"

#: Where a plan's one-shot claim lives. Deliberately outside the repository and outside every
#: evidence root: a claim that moved with the evidence would be a claim a caller could reset.
_CLAIM_ENV = "ATLAS_BPLUS_CLAIM_ROOT"


def now() -> str:
    return datetime.now(UTC).strftime(_ISO)


def remove_tree(path: Path) -> None:
    """Remove a directory tree, including read-only files.

    Git object files are written read-only, and on Windows ``shutil.rmtree`` refuses to unlink them.
    A fixture or a scratch area therefore has to have the write bit restored before deletion, or a
    second build over the same directory fails for a reason that has nothing to do with the
    experiment. A missing path is not an error.
    """

    def _retry(function: Any, target: str, _error: Any) -> None:
        os.chmod(target, 0o700)
        function(target)

    if not path.exists():
        return
    shutil.rmtree(path, onexc=_retry)


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


# -- identity ---------------------------------------------------------------------------------


def executor_identity(repo_root: Path | None = None) -> dict[str, Any]:
    root = Path(repo_root) if repo_root is not None else contract.REPO_ROOT
    files: dict[str, str] = {}
    for relative in EXECUTOR_FILES:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"executor identity file is missing: {relative}")
        files[relative] = _digest_bytes(path.read_bytes())
    return {
        "executor_version": EXECUTOR_VERSION,
        "executor_digest": authority_module.digest_of(files),
        "files": files,
    }


def git_head(repo_root: Path | None = None) -> str:
    root = str(repo_root or contract.REPO_ROOT)
    completed = subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"cannot resolve git HEAD in {root}: {completed.stderr.strip()[:200]}")
    return completed.stdout.strip()


def git_dirty(repo_root: Path | None = None) -> list[str]:
    """Tracked and untracked changes, so a live dispatch can refuse an unapproved tree."""

    root = str(repo_root or contract.REPO_ROOT)
    completed = subprocess.run(
        ["git", "-C", root, "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        return ["<git status failed>"]
    return [line for line in completed.stdout.splitlines() if line.strip()]


def claim_root() -> tuple[Path, bool]:
    """The global claim directory and whether it is the production default.

    The override exists for offline proofs only. It is an environment variable, not a dispatch
    argument, so no ordinary invocation can point the claim at a directory of its choosing — and
    ``dispatch`` refuses to run a live cell when the override is in force.
    """

    override = os.environ.get(_CLAIM_ENV)
    if override:
        return Path(override), False
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if base:
        root = Path(base) / "AtlasRuntime" / "stage-b-plus" / "claims"
    else:
        root = Path.home() / ".local" / "state" / "atlarruntime" / "stage-b-plus" / "claims"
    return root, True


def claim_path(plan_digest: str) -> Path:
    identifier = plan_digest.split(":", 1)[-1]
    if not identifier or "/" in identifier or "\\" in identifier:
        raise ValueError(f"plan digest is not usable as a claim identity: {plan_digest!r}")
    return claim_root()[0] / f"{identifier}.claim"


@contextlib.contextmanager
def claim_root_override(path: Path) -> Any:
    """Proof-only: point the global claim at a throwaway directory.

    A live dispatch refuses to run while this is in force (``STOP_MODE_VIOLATION``), so the seam can
    be used by the offline proofs without becoming a way to reset a production claim.
    """

    previous = os.environ.get(_CLAIM_ENV)
    os.environ[_CLAIM_ENV] = str(path)
    try:
        yield Path(path)
    finally:
        if previous is None:
            os.environ.pop(_CLAIM_ENV, None)
        else:
            os.environ[_CLAIM_ENV] = previous


# -- the sandbox plan -------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxPlan:
    """Host-owned sandbox configuration, in one place, comparable with the frozen budget."""

    provider_name: str
    image: str
    cpu: str
    memory: str
    timeout_minutes: int
    network: str
    require_real: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "image": self.image,
            "cpu": self.cpu,
            "memory": self.memory,
            "timeout_minutes": self.timeout_minutes,
            "network": self.network,
            "require_real": self.require_real,
        }

    def problems(self) -> list[str]:
        """Compare with the frozen contract. A mismatch is a configuration defect, not a note."""

        problems: list[str] = []
        expected_cpu = str(contract.BUDGET["cpu"])
        if self.cpu != expected_cpu:
            problems.append(f"cpu is {self.cpu!r}, contract says {expected_cpu!r}")
        expected_memory = f"{float(contract.BUDGET['memory_gib']):g}Gi"
        if self.memory != expected_memory:
            problems.append(f"memory is {self.memory!r}, contract says {expected_memory!r}")
        if self.image != str(contract.BUDGET["image"]):
            problems.append(f"image is {self.image!r}, contract says {contract.BUDGET['image']!r}")
        if self.network != str(contract.BUDGET["network"]):
            problems.append(
                f"network is {self.network!r}, contract says {contract.BUDGET['network']!r}"
            )
        if self.timeout_minutes != int(contract.BUDGET["sandbox_timeout_minutes"]):
            problems.append(
                f"timeout is {self.timeout_minutes}, contract says "
                f"{contract.BUDGET['sandbox_timeout_minutes']}"
            )
        return problems


def sandbox_plan(*, require_real: bool = True, provider_name: str | None = None) -> SandboxPlan:
    return SandboxPlan(
        provider_name=provider_name or str(contract.BUDGET["sandbox_provider"]),
        image=str(contract.BUDGET["image"]),
        cpu=str(contract.BUDGET["cpu"]),
        memory=f"{float(contract.BUDGET['memory_gib']):g}Gi",
        timeout_minutes=int(contract.BUDGET["sandbox_timeout_minutes"]),
        network=str(contract.BUDGET["network"]),
        require_real=require_real,
    )


def atlas_config(plan: SandboxPlan) -> Any:
    from atlasos.config import AtlasConfig, SandboxConfig, ToolsConfig

    return AtlasConfig(
        tools=ToolsConfig(mode="progressive"),
        sandbox=SandboxConfig(
            enabled=True,
            provider=plan.provider_name,  # type: ignore[arg-type]
            image=plan.image,
            cpu=plan.cpu,
            memory=plan.memory,
            timeout_minutes=plan.timeout_minutes,
            network=plan.network,  # type: ignore[arg-type]
        ),
    )


# -- the cell ---------------------------------------------------------------------------------


@dataclass
class CellRun:
    """One executed cell, as the runner saw it. ``session`` is ``None`` iff the run raised."""

    session: dict[str, Any] | None
    run_dir: Path | None
    fixture_root: Path | None
    oracle: dict[str, Any] | None
    duration_s: float = 0.0
    error: str | None = None
    error_kind: str | None = None
    notes: tuple[str, ...] = ()
    fixture: dict[str, Any] | None = None
    export: dict[str, Any] | None = None
    sandbox_plan: dict[str, Any] | None = None
    oracle_sandbox: dict[str, Any] | None = None
    expected_run_id: str | None = None
    #: Stage-level wall clock, in milliseconds. A stage the arm does not time stays absent, which
    #: the evidence layer reports as UNKNOWN rather than as zero.
    timings: dict[str, Any] | None = None


CellRunner = Callable[[dict[str, Any], Mapping[str, Any]], Awaitable[CellRun]]


# -- usage and cost ---------------------------------------------------------------------------


def _token_problem(name: str, value: Any) -> str | None:
    if isinstance(value, bool):
        return f"{name} is a bool, not a token count"
    if not isinstance(value, int):
        return f"{name} is {value!r}, not an integer"
    if value < 0:
        return f"{name} is negative ({value})"
    return None


def validate_usage(metrics: Mapping[str, Any] | None) -> dict[str, Any]:
    """Every counter a matrix needs, checked for type and sign. Unknown is not zero."""

    problems: list[str] = []
    values: dict[str, Any] = {}
    if not isinstance(metrics, Mapping):
        return {"ok": False, "problems": ["the run carries no metrics block"], "values": {}}
    for field_name in REQUIRED_USAGE_FIELDS:
        value = metrics.get(field_name)
        values[field_name] = value
        problem = _token_problem(field_name, value)
        if value is None:
            problems.append(f"{field_name} was not reported")
        elif problem is not None:
            problems.append(problem)
    if not problems:
        total = (
            int(values["main_cumulative_input_tokens"])
            + int(values["main_completion_tokens"])
            + int(values["child_total_prompt_tokens"])
            + int(values["child_completion_tokens"])
        )
        if total != int(values["system_total_tokens"]):
            problems.append(
                f"system_total_tokens is {values['system_total_tokens']}, the parts sum to {total}"
            )
    # The cache split is checked only when the provider reported it: a missing split is UNKNOWN,
    # which is a limitation of the data, not a reason to invent a number.
    hit = metrics.get("main_cache_hit_tokens")
    miss = metrics.get("main_cache_miss_tokens")
    input_tokens = metrics.get("main_cumulative_input_tokens")
    if hit is not None and miss is not None and input_tokens is not None:
        for name, value in (("main_cache_hit_tokens", hit), ("main_cache_miss_tokens", miss)):
            problem = _token_problem(name, value)
            if problem is not None:
                problems.append(problem)
        if not problems and int(hit) + int(miss) != int(input_tokens):
            problems.append(
                f"cache hit + miss is {int(hit) + int(miss)}, main input is {input_tokens}"
            )
    return {
        "ok": not problems,
        "problems": problems,
        "values": values,
        "cache_split": "UNKNOWN" if hit is None or miss is None else "COMPLETE",
    }


def cell_cost(
    metrics: Mapping[str, Any] | None, prices: Mapping[str, Any] | None = None
) -> float | None:
    """Frozen-price estimate, or ``None`` when the consumption is not measurable.

    ``None`` is the point: a run whose counters are unknown did not cost zero, and a cost model that
    says 0.0 makes an unknown look cheap. Child prompt tokens are priced as misses because the
    provider reports no per-child split.
    """

    report = validate_usage(metrics)
    if not report["ok"]:
        return None
    price = dict(prices or cost.stage_b_estimate.pricing())
    values = report["values"]
    hit = int(metrics.get("main_cache_hit_tokens") or 0)  # type: ignore[union-attr]
    miss = int(metrics.get("main_cache_miss_tokens") or 0)  # type: ignore[union-attr]
    main_input = int(values["main_cumulative_input_tokens"])
    if not hit and not miss:
        miss = main_input  # no split reported: price the whole input as a miss
    child_prompt = int(values["child_total_prompt_tokens"])
    completion = int(values["main_completion_tokens"]) + int(values["child_completion_tokens"])
    per_million = 1_000_000.0
    return (
        hit * float(price["cache_hit_input_per_million"])
        + (miss + child_prompt) * float(price["cache_miss_input_per_million"])
        + completion * float(price["output_per_million"])
    ) / per_million


# -- evidence helpers -------------------------------------------------------------------------


def public_test_verdict(
    public_tests: Mapping[str, Any] | None, *, expected: Any = None
) -> dict[str, Any]:
    """The public channel's final verdict, read from the runs the producer recorded.

    ``session["public_tests"]`` has no top-level ``status`` in a normal run:
    ``main_driven._public_test_evidence`` records ``contract``, ``command_digest``, ``calls`` and
    one ``runs`` entry per call. The verdict is the *last recorded run*, not the last run that
    happens to carry a status: a final record with no status means the gate was not observed, and an
    earlier PASS must not be inherited.

    ``PublicTestRunner`` increments ``calls`` *before* it talks to the sandbox and returns without
    appending a record when the sandbox raises, so ``calls > len(runs)`` is a reachable production
    state. It is reported as ``INCOMPLETE`` — never as a pass.

    Identity is checked against ``expected`` (the frozen ``PublicTestContract`` for this cell), so a
    run that recorded a different suite cannot be read as this one. ``experiments/stage_benchmark``
    is a protected tree, so this cannot be shared with the Stage B analyzer by import; the offline
    proof instead asserts the two agree on the same real session.
    """

    block = public_tests if isinstance(public_tests, Mapping) else {}
    if not block or str(block.get("status") or "").upper() == "NOT_CONFIGURED":
        return {
            "state": "NOT_CONFIGURED",
            "passed": None,
            "runs": 0,
            "statuses": [],
            "calls": None,
            "calls_consistent": False,
            "identity_consistent": None,
            "index_consistent": None,
            "outcome_consistent": None,
            "evidence_complete": False,
            "last_exit_code": None,
            "configured": False,
            "problems": ["the public suite is not configured"],
        }

    raw_runs = list(block.get("runs") or ())
    runs = [item for item in raw_runs if isinstance(item, Mapping)]
    dropped = len(raw_runs) - len(runs)
    calls = block.get("calls")
    calls_ok = isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0
    expected_test_id = str(getattr(expected, "test_id", "") or "")
    expected_digest = expected.command_digest() if expected is not None else ""

    problems: list[str] = []
    if not calls_ok:
        problems.append(f"calls is {calls!r}, not a non-negative integer")
    elif calls != len(runs):
        problems.append(
            f"{calls} call(s) were made but {len(runs)} result(s) were recorded "
            "(a call returned no record, so the last observation is unknown)"
        )
    if dropped:
        problems.append(f"{dropped} run record(s) are not objects")

    index_ok = True
    for position, run in enumerate(runs, start=1):
        if run.get("call_index") != position:
            index_ok = False
            problems.append(
                f"run {position} carries call_index {run.get('call_index')!r}, expected {position}"
            )

    identity_ok: bool | None = None
    if expected is not None:
        identity_ok = True
        for position, run in enumerate(runs, start=1):
            recorded_id = str(run.get("test_id") or "")
            recorded_digest = str(run.get("command_digest") or "")
            if recorded_id != expected_test_id:
                identity_ok = False
                problems.append(
                    f"run {position} is for test {recorded_id!r}, the frozen contract is "
                    f"{expected_test_id!r}"
                )
            if recorded_digest != expected_digest:
                identity_ok = False
                problems.append(
                    f"run {position} ran command digest {recorded_digest[:19]!r}, the frozen "
                    f"contract is {expected_digest[:19]!r}"
                )
        block_digest = str(block.get("command_digest") or "")
        if block_digest and block_digest != expected_digest:
            identity_ok = False
            problems.append("the recorded suite digest differs from the frozen contract")

    outcome_ok = True
    for position, run in enumerate(runs, start=1):
        status = str(run.get("status") or "").upper()
        exit_code = run.get("exit_code")
        if status not in {"PASS", "FAIL", "ERROR"}:
            outcome_ok = False
            problems.append(f"run {position} has status {run.get('status')!r}")
        elif status == "PASS" and exit_code != 0:
            outcome_ok = False
            problems.append(f"run {position} says PASS but exited {exit_code!r}")
        elif status == "FAIL" and not (isinstance(exit_code, int) and exit_code != 0):
            outcome_ok = False
            problems.append(f"run {position} says FAIL but exited {exit_code!r}")

    statuses = [str(run.get("status") or "").upper() for run in runs]
    evidence_complete = calls_ok and calls == len(runs) and dropped == 0

    if problems:
        # A missing observation is INCOMPLETE; a contradiction inside records that do exist is
        # INVALID. Neither is a pass, and the audit treats them differently.
        observation_only = (
            not evidence_complete and index_ok and outcome_ok and identity_ok is not False
        )
        state = "INCOMPLETE" if observation_only else "INVALID"
        passed = None
    elif not runs:
        state, passed = "NOT_RUN", None
    else:
        last = str(runs[-1].get("status") or "").upper()
        if last == "PASS":
            state, passed = "PASS", True
        elif last == "FAIL":
            state, passed = "FAIL", False
        elif last == "ERROR":
            state, passed = "ERROR", None
        else:
            state, passed = "INVALID", None

    return {
        "state": state,
        "passed": passed,
        "runs": len(runs),
        "statuses": statuses,
        "calls": calls if calls_ok else None,
        "calls_consistent": calls_ok and calls == len(runs),
        "identity_consistent": identity_ok,
        "index_consistent": index_ok,
        "outcome_consistent": outcome_ok,
        "evidence_complete": evidence_complete,
        "last_exit_code": runs[-1].get("exit_code") if runs else None,
        "configured": True,
        "problems": problems,
    }


def _verdict_of(value: Any) -> bool | None:
    """Read a probe verdict that may be a bool, a string or a mapping. ``None`` means unreadable."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in {"PASS", "PASSED", "OK", "TRUE"}:
            return True
        if normalized in {"FAIL", "FAILED", "ERROR", "FALSE", "TIMEOUT"}:
            return False
        return None
    if isinstance(value, Mapping):
        for key in ("ok", "passed", "pass", "status", "verdict"):
            if key in value:
                return _verdict_of(value[key])
    return None


def _sandbox_evidence_problems(session: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Return (lifecycle problems, isolation problems) from the run's own sandbox evidence."""

    lifecycle: list[str] = []
    isolation: list[str] = []
    sandbox = session.get("sandbox")
    if not isinstance(sandbox, Mapping):
        lifecycle.append("the run carries no sandbox evidence (host mode cannot be dispatched)")
        return lifecycle, isolation
    if sandbox.get("created") is not True:
        lifecycle.append("the sandbox was never created")
    if sandbox.get("retained"):
        lifecycle.append("the sandbox was retained instead of destroyed")
    if sandbox.get("destroyed") is not True:
        lifecycle.append("the sandbox was not destroyed")
    if sandbox.get("error"):
        lifecycle.append(f"the sandbox reported {str(sandbox.get('error'))[:120]}")
    if sandbox.get("provider_is_real") is False and sandbox.get("provider") != "fake":
        isolation.append("the provider is not a declared real sandbox provider")
    network = sandbox.get("network_probe")
    if network is None or (isinstance(network, str) and network.upper() == "NOT_RUN"):
        isolation.append("the network probe did not run")
    elif _verdict_of(network) is False:
        isolation.append(f"the network probe failed ({network!r})")
    elif _verdict_of(network) is None:
        isolation.append(f"the network probe verdict is unreadable ({network!r})")
    separation = sandbox.get("host_separation")
    if not isinstance(separation, Mapping) or not separation:
        isolation.append("host separation was not demonstrated")
    elif _verdict_of(separation) is False:
        isolation.append("host separation failed")
    return lifecycle, isolation


#: The producer's export digest. ``GoalSandbox.export`` records ``export.archive_digest`` — the
#: SHA-256 of the tar bytes downloaded from the sandbox. It is a transfer-integrity value: the
#: archive is not retained, so it cannot be recomputed after the fact and it is *not* a hash of the
#: extracted directory. v2 compared the two and refused a cell whose oracle had passed.
EXPORT_ARCHIVE_DIGEST = "workspace_digest"
#: Added by this module, after the export and before the oracle: the Host's own identity of the
#: directory the oracle scores. Only this one can be recomputed at audit time.
EXPORT_DIRECTORY_DIGEST = "directory_digest"
EXPORT_DIRECTORY_FILES = "directory_files"


def workspace_identity(workspace: Path) -> dict[str, Any]:
    """The Host identity of one exported directory, recomputable at any later point."""

    hashes = workspace_hashes(workspace)
    return {
        EXPORT_DIRECTORY_DIGEST: "sha256:" + workspace_digest(workspace),
        EXPORT_DIRECTORY_FILES: len(hashes),
        "directory_digest_source": "host, computed from the extracted export directory",
    }


def _export_problems(run: CellRun) -> list[str]:
    """The exported workspace is what the oracle scores; its Host identity must still hold.

    Two identities are kept apart on purpose. The archive digest is the producer's transfer value
    and is only *recorded* here: the archive bytes are gone, so claiming to have re-verified them
    would be an invented observation. The directory digest is the Host's own value, taken after the
    export and before the oracle, and it is recomputed here — that is what detects a workspace that
    moved after export.
    """

    problems: list[str] = []
    artifacts = (run.session or {}).get("artifacts")
    if not isinstance(artifacts, Mapping):
        return ["the run exported no artifacts"]
    if not isinstance(run.export, Mapping):
        return ["the runner recorded no export; a session field has no host source"]
    runner_export = run.export

    path_value = artifacts.get("workspace_path")
    if not path_value:
        return ["the export carries no workspace_path"]
    workspace = Path(str(path_value))
    if not workspace.is_dir():
        return [f"the exported workspace {workspace} does not exist"]

    # Both sides must name the same scoring object. Comparing one path with a different one would
    # let the oracle score an artifact the audit never looked at.
    runner_path = runner_export.get("workspace_path")
    if not runner_path:
        problems.append("the runner export carries no workspace_path")
    elif Path(str(runner_path)) != workspace:
        problems.append(
            f"the runner scored {runner_path!r} but the session records {str(path_value)!r}"
        )

    # The host identity must come from the runner, which takes it after the export and before the
    # oracle. A value that only exists in the session has no provenance.
    recorded_directory = runner_export.get(EXPORT_DIRECTORY_DIGEST)
    recorded_files = runner_export.get(EXPORT_DIRECTORY_FILES)
    if not recorded_directory:
        problems.append(
            "the host recorded no directory digest for the export; the archive digest cannot "
            "stand in for it"
        )
    elif not (
        isinstance(recorded_directory, str)
        and recorded_directory.startswith("sha256:")
        and len(recorded_directory) == len("sha256:") + 64
    ):
        problems.append(f"the host directory digest is not a sha256 digest: {recorded_directory!r}")
    else:
        actual = "sha256:" + workspace_digest(workspace)
        if actual != recorded_directory:
            problems.append(
                f"the exported workspace hashes to {actual}, the host recorded {recorded_directory}"
            )

    if isinstance(recorded_files, bool) or not isinstance(recorded_files, int):
        problems.append(f"the host recorded {recorded_files!r} exported files, not an integer")
    elif recorded_files <= 0:
        problems.append(f"the host recorded {recorded_files} exported files")
    else:
        actual_files = len(workspace_hashes(workspace))
        if recorded_files != actual_files:
            problems.append(
                f"the host recorded {recorded_files} exported files, the directory holds "
                f"{actual_files}"
            )

    session_archive = artifacts.get(EXPORT_ARCHIVE_DIGEST)
    runner_archive = runner_export.get(EXPORT_ARCHIVE_DIGEST)
    if not session_archive:
        problems.append("the session carries no archive digest")
    if not runner_archive:
        problems.append("the runner export carries no archive digest")
    elif session_archive and str(runner_archive) != str(session_archive):
        problems.append(
            f"the runner recorded archive digest {runner_archive} and the session {session_archive}"
        )

    files = artifacts.get("workspace_files")
    if not isinstance(files, int) or isinstance(files, bool) or files <= 0:
        problems.append(f"the export reports {files!r} files")
    elif files != len(workspace_hashes(workspace)):
        problems.append(
            f"the export reports {files} files, the directory holds "
            f"{len(workspace_hashes(workspace))}"
        )
    if not artifacts.get("patch_digest"):
        problems.append("the export carries no patch_digest")
    return problems


def _fixture_problems(run: CellRun) -> list[str]:
    problems: list[str] = []
    fixture = run.fixture
    if not isinstance(fixture, Mapping):
        return ["the cell carries no fixture evidence"]
    path_value = fixture.get("path")
    if not path_value or not Path(str(path_value)).is_dir():
        return ["the per-cell fixture does not exist"]
    if fixture.get("matches_frozen_seed") is not True:
        problems.append("the fixture seed does not match the frozen seed digest")
    if not fixture.get("commit"):
        problems.append("the fixture repository has no commit")
    root = Path(str(path_value))
    written: dict[str, str] = {}
    unreadable: list[str] = []
    for name in sorted(fixture.get("files_list") or ()):
        try:
            written[name] = (root / name).read_text(encoding="utf-8")
        except OSError:
            unreadable.append(name)
    if unreadable:
        # A missing file is the strongest form of "the fixture moved": report it, do not raise.
        problems.append(f"the fixture lost {len(unreadable)} file(s): {unreadable[:3]}")
    elif fixture.get("seed_digest") and canonical_digest(written) != fixture["seed_digest"]:
        problems.append("the fixture was modified during the run")
    return problems


def _oracle_problems(run: CellRun) -> tuple[list[str], list[str]]:
    """(environment problems, cleanup problems). A FAIL is neither: it is a measured outcome."""

    oracle = run.oracle or {}
    environment: list[str] = []
    cleanup: list[str] = []
    if oracle.get("cleanup_failure"):
        cleanup.append(f"the oracle sandbox was not destroyed: {oracle['cleanup_failure']}")
    if oracle.get("environment_failure") is True:
        environment.append(f"the oracle environment failed ({oracle.get('kind')})")
    status = str(oracle.get("status") or "")
    if status not in {"PASS", "FAIL"} and not environment:
        environment.append(f"the oracle did not produce a verdict (status={status!r})")
    if oracle.get("execution_mode") != "sandbox":
        environment.append(
            f"the oracle ran in {oracle.get('execution_mode')!r} mode; a sandbox is required"
        )
    if status in {"PASS", "FAIL"} and not oracle.get("sandbox_id") and not environment:
        environment.append("the oracle sandbox has no identity")
    return environment, cleanup


# -- the post-cell gate -----------------------------------------------------------------------


def audit_cell(
    *,
    cell: Mapping[str, Any],
    run: CellRun,
    plan: Mapping[str, Any],
    usage_before: Mapping[str, Any],
    cost_before: float | None,
    prices: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one finished cell and decide whether the matrix may continue.

    Pure: it reads the run and the plan, writes nothing. Every integrity failure produces a stop
    code, so no cell can be reported ``PASS`` while one of its own checks failed.

    A cell whose arm has no main-driven session (Stage F ``A0``/``A2``) carries Unified Cell
    instead. That record is audited by its own schema, which is the only honest way to score a run
    that has no Main: the alternative would be inventing session fields for it.
    """

    from experiments.stage_f import normalize as stage_f_normalize

    if (
        isinstance(run.session, Mapping)
        and run.session.get("schema_version") == stage_f_normalize.SCHEMA_VERSION
    ):
        from experiments.stage_f import runners as stage_f_runners

        return stage_f_runners.audit_stage_f_cell(
            cell=cell,
            run=run,
            plan=plan,
            usage_before=usage_before,
            cost_before=cost_before,
            prices=prices,
        )

    stops: list[str] = []
    checks: list[dict[str, Any]] = []
    notes: list[str] = []

    def check(name: str, ok: bool, detail: str, stop: str | None = None) -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok and stop is not None and stop not in stops:
            stops.append(stop)
        return bool(ok)

    # A Stage F cell names an execution arm, which is not in the B+ capability registry; it carries
    # its own profile and exposure instead.
    arm = contract.ARMS_BY_ID.get(str(cell["arm_id"]))
    declared_profiles = list(cell.get("worker_profiles") or ())
    expected_profile = (
        arm.profile_id
        if arm
        else cell.get("worker_profile") or (declared_profiles[0] if declared_profiles else None)
    )
    expected_exposure = arm.exposure if arm else cell.get("worker_exposure")
    check(
        "identity_matches_plan",
        cell.get("plan_id") == plan["plan_id"] and cell.get("scope") == plan["scope"],
        f"cell says {cell.get('plan_id')}/{cell.get('scope')}",
        STOP_IDENTITY_MISMATCH,
    )

    session = run.session
    if session is None:
        check(
            "execution_returned",
            False,
            f"{run.error_kind or 'exception'}: {run.error}",
            _exception_stop(run),
        )
        return {
            "cell_id": cell["cell_id"],
            "arm_id": cell["arm_id"],
            "task_id": cell["task_id"],
            "profile_id": expected_profile,
            "exposure": expected_exposure,
            "checks": checks,
            "stops": stops,
            "agent_success": None,
            "public_test_status": "NOT_RUN",
            "public_test_runs": 0,
            "static_acceptance": None,
            "oracle_passed": None,
            "goal_correctness": None,
            "execution_integrity": "FAIL",
            "usage": {key: None for key in REQUIRED_USAGE_FIELDS},
            "delta_cost_usd": None,
            "cost_usd": None,
            "verdict": "STOPPED",
            "duration_s": round(run.duration_s, 3),
            "notes": [*run.notes, "no session: consumption is unknown, not zero"],
        }
    check("execution_returned", True, f"status={session.get('status')}")

    # runtime status: a measured goal failure continues, an unknown one stops
    failure = session.get("failure") or {}
    kind = str(failure.get("kind") or "") if isinstance(failure, Mapping) else ""
    status = str(session.get("status") or "")
    agent_success = status == "succeeded"
    if status not in {"succeeded", "failed"}:
        check("runtime_status_known", False, f"status is {status!r}", STOP_UNKNOWN_FAILURE)
    elif status == "failed":
        check(
            "failure_is_measurable",
            kind in NORMAL_FAILURE_KINDS,
            f"kind={kind or 'MISSING'}",
            STOP_UNKNOWN_FAILURE,
        )
    else:
        check("runtime_status_known", True, "succeeded")

    # sandbox lifecycle and isolation, read from the run's own evidence
    lifecycle, isolation = _sandbox_evidence_problems(session)
    if lifecycle:
        check("sandbox_lifecycle", False, "; ".join(lifecycle), _lifecycle_stop(lifecycle))
    else:
        check("sandbox_lifecycle", True, "created, exported and destroyed")
    if isolation:
        check("sandbox_isolation", False, "; ".join(isolation), STOP_HOST_ISOLATION)
    else:
        check("sandbox_isolation", True, "network none and host separation demonstrated")

    # the sandbox that ran must be the sandbox the contract asked for
    plan_problems = (
        SandboxPlan(**run.sandbox_plan).problems()
        if isinstance(run.sandbox_plan, Mapping)
        else ["the runner recorded no sandbox plan"]
    )
    check(
        "sandbox_matches_contract",
        not plan_problems,
        "; ".join(plan_problems) or "cpu/memory/image/network/timeout as frozen",
        STOP_SANDBOX_CONFIG,
    )

    # the fixture the cell ran on, and the export the oracle will score
    for problem in _fixture_problems(run):
        check("fixture_valid", False, problem, STOP_FIXTURE)
    if not any(not item["ok"] for item in checks if item["check"] == "fixture_valid"):
        check("fixture_valid", True, "per-cell clean repository, seed digest matches")

    export_problems = _export_problems(run)
    if export_problems:
        check("export_valid", False, "; ".join(export_problems), STOP_EXPORT)
    else:
        check("export_valid", True, "workspace_path exists and matches its digest")

    # disclosure actually applied
    controls = session.get("delegation_controls") or {}
    profiles = controls.get("profiles") or {}
    exposure = (controls.get("exposure") or {}).get("level")
    profile = profiles.get(expected_profile or "") or {}
    # An arm with no Atlas worker profile (the native single agent) has nothing to compare here;
    # that is NOT_APPLICABLE, not a mismatch, and it is said out loud instead of being skipped.
    if expected_profile is None:
        check(
            "worker_profile_matches_arm",
            True,
            "NOT_APPLICABLE: this arm runs no Atlas worker profile",
        )
        check("exposure_matches_arm", True, "NOT_APPLICABLE: native tool exposure")
        check("worker_turns_effective", True, "NOT_APPLICABLE: no Atlas worker turn cap")
    else:
        check(
            "worker_profile_matches_arm",
            bool(profile) and profile.get("profile_id", expected_profile) == expected_profile,
            f"offered={list(profiles)} expected={expected_profile}",
            STOP_IDENTITY_MISMATCH,
        )
        check(
            "exposure_matches_arm",
            exposure == expected_exposure,
            f"applied={exposure!r} expected={expected_exposure!r}",
            STOP_IDENTITY_MISMATCH,
        )
        check(
            "worker_turns_effective",
            profile.get("effective_max_turns") == int(contract.BUDGET["worker_max_turns"]),
            f"effective={profile.get('effective_max_turns')} "
            f"expected={contract.BUDGET['worker_max_turns']}",
            STOP_IDENTITY_MISMATCH,
        )

    # usage and cost: unknown stays unknown
    usage_report = validate_usage(session.get("metrics"))
    check(
        "usage_is_complete",
        usage_report["ok"],
        "; ".join(usage_report["problems"]) or "ok",
        STOP_UNKNOWN_USAGE,
    )
    metrics = session.get("metrics") if isinstance(session.get("metrics"), Mapping) else {}
    delta_cost = cell_cost(metrics, prices) if usage_report["ok"] else None

    # evidence: the run directory must hold a parseable session that names this cell
    session_path = Path(run.run_dir) / "session.json" if run.run_dir is not None else None
    parsed: dict[str, Any] | None = None
    if session_path is None or not session_path.is_file():
        check("run_evidence_present", False, "session.json is missing", STOP_MISSING_EVIDENCE)
    else:
        try:
            candidate = json.loads(session_path.read_text(encoding="utf-8"))
            parsed = candidate if isinstance(candidate, dict) else None
        except (OSError, json.JSONDecodeError) as error:
            notes.append(f"session.json is not parseable: {error}")
            parsed = None
        expected = run.expected_run_id or str(cell["cell_id"])
        if parsed is None:
            check(
                "run_evidence_present",
                False,
                "session.json is empty or not a JSON object",
                STOP_MISSING_EVIDENCE,
            )
        elif run.expected_run_id is not None:
            if parsed.get("run_id") != expected:
                check(
                    "run_evidence_present",
                    False,
                    f"session run_id {parsed.get('run_id')!r} is not this cell's {expected!r}",
                    STOP_IDENTITY_MISMATCH,
                )
            else:
                check("run_evidence_present", True, f"session.json names {expected}")
        elif not str(parsed.get("run_id") or "").startswith(expected):
            check(
                "run_evidence_present",
                False,
                f"session run_id {parsed.get('run_id')!r} does not belong to {cell['cell_id']}",
                STOP_IDENTITY_MISMATCH,
            )
        else:
            check("run_evidence_present", True, f"session.json names {parsed.get('run_id')}")

    # the independent oracle
    oracle_environment, oracle_cleanup = _oracle_problems(run)
    oracle = run.oracle or {}
    oracle_status = str(oracle.get("status") or "")
    oracle_passed = True if oracle_status == "PASS" else False if oracle_status == "FAIL" else None
    if oracle_cleanup:
        check("oracle_cleanup", False, "; ".join(oracle_cleanup), STOP_ORACLE_CLEANUP)
    else:
        check("oracle_cleanup", True, "the oracle sandbox was destroyed")
    if oracle_environment:
        check("oracle_verdict", False, "; ".join(oracle_environment), STOP_ORACLE_ENVIRONMENT)
    else:
        check("oracle_verdict", True, f"independent oracle {oracle_status}")

    # usage aggregate
    usage: dict[str, Any] = dict(usage_before)
    metrics_in = session.get("metrics") or {}
    for key in REQUIRED_USAGE_FIELDS:
        value = metrics_in.get(key)
        if value is None:
            usage[key] = None
        elif isinstance(value, int) and not isinstance(value, bool):
            previous = usage.get(key)
            usage[key] = None if previous is None else int(previous) + value

    if delta_cost is None or cost_before is None:
        total_cost: float | None = None
    else:
        total_cost = round(float(cost_before) + delta_cost, 6)

    suite = public_test_verdict(
        session.get("public_tests") if isinstance(session.get("public_tests"), Mapping) else None,
        expected=contract.public_test_spec(task_for_cell(cell)),
    )
    acceptance = session.get("acceptance")
    # The evidence consistency the reader computed is part of the gate, not a diagnostic it drops.
    check(
        "public_test_configured",
        suite["configured"],
        "the cell must run the frozen public suite; NOT_CONFIGURED is not a way past it",
        STOP_IDENTITY_MISMATCH,
    )
    check(
        "public_test_evidence_intact",
        suite["state"] not in {"INCOMPLETE", "INVALID"},
        "; ".join(suite["problems"]) or f"{suite['runs']} recorded run(s), complete",
        STOP_MISSING_EVIDENCE,
    )

    goal_correctness: bool | None
    if oracle_passed is None or not agent_success:
        goal_correctness = False if oracle_passed is False else None
    elif suite["state"] == "FAIL":
        # The last public run failed; an earlier pass does not undo it. A measured failure.
        goal_correctness = False
    elif suite["state"] == "PASS":
        goal_correctness = bool(oracle_passed and agent_success and (acceptance in (None, "PASS")))
    else:
        # NOT_RUN / ERROR / INCOMPLETE / INVALID: the gate was not observed, so correctness is
        # unknown. An unobserved gate is never a pass; the integrity ones have already stopped.
        goal_correctness = None

    execution_integrity = "PASS" if not stops else "FAIL"
    if stops:
        verdict = "STOPPED"
    elif goal_correctness:
        verdict = "PASS"
    else:
        # The agent may have claimed success; the independent oracle decides. Either way this is a
        # measured outcome, not an infrastructure defect, and the matrix continues.
        verdict = "MEASURED_FAILURE"

    return {
        "cell_id": cell["cell_id"],
        "arm_id": cell["arm_id"],
        "task_id": cell["task_id"],
        "profile_id": expected_profile,
        "exposure": expected_exposure,
        "status": status,
        "failure_kind": kind or None,
        "agent_success": agent_success,
        "public_test_status": suite["state"],
        "public_test_runs": suite["runs"],
        "public_test_problems": list(suite["problems"]),
        "static_acceptance": acceptance,
        "oracle_passed": oracle_passed,
        "oracle_status": oracle_status or None,
        "goal_correctness": goal_correctness,
        "execution_integrity": execution_integrity,
        "executor_verdict": verdict,
        "checks": checks,
        "stops": stops,
        "usage": {key: usage.get(key) for key in REQUIRED_USAGE_FIELDS},
        "delta_cost_usd": None if delta_cost is None else round(delta_cost, 6),
        "cost_usd": total_cost,
        "verdict": verdict,
        "duration_s": round(run.duration_s, 3),
        "notes": list(run.notes),
    }


def _lifecycle_stop(problems: list[str]) -> str:
    for problem in problems:
        if "destroy" in problem or "retain" in problem or "reported" in problem:
            return STOP_SANDBOX_CLEANUP
    return STOP_SANDBOX_FAILURE


def _exception_stop(run: CellRun) -> str:
    if run.error_kind == "preflight":
        return STOP_LIVE_PREFLIGHT
    if run.error_kind in {"provider", "timeout"}:
        return STOP_PROVIDER_FAILURE
    if run.error_kind == "sandbox":
        return STOP_SANDBOX_FAILURE
    if run.error_kind == "oracle":
        return STOP_ORACLE_ENVIRONMENT
    if run.error_kind == "fixture":
        return STOP_FIXTURE
    return STOP_EXECUTION_EXCEPTION


# -- persistence ------------------------------------------------------------------------------


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(payload), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _claim(path: Path, payload: Mapping[str, Any]) -> bool:
    """Create ``path`` exactly once. Returns False when the claim already exists."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return True


def _fresh_ledger(path: Path) -> bool:
    if not path.exists():
        return True
    return path.is_file() and path.stat().st_size == 0


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# -- dispatch ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchContext(Mapping[str, Any]):
    """What a runner is allowed to know. A mapping, so a runner reads it like a dict."""

    plan: Mapping[str, Any]
    authority: Mapping[str, Any]
    runs_root: Path
    evidence_root: Path
    head: str
    prices: Mapping[str, Any] = field(default_factory=dict)

    def _fields(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "authority": self.authority,
            "runs_root": self.runs_root,
            "evidence_root": self.evidence_root,
            "head": self.head,
            "prices": self.prices,
        }

    def __getitem__(self, key: str) -> Any:
        return self._fields()[key]

    def __iter__(self) -> Any:
        return iter(self._fields())

    def __len__(self) -> int:
        return len(self._fields())


def _is_stage_f_batch(plan: Mapping[str, Any]) -> bool:
    """A Stage F batch plan, identified by the frozen master identity and its batch number."""

    return (
        str(plan.get("master_id") or "") == "stage-f-96-manifest-v1"
        and plan.get("batch") in (1, 2, 3)
        and bool(plan.get("cells"))
    )


def _stage_f_evidence_root(
    plan: Mapping[str, Any], evidence_root: Path, stage_f_root: Path | None
) -> Path:
    """The canonical layout: ``<stage-f-root>/<plan_id>/{matrix-report,...}``.

    One root holds all three batches, so the settlement reader and the dispatch writer cannot
    disagree about where a batch's report lives. A caller that passes the batch directory itself
    (whose name *is* the plan id) is normalised to the same layout instead of being silently
    treated as a second root.
    """

    if _is_stage_f_batch(plan):
        root = Path(stage_f_root) if stage_f_root is not None else Path(evidence_root)
        if root.name != str(plan["plan_id"]):
            root = root / str(plan["plan_id"])
        return root
    return Path(evidence_root)


def _stage_f_settlement(
    plan: Mapping[str, Any], *, stage_root: Path, identity: Mapping[str, Any]
) -> dict[str, Any]:
    """Settle a Stage F batch against its predecessors and this dispatch's own identity.

    The manifest is the frozen source of both the batch digests and the global ceiling; the prior
    reports are the evidence; the claim files say whether a batch was actually spent. A batch whose
    manifest entry does not name this plan digest has been rebuilt after the freeze, which is a
    refusal rather than a warning.
    """

    from experiments.stage_f import cumulative
    from experiments.stage_f import plan as f_plan

    manifest = f_plan.manifest()
    batch = int(plan["batch"])
    problems: list[str] = []
    frozen = next((item for item in manifest["batches"] if item["batch"] == batch), None)
    if frozen is None:
        problems.append(f"the master manifest has no batch {batch}")
    elif frozen["plan_digest"] != plan.get("digest"):
        problems.append(
            f"the dispatched plan digest {plan.get('digest')!r} is not the frozen "
            f"{frozen['plan_digest']!r}"
        )
    payload = cumulative.settlement(
        manifest,
        batch=batch,
        evidence_root=Path(stage_root),
        executor_digest=str(identity["executor_digest"]),
        claim_consumed=lambda digest: claim_path(digest).exists(),
    )
    payload.update(
        {
            "plan_id": plan["plan_id"],
            "plan_digest": plan.get("digest"),
            "manifest_digest": authority_module.digest_of(manifest),
            "executor_digest": identity["executor_digest"],
            "authorized_against": "stage-f-96-manifest-v1",
        }
    )
    if problems:
        payload["problems"] = [*payload["problems"], *problems]
        payload["allowed_tokens"] = 0
        payload["allowed_cost_usd"] = 0.0
        payload["may_start"] = False
    return payload


async def dispatch(
    plan_id: str,
    *,
    runs_root: Path,
    evidence_root: Path,
    authority_path: Path | None = None,
    cell_runner: CellRunner | None = None,
    repo_root: Path | None = None,
    mode: str = MODE_LIVE,
    head_override: str | None = None,
    prices: Mapping[str, Any] | None = None,
    clock: Callable[[], str] = now,
    plan: Mapping[str, Any] | None = None,
    stage_f_root: Path | None = None,
) -> dict[str, Any]:
    """Run one plan, or refuse and say why. The only entry point that may execute a cell.

    ``plan`` is the proof seam: an offline harness may pass a plan record it built itself (a
    temporary four-arm runner proof) instead of registering a new frozen plan id. Offline mode is
    required for it, because every live refusal rule — head, claim root, dirty worktree, admission —
    is about a plan that will spend a real budget.

    ``stage_f_root`` names the one root the three Stage F batches share. It is only read for a
    Stage F batch plan; every earlier stage keeps ``evidence_root`` exactly as it was.
    """

    if mode not in MODES:
        raise ValueError(f"unknown dispatch mode {mode!r}; expected one of {list(MODES)}")
    offline = mode == MODE_OFFLINE_PROOF
    if plan is not None and not offline:
        raise ValueError("an injected plan record is only accepted in offline-proof mode")

    plan = dict(plan) if plan is not None else contract.scope_plan(plan_id)
    evidence = _stage_f_evidence_root(plan, Path(evidence_root), stage_f_root)
    await asyncio.to_thread(evidence.mkdir, parents=True, exist_ok=True)
    document = contract.protocol_document()
    identity = executor_identity(repo_root)
    actual_head = head_override if (offline and head_override) else git_head(repo_root)
    root, claim_is_default = claim_root()
    claim_file = claim_path(plan["digest"])
    record = _read_json(Path(authority_path)) if authority_path else None

    violations: list[str] = []
    if not offline:
        if head_override:
            violations.append("a live dispatch cannot be given a head; it reads the repository")
        if not claim_is_default:
            violations.append(
                f"{_CLAIM_ENV} is set, so the global claim is not the production claim"
            )
        runner = cell_runner or live_cell_runner()
        if not getattr(runner, "is_live", False):
            violations.append("a live dispatch requires the live cell runner")
        dirty = git_dirty(repo_root)
        if dirty:
            violations.append(f"the worktree has {len(dirty)} uncommitted path(s)")
        if prices is not None:
            violations.append("a live dispatch uses the frozen price block, not injected prices")

    claim_consumed = claim_file.exists()
    authority_verdict = authority_module.verify(
        record,
        plan=plan,
        head=actual_head,
        executor=identity,
        protocol_digest_value=authority_module.protocol_digest(document),
        repo_root=repo_root,
        claim_consumed=claim_consumed,
    )
    problems = [*violations, *authority_verdict["problems"]]
    if problems:
        stop = (
            STOP_MODE_VIOLATION
            if violations
            else STOP_CLAIM_CONSUMED
            if claim_consumed
            else STOP_NOT_AUTHORIZED
        )
        report = _refused_report(plan, identity, actual_head, problems, stop, clock, mode)
        (evidence / "dispatch-refused.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        return report

    # -- cross-batch settlement, before the claim ------------------------------------------------
    #
    # Three batch plans with three claims would each see the full ceiling. For a Stage F batch the
    # settlement of the earlier batches is consulted here, and its allowance becomes the batch's
    # effective stop. This is a read of evidence that already exists; it writes its decision next
    # to that batch's report.
    settlement: dict[str, Any] | None = None
    if _is_stage_f_batch(plan):
        settlement = _stage_f_settlement(
            plan,
            stage_root=evidence.parent,
            identity=identity,
        )
        settlement["stage_f_root"] = str(evidence.parent)
        settlement["evidence_root"] = str(evidence)
        (evidence / "cumulative-settlement.json").write_text(
            json.dumps(settlement, indent=2, default=str) + "\n", encoding="utf-8"
        )
        if not settlement["may_start"]:
            report = _refused_report(
                plan,
                identity,
                actual_head,
                list(settlement["problems"]) or ["the cross-batch settlement refuses this batch"],
                STOP_CUMULATIVE,
                clock,
                mode,
                settlement=settlement,
            )
            (evidence / "dispatch-refused.json").write_text(
                json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
            )
            return report

    # A live dispatch admits the six production configurations *before* the claim exists, so a
    # configuration that cannot run costs no attempt. v1 discovered its refusal on the first cell,
    # after the claim was spent.
    admission: dict[str, Any] | None = None
    if not offline:
        admission = run_admission_preflight(plan, repo_root=repo_root)
        # The report is written whether it admits or refuses: a READY run must leave the same
        # six-arm evidence a refused one does, instead of a null.
        (evidence / "admission.json").write_text(
            json.dumps(admission, indent=2, default=str) + "\n", encoding="utf-8"
        )
        if admission["status"] != "READY":
            report = _refused_report(
                plan,
                identity,
                actual_head,
                list(admission["blocking_failures"]),
                STOP_LIVE_PREFLIGHT,
                clock,
                mode,
                admission=admission,
            )
            (evidence / "dispatch-refused.json").write_text(
                json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
            )
            return report

    ledger_path = evidence / "ledger.jsonl"
    if not _fresh_ledger(ledger_path):
        return _refused_report(
            plan,
            identity,
            actual_head,
            ["the ledger is not fresh"],
            STOP_LEDGER_INTEGRITY,
            clock,
            mode,
        )

    claimed = _claim(
        claim_file,
        {
            "plan_id": plan["plan_id"],
            "plan_digest": plan["digest"],
            "execution_head": actual_head,
            "executor_digest": identity["executor_digest"],
            "authority_digest": authority_verdict["authority_digest"],
            "claimed_at": clock(),
            "claim_root_is_default": claim_is_default,
            "mode": mode,
        },
    )
    if not claimed:
        return _refused_report(
            plan,
            identity,
            actual_head,
            ["the dispatch claim for this plan is already consumed"],
            STOP_CLAIM_CONSUMED,
            clock,
            mode,
        )

    runner = cell_runner or live_cell_runner()
    frozen_prices = dict(cost.stage_b_estimate.pricing())
    context = DispatchContext(
        plan=plan,
        authority=record or {},
        runs_root=Path(runs_root),
        evidence_root=evidence,
        head=actual_head,
        prices=frozen_prices if not offline else dict(prices or frozen_prices),
    )
    started = clock()
    usage: dict[str, Any] = dict.fromkeys(REQUIRED_USAGE_FIELDS, 0)
    total_cost: float | None = 0.0
    stop_reason = STOP_NONE
    stopped_at: int | None = None
    cells: list[dict[str, Any]] = []

    cost_stop = float((record or {}).get("cost_stop_usd") or 0.0)
    per_cell_stop = float((record or {}).get("per_cell_cost_stop_usd") or 0.0)
    token_stop = int((record or {}).get("token_stop") or 0)
    authorized_stops = {"cost_stop_usd": cost_stop, "token_stop": token_stop}
    if settlement is not None:
        # The effective ceiling is the smallest of the three: the frozen batch allowance, what the
        # Owner authorized, and what is left of the global budget. It is the value the loop below
        # actually compares against, not a number that only appears in the report.
        allowances = [float(settlement["allowed_cost_usd"])]
        if cost_stop > 0:
            allowances.append(cost_stop)
        cost_stop = min(allowances)
        token_allowances = [int(settlement["allowed_tokens"])]
        if token_stop > 0:
            token_allowances.append(token_stop)
        token_stop = min(token_allowances)

    for cell in plan["cells"]:
        # Same short key the runner uses, so the attempt marker, the analysis and the run sit
        # together. The identity is the cell id inside those files, not the directory name.
        cell_root = evidence / "cells" / f"c{int(cell['order']):02d}"
        attempt_path = cell_root / "attempt.json"
        if not _claim(
            attempt_path,
            {
                "cell_id": cell["cell_id"],
                "plan_id": plan["plan_id"],
                "state": "CLAIMED",
                "claimed_at": clock(),
                "attempt_limit": 1,
            },
        ):
            stop_reason, stopped_at = STOP_CLAIM_CONSUMED, int(cell["order"])
            break

        _append_jsonl(
            evidence / "dispatch.jsonl",
            {
                "order": cell["order"],
                "cell_id": cell["cell_id"],
                "cost_before_usd": total_cost,
                "tokens_before": dict(usage),
                "dispatched_at": clock(),
                "execution_head": actual_head,
                "plan_digest": plan["digest"],
            },
        )

        try:
            # A Stage F plan routes every cell to the runner of its execution mode. A plan with
            # ``execution_mode`` keeps the one live runner, so every earlier stage is unchanged.
            cell_runner_for_cell = cell_runner or cell_runner_for(cell, fallback=runner)
            run = await cell_runner_for_cell(cell, context)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 - an exception is a stop, not a shrug
            run = CellRun(
                session=None,
                run_dir=None,
                fixture_root=None,
                oracle=None,
                error=f"{type(error).__name__}: {error}"[:500],
                error_kind="sandbox"
                if "sandbox" in type(error).__name__.lower()
                else "provider"
                if "provider" in type(error).__name__.lower()
                else "exception",
            )

        outcome = audit_cell(
            cell=cell,
            run=run,
            plan=plan,
            usage_before=usage,
            cost_before=total_cost,
            prices=context.prices,
        )
        delta = outcome["delta_cost_usd"]
        over_cell_stop = (
            per_cell_stop > 0
            and delta is not None
            and delta >= per_cell_stop
            and STOP_COST_REACHED not in outcome["stops"]
        )
        if over_cell_stop:
            outcome["stops"].append(STOP_COST_REACHED)
            outcome["verdict"] = "STOPPED"
        usage = dict(outcome["usage"])
        total_cost = outcome["cost_usd"]
        cells.append(outcome)

        _append_jsonl(
            ledger_path,
            {
                "order": cell["order"],
                "cell_id": cell["cell_id"],
                "verdict": outcome["verdict"],
                "stops": outcome["stops"],
                "cost_usd": outcome["cost_usd"],
                "cumulative_tokens": usage.get("system_total_tokens"),
                "recorded_at": clock(),
            },
        )
        _write_analysis(cell_root, plan, cell, outcome, run)

        if outcome["stops"]:
            stop_reason, stopped_at = outcome["stops"][0], int(cell["order"])
            break
        if cost_stop > 0 and total_cost is not None and total_cost >= cost_stop:
            stop_reason, stopped_at = STOP_COST_REACHED, int(cell["order"])
            break
        cumulative_tokens = usage.get("system_total_tokens")
        if (
            token_stop > 0
            and cumulative_tokens is not None
            and int(cumulative_tokens) >= token_stop
        ):
            stop_reason, stopped_at = STOP_TOKEN_REACHED, int(cell["order"])
            break

    finished = clock()
    # The floor is the sum of what each cell measurably *cost*, not of the running totals: adding
    # cumulative values counts the first cell once per later cell.
    observed_floor = round(
        sum(cell["delta_cost_usd"] for cell in cells if cell["delta_cost_usd"] is not None),
        6,
    )
    completeness = "COMPLETE"
    if any(cell["usage"].get(key) is None for cell in cells for key in REQUIRED_USAGE_FIELDS):
        completeness = "UNKNOWN"
    if any(cell["cost_usd"] is None for cell in cells):
        completeness = "UNKNOWN"
    report = {
        "stage": contract.STAGE_ID,
        "protocol_version": contract.PROTOCOL_VERSION,
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "executor_files": identity["files"],
        "mode": mode,
        "plan_id": plan["plan_id"],
        "scope": plan["scope"],
        "plan_digest": plan["digest"],
        "execution_head": actual_head,
        "authority_digest": authority_verdict["authority_digest"],
        "authorized_cells": list(authority_verdict["cell_ids"]),
        "claim_path": str(claim_file),
        "claim_root_is_default": claim_is_default,
        "is_benchmark_sample": bool(plan["is_benchmark_sample"]),
        "started_at": started,
        "finished_at": finished,
        "cells_planned": len(plan["cells"]),
        "cells_started": len(cells),
        "cells_passed": sum(1 for cell in cells if cell["verdict"] == "PASS"),
        "cells_measured_failure": sum(1 for cell in cells if cell["verdict"] == "MEASURED_FAILURE"),
        "cells_stopped": sum(1 for cell in cells if cell["verdict"] == "STOPPED"),
        "cells_not_run": len(plan["cells"]) - len(cells),
        "stop_reason": stop_reason,
        "stopped_at_order": stopped_at,
        "complete": stop_reason == STOP_NONE and len(cells) == len(plan["cells"]),
        "usage_completeness": completeness,
        "total_tokens": usage.get("system_total_tokens") if completeness == "COMPLETE" else None,
        "total_cost_usd": total_cost if completeness == "COMPLETE" else None,
        "observed_cost_floor_usd": observed_floor,
        "cost_stop_usd": cost_stop,
        "per_cell_cost_stop_usd": per_cell_stop,
        "token_stop": token_stop,
        "effective_stops": {
            "authorized": authorized_stops,
            "applied": {"cost_stop_usd": cost_stop, "token_stop": token_stop},
            "rule": "min(frozen batch allowance, owner authorization, global remaining)",
        },
        "cumulative_settlement": settlement,
        "pricing": dict(context.prices),
        "cells": cells,
        "aggregate_includes_sample": bool(plan["is_benchmark_sample"]),
        "retry": "FORBIDDEN",
        "resume": "FORBIDDEN",
    }
    (evidence / "matrix-report.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return report


def _refused_report(
    plan: Mapping[str, Any],
    identity: Mapping[str, Any],
    head: str,
    problems: list[str],
    stop: str,
    clock: Callable[[], str],
    mode: str,
    *,
    admission: Mapping[str, Any] | None = None,
    settlement: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "stage": contract.STAGE_ID,
        "executor_version": identity["executor_version"],
        "executor_digest": identity["executor_digest"],
        "mode": mode,
        "plan_id": plan["plan_id"],
        "scope": plan["scope"],
        "plan_digest": plan["digest"],
        "execution_head": head,
        "started_at": clock(),
        "finished_at": clock(),
        "cells_planned": len(plan["cells"]),
        "cells_started": 0,
        "cells_not_run": len(plan["cells"]),
        "stop_reason": stop,
        "complete": False,
        "problems": problems,
        "refused": True,
        "retry": "FORBIDDEN",
    }
    if admission is not None:
        report["admission"] = dict(admission)
    if settlement is not None:
        report["cumulative_settlement"] = dict(settlement)
    return report


def _write_analysis(
    cell_root: Path,
    plan: Mapping[str, Any],
    cell: Mapping[str, Any],
    outcome: Mapping[str, Any],
    run: CellRun,
) -> None:
    cell_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "cell_id": cell["cell_id"],
        "plan_id": plan["plan_id"],
        "scope": plan["scope"],
        "arm_id": cell["arm_id"],
        "task_id": cell["task_id"],
        "repetition": cell.get("repetition"),
        "is_benchmark_sample": plan["is_benchmark_sample"],
        "outcome": dict(outcome),
        "oracle": run.oracle,
        "fixture": run.fixture,
        "export": run.export,
        "sandbox_plan": run.sandbox_plan,
        "oracle_sandbox": run.oracle_sandbox,
        "duration_s": round(run.duration_s, 3),
        "notes": list(run.notes),
    }
    (cell_root / "analysis.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# -- fixture, export and oracle ----------------------------------------------------------------


def prepare_fixture(task: Any, destination: Path) -> dict[str, Any]:
    """A per-cell clean repository built from the frozen seed, and its identity.

    The seed is written through the shared fixture materialiser and committed with the shared
    stager, so the "clean git repository" requirement the provisioner enforces is met by
    construction rather than by a second implementation. The digest is then recomputed from what is
    on disk and compared with the frozen one: a fixture that does not match its seed is a stop.
    """

    benchmark = closure2_catalog.build_task(task, topology="single")
    remove_tree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    materialize_fixture(benchmark, destination)
    commit = stage_git_repo(destination, label=f"stage-b-plus {task.task_id}")
    written = {
        path.relative_to(destination).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(destination.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(destination).parts
    }
    seed_digest = canonical_digest(dict(sorted(written.items())))
    # The visible fixture is the seed *plus* the declared public suite. A task that keeps its public
    # tests outside ``seed`` still hands them to the agent, so the frozen value has to include them;
    # for a task whose seed already carries them the union is the seed and nothing moves.
    visible = {**dict(task.seed), **dict(getattr(task, "public_tests", {}) or {})}
    frozen = canonical_digest(dict(sorted(visible.items())))
    return {
        "path": str(destination),
        "commit": commit,
        "seed_digest": seed_digest,
        "frozen_seed_digest": frozen,
        "matches_frozen_seed": seed_digest == frozen,
        "files": len(written),
        "files_list": sorted(written),
    }


def judge_export(
    workspace: Path,
    task: Any,
    *,
    provider: Any,
    run_id: str,
    image: str | None,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Score an exported workspace with the hidden suite, inside its own throwaway sandbox.

    Reused: Closure-2's ``run_hidden_tests_in_sandbox`` uploads the export and the hidden files,
    runs ``pytest`` there, and destroys the sandbox. Nothing in this module executes agent code, and
    the hidden tests never exist in the agent's workspace or in any Host-visible scoring directory.
    """

    benchmark = closure2_catalog.build_task(task, topology="single")
    before = workspace_hashes(workspace)
    hidden_paths = set(benchmark.hidden_files)
    leaked = sorted(hidden_paths & set(before))
    if leaked:
        return {
            "status": "ERROR",
            "kind": "HIDDEN_FILES_IN_WORKSPACE",
            "environment_failure": True,
            "cleanup_failure": None,
            "execution_mode": "sandbox",
            "sandbox_id": None,
            "leaked_paths": leaked,
        }
    config = OracleSandboxConfig(image=image, timeout_seconds=timeout_seconds)
    outcome = asyncio.run(
        run_hidden_tests_in_sandbox(
            provider, task=benchmark, workspace=workspace, run_id=run_id, config=config
        )
    )
    status = "PASS" if outcome.passed else "FAIL"
    if outcome.environment_failure:
        status = "ERROR"
    return {
        "status": status,
        "kind": outcome.kind,
        "passed": outcome.passed,
        "environment_failure": outcome.environment_failure,
        "cleanup_failure": outcome.cleanup_failure,
        "execution_mode": "sandbox",
        "sandbox_id": outcome.sandbox_id,
        "returncode": outcome.returncode,
        "timings_ms": outcome.timings_ms,
        "image": image,
        "workspace_files": len(before),
        "hidden_files_in_workspace": leaked,
        "stderr_excerpt": (outcome.stderr or "")[:400],
        "source": "experiments.evaluation.sandboxed_oracle::run_hidden_tests_in_sandbox",
    }


# -- runners ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunnerConfig:
    """Everything a cell runner is allowed to differ on. Production values are the defaults."""

    llm_provider: str = str(contract.BUDGET["provider"])
    llm_factory: Callable[[], Any] | None = None
    sandbox_provider_name: str = str(contract.BUDGET["sandbox_provider"])
    sandbox_factory: Callable[[Path], Any] | None = None
    require_real_sandbox: bool = True
    oracle_timeout_seconds: int = 300
    main_max_turns: int = int(contract.BUDGET["main_max_turns"])
    plan: SandboxPlan | None = None

    def sandbox_plan(self) -> SandboxPlan:
        return self.plan or sandbox_plan(
            require_real=self.require_real_sandbox,
            provider_name=self.sandbox_provider_name,
        )


def _sandbox_provider(config: RunnerConfig, root: Path) -> Any:
    if config.sandbox_factory is not None:
        root.mkdir(parents=True, exist_ok=True)
        return config.sandbox_factory(root)
    from atlasos.sandbox import build_sandbox_provider
    from atlasos.sandbox.opensandbox_provider import OpenSandboxOptions

    plan = config.sandbox_plan()
    return build_sandbox_provider(
        config.sandbox_provider_name,
        opensandbox_options=OpenSandboxOptions(
            cpu=plan.cpu,
            memory=plan.memory,
            timeout_minutes=plan.timeout_minutes,
        ),
    )


def task_for_cell(cell: Mapping[str, Any]) -> Any:
    """A cell's task, from whichever catalog declares it.

    The Stage B+ / C-series plans use the three frozen Closure-2 tasks; a Stage F plan uses the
    eight-task Stage F catalog. Both are the same frozen ``Task`` dataclass, so nothing downstream
    needs to know which one answered.
    """

    task_id = str(cell["task_id"])
    if task_id in contract.TASKS_BY_ID:
        return contract.TASKS_BY_ID[task_id]
    from experiments.stage_f.tasks import TASKS_BY_ID as stage_f_tasks

    if task_id in stage_f_tasks:
        return stage_f_tasks[task_id]
    raise KeyError(f"unknown task {task_id!r} in any catalog")


def _delegation_for_cell(cell: Mapping[str, Any]) -> Any:
    """The cell's own delegation switch and limits, for an arm that declares them."""

    from atlasos.delegation.config import DelegationConfig

    limits = (cell.get("delegation") or {}).get("limits") or {}
    return DelegationConfig(
        enabled=bool((cell.get("delegation") or {}).get("enabled", True)),
        max_parallel_children=int(limits.get("max_parallel_children", 2)),
        max_children_per_call=int(limits.get("max_children_per_call", 2)),
        max_delegation_rounds=int(limits.get("max_delegation_rounds", 3)),
        child_timeout_seconds=180.0,
        child_max_turns=int(limits.get("child_max_turns", 20)),
        result_preview_chars=int(limits.get("result_preview_chars", 900)),
    )


#: Cells that execute through the goal runtime and therefore carry ``GoalRunOptions``.
MAIN_DRIVEN = "main_driven"
MAIN_DRIVEN_MODES: tuple[str, ...] = (MAIN_DRIVEN, "atlas_direct", "atlas_dynamic")


def cell_runner_config(cell: Mapping[str, Any], base: RunnerConfig) -> RunnerConfig:
    """The runner configuration a cell actually executes with.

    A cell that declares its own turn cap wins over the process default. Everything else (provider,
    sandbox plan, oracle timeout) is inherited, so no other behaviour moves.
    """

    declared = cell.get("max_turns")
    if declared is None or int(declared) == base.main_max_turns:
        return base
    return dataclasses.replace(base, main_max_turns=int(declared))


def cell_runner_for(cell: Mapping[str, Any], *, fallback: CellRunner | None = None) -> CellRunner:
    """The runner for a cell's execution mode.

    ``None`` and every main-driven mode use the goal-runtime runner, which is what every earlier
    stage runs. Only the two non-goal topologies need their own entry, and both live in
    ``experiments.stage_f.runners`` so this module keeps no Stage F logic.
    """

    mode = str(cell.get("execution_mode") or MAIN_DRIVEN)
    if mode in MAIN_DRIVEN_MODES:
        if fallback is not None:
            return fallback
        return live_cell_runner()
    from experiments.stage_f import runners

    if mode == "native":
        return runners.native_cell_runner()
    if mode == "atlas_static_dag":
        return runners.static_dag_cell_runner()
    raise ValueError(f"unknown execution mode {mode!r}")


def production_goal_options(
    cell: Mapping[str, Any],
    *,
    evidence_root: Path,
    fixture_dir: Path,
    config: RunnerConfig,
    run_id: str,
) -> Any:
    """The one place a production ``GoalRunOptions`` is built.

    The live runner and the pre-claim admission proof both call this, so the configuration that is
    admitted and the configuration that runs cannot drift apart. Nothing here executes anything.
    """

    from atlasos.app.goal_sandbox import GoalSandboxOptions
    from atlasos.app.main_driven import GoalRunOptions

    task = task_for_cell(cell)
    arm = contract.ARMS_BY_ID.get(str(cell.get("arm_id")))
    # A Stage F arm is an execution architecture, not a worker-capability arm: it declares its own
    # profile and exposure on the cell and is not in the frozen B+ arm registry.
    profile_id = arm.profile_id if arm else str(cell.get("worker_profile") or contract.CODING)
    exposure = arm.exposure if arm else str(cell.get("worker_exposure") or "card")
    plan = config.sandbox_plan()
    return GoalRunOptions(
        goal=contract.goal_for(cell),
        runs_root=evidence_root / "agent",
        workspace_root=evidence_root / "host-workspace",
        config=atlas_config(plan),
        # A cell that declares its own delegation block (a Stage F execution arm) uses it; a B+ or
        # C-series cell carries none and keeps the frozen configuration.
        delegation=_delegation_for_cell(cell)
        if "enabled" in (cell.get("delegation") or {})
        else contract.delegation_config(),
        provider=config.llm_provider,
        model=contract.BUDGET["model"],
        max_turns=config.main_max_turns,
        # The cell's effective budget, not the frozen default: a plan whose one intervention is a
        # resource parameter binds it into the cell, and this is the single place it becomes a
        # ``GoalRunOptions.token_budget`` — and therefore ``RunBudgetState(limit_tokens=…)``.
        token_budget=contract.token_budget_for(cell),
        main_write_paths=tuple(task.allowed_paths),
        child_write_grants=contract.child_grants_for(cell),
        worker_profiles=tuple(cell.get("worker_profiles") or (profile_id,)),
        worker_max_turns=int(cell.get("worker_max_turns") or contract.BUDGET["worker_max_turns"]),
        worker_exposure=exposure,
        provider_factory=config.llm_factory,
        run_id=run_id,
        main_prompt=contract.main_prompt_for(cell),
        public_tests=contract.public_test_spec(task),
        acceptance=contract.acceptance_spec(task),
        sandbox=GoalSandboxOptions(
            provider=plan.provider_name,
            source_repo=fixture_dir,
            image=plan.image,
            require_real=plan.require_real,
            provider_factory=config.sandbox_factory,
        ),
    )


def _dotenv_like_production() -> None:
    """Load the same ``.env`` the goal path loads, at the same point, and nothing else.

    ``run_goal`` calls ``load_dotenv(REPO_ROOT / ".env", override=False)`` immediately before it
    checks for the credential. Admission has to see the same environment it would decide on, or the
    pre-claim report describes a different configuration from the one that runs.
    """

    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a declared dependency
        return
    load_dotenv(contract.REPO_ROOT / ".env", override=False)


def run_admission_preflight(
    plan: Mapping[str, Any],
    *,
    repo_root: Path | None = None,
    scratch_root: Path | None = None,
    config: RunnerConfig | None = None,
) -> dict[str, Any]:
    """Admit every cell's production configuration before the one-shot claim is consumed.

    This is the same ``run_live_preflight`` that ``run_goal`` applies, run on the same
    ``GoalRunOptions`` the live runner builds. The fixture is materialised in a throwaway
    directory so the ``source_repo_clean`` and hidden-path checks read real bytes. Nothing is
    executed: no provider is built, no model is called, no sandbox is created, no oracle runs, and
    no claim is written.

    ``contract_blocking_failures`` excludes ``api_key_present``: a missing credential is an
    environment fact about the machine, not a defect in the execution contract, and the two are
    reported separately rather than one being hidden inside the other.
    """

    from atlasos.app.live_preflight import BLOCKED, READY, run_live_preflight

    active = config or RunnerConfig()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="bplus-admission-"))
    )
    _dotenv_like_production()
    cells: list[dict[str, Any]] = []
    failures: list[str] = []
    try:
        for cell in plan["cells"]:
            key = f"c{int(cell['order']):02d}"
            evidence = root / key
            fixture_dir = root / "fixtures" / cell["cell_id"]
            try:
                fixture = prepare_fixture(task_for_cell(cell), fixture_dir)
            except Exception as error:  # noqa: BLE001 - an unusable fixture is a refusal
                detail = f"{type(error).__name__}: {error}"[:200]
                cells.append(
                    {
                        "cell_id": cell["cell_id"],
                        "arm_id": cell["arm_id"],
                        "status": BLOCKED,
                        "blocking_failures": ["fixture_preparation"],
                        "checks": 0,
                        "detail": detail,
                    }
                )
                failures.append(f"{cell['cell_id']}: fixture_preparation ({detail})")
                continue
            mode = str(cell.get("execution_mode") or MAIN_DRIVEN)
            if mode in MAIN_DRIVEN_MODES:
                options = production_goal_options(
                    cell,
                    evidence_root=evidence,
                    fixture_dir=fixture_dir,
                    config=cell_runner_config(cell, active),
                    run_id=f"admission-{key}",
                )
                report = run_live_preflight(options)
                detail = {
                    "provider_factory_injected": options.provider_factory is not None,
                    "worker_profile": (options.worker_profiles or (None,))[0],
                    "worker_exposure": options.worker_exposure,
                    "oracle_mode": getattr(options.acceptance, "oracle_mode", None),
                    "main_max_turns": options.max_turns,
                    "worker_max_turns": options.worker_max_turns,
                    "token_budget": options.token_budget,
                }
            else:
                # A native or static-DAG cell has no GoalRunOptions. Admitting it through one would
                # be admitting a configuration it never runs; it is admitted against its own
                # contract instead, and the record says which checks were applied.
                from experiments.stage_f import runners as stage_f_runners

                report, detail = stage_f_runners.arm_admission(cell, fixture=fixture, config=active)
            blocked = list(report["blocking_failures"])
            cells.append(
                {
                    "cell_id": cell["cell_id"],
                    "arm_id": cell["arm_id"],
                    "execution_mode": mode,
                    "status": report["status"],
                    "blocking_failures": blocked,
                    "checks": len(report["checks"]),
                    "fixture_matches_frozen_seed": bool(fixture.get("matches_frozen_seed")),
                    **detail,
                }
            )
            failures.extend(f"{cell['cell_id']}: {item}" for item in blocked)
    finally:
        if scratch_root is None:
            remove_tree(root)

    contract_failures = [item for item in failures if not item.endswith(": api_key_present")]
    credential_present = bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
    return {
        "status": READY if not failures else BLOCKED,
        "generated_at": now(),
        "execution_head": git_head(repo_root),
        "cells": cells,
        "blocking_failures": failures,
        "contract_blocking_failures": contract_failures,
        "credential_present": credential_present,
        "environment_note": (
            "a missing DEEPSEEK_API_KEY blocks a live run but is reported apart from the contract "
            "checks; it is a fact about this machine, not about the experiment"
        ),
    }


def admission_gate(
    plan: Mapping[str, Any],
    *,
    repo_root: Path | None = None,
    config: RunnerConfig | None = None,
) -> dict[str, Any] | None:
    """Return the admission report when dispatch must be refused, else ``None``.

    Called before the claim exists, so a refused configuration costs no attempt.
    """

    report = run_admission_preflight(plan, repo_root=repo_root, config=config)
    return None if report["status"] == "READY" else report


def _exception_kind(error: BaseException, run_dir: Path | None) -> str:
    """Classify a raised exception from the run's own evidence first, and its type second.

    ``run_goal`` writes ``preflight.json`` before it refuses, so an admission refusal is read from
    the report it produced rather than guessed from a message. v1 classified that refusal as a
    provider failure because every unrecognised exception was assumed to come from the provider.
    """

    if run_dir is not None:
        report = _read_json(Path(run_dir) / "preflight.json")
        if isinstance(report, dict) and report.get("status") == "BLOCKED":
            return "preflight"
    name = type(error).__name__.lower()
    if "sandbox" in name:
        return "sandbox"
    if "provider" in name or "transport" in name:
        return "provider"
    return "exception"


def live_cell_runner(config: RunnerConfig | None = None) -> CellRunner:
    """The production cell: real model, real sandbox, per-cell fixture, sandboxed oracle.

    ``RunnerConfig`` is the only injection point. With the defaults nothing is injected, and the
    closure carries ``is_live = True`` so ``dispatch`` can refuse to treat an injected runner as a
    production one.
    """

    active = config or RunnerConfig()

    async def run(cell: dict[str, Any], context: Mapping[str, Any]) -> CellRun:
        from atlasos.app.main_driven import run_goal

        task = task_for_cell(cell)
        # A short, stable directory key: the cell id is 40+ characters and the sandbox nests a git
        # repository several levels deep, which a Windows Host should not have to reach.
        key = f"c{int(cell['order']):02d}"
        evidence = Path(context["evidence_root"]) / "cells" / key
        fixture_dir = Path(context["runs_root"]) / "fixtures" / cell["cell_id"]
        run_id = f"bplus-{key}"
        # The cell's own resource contract wins over the process default: a Stage F cell declares
        # 40 Main turns, and the historical default of 20 must not silently override it.
        cell_config = cell_runner_config(cell, active)
        plan = cell_config.sandbox_plan()
        started = time.monotonic()

        try:
            fixture = await asyncio.to_thread(prepare_fixture, task, fixture_dir)
        except Exception as error:  # noqa: BLE001 - a fixture defect is a stop
            return CellRun(
                session=None,
                run_dir=None,
                fixture_root=fixture_dir,
                oracle=None,
                duration_s=time.monotonic() - started,
                error=f"{type(error).__name__}: {error}"[:500],
                error_kind="fixture",
                sandbox_plan=plan.as_dict(),
                expected_run_id=run_id,
            )

        options = production_goal_options(
            cell,
            evidence_root=evidence,
            fixture_dir=fixture_dir,
            config=cell_config,
            run_id=run_id,
        )

        goal_started = time.monotonic()
        try:
            session = await run_goal(options)
        except BaseException as error:  # noqa: BLE001 - classified by the gate
            return CellRun(
                session=None,
                run_dir=None,
                fixture_root=fixture_dir,
                oracle=None,
                duration_s=time.monotonic() - started,
                error=f"{type(error).__name__}: {error}"[:500],
                error_kind=_exception_kind(error, options.runs_root / run_id),
                fixture=fixture,
                sandbox_plan=plan.as_dict(),
                expected_run_id=run_id,
            )

        agent_ms = round((time.monotonic() - goal_started) * 1000.0, 3)
        artifacts = session.get("artifacts")
        export = dict(artifacts) if isinstance(artifacts, Mapping) else None
        if export and export.get("workspace_path"):
            # Recorded here — after the export, before the oracle — so the audit has a Host value it
            # can recompute. The producer's archive digest stays untouched beside it.
            exported = Path(str(export["workspace_path"]))
            if await asyncio.to_thread(exported.is_dir):
                export.update(await asyncio.to_thread(workspace_identity, exported))
                export["archive_digest_verifiable"] = False
                export["archive_digest_source"] = (
                    "producer (GoalSandbox export.archive_digest, sha256 of the transferred "
                    "archive); the archive bytes are not retained, so this value is recorded, "
                    "not re-derived"
                )
        workspace_value = (export or {}).get("workspace_path")
        oracle: dict[str, Any] | None = None
        oracle_sandbox: dict[str, Any] | None = None
        oracle_ms: float | None = None
        if not workspace_value:
            oracle = {
                "status": "ERROR",
                "kind": "NO_EXPORT",
                "environment_failure": True,
                "cleanup_failure": None,
                "execution_mode": "sandbox",
                "sandbox_id": None,
            }
        else:
            # A second, independent sandbox: the agent's sandbox is already destroyed by now, so the
            # oracle can never inherit its state.
            oracle_root = evidence / "oracle-sandbox"
            agent_after = await asyncio.to_thread(workspace_digest, fixture_dir)
            oracle_started = time.monotonic()
            oracle = await asyncio.to_thread(
                judge_export,
                Path(str(workspace_value)),
                task,
                provider=_sandbox_provider(active, oracle_root),
                run_id=f"{run_id}-oracle",
                image=plan.image,
                timeout_seconds=active.oracle_timeout_seconds,
            )
            oracle_ms = round((time.monotonic() - oracle_started) * 1000.0, 3)
            oracle_sandbox = {
                "provider": plan.provider_name,
                "root": str(oracle_root),
                "sandbox_id": oracle.get("sandbox_id"),
                "cleanup_failure": oracle.get("cleanup_failure"),
            }
            if agent_after != fixture.get("seed_digest"):
                oracle["fixture_after_run"] = agent_after

        duration_s = time.monotonic() - started
        return CellRun(
            session=session,
            run_dir=Path(session["run_dir"]),
            fixture_root=fixture_dir,
            oracle=oracle,
            duration_s=duration_s,
            fixture=fixture,
            export=export,
            sandbox_plan=plan.as_dict(),
            oracle_sandbox=oracle_sandbox,
            expected_run_id=run_id,
            timings={
                # ``run_goal`` owns the sandbox lifecycle and the export, so it is measured as one
                # stage; the split is unavailable rather than zero.
                "agent_execution_ms": agent_ms,
                "oracle_ms": oracle_ms,
                "end_to_end_ms": round(duration_s * 1000.0, 3),
            },
            notes=(
                "host-side scoring is not used: the oracle ran in its own sandbox",
                f"provider={plan.provider_name}",
            ),
        )

    run.is_live = active.require_real_sandbox and active.sandbox_factory is None  # type: ignore[attr-defined]
    run.runner_config = active  # type: ignore[attr-defined]
    return run


__all__ = [
    "CellRun",
    "CellRunner",
    "DispatchContext",
    "EXECUTOR_FILES",
    "EXECUTOR_VERSION",
    "MODE_LIVE",
    "MODE_OFFLINE_PROOF",
    "MODES",
    "NORMAL_FAILURE_KINDS",
    "RunnerConfig",
    "SandboxPlan",
    "admission_gate",
    "atlas_config",
    "audit_cell",
    "cell_cost",
    "claim_path",
    "claim_root",
    "claim_root_override",
    "dispatch",
    "executor_identity",
    "git_dirty",
    "git_head",
    "judge_export",
    "live_cell_runner",
    "now",
    "prepare_fixture",
    "production_goal_options",
    "remove_tree",
    "run_admission_preflight",
    "sandbox_plan",
    "validate_usage",
]
