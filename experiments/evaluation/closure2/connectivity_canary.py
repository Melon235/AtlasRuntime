"""Closure-2B-1: the isolated connectivity canary that must pass before the live pilot.

What this module is:

* a **separate execution scope** — its own id, its own plan digest, its own approval record, its own
  SQLite claim file, its own runs directory and its own evidence file. It is not a cell of
  ``closure2-pilot-1``, and its result may never be counted as a benchmark sample;
* a **connectivity probe** — one trivial synthetic task run through the *existing* chain
  (``experiments.evaluation.runner.run_sample`` -> DeepSeek adapter -> OpenSandbox -> tools ->
  isolated oracle -> cleanup), so the only thing it can prove is "the chain works";
* a **gate** — nothing is created, requested or written until the canary plan is approved by digest,
  the live intent is explicit, the model and resource contract are the frozen ones, and a durable
  claim has been taken.

What this module is not: an agent loop, a scheduler, a sandbox, an oracle, a second evaluation
runner, or an approval framework. The engine path is the shipped one; the canary only decides
whether it may be called and records what happened.

Three levels, never confused:

``OFFLINE_PRECHECK``
    Environment, configuration, dependency, path and approval inspection. No provider, no sandbox,
    no API key value, no network call. This is the default CLI mode.

``CONNECTIVITY_CANARY``
    The gated, one-attempt, one-task chain check. Requires an approved canary plan digest.

``FORMAL_PILOT``
    ``closure2.pilot_runner``. This module never imports its entry point, never reads its approval
    record and never touches its ledger file.

Missing evidence stays missing: an unobserved sandbox, token split or cost is ``None``, never ``0``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2.pilot_ledger import (
    PilotLedger,
    PilotLedgerError,
)
from experiments.evaluation.closure2.pilot_ledger import (
    default_ledger_path as pilot_default_ledger_path,
)
from experiments.evaluation.closure2.protocol import (
    BUDGET,
    ROUTING_POLICY_VERSION,
    resolve_repo_path,
)
from experiments.evaluation.models import BenchmarkTask, WorkerStep, canonical_digest

REPO_ROOT = Path(__file__).resolve().parents[3]

# ---------------------------------------------------------------------------------------- identity

#: The canary's own identity. Never the frozen benchmark id and never the pilot plan id.
CANARY_ID = "closure2-connectivity-canary-v1"
CANARY_KIND = "connectivity_canary"
CANARY_STAGE = "closure-2b-1"
CANARY_EVIDENCE_SCHEMA_VERSION = "closure-2b-1"
CANARY_RUNNER_REVISION = "closure-2b-1"

#: The ``benchmark_id`` written into every run of this canary. It is a separate namespace on disk,
#: so a canary run can never be mistaken for (or aggregated with) a Closure-2A sample.
CANARY_BENCHMARK_ID = "closure2-connectivity-canary"

#: The formal pilot this stage must not disturb. Pinned as constants so the isolation guard is a
#: pure comparison: no import of the pilot runner, no YAML read, no digest recomputation at run
#: time. A test asserts these still equal the live objects, so drift is caught by the suite.
FORMAL_PILOT_PLAN_ID = "closure2-pilot-1"
FORMAL_PILOT_PLAN_DIGEST = "a99342dd55adab0fa14a0f69797f42462e38e2f70fba238e28cde7167427bcf8"
FORMAL_BENCHMARK_ID = "closure2-final-benchmark-v1"
FORMAL_RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "closure2"
FORMAL_RUNS_ROOT = REPO_ROOT / "experiments" / "runs" / "closure2"
FORMAL_DRY_RUN_ARTIFACT = FORMAL_RESULTS_ROOT / "pilot-dry-run.json"

DEFAULT_CANARY_RUNS_ROOT = REPO_ROOT / "experiments" / "runs" / "closure2-canary"
DEFAULT_CANARY_RESULTS_ROOT = REPO_ROOT / "experiments" / "results" / "closure2-canary"

# ------------------------------------------------------------------------------------- the task

#: A frozen config, reused read-only. It is the A0 arm's configuration, so the canary exercises the
#: same sandbox block, provider and Context Plane wiring the single-worker Atlas arm uses.
CANARY_CONFIG = "experiments/configs/final4-context.yaml"
CANARY_TASK_ID = "canary-add-function"
CANARY_ENGINE = "atlas"
CANARY_TOPOLOGY = "single"
CANARY_ATTEMPTS = 1

#: Explicit model. The gate re-derives it from the frozen config and refuses a mismatch.
CANARY_MODEL = "deepseek-chat"

CANARY_SOURCE = '''"""Small arithmetic helpers used by the canary fixture."""


def add(a: int, b: int) -> int:
    """Return ``a + b``."""
    raise NotImplementedError("add is not implemented")
'''

CANARY_PUBLIC_TESTS = '''"""Public acceptance for the canary fixture."""

from calculator import add


def test_add_positive() -> None:
    assert add(1, 2) == 3


def test_add_mixed_sign() -> None:
    assert add(-2, 5) == 3


def test_add_zeros() -> None:
    assert add(0, 0) == 0
'''

CANARY_HIDDEN_TESTS = '''"""Hidden acceptance; never uploaded to the agent workspace."""

from calculator import add


def test_add_positive() -> None:
    assert add(1, 2) == 3


def test_add_mixed_sign() -> None:
    assert add(-2, 5) == 3


def test_add_zeros() -> None:
    assert add(0, 0) == 0
'''

CANARY_VISIBLE_FILES: dict[str, str] = {
    "calculator.py": CANARY_SOURCE,
    "test_public.py": CANARY_PUBLIC_TESTS,
}
CANARY_HIDDEN_FILES: dict[str, str] = {"test_acceptance.py": CANARY_HIDDEN_TESTS}

CANARY_PROMPT = "\n\n".join(
    [
        "[CONNECTIVITY CANARY — not a benchmark sample]",
        "The project is in the sandbox workspace root (`/workspace/project` in the frozen "
        "configuration). It contains `calculator.py` and `test_public.py`.",
        "`calculator.add` currently raises `NotImplementedError`.",
        "Implement `add` in `calculator.py` so that:",
        "- `add(1, 2) == 3`",
        "- `add(-2, 5) == 3`",
        "- `add(0, 0) == 0`",
        "Rules:",
        "- change only `calculator.py`; `test_public.py` must stay byte-identical",
        "- no network access and no third-party packages",
        "- run `python -m pytest -q test_public.py` in the project root and report its real output",
    ]
)

# ---------------------------------------------------------------------------------------- budget

#: The proposed canary resource contract. Proposed, **not approved**: the numbers are part of the
#: plan digest, so approving the digest approves them, and the approval record is still pending.
CANARY_LIMITS: dict[str, int] = {
    "max_agent_turns": 6,
    "max_model_requests": 6,
    "max_output_tokens_per_request": 1024,
    "max_prompt_token_budget": 40_000,
    "max_tool_calls": 12,
    "timeout_seconds": 300,
    "outer_timeout_seconds": 480,
}

#: Structural upper bounds. A plan above any of these is refused *even if its digest was approved*,
#: because an approval of a drifted digest must not be able to authorise an unbounded probe.
CANARY_HARD_CEILINGS: dict[str, int] = {
    "max_agent_turns": 8,
    "max_model_requests": 8,
    "max_output_tokens_per_request": 2048,
    "max_prompt_token_budget": 60_000,
    "max_tool_calls": 20,
    "timeout_seconds": 600,
    "outer_timeout_seconds": 900,
}

#: What each knob can actually do, and what it cannot. Recorded in the precheck and in the evidence
#: so no budget number is read as a guarantee it does not provide.
RESOURCE_ENFORCEMENT: dict[str, dict[str, str]] = {
    "max_agent_turns": {
        "mechanism": "AnyCode AgentConfig.max_turns (from RunnerSettings.max_turns)",
        "strength": "ENFORCED_PER_AGENT",
        "note": "the agent loop stops at the cap; the cap counts turns, not provider requests",
    },
    "max_model_requests": {
        "mechanism": "none",
        "strength": "DERIVED_NOT_ENFORCED",
        "note": "requests follow turns; no separate request counter exists in either engine",
    },
    "max_output_tokens_per_request": {
        "mechanism": "provider max_tokens per request",
        "strength": "ENFORCED_BY_REQUEST",
        "note": "an upper bound per response, not a cap on the sum of responses",
    },
    "max_prompt_token_budget": {
        "mechanism": "Atlas RunBudgetState admission (budget_mode='guarded')",
        "strength": "GUARDED_BEFORE_SEND",
        "note": "estimate-based reservation; RunBudgetState records any overrun instead of denying "
        "that it happened, so this is not a mathematical cap",
    },
    "max_tool_calls": {
        "mechanism": "post-run comparison of metrics.tool_calls",
        "strength": "POST_HOC_ONLY",
        "note": "the run completes; the verdict is applied afterwards",
    },
    "timeout_seconds": {
        "mechanism": "asyncio.wait_for around the engine run",
        "strength": "WALL_CLOCK",
        "note": "bounds the wait, not the spend: a request already sent is not cancelled at the "
        "provider",
    },
    "outer_timeout_seconds": {
        "mechanism": "asyncio.wait_for around the whole canary execution",
        "strength": "WALL_CLOCK",
        "note": "covers provisioning and the oracle as well as the agent run; same limitation",
    },
    "max_estimated_cost_usd": {
        "mechanism": "none",
        "strength": "ADVISORY_AFTER_THE_FACT",
        "note": "neither engine has a pre-send dollar guard; the ceiling is compared with the "
        "estimate in the evidence",
    },
}

#: Facts this stage cannot observe at all. Listed so they are never inferred from something else.
UNOBSERVABLE: tuple[str, ...] = (
    "billed_amount: no runner reports an invoice; only a provider usage estimate exists",
    "oracle_sandbox_runs: the oracle sandbox is created inside the oracle path and is not counted",
    "provider_retries: the adapter sets max_retries=0 and resilience max_attempts=1, so a retry "
    "outside that configuration would be invisible",
    "cache_hit_tokens when the provider omits the split: stays None, never a 0 % hit rate",
    "sandbox_creation when the executor raises before returning: stays None, never False",
)

PROPOSED_COST_CEILING_USD = 0.05


# -------------------------------------------------------------------------------------- the plan


@dataclass(frozen=True)
class CanaryPlan:
    """The scope one canary approval covers. Immutable, digestible, approval-independent."""

    canary_id: str
    plan_revision: str
    benchmark_id: str
    task_id: str
    engine: str
    topology: str
    model: str
    config: str
    attempts: int
    max_agent_turns: int
    max_model_requests: int
    max_output_tokens_per_request: int
    max_prompt_token_budget: int
    max_tool_calls: int
    timeout_seconds: int
    outer_timeout_seconds: int
    estimated_cost_ceiling_usd: float | None

    def limits(self) -> dict[str, int]:
        return {
            "max_agent_turns": self.max_agent_turns,
            "max_model_requests": self.max_model_requests,
            "max_output_tokens_per_request": self.max_output_tokens_per_request,
            "max_prompt_token_budget": self.max_prompt_token_budget,
            "max_tool_calls": self.max_tool_calls,
            "timeout_seconds": self.timeout_seconds,
            "outer_timeout_seconds": self.outer_timeout_seconds,
        }

    def payload(self) -> dict[str, Any]:
        """Every field that defines the scope. No timestamp, no path, no random id."""

        return {
            "kind": CANARY_KIND,
            # Part of the identity on purpose: an approval can never cover something that claims to
            # be a benchmark sample or an official pilot cell.
            "is_benchmark": False,
            "is_official_pilot": False,
            "counts_toward_formal_results": False,
            "canary_id": self.canary_id,
            "plan_revision": self.plan_revision,
            "benchmark_id": self.benchmark_id,
            "task_id": self.task_id,
            "engine": self.engine,
            "topology": self.topology,
            "model": self.model,
            "config": self.config,
            "attempts": self.attempts,
            "limits": self.limits(),
            "estimated_cost_ceiling_usd": self.estimated_cost_ceiling_usd,
        }

    def digest(self) -> str:
        return canonical_digest(self.payload())


#: The one canary this stage proposes. The ceiling was ``None`` until the owner approved one attempt
#: at USD 0.05; the number is part of the digest, so approving it is what fixed this identity.
CANARY_PLAN = CanaryPlan(
    canary_id=CANARY_ID,
    plan_revision=CANARY_RUNNER_REVISION,
    benchmark_id=CANARY_BENCHMARK_ID,
    task_id=CANARY_TASK_ID,
    engine=CANARY_ENGINE,
    topology=CANARY_TOPOLOGY,
    model=CANARY_MODEL,
    config=CANARY_CONFIG,
    attempts=CANARY_ATTEMPTS,
    max_agent_turns=CANARY_LIMITS["max_agent_turns"],
    max_model_requests=CANARY_LIMITS["max_model_requests"],
    max_output_tokens_per_request=CANARY_LIMITS["max_output_tokens_per_request"],
    max_prompt_token_budget=CANARY_LIMITS["max_prompt_token_budget"],
    max_tool_calls=CANARY_LIMITS["max_tool_calls"],
    timeout_seconds=CANARY_LIMITS["timeout_seconds"],
    outer_timeout_seconds=CANARY_LIMITS["outer_timeout_seconds"],
    estimated_cost_ceiling_usd=0.05,
)

#: Mutable approval record for the canary scope. A caller cannot pass its own: the entry point reads
#: this module-level object, and only a test can substitute it.
CANARY_APPROVAL: dict[str, Any] = {
    "status": "PENDING_OWNER_APPROVAL",
    "approved_plan_digest": None,
    "approved_by": None,
    "approval_commit": None,
    "approved_at": None,
}


class CanaryError(RuntimeError):
    """Base class for every canary refusal. Never downgraded, never retried."""


class CanaryNotAuthorized(CanaryError):
    """The canary scope is not approved, or the requested scope is not the approved one."""

    def __init__(self, message: str, *, reason_codes: list[str] | None = None) -> None:
        super().__init__(message)
        self.reason_codes = list(reason_codes or [])

    @property
    def reason_code(self) -> str:
        return self.reason_codes[0] if self.reason_codes else "CANARY_NOT_AUTHORIZED"


class CanaryIsolationError(CanaryError):
    """A canary path would write into, or claim, formal benchmark state."""

    reason_code = "CANARY_ISOLATION_VIOLATION"


class CanaryEvidenceError(CanaryError):
    """The canary evidence could not be written; nothing is claimed to be saved."""

    reason_code = "CANARY_EVIDENCE_NOT_SAVED"


# ------------------------------------------------------------------------------------ the helpers


def _finite_number(value: Any) -> float | None:
    """A usable finite number, or ``None``. ``bool`` is rejected: ``True`` is not a dollar."""

    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _cost_policy(plan: CanaryPlan) -> dict[str, Any]:
    """What the canary ceiling guarantees — which is nothing hard, and that is recorded."""

    base = {
        "enforcement": "ADVISORY_AFTER_THE_FACT",
        "hard_enforcement": False,
        "guarantee": "COST_CEILING_NOT_HARD_ENFORCED",
    }
    raw = plan.estimated_cost_ceiling_usd
    if raw is None:
        return {
            **base,
            "valid": False,
            "reason": "CANARY_COST_CEILING_NOT_APPROVED",
            "ceiling_usd": None,
        }
    number = _finite_number(raw)
    if number is None:
        return {
            **base,
            "valid": False,
            "reason": "CANARY_COST_CEILING_INVALID",
            "ceiling_usd": None,
            "ceiling_raw": repr(raw),
        }
    if number <= 0:
        return {
            **base,
            "valid": False,
            "reason": "CANARY_COST_CEILING_NOT_POSITIVE",
            "ceiling_usd": number,
        }
    return {
        **base,
        "valid": True,
        "reason": "CANARY_COST_CEILING_APPROVED",
        "ceiling_usd": number,
    }


def canary_config_model() -> str | None:
    """The frozen config's ``provider.model``, or ``None`` when it cannot be read."""

    from atlasos.config import load_config

    try:
        config = load_config(resolve_repo_path(CANARY_CONFIG))
    except Exception:  # noqa: BLE001 - an unreadable config is a reportable state, not a crash
        return None
    model = getattr(config.provider, "model", None)
    return model if isinstance(model, str) and model else None


