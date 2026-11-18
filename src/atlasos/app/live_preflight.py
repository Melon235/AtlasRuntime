"""Stage P1 admission: can this goal run be allowed to call a real model?

It answers that from the configuration alone. Nothing here starts a sandbox, loads a provider or
spends a token, so the report can be produced, reviewed and filed before anyone authorizes a run.
Every check states what it proved, and a blocking failure stops ``run_goal`` from building a live
provider at all.

The list is deliberately short and concrete: the failure modes this project has actually hit
(dirty source tree, fake provider presented as isolation, an oracle the model can see, a budget
nobody noted was advisory) plus the few fields a live run cannot be interpreted without.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

READY = "READY"
BLOCKED = "BLOCKED"

REPO_ROOT = Path(__file__).resolve().parents[3]
FROZEN_RESULTS = (REPO_ROOT / "experiments" / "results", REPO_ROOT / "experiments" / "runs")
FAKE_PROVIDERS = frozenset({"fake", ""})
#: Providers whose isolation is verified by probes in ``GoalSandbox``. ``daytona`` is accepted
#: because it is a remote container service, not a local stand-in.
REAL_PROVIDERS = frozenset({"opensandbox", "daytona"})


@dataclass(frozen=True)
class PreflightCheck:
    check_id: str
    ok: bool
    detail: str
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "ok": self.ok,
            "blocking": self.blocking,
            "detail": self.detail,
        }


def _check(check_id: str, ok: bool, detail: str, *, blocking: bool = True) -> PreflightCheck:
    return PreflightCheck(check_id=check_id, ok=ok, detail=detail, blocking=blocking)


def _git_state(repo: Path) -> tuple[bool, str]:
    """Read-only equivalent of the provisioner's inspect, without creating a bundle."""

    try:
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"could not inspect {repo}: {error}"
    if status.returncode != 0:
        return False, f"{repo} is not a Git repository"
    if status.stdout.strip():
        return False, f"{repo} has uncommitted changes; M5 provisioning refuses a dirty tree"
    if head.returncode != 0 or not head.stdout.strip():
        return False, f"{repo} has no resolvable HEAD commit"
    return True, f"{repo} is clean at {head.stdout.strip()[:12]}"


def _inside(path: Path, parents: tuple[Path, ...]) -> bool:
    for parent in parents:
        try:
            path.relative_to(parent)
        except ValueError:
            continue
        return True
    return False


def run_live_preflight(options: Any) -> dict[str, Any]:
    """Build the P1 admission report for one ``GoalRunOptions``.

    ``options`` is duck-typed to avoid an import cycle with ``main_driven``; the fields read are
    ``config``, ``sandbox``, ``public_tests``, ``acceptance``, ``token_budget``,
    ``main_write_paths``, ``delegation``, ``runs_root``, ``provider``, ``model`` and
    ``provider_factory``.
    """

    checks: list[PreflightCheck] = []
    sandbox = getattr(options, "sandbox", None)
    acceptance = getattr(options, "acceptance", None)
    public_tests = getattr(options, "public_tests", None)
    config = options.config

    checks.append(
        _check(
            "sandbox_configured",
            sandbox is not None,
            "a sandbox is required for a live run; the host path has no isolation"
            if sandbox is None
            else "sandbox configured",
        )
    )

    provider = ""
    if sandbox is not None:
        provider = str(getattr(sandbox, "provider", "") or "")
    checks.append(
        _check(
            "sandbox_provider_real",
            provider in REAL_PROVIDERS,
            f"sandbox.provider={provider!r}; expected one of {sorted(REAL_PROVIDERS)}",
        )
    )
    checks.append(
        _check(
            "sandbox_isolation_required",
            bool(getattr(sandbox, "require_real", False)) if sandbox is not None else False,
            "require_real must stay True: the fake provider runs on the host",
        )
    )
    if sandbox is not None:
        workspace_root = str(getattr(sandbox, "workspace_root", "") or "")
        checks.append(
            _check(
                "sandbox_workspace_root",
                workspace_root.startswith("/"),
                f"sandbox workspace root {workspace_root!r} must be an absolute in-container path",
            )
        )
        source_repo = Path(getattr(sandbox, "source_repo", "")).resolve()
        clean, detail = _git_state(source_repo)
        checks.append(_check("source_repo_clean", clean, detail))
    else:
        checks.append(_check("sandbox_workspace_root", False, "no sandbox configured"))
        checks.append(_check("source_repo_clean", False, "no sandbox configured"))

    write_paths = tuple(getattr(options, "main_write_paths", ()) or ())
    checks.append(
        _check(
            "main_write_scope",
            bool(write_paths),
            f"the Main needs an explicit host-declared write scope; got {list(write_paths)}",
        )
    )
    absolute = [item for item in write_paths if Path(item).is_absolute()]
    checks.append(
        _check(
            "main_write_scope_relative",
            not absolute,
            "write scope must be workspace-relative paths"
            if absolute
            else "all write paths are workspace-relative",
        )
    )

    checks.append(
        _check(
            "public_tests_configured",
            public_tests is not None,
            "no host-fixed public suite: the Main could not verify its own work"
            if public_tests is None
            else f"public suite {getattr(public_tests, 'test_id', '?')!r}",
        )
    )

    oracle_command = str(getattr(acceptance, "dynamic_command", "") or "").strip()
    public_command = str(getattr(public_tests, "command", "") or "").strip() if public_tests else ""
    separate = bool(oracle_command) and bool(public_command) and oracle_command != public_command
    checks.append(
        _check(
            "public_tests_separate_from_oracle",
            separate,
            "public and oracle commands must differ, otherwise the Main observes the oracle"
            if public_command and oracle_command
            else "one of the two commands is not configured",
        )
    )

    checks.append(
        _check(
            "acceptance_configured",
            acceptance is not None and bool(oracle_command),
            "an acceptance contract with a dynamic command is required; "
            "'NOT_CONFIGURED' can never be a pipeline PASS",
        )
    )
    if acceptance is not None:
        spec = getattr(acceptance, "spec", None)
        declared = bool(getattr(spec, "declared_digest", None))
        checks.append(
            _check(
                "acceptance_digest_declared",
                declared,
                "the verification spec must carry a declared digest (it is signed by the host)",
            )
        )
    else:
        checks.append(_check("acceptance_digest_declared", False, "no acceptance contract"))

    token_budget = getattr(options, "token_budget", None)
    checks.append(
        _check(
            "budget_configured",
            bool(token_budget and int(token_budget) > 0),
            f"token_budget={token_budget} (admission is estimate-based; this is not a hard "
            "monetary cap)",
        )
    )

    delegation = getattr(options, "delegation", None)
    enabled = bool(getattr(delegation, "enabled", False))
    checks.append(
        _check(
            "delegation_bounds",
            enabled and int(getattr(delegation, "max_parallel_children", 0)) >= 1,
            "delegation must be enabled with max_parallel_children >= 1",
        )
    )

    checks.append(
        _check(
            "context_metrics",
            True,
            "UsageAttributionHook records per-request main context unconditionally",
            blocking=False,
        )
    )

    provider_name = str(getattr(options, "provider", "") or "")
    checks.append(
        _check(
            "provider_deepseek",
            provider_name == "deepseek",
            f"provider={provider_name!r}; stage P1 is authorized for the DeepSeek adapter only",
        )
    )
    model = str(getattr(options, "model", "") or "")
    configured_model = str(getattr(getattr(config, "provider", None), "model", "") or "")
    checks.append(
        _check(
            "model_fixed",
            bool(model) and model == configured_model,
            f"model={model!r} must equal config.provider.model={configured_model!r} so the arm "
            "identity is the configuration, not a runtime override",
        )
    )
    checks.append(
        _check(
            "api_key_present",
            bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
            "DEEPSEEK_API_KEY is set (the preflight reads it, it never sends it)",
        )
    )
    checks.append(
        _check(
            "live_provider_factory",
            getattr(options, "provider_factory", None) is None,
            "a live run must build the real adapter; an injected factory is a deterministic test",
        )
    )

    runs_root = Path(getattr(options, "runs_root", Path("."))).resolve()
    scratch = not _inside(runs_root, FROZEN_RESULTS)
    checks.append(
        _check(
            "scratch_run_directory",
            scratch,
            f"runs_root={runs_root} must stay outside experiments/results and experiments/runs",
        )
    )

    blocking_failures = [item.check_id for item in checks if item.blocking and not item.ok]
    return {
        "status": READY if not blocking_failures else BLOCKED,
        "generated_at": datetime.now(UTC).isoformat(),
        "checks": [item.as_dict() for item in checks],
        "blocking_failures": blocking_failures,
        "notes": {
            "budget": "admission is a conservative estimate; the run stops admitting when the "
            "budget is exhausted and does not guarantee a zero-overrun bill",
            "metrics": "main_context_tokens_by_request is provider-observed; unobserved fields "
            "are UNAVAILABLE rather than estimated",
            "oracle": "the harness runs the acceptance contract after the Main stops; a public "
            "PASS is never used as the oracle verdict",
        },
    }


def assert_live_ready(options: Any) -> dict[str, Any]:
    """Raise instead of starting a live provider when the preflight is not READY."""

    report = run_live_preflight(options)
    if report["status"] != READY:
        failed = ", ".join(report["blocking_failures"])
        raise RuntimeError(f"live preflight is BLOCKED: {failed}")
    return report


__all__ = [
    "BLOCKED",
    "FAKE_PROVIDERS",
    "PreflightCheck",
    "READY",
    "REAL_PROVIDERS",
    "assert_live_ready",
    "run_live_preflight",
]