def _limit_problems(plan: CanaryPlan) -> list[str]:
    """Every structural resource problem, named. Empty means "within the hard ceilings"."""

    problems: list[str] = []
    limits = plan.limits()
    for key, ceiling in CANARY_HARD_CEILINGS.items():
        value = limits.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{key}={value!r} is not an integer")
            continue
        if value <= 0:
            problems.append(f"{key}={value} is not positive")
            continue
        if value > ceiling:
            problems.append(f"{key}={value} exceeds the canary hard ceiling {ceiling}")
    return problems


def canary_authorization_gate(
    *,
    live_intent: bool,
    plan: CanaryPlan | None = None,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide whether this canary may execute. Every check is a refusal, not a warning.

    Order matters only for readability: all problems are collected, so one refusal never hides the
    next one.
    """

    target = plan or CANARY_PLAN
    active = CANARY_APPROVAL if record is None else record
    problems: list[str] = []
    codes: list[str] = []

    def fail(code: str, message: str) -> None:
        codes.append(code)
        problems.append(message)

    if live_intent is not True:
        fail(
            "LIVE_INTENT_NOT_CONFIRMED",
            "explicit live intent is required (CLI: --live --confirm-live); no environment "
            "variable can enable a canary run",
        )
    if target.canary_id != CANARY_ID:
        fail("CANARY_IDENTITY_MISMATCH", f"canary_id {target.canary_id!r} is not {CANARY_ID!r}")
    if target.benchmark_id != CANARY_BENCHMARK_ID:
        fail(
            "CANARY_IDENTITY_MISMATCH",
            f"benchmark_id {target.benchmark_id!r} is not the canary namespace "
            f"{CANARY_BENCHMARK_ID!r}",
        )
    digest = target.digest()
    if target.canary_id == FORMAL_PILOT_PLAN_ID or digest == FORMAL_PILOT_PLAN_DIGEST:
        fail(
            "CANARY_FORMAL_IDENTITY_COLLISION",
            "the canary identity collides with the formal pilot plan",
        )

    if target.task_id != CANARY_TASK_ID:
        fail(
            "CANARY_TASK_MISMATCH",
            f"task_id {target.task_id!r} is not the frozen canary task {CANARY_TASK_ID!r}; the "
            "canary never runs a benchmark task",
        )
    if target.engine != CANARY_ENGINE or target.topology != CANARY_TOPOLOGY:
        fail(
            "CANARY_ENGINE_MISMATCH",
            f"engine/topology {target.engine!r}/{target.topology!r} is not the canary "
            f"{CANARY_ENGINE!r}/{CANARY_TOPOLOGY!r}",
        )
    if isinstance(target.attempts, bool) or not isinstance(target.attempts, int):
        fail("CANARY_ATTEMPTS_EXCEEDED", f"attempts={target.attempts!r} is not an integer")
    elif target.attempts != CANARY_ATTEMPTS:
        fail(
            "CANARY_ATTEMPTS_EXCEEDED",
            f"attempts={target.attempts} exceeds the {CANARY_ATTEMPTS} attempt(s) this canary "
            "authorises",
        )
    budget_problems = _limit_problems(target)
    if budget_problems:
        fail("CANARY_BUDGET_DRIFT", "; ".join(budget_problems))

    frozen_model = canary_config_model()
    if frozen_model is None:
        fail("CANARY_CONFIG_UNREADABLE", f"{CANARY_CONFIG} could not be read")
    if target.model != CANARY_MODEL:
        fail(
            "CANARY_MODEL_MISMATCH",
            f"model {target.model!r} is not the frozen canary model {CANARY_MODEL!r}",
        )
    elif frozen_model is not None and frozen_model != target.model:
        fail(
            "CANARY_MODEL_MISMATCH",
            f"{CANARY_CONFIG} now resolves provider.model={frozen_model!r}, not {target.model!r}",
        )

    approved_digest = active.get("approved_plan_digest")
    if active.get("status") != "APPROVED":
        fail(
            "CANARY_APPROVAL_NOT_APPROVED",
            f"canary approval status is {active.get('status')!r}, not 'APPROVED'",
        )
    if approved_digest != digest:
        fail(
            "CANARY_PLAN_DIGEST_MISMATCH",
            f"canary plan digest {digest!r} is not the approved {approved_digest!r}",
        )
    if not active.get("approved_by"):
        fail("CANARY_APPROVAL_INCOMPLETE", "canary approval has no approver")
    if not active.get("approval_commit"):
        fail("CANARY_APPROVAL_INCOMPLETE", "canary approval has no approval commit")
    if not active.get("approved_at"):
        fail("CANARY_APPROVAL_INCOMPLETE", "canary approval has no approval timestamp")

    policy = _cost_policy(target)
    if not policy["valid"]:
        fail(policy["reason"], f"canary cost policy is not usable: {policy['reason']}")

    return {
        "live_ready": not problems,
        "problems": problems,
        "reason_codes": codes,
        "live_intent": live_intent is True,
        "canary_id": target.canary_id,
        "plan_revision": target.plan_revision,
        "plan_digest": digest,
        "approved_plan_digest": approved_digest,
        "approval_status": active.get("status"),
        "approved_by": active.get("approved_by"),
        "approval_commit": active.get("approval_commit"),
        "approved_at": active.get("approved_at"),
        "model": target.model,
        "expected_model": frozen_model,
        "task_id": target.task_id,
        "resource_limits": target.limits(),
        "hard_ceilings": dict(CANARY_HARD_CEILINGS),
        "cost_policy": policy,
        "formal_pilot_plan_id": FORMAL_PILOT_PLAN_ID,
    }


def require_canary_authorization(
    *, live_intent: bool, plan: CanaryPlan | None = None
) -> dict[str, Any]:
    """Raise unless the canary may execute. Called by the live entry point before anything else."""

    gate = canary_authorization_gate(live_intent=live_intent, plan=plan)
    if not gate["live_ready"]:
        raise CanaryNotAuthorized(
            "CANARY_NOT_AUTHORIZED: " + "; ".join(gate["problems"]),
            reason_codes=gate["reason_codes"],
        )
    return gate


# -------------------------------------------------------------------------------- isolation guard


def _assert_isolated_output(path: Path, label: str) -> None:
    """Refuse any output path inside the formal benchmark trees.

    ``is_relative_to`` compares path components, so ``experiments/results/closure2-canary`` is not
    inside ``experiments/results/closure2`` and is accepted, while every subdirectory of the formal
    root is refused.
    """

    resolved = path.resolve()
    for formal in (FORMAL_RESULTS_ROOT, FORMAL_RUNS_ROOT):
        if resolved.is_relative_to(formal.resolve()):
            raise CanaryIsolationError(
                f"CANARY_ISOLATION_VIOLATION: {label}={resolved} is inside the formal benchmark "
                f"tree {formal}; canary evidence must never be written there"
            )


def _claim_namespace_is_isolated(path: Path) -> None:
    """Refuse a canary ledger that is the pilot ledger: the claim namespaces must not be shared."""

    if path.resolve() == pilot_default_ledger_path().resolve():
        raise CanaryIsolationError(
            "CANARY_ISOLATION_VIOLATION: the canary ledger must not be the pilot ledger "
            f"{pilot_default_ledger_path()}"
        )


def default_canary_ledger_path() -> Path:
    """The canary's own claim file, beside — never equal to — the pilot ledger."""

    base = pilot_default_ledger_path()
    return base.with_name("canary-ledger.sqlite")


def _ledger_factory() -> PilotLedger:
    """The canary ledger. Deliberately not a CLI option, for the same reason the pilot's is not."""

    return PilotLedger(default_canary_ledger_path())


# ------------------------------------------------------------------------------- offline precheck


def _resolve_output_roots(runs_root: Path | None, results_root: Path | None) -> tuple[Path, Path]:
    """Resolve both output roots and refuse either one inside a formal benchmark tree.

    Sync on purpose: path resolution is not I/O worth awaiting, and keeping it out of the async body
    keeps the refusal order obvious.
    """

    runs = Path(runs_root or DEFAULT_CANARY_RUNS_ROOT).resolve()
    results = Path(results_root or DEFAULT_CANARY_RESULTS_ROOT).resolve()
    _assert_isolated_output(runs, "runs_root")
    _assert_isolated_output(results, "results_root")
    _claim_namespace_is_isolated(default_canary_ledger_path())
    return runs, results


def _probe_writable(directory: Path) -> dict[str, Any]:
    """Create the directory and write one probe file. Reports a reason instead of raising."""

    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".canary-precheck-probe"
        probe.write_text("probe\n", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        return {"writable": False, "error": f"{type(error).__name__}: {error}"}
    return {"writable": True, "error": None}


def _api_key_state(api_key_env: str) -> dict[str, Any]:
    """Report *whether* the key is configured, never the key or any part of it."""

    in_environment = bool(os.environ.get(api_key_env))
    dotenv_present = (REPO_ROOT / ".env").is_file()
    if in_environment:
        source = "environment"
    elif dotenv_present:
        source = "dotenv_file_only"
    else:
        source = "missing"
    return {
        "variable": api_key_env,
        "present": in_environment,
        "dotenv_file_present": dotenv_present,
        "source": source,
        "value_printed": False,
    }


def build_precheck(
    *,
    runs_root: Path | None = None,
    results_root: Path | None = None,
) -> dict[str, Any]:
    """Stage 0. Reads configuration and the environment; creates no provider and no sandbox.

    The only side effect is a probe file inside the canary output directory, removed immediately.
    """

    import importlib.util

    runs = Path(runs_root or DEFAULT_CANARY_RUNS_ROOT).resolve()
    results = Path(results_root or DEFAULT_CANARY_RESULTS_ROOT).resolve()
    blocking: list[str] = []
    advisory: list[str] = []

    config_error: str | None = None
    config = None
    try:
        from atlasos.config import load_config

        config = load_config(resolve_repo_path(CANARY_CONFIG))
    except Exception as error:  # noqa: BLE001 - any load failure is a blocking precheck result
        config_error = f"{type(error).__name__}: {error}"
        blocking.append(f"configuration: {CANARY_CONFIG} could not be loaded: {config_error}")

    provider: dict[str, Any] = {"name": None, "model": None, "api_key_variable": "DEEPSEEK_API_KEY"}
    sandbox: dict[str, Any] = {"provider": None, "image": None, "network": None, "workspace": None}
    pricing: dict[str, Any] = {"complete": False, "snapshot_date": None}
    if config is not None:
        provider = {
            "name": config.provider.name,
            "model": config.provider.model,
            "api_key_variable": config.provider.api_key_env,
            "base_url_configured": bool(config.provider.base_url),
        }
        sandbox = {
            "provider": config.sandbox.provider,
            "image": config.sandbox.image,
            "network": config.sandbox.network,
            "workspace": config.sandbox.workspace_root,
            "cpu": config.sandbox.cpu,
            "memory": config.sandbox.memory,
            "endpoint": config.sandbox.endpoint,
            "protocol": config.sandbox.protocol,
        }
        pricing_config = config.pricing
        if pricing_config is None:
            pricing = {
                "complete": False,
                "snapshot_date": None,
                "live_verified": False,
                "pricing_status": "PRICING_NOT_CONFIGURED",
            }
        else:
            snapshot = pricing_config.snapshot_date
            pricing = {
                "complete": pricing_config.is_complete,
                "snapshot_date": None if snapshot is None else str(snapshot),
                "cache_hit_input_per_million": pricing_config.cache_hit_input_per_million,
                "cache_miss_input_per_million": pricing_config.cache_miss_input_per_million,
                "output_per_million": pricing_config.output_per_million,
                "live_verified": False,
                "pricing_status": "PRICING_NOT_LIVE_VERIFIED",
            }
        if config.sandbox.provider == "fake":
            blocking.append("sandbox: provider is 'fake'; the evaluation runner refuses it")
        if config.provider.model != CANARY_MODEL:
            blocking.append(
                f"model: {CANARY_CONFIG} resolves provider.model={config.provider.model!r}, the "
                f"canary plan requires {CANARY_MODEL!r}"
            )
        if not pricing["complete"]:
            advisory.append(
                "pricing: incomplete, so estimated cost will be None and the cost ceiling cannot "
                "be checked against a number"
            )

    key = _api_key_state(str(provider.get("api_key_variable") or "DEEPSEEK_API_KEY"))
    if key["source"] == "missing":
        blocking.append(
            f"credentials: {key['variable']} is not set and no .env file is present; the engine "
            "would refuse before its first request"
        )
    elif key["source"] == "dotenv_file_only":
        advisory.append(
            f"credentials: {key['variable']} is not in the environment; the runner would rely on "
            "the repository .env file"
        )

    dependencies = {
        "opensandbox_module": importlib.util.find_spec("opensandbox") is not None,
        "opensandbox_server_cli": shutil.which("opensandbox-server") is not None,
        "git_executable": shutil.which("git") is not None,
        "oracle_pytest_in_image": None,
    }
    if not dependencies["opensandbox_module"]:
        blocking.append("dependencies: the opensandbox package is not importable")
    if not dependencies["git_executable"]:
        blocking.append("dependencies: git is required to stage the fixture repository")
    if not dependencies["opensandbox_server_cli"]:
        advisory.append(
            "dependencies: the opensandbox-server CLI is not on this host; the sandbox endpoint "
            "may be served from WSL or another machine. Connectivity is proven only by the canary "
            "run, not by this precheck"
        )

    paths: dict[str, Any] = {}
    for label, target in (("runs_root", runs), ("results_root", results)):
        entry = _probe_writable(target)
        try:
            _assert_isolated_output(target, label)
            entry["isolated"] = True
        except CanaryIsolationError as error:
            entry["isolated"] = False
            blocking.append(str(error))
        if not entry["writable"]:
            blocking.append(f"paths: {label}={target} is not writable: {entry['error']}")
        paths[label] = str(target)
        paths[f"{label}_probe"] = entry

    limits = CANARY_PLAN.limits()
    limit_problems = _limit_problems(CANARY_PLAN)
    if limit_problems:
        blocking.extend(f"resources: {problem}" for problem in limit_problems)

    gate = canary_authorization_gate(live_intent=False)

    return {
        "mode": "OFFLINE_PRECHECK",
        "stage": CANARY_STAGE,
        "evidence_schema_version": CANARY_EVIDENCE_SCHEMA_VERSION,
        "canary_id": CANARY_ID,
        "canary_kind": CANARY_KIND,
        "is_benchmark": False,
        "is_official_pilot": False,
        "counts_toward_formal_results": False,
        "benchmark_id": CANARY_BENCHMARK_ID,
        "generated_at": _now(),
        "model_calls": 0,
        "sandbox_runs": 0,
        "network_calls_to_provider": 0,
        "config": {
            "path": CANARY_CONFIG,
            "loaded": config is not None,
            "error": config_error,
            "task_id": CANARY_TASK_ID,
            "engine": CANARY_ENGINE,
            "topology": CANARY_TOPOLOGY,
        },
        "provider": provider,
        "credentials": key,
        "sandbox": sandbox,
        "pricing": pricing,
        "dependencies": dependencies,
        "paths": paths,
        "resource_limits": limits,
        "hard_ceilings": dict(CANARY_HARD_CEILINGS),
        "resource_enforcement": RESOURCE_ENFORCEMENT,
        "unobservable": list(UNOBSERVABLE),
        "proposed_cost_ceiling_usd": PROPOSED_COST_CEILING_USD,
        "plan_digest": CANARY_PLAN.digest(),
        "approval": {
            "status": gate["approval_status"],
            "approved_plan_digest": gate["approved_plan_digest"],
            "live_ready": gate["live_ready"],
            "problems": gate["problems"],
            "reason_codes": gate["reason_codes"],
        },
        "blocking_problems": blocking,
        "advisory_problems": advisory,
        "precheck_ready": not blocking,
        # The precheck never authorises a live run: approval is a separate owner decision.
        "live_authorized": False,
    }


def write_precheck(
    path: Path, *, runs_root: Path | None = None, results_root: Path | None = None
) -> dict:
    """Write the precheck artifact, refusing any path inside the formal benchmark trees.

    ``--out`` is operator-supplied, so it needs the same guard as the output roots: a typo must not
    be able to overwrite the frozen dry-run manifest.
    """

    target = Path(path)
    _assert_isolated_output(target, "--out")
    payload = build_precheck(runs_root=runs_root, results_root=results_root)
    _write_json_atomic(target, payload)
    return payload


# ---------------------------------------------------------------------------------- the canary task


def build_canary_task() -> BenchmarkTask:
    """The synthetic canary sample. A fresh object every call; nothing here is frozen task data."""

    return BenchmarkTask(
        sample_id=CANARY_TASK_ID,
        category="feature",
        mode="coding",
        prompt=CANARY_PROMPT,
        target="calculator.py:add",
        visible_files=dict(CANARY_VISIBLE_FILES),
        hidden_files=dict(CANARY_HIDDEN_FILES),
        workers=(WorkerStep(task_id="main", title=CANARY_TASK_ID, prompt=CANARY_PROMPT),),
        allowed_paths=("calculator.py",),
        forbidden_paths=("test_public.py", "tests/**"),
        required_artifacts=("calculator.py",),
        max_turns=CANARY_LIMITS["max_agent_turns"],
        tags=("canary", "connectivity", CANARY_ID),
        notes=(
            "Connectivity canary. Not a benchmark sample: no P1/P2/N1 data, no hidden oracle "
            "from the frozen catalog, no result may be counted as a benchmark observation."
        ),
    )


def canary_settings(*, runs_root: Path, plan: CanaryPlan | None = None) -> Any:
    """Build the ``RunnerSettings`` for the canary. Same engine, canary numbers.

    Every value that matters is passed explicitly: the ``RunnerSettings`` defaults (14 turns, 120k
    tokens, 40 tool calls, 900 s, 4096 tokens) are not the canary contract.
    """

    from experiments.evaluation.runner import (
        CONTEXT_PLANE_TASK_SCOPED,
        RunnerSettings,
    )

    target = plan or CANARY_PLAN
    return RunnerSettings(
        benchmark_id=target.benchmark_id,
        dataset_version=CANARY_ID,
        scorer_version="canary-oracle-1",
        config_path=resolve_repo_path(target.config),
        runs_root=runs_root,
        timeout_seconds=target.timeout_seconds,
        temperature=0.0,
        max_turns=target.max_agent_turns,
        max_tokens=target.max_output_tokens_per_request,
        token_budget=target.max_prompt_token_budget,
        budget_mode="guarded",
        max_tool_calls=target.max_tool_calls,
        progressive_tools=True,
        model_override=target.model,
        # A canary that leaks its sandbox has failed the cleanup leg; never retain it.
        retain_sandbox_on_failure=False,
        variant="connectivity-canary",
        capture_evidence=True,
        handoff_mode="legacy",
        compaction_mode="off",
        tool_error_mode="corrected",
        tool_result_mode="off",
        worker_contract_mode="legacy",
        ownership_mode="off",
        execution_integrity_mode="off",
        verification_mode="off",
        oracle_mode="sandbox",
        delegation_route_mode="off",
        delegation_policy_version=ROUTING_POLICY_VERSION,
        context_plane_expectation=CONTEXT_PLANE_TASK_SCOPED,
        prompt_layout="legacy",
        request_evidence=True,
    )


#: ``(task, settings, repetition_id, config_path) -> EvalRunResult``
CanaryExecutor = Callable[..., Awaitable[Any]]


async def _default_engine_executor(
    task: BenchmarkTask,
    *,
    settings: Any,
    repetition_id: int,
    config_path: Path,
) -> Any:
    """The shipped Atlas runner. The canary never reimplements the agent loop."""

    from experiments.evaluation.runner import run_sample

    return await run_sample(
        task,
        settings=settings,
        repetition_id=repetition_id,
        config_path=config_path,
    )


# -------------------------------------------------------------------------------------- evidence


def _attr(obj: Any, name: str) -> Any:
    return getattr(obj, name, None) if obj is not None else None


def _oracle_sandbox_status(oracle: Any) -> str:
    """Whether the oracle's own sandbox is confirmed to have existed. ``UNKNOWN`` is a real answer.

    The identifier is only present when the oracle actually created a sandbox; a provisioning
    failure before creation is a confirmed absence, and anything else stays unknown.
    """

    if oracle is None:
        return "UNKNOWN"
    if _attr(oracle, "execution_mode") != "sandbox":
        return "NOT_APPLICABLE"
    details = _attr(oracle, "details") or {}
    hidden = details.get("hidden_tests") if isinstance(details, dict) else None
    sandbox_id = hidden.get("sandbox_id") if isinstance(hidden, dict) else None
    if sandbox_id:
        return "CREATED_CONFIRMED"
    if _attr(oracle, "failure_kind") == "SANDBOX_PROVISIONING_FAILURE":
        return "NOT_CREATED_CONFIRMED"
    return "UNKNOWN"


def _usage_state(prompt: Any, completion: Any) -> bool | None:
    """True when both totals are present, ``None`` when only one is, False when neither is."""

    if prompt is None and completion is None:
        return False
    if prompt is None or completion is None:
        return None
    return True


def normalize_canary_result(result: Any, *, plan: CanaryPlan | None = None) -> dict[str, Any]:
    """Map one ``EvalRunResult`` onto the canary evidence record.

    Rules that this function exists to enforce: a missing number stays ``None``; an unknown cache
    split stays unknown; the canary's own outcome is never labelled a benchmark result.
    """

    target = plan or CANARY_PLAN
    metrics = _attr(result, "metrics")
    oracle = _attr(result, "oracle")
    identity = _attr(result, "identity")
    environment = _attr(result, "environment") or {}
    safety = _attr(result, "safety") or {}
    extra = (_attr(metrics, "extra") or {}) if metrics is not None else {}

    prompt_tokens = _attr(metrics, "prompt_tokens")
    completion_tokens = _attr(metrics, "completion_tokens")
    raw_cost = _attr(metrics, "estimated_cost_usd")
    cost = _finite_number(raw_cost)
    if raw_cost is None:
        cost_state = "UNKNOWN"
    elif cost is None or cost < 0:
        # A NaN, an infinity or a negative is invalid, never coerced to 0.
        cost, cost_state = None, "INVALID"
    else:
        cost_state = "COMPLETE"
    ceiling = _finite_number(target.estimated_cost_ceiling_usd)
    if cost is None or ceiling is None:
        cost_vs_ceiling = "UNKNOWN"
    elif cost > ceiling:
        cost_vs_ceiling = "EXCEEDED"
    else:
        cost_vs_ceiling = "WITHIN_CEILING"

    created = _attr(metrics, "sandbox_created")
    destroyed = _attr(metrics, "sandbox_destroyed")
    run_dir = _attr(result, "run_dir")
    # Same preference as the pilot: a request count outranks a completed-turn count when the probe
    # actually observed the outgoing requests.
    request_evidence = extra.get("request_evidence") if isinstance(extra, dict) else None
    if isinstance(request_evidence, dict) and "requests_observed" in request_evidence:
        model_calls: Any = request_evidence["requests_observed"]
        model_calls_source = "request_evidence.requests_observed"
        model_calls_semantics: str | None = "requests_observed"
    elif _attr(metrics, "model_turns") is not None:
        model_calls = _attr(metrics, "model_turns")
        model_calls_source = "metrics.model_turns"
        model_calls_semantics = "completed_model_turns"
    else:
        model_calls = None
        model_calls_source = "not_exposed_by_runner"
        model_calls_semantics = None
    return {
        "run_id": _attr(identity, "run_id"),
        "run_dir": run_dir,
        "raw_result_path": str(Path(run_dir) / "result.json") if run_dir else None,
        "raw_evidence_preserved": run_dir is not None,
        "model": _attr(identity, "model"),
        "provider": _attr(identity, "provider"),
        "git_commit": _attr(identity, "git_commit"),
        "git_dirty": _attr(identity, "git_dirty"),
        "agent_success": _attr(result, "agent_success"),
        "task_success": _attr(result, "success"),
        "failure_class": _attr(result, "failure_class"),
        "failure_reason": _attr(result, "failure_reason"),
        "oracle_passed": _attr(oracle, "passed"),
        "oracle_environment_failure": _attr(oracle, "environment_failure"),
        "oracle_failure_kind": _attr(oracle, "failure_kind"),
        "oracle_execution_mode": _attr(oracle, "execution_mode"),
        "oracle_cleanup_failure": _attr(oracle, "cleanup_failure"),
        "oracle_sandbox_status": _oracle_sandbox_status(oracle),
        "provider_prompt_tokens": prompt_tokens,
        "provider_completion_tokens": completion_tokens,
        "provider_usage_observed": _usage_state(prompt_tokens, completion_tokens),
        "provider_usage_complete": None,
        "provider_usage_source": (
            "not_exposed_by_runner: usage is aggregated across requests but completeness is not "
            "attested"
        ),
        "cache_hit_tokens": _attr(metrics, "cache_hit_tokens"),
        "cache_miss_tokens": _attr(metrics, "cache_miss_tokens"),
        "cache_split_status": extra.get("cache_split_status"),
        "cache_metrics_complete": extra.get("cache_metrics_complete"),
        "model_calls": model_calls,
        "model_calls_source": model_calls_source,
        "model_calls_semantics": model_calls_semantics,
        "tool_calls": _attr(metrics, "tool_calls"),
        "agent_latency_ms": environment.get("agent_latency_ms"),
        "end_to_end_latency_ms": _attr(metrics, "latency_ms"),
        "estimated_cost_usd": cost,
        "estimated_cost_usd_state": cost_state,
        "cost_ceiling_usd": ceiling,
        "cost_vs_ceiling": cost_vs_ceiling,
        "agent_sandbox_created": created if isinstance(created, bool) else None,
        "agent_sandbox_destroyed": destroyed if isinstance(destroyed, bool) else None,
        "sandbox_count_scope": "agent_sandbox_only",
        "cleanup_error": safety.get("cleanup_error"),
        "sandbox_image": environment.get("sandbox_image"),
        "budget_record": extra.get("budget"),
        "budget_admission_rejected": (
            extra.get("budget", {}).get("admission_rejections")
            if isinstance(extra.get("budget"), dict)
            else None
        ),
        # Explicitly not a benchmark observation. These three fields exist so a reader cannot
        # mistake the canary for a sample that counts.
        "is_benchmark_sample": False,
        "counts_toward_formal_results": False,
        "benchmark_success_claimed": False,
    }


def _canary_passed(record: dict[str, Any]) -> bool:
    """A canary passes only when every leg of the chain is *confirmed*, not merely not-failed.

    That includes cleanup on both sides: an oracle sandbox that could not be destroyed is a leaked
    resource, so an otherwise correct answer must not be reported as a pass.
    """

    return bool(
        record.get("oracle_passed") is True
        and record.get("oracle_environment_failure") is False
        and record.get("oracle_cleanup_failure") is None
        and record.get("task_success") is True
        and record.get("agent_sandbox_created") is True
        and record.get("agent_sandbox_destroyed") is True
    )


def _ledger_integrity(
    attempted_status: str,
    error: PilotLedgerError,
    *,
    executed: bool,
) -> dict[str, Any]:
    return {
        "status": "BLOCKED_LEDGER_POST_EXECUTION" if executed else "BLOCKED_LEDGER_PRE_EXECUTION",
        "phase": "POST_EXECUTION" if executed else "PRE_EXECUTION",
        "canary_id": CANARY_ID,
        "task_id": CANARY_TASK_ID,
        "attempted_status": attempted_status,
        "error_code": getattr(error, "reason_code", type(error).__name__),
        "error_class": type(error).__name__,
        "message": str(error),
        "executed_before_failure": executed,
        "claim_status": "CONSUMED",
        "auto_retry": False,
    }


def _git_commit() -> str | None:
    """Best-effort commit for the ledger's audit column; a failure is not a reason to abort."""

    import subprocess

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write the evidence or raise: a canary that ran must not pretend its record was saved."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        raise CanaryEvidenceError(
            f"CANARY_EVIDENCE_NOT_SAVED: {path}: {type(error).__name__}: {error}"
        ) from error


# ---------------------------------------------------------------------------------- the entry point


async def run_connectivity_canary(
    *,
    runs_root: Path | None = None,
    results_root: Path | None = None,
    live_intent: bool = False,
    executor: CanaryExecutor | None = None,
    outer_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Execute one canary attempt. Order is the contract:

    1. the output paths must be outside the formal benchmark trees;
    2. ``live_intent`` is explicit, then the canary plan digest must be the approved one, the model
       and resource contract must be the frozen ones and the cost ceiling must be usable;
    3. a durable claim is taken in the canary ledger, before any provider request or sandbox;
    4. ``RUNNING`` is persisted, then — and only then — the shipped runner is called;
    5. the terminal state is persisted, the result is normalised and the evidence is written;
    6. nothing is retried, and no claim is ever released.

    A refused claim raises: nothing ran, so there is nothing to record. From step 3 onwards the
    evidence is written even when the execution failed, because the failure is the finding.
    """

    runs, results = _resolve_output_roots(runs_root, results_root)

    plan = CANARY_PLAN
    gate = require_canary_authorization(live_intent=live_intent, plan=plan)

    lease = _ledger_factory().claim_plan(
        plan_digest=plan.digest(),
        plan_id=CANARY_ID,
        cells=[(plan.task_id, 0)],
        runner_revision=CANARY_RUNNER_REVISION,
        git_commit=_git_commit(),
    )

    started = time.perf_counter()
    integrity = "COMPLETE"
    integrity_record: dict[str, Any] | None = None
    status = "CANARY_NOT_EXECUTED"
    record: dict[str, Any] | None = None
    error_record: dict[str, Any] | None = None

    try:
        lease.mark_running(plan.task_id, 0)
    except PilotLedgerError as error:
        integrity = "BLOCKED_LEDGER_PRE_EXECUTION"
        integrity_record = _ledger_integrity("RUNNING", error, executed=False)
        status = "BLOCKED_LEDGER_PRE_EXECUTION"
    else:
        settings = canary_settings(runs_root=runs, plan=plan)
        task = build_canary_task()
        timeout = (
            float(outer_timeout_seconds)
            if outer_timeout_seconds is not None
            else float(plan.outer_timeout_seconds)
        )
        engine = executor or _default_engine_executor
        try:
            result = await asyncio.wait_for(
                engine(
                    task,
                    settings=settings,
                    repetition_id=0,
                    config_path=settings.config_path,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            status = "EXECUTION_TIMEOUT"
            error_record = {
                "error_class": "TimeoutError",
                "message": f"the canary exceeded its {timeout:g}s outer wall clock",
                "sandbox_state": "UNKNOWN",
                "note": "a provider request already sent is not cancelled by this timeout",
                "auto_retry": False,
            }
        except Exception as error:  # noqa: BLE001 - a raised canary is evidence, not a crash
            status = "EXECUTION_ERROR"
            error_record = {
                "error_class": type(error).__name__,
                "message": str(error),
                "sandbox_state": "UNKNOWN",
                "note": "the executor raised before returning, so no lifecycle was observed",
                "auto_retry": False,
            }
        else:
            try:
                record = normalize_canary_result(result, plan=plan)
            except Exception as error:  # noqa: BLE001 - never invent a value from a bad result
                status = "EVIDENCE_NORMALIZATION_FAILED"
                error_record = {
                    "error_class": type(error).__name__,
                    "message": str(error),
                    "sandbox_state": "UNKNOWN",
                    "auto_retry": False,
                }
            else:
                status = "CANARY_PASSED" if _canary_passed(record) else "CANARY_FAILED"

        terminal = "COMPLETED" if record is not None else "FAILED"
        try:
            if record is not None:
                lease.mark_completed(
                    plan.task_id,
                    0,
                    result_path=record.get("raw_result_path"),
                )
            else:
                lease.mark_failed(
                    plan.task_id,
                    0,
                    failure_reason=status,
                )
        except PilotLedgerError as error:
            integrity = "BLOCKED_LEDGER_POST_EXECUTION"
            integrity_record = _ledger_integrity(terminal, error, executed=True)
            if status.startswith("CANARY"):
                status = "BLOCKED_LEDGER_POST_EXECUTION"

    summary = {
        "mode": "CONNECTIVITY_CANARY",
        "stage": CANARY_STAGE,
        "evidence_schema_version": CANARY_EVIDENCE_SCHEMA_VERSION,
        "canary_id": CANARY_ID,
        "canary_kind": CANARY_KIND,
        # The three fields that keep this record out of every benchmark table.
        "is_benchmark": False,
        "is_official_pilot": False,
        "counts_toward_formal_results": False,
        "benchmark_id": plan.benchmark_id,
        "formal_benchmark_id": FORMAL_BENCHMARK_ID,
        "formal_pilot_plan_id": FORMAL_PILOT_PLAN_ID,
        "task_id": plan.task_id,
        "engine": plan.engine,
        "topology": plan.topology,
        "model": plan.model,
        "config": plan.config,
        "attempts": plan.attempts,
        "attempt_index": 0,
        "generated_at": _now(),
        "status": status,
        "passed": status == "CANARY_PASSED" and integrity == "COMPLETE",
        "plan_digest": plan.digest(),
        "approved_plan_digest": gate["approved_plan_digest"],
        "approval_status": gate["approval_status"],
        "live_intent_confirmed": gate["live_intent"],
        "cost_policy": gate["cost_policy"],
        "resource_limits": plan.limits(),
        "hard_ceilings": dict(CANARY_HARD_CEILINGS),
        "resource_enforcement": RESOURCE_ENFORCEMENT,
        "unobservable": list(UNOBSERVABLE),
        "execution_claim": {
            "claimed": True,
            "claim_status": "CONSUMED",
            "plan_id": CANARY_ID,
            "plan_digest": plan.digest(),
            "attempt_id": lease.attempt_id,
            "ledger_path": str(lease.path),
            "ledger_scope": "local_user_state_canary",
            "namespace_isolated_from_pilot": True,
        },
        "execution_integrity_status": integrity,
        "execution_integrity_ok": integrity == "COMPLETE",
        "ledger_integrity": integrity_record,
        "auto_retry": False,
        "retry_count": 0,
        "wall_clock_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "record": record,
        "error": error_record,
        "evidence_path": None,
        "not_benchmark_evidence": (
            "This file is a connectivity canary record. It is not a Closure-2A sample, not a "
            "Closure-2B-0 pilot cell and must never be aggregated with either."
        ),
    }
    evidence_path = results / "canary-summary.json"
    summary["evidence_path"] = str(evidence_path)
    _write_json_atomic(evidence_path, summary)
    return summary


# --------------------------------------------------------------------------------- cost preparation

#: Per-cell token assumptions for the three cost scenarios. Every number is an assumption anchored
#: to the frozen budget table and to the historical E1 measurements — not a measurement of these
#: tasks, and not a bill.
COST_SCENARIO_ASSUMPTIONS: dict[str, dict[str, float]] = {
    "LOW": {
        "single_prompt_tokens": 45_000,
        "single_completion_tokens": 3_500,
        "single_model_requests": 8,
        "multi_prompt_tokens": 110_000,
        "multi_completion_tokens": 6_000,
        "multi_model_requests": 18,
        "atlas_cache_hit_ratio": 0.60,
        "native_cache_hit_ratio": 0.40,
    },
    "EXPECTED": {
        "single_prompt_tokens": 90_000,
        "single_completion_tokens": 6_000,
        "single_model_requests": 20,
        "multi_prompt_tokens": 200_000,
        "multi_completion_tokens": 9_000,
        "multi_model_requests": 30,
        "atlas_cache_hit_ratio": 0.35,
        "native_cache_hit_ratio": 0.25,
    },
    "HIGH": {
        "single_prompt_tokens": 260_000,
        "single_completion_tokens": 12_000,
        "single_model_requests": 36,
        "multi_prompt_tokens": 260_000,
        "multi_completion_tokens": 12_000,
        "multi_model_requests": 36,
        "atlas_cache_hit_ratio": 0.0,
        "native_cache_hit_ratio": 0.0,
    },
}

#: A2-P on N1 is refused by the router and executes as one worker, so it must be costed as a
#: single-worker run. Costing it as multi would overstate the pilot by one cell's worth.
COST_DIRECT_CELLS: frozenset[str] = frozenset({"closure2-state-machine-refactor::A2-P"})


def _pricing_block() -> dict[str, Any]:
    from atlasos.config import load_config

    try:
        pricing = load_config(resolve_repo_path(CANARY_CONFIG)).pricing
    except Exception as error:  # noqa: BLE001 - an unreadable config is reported, not guessed
        return {"available": False, "error": f"{type(error).__name__}: {error}"}
    if pricing is None:
        return {
            "available": False,
            "source": CANARY_CONFIG,
            "error": "the frozen configuration declares no pricing block",
            "pricing_status": "PRICING_NOT_CONFIGURED",
        }
    snapshot = pricing.snapshot_date
    return {
        "available": pricing.is_complete,
        "source": CANARY_CONFIG,
        "provider": pricing.provider,
        "cache_hit_input_per_million": pricing.cache_hit_input_per_million,
        "cache_miss_input_per_million": pricing.cache_miss_input_per_million,
        "output_per_million": pricing.output_per_million,
        "snapshot_date": None if snapshot is None else str(snapshot),
        "live_verified": False,
        "pricing_status": "PRICING_NOT_LIVE_VERIFIED",
        "note": (
            "snapshot taken from the frozen repository configuration, not re-checked against the "
            "provider's current price list in this environment"
        ),
    }


def _cell_tokens(scenario: str, *, topology: str) -> dict[str, int]:
    key = "single" if topology == "single" else "multi"
    assumptions = COST_SCENARIO_ASSUMPTIONS[scenario]
    return {
        "prompt_tokens": int(assumptions[f"{key}_prompt_tokens"]),
        "completion_tokens": int(assumptions[f"{key}_completion_tokens"]),
        "model_requests": int(assumptions[f"{key}_model_requests"]),
    }


def _cell_cost(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_ratio: float,
    pricing: dict[str, Any],
) -> dict[str, Any]:
    hit_tokens = round(prompt_tokens * cache_hit_ratio)
    miss_tokens = prompt_tokens - hit_tokens
    hit_rate = float(pricing["cache_hit_input_per_million"])
    miss_rate = float(pricing["cache_miss_input_per_million"])
    output_rate = float(pricing["output_per_million"])
    cost = (
        hit_tokens * hit_rate / 1_000_000
        + miss_tokens * miss_rate / 1_000_000
        + completion_tokens * output_rate / 1_000_000
    )
    return {
        "cache_hit_tokens": hit_tokens,
        "cache_miss_tokens": miss_tokens,
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "estimated_cost_usd": cost,
    }


def cost_scenarios() -> dict[str, Any]:
    """LOW / EXPECTED / HIGH estimates for the proposed eight-cell pilot, plus the contract bound.

    Pure arithmetic over the frozen budget and the repository pricing snapshot. No model call, no
    sandbox, no network. The numbers are conditional on the stated assumptions and are never a bill.
    """

    from experiments.evaluation.closure2.pilot_runner import ARM_SPECS, PROPOSED_PILOT_CELLS

    pricing = _pricing_block()
    if not pricing.get("available"):
        return {
            "status": "INSUFFICIENT_COST_EVIDENCE",
            "reason": "the frozen pricing block is absent or incomplete",
            "pricing": pricing,
            "scenarios": {},
        }

    cells = [
        {
            "cell_id": f"{task_id}::{arm_id}",
            "task_id": task_id,
            "arm_id": arm_id,
            "engine": ARM_SPECS[arm_id].runner,
            "topology": (
                "single"
                if f"{task_id}::{arm_id}" in COST_DIRECT_CELLS
                else ARM_SPECS[arm_id].topology
            ),
        }
        for task_id, arm_id in PROPOSED_PILOT_CELLS
    ]

    scenarios: dict[str, Any] = {}
    for name, assumptions in COST_SCENARIO_ASSUMPTIONS.items():
        per_cell: list[dict[str, Any]] = []
        for cell in cells:
            tokens = _cell_tokens(name, topology=cell["topology"])
            ratio = float(assumptions[f"{cell['engine']}_cache_hit_ratio"])
            cost = _cell_cost(
                prompt_tokens=tokens["prompt_tokens"],
                completion_tokens=tokens["completion_tokens"],
                cache_hit_ratio=ratio,
                pricing=pricing,
            )
            per_cell.append(
                {
                    **cell,
                    **tokens,
                    "cache_hit_ratio_assumed": ratio,
                    **cost,
                }
            )
        scenarios[name] = {
            "status": "ESTIMATE_CONDITIONAL_ON_ASSUMPTION",
            "input_tokens": sum(cell["input_tokens"] for cell in per_cell),
            "output_tokens": sum(cell["output_tokens"] for cell in per_cell),
            "cache_hit_tokens": sum(cell["cache_hit_tokens"] for cell in per_cell),
            "cache_miss_tokens": sum(cell["cache_miss_tokens"] for cell in per_cell),
            "model_requests": sum(cell["model_requests"] for cell in per_cell),
            "estimated_cost_usd": round(sum(cell["estimated_cost_usd"] for cell in per_cell), 4),
            "assumptions": {
                "single_profile": {
                    "prompt_tokens": int(assumptions["single_prompt_tokens"]),
                    "completion_tokens": int(assumptions["single_completion_tokens"]),
                    "model_requests": int(assumptions["single_model_requests"]),
                },
                "multi_profile": {
                    "prompt_tokens": int(assumptions["multi_prompt_tokens"]),
                    "completion_tokens": int(assumptions["multi_completion_tokens"]),
                    "model_requests": int(assumptions["multi_model_requests"]),
                },
                "atlas_cache_hit_ratio": float(assumptions["atlas_cache_hit_ratio"]),
                "native_cache_hit_ratio": float(assumptions["native_cache_hit_ratio"]),
                "direct_cells_costed_as_single": sorted(COST_DIRECT_CELLS),
                "cache_split": "assumed, not measured; a cell with an unknown split has no cost",
            },
            "cells": per_cell,
        }

    # The contract bound is not a scenario: it is what the frozen numbers allow. Every term is read
    # from the frozen budget table so this cannot drift into a retyped constant.
    atlas_cells = [cell for cell in cells if cell["engine"] == "atlas"]
    native_cells = [cell for cell in cells if cell["engine"] == "native"]
    atlas_total_tokens = int(BUDGET["single"]["token_budget"])
    turn_cap = int(BUDGET["single"]["max_turns"])
    per_request_cap = int(BUDGET["shared"]["max_tokens_per_request"])
    worst_output_rate = float(pricing["output_per_million"])
    worst_input_rate = max(
        float(pricing["cache_miss_input_per_million"]),
        float(pricing["output_per_million"]),
    )
    atlas_cell_bound = round(atlas_total_tokens * worst_input_rate / 1_000_000, 4)
    native_output_bound = round(turn_cap * per_request_cap * worst_output_rate / 1_000_000, 4)
    return {
        "status": "ESTIMATE_CONDITIONAL_ON_ASSUMPTION",
        "pricing": pricing,
        "cells": cells,
        "scenarios": scenarios,
        "upper_bound": {
            "status": "UPPER_BOUND_INCOMPLETE",
            "reason": (
                "the native engine has no in-run token guard: only the turn cap and the "
                "per-response token cap bound it, so prompt tokens are not bounded by the contract"
            ),
            "atlas": {
                "cells": len(atlas_cells),
                "bound_basis": (
                    f"{atlas_total_tokens:,} tokens per run at the higher of the miss/output rates"
                ),
                "per_cell_usd_approximate": atlas_cell_bound,
                "total_usd_approximate": round(atlas_cell_bound * len(atlas_cells), 4),
                "note": (
                    "admission is estimate-based and records any overrun, so this is an "
                    "approximation, not a cap"
                ),
            },
            "native": {
                "cells": len(native_cells),
                "output_tokens_cap": turn_cap * per_request_cap,
                "output_only_usd": native_output_bound,
                "prompt_tokens": "UNBOUNDED_BY_CONTRACT",
                "total_usd": None,
            },
            "total_usd": None,
        },
        "reporting_rules": [
            "a scenario is an assumption set, not a measurement",
            "a cell with an unknown cache split has no cost and is never costed as a cache miss",
            "the estimated cost is not the billed amount and never substitutes for it",
            "no estimate here includes the canary itself",
        ],
    }


# ------------------------------------------------------------------------------------------- cli


def _parse_roots(argv: argparse.Namespace) -> tuple[Path, Path]:
    return Path(argv.runs_root), Path(argv.results_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="closure-2b connectivity canary: offline precheck and gated live probe"
    )
    parser.add_argument("--precheck", action="store_true", help="offline precheck (the default)")
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="print the eight-cell cost scenarios; offline, writes nothing",
    )
    parser.add_argument("--live", action="store_true", help="request the real canary")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="second, explicit confirmation required together with --live",
    )
    parser.add_argument("--runs-root", default=str(DEFAULT_CANARY_RUNS_ROOT))
    parser.add_argument("--results-root", default=str(DEFAULT_CANARY_RESULTS_ROOT))
    parser.add_argument(
        "--out",
        default=None,
        help="precheck output path (default: <results-root>/canary-precheck.json)",
    )
    args = parser.parse_args(argv)
    runs_root, results_root = _parse_roots(args)

    if args.estimate:
        print(json.dumps(cost_scenarios(), indent=2, sort_keys=True))
        return 0

    if not args.live:
        out = Path(args.out) if args.out else results_root / "canary-precheck.json"
        try:
            payload = write_precheck(out, runs_root=runs_root, results_root=results_root)
        except CanaryIsolationError as error:
            print(
                json.dumps(
                    {"status": "REFUSED", "reason": error.reason_code, "detail": str(error)},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 3
        except CanaryEvidenceError as error:
            print(
                json.dumps(
                    {"status": "EVIDENCE_NOT_SAVED", "detail": str(error)},
                    indent=2,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 5
        print(
            json.dumps(
                {
                    "mode": payload["mode"],
                    "canary_id": payload["canary_id"],
                    "precheck_ready": payload["precheck_ready"],
                    "live_authorized": payload["live_authorized"],
                    "approval": payload["approval"]["status"],
                    "blocking_problems": payload["blocking_problems"],
                    "model_calls": payload["model_calls"],
                    "sandbox_runs": payload["sandbox_runs"],
                    "out": str(out),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if payload["precheck_ready"] else 1

    if not args.confirm_live:
        print(
            json.dumps(
                {
                    "status": "REFUSED",
                    "reason": "LIVE_INTENT_NOT_CONFIRMED",
                    "detail": "--live requires --confirm-live; no environment variable enables it",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    try:
        summary = asyncio.run(
            run_connectivity_canary(
                runs_root=runs_root,
                results_root=results_root,
                live_intent=True,
            )
        )
    except (CanaryNotAuthorized, CanaryIsolationError, PilotLedgerError) as error:
        print(
            json.dumps(
                {
                    "status": "REFUSED",
                    "reason": getattr(error, "reason_code", type(error).__name__),
                    "detail": str(error),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 3
    except CanaryEvidenceError as error:
        print(
            json.dumps(
                {"status": "EVIDENCE_NOT_SAVED", "detail": str(error)}, indent=2, sort_keys=True
            ),
            file=sys.stderr,
        )
        return 5

    print(
        json.dumps(
            {
                "mode": summary["mode"],
                "status": summary["status"],
                "passed": summary["passed"],
                "execution_integrity_status": summary["execution_integrity_status"],
                "execution_claim": summary["execution_claim"],
                "counts_toward_formal_results": summary["counts_toward_formal_results"],
                "out": summary["evidence_path"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if summary["passed"] else 4


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CANARY_APPROVAL",
    "CANARY_BENCHMARK_ID",
    "CANARY_HARD_CEILINGS",
    "CANARY_ID",
    "CANARY_LIMITS",
    "CANARY_MODEL",
    "CANARY_PLAN",
    "CANARY_TASK_ID",
    "CanaryEvidenceError",
    "CanaryError",
    "CanaryIsolationError",
    "CanaryNotAuthorized",
    "CanaryPlan",
    "FORMAL_PILOT_PLAN_DIGEST",
    "FORMAL_PILOT_PLAN_ID",
    "PROPOSED_COST_CEILING_USD",
    "RESOURCE_ENFORCEMENT",
    "UNOBSERVABLE",
    "build_canary_task",
    "build_precheck",
    "canary_authorization_gate",
    "canary_settings",
    "cost_scenarios",
    "default_canary_ledger_path",
    "normalize_canary_result",
    "require_canary_authorization",
    "run_connectivity_canary",
    "write_precheck",
]
