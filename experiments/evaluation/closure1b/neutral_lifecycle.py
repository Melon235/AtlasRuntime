"""Closure-1B: the neutral OpenSandbox lifecycle behind the native AnyCode baseline.

What this is
------------
The same environment Atlas creates, in the same order, with the same pieces:

    materialize_fixture → stage_git_repo → GitProjectProvisioner.inspect
    → OpenSandboxProvider.create → GitProjectProvisioner.provision
    → enforce_evaluation_isolation → (native agent) → export_patch/export_workspace
    → provider.destroy → evaluate_oracle_isolated

What this is not
----------------
No Atlas lifecycle state: no durable binding store, no ownership lock, no trajectory recorder, no
reattachment, no recovery coordinator. Those exist to make *Atlas* runs durable and auditable; a
baseline that inherited them would no longer be a baseline. The native arm gets the environment and
nothing else.

The create spec itself is built by ``trusted_sandbox_spec`` — the same function Atlas uses — so the
image, network policy, workspace root, labels and secret references are byte-identical by
construction rather than by convention.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycode.sandbox import SandboxCommand, SandboxProvider
from experiments.evaluation.stage_g3_4a_integrity import enforce_evaluation_isolation

from atlasos.sandbox.models import (
    SandboxProjectConfig,
    SandboxProvisionError,
    trusted_sandbox_spec,
)
from atlasos.sandbox.provider import require_capabilities
from atlasos.sandbox.provision import GitProjectProvisioner


class NeutralLifecycleError(RuntimeError):
    """A native-arm lifecycle failure, carrying the cleanup evidence that came with it.

    ``stage`` names the step that failed, ``sandbox_id`` records that a container really was created
    even though no usable sandbox was returned, and ``destroy_confirmed`` is the provider's own
    answer — ``None`` when no handle existed, so "nothing to destroy" is never reported as
    "destroyed successfully".
    """

    stage: str = "lifecycle"

    def __init__(
        self,
        message: str,
        *,
        cleanup_failure: str | None = None,
        sandbox_id: str | None = None,
        destroy_confirmed: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.cleanup_failure = cleanup_failure
        self.sandbox_id = sandbox_id
        self.destroy_confirmed = destroy_confirmed


class NeutralCreateError(NeutralLifecycleError):
    stage = "create"


class NeutralProvisionError(NeutralLifecycleError):
    stage = "provision"


class NeutralIsolationError(NeutralLifecycleError):
    stage = "isolation"


@dataclass(frozen=True)
class NeutralSandbox:
    """A container the native arm may use, plus the facts needed to audit and destroy it."""

    provider_name: str
    sandbox_id: str
    handle: Any
    workspace_root: str
    source_commit: str
    source_digest: str
    isolation: dict[str, Any]
    provision_commands: int
    provision_uploads: int
    timings_ms: dict[str, float] = field(default_factory=dict)


async def _destroy(provider: SandboxProvider, handle: Any) -> tuple[bool, str | None]:
    """Destroy and report the provider's own answer. Never assume success."""

    try:
        result = await provider.destroy(handle)
    except BaseException as error:  # noqa: BLE001 - cleanup evidence, not control flow
        return False, f"{type(error).__name__}: {error}"
    if not result.ok:
        return False, str(result.error or "provider did not confirm destroy")
    return True, None


async def open_neutral_sandbox(
    provider: SandboxProvider,
    config: SandboxProjectConfig,
    *,
    run_id: str,
    task_id: str,
    context: Any,
    fixture_repo: str | Path,
) -> NeutralSandbox:
    """Create, provision and verify one sandbox for the whole native run.

    One container per run, not per worker: every worker of a multi-worker arm shares the run's
    project workspace, exactly as the Atlas arm does.
    """

    config.validate_v1()
    capabilities = provider.capabilities()
    if capabilities.provider != config.provider_name:
        raise NeutralCreateError(
            f"provider identity mismatch: config selects {config.provider_name!r}, "
            f"provider reports {capabilities.provider!r}"
        )
    require_capabilities(capabilities)

    provisioner = GitProjectProvisioner(
        max_bundle_bytes=config.max_bundle_bytes,
        workspace_root=config.workspace_root,
    )
    source = provisioner.inspect(fixture_repo)
    handle: Any = None
    # The Host bundle is a staging artifact. It is removed on every exit path, including a create
    # that raises before any handle exists.
    try:
        spec = trusted_sandbox_spec(
            run_id=run_id,
            task_id=task_id,
            worker_id=None,
            context=context,
            config=config,
        )
        try:
            created = await provider.create(spec)
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            # No handle exists, so nothing is destroyed and nothing is claimed; the provider's own
            # failure is preserved as the cause.
            raise NeutralCreateError(
                f"sandbox create raised {type(error).__name__}: {error}",
                destroy_confirmed=None,
            ) from error
        handle = created.handle
        if handle is None:
            # Nothing was created, so there is nothing to destroy and nothing to claim.
            raise NeutralCreateError(
                f"sandbox create failed: {created.error}",
                destroy_confirmed=None,
            )
        if not created.ok:
            # A failed result that still carries a handle: the container may exist. Destroy it and
            # report the provider's answer rather than assuming either way.
            confirmed, cleanup_error = await _destroy(provider, handle)
            raise NeutralCreateError(
                f"sandbox create reported failure but returned handle {handle.id}: {created.error}",
                cleanup_failure=cleanup_error,
                sandbox_id=str(handle.id),
                destroy_confirmed=confirmed,
            )
        report, isolation, timings = await _provision_and_verify(
            provider, provisioner, handle, source
        )
    except BaseException as error:
        already_handled = (
            isinstance(error, NeutralLifecycleError) and error.destroy_confirmed is not None
        )
        if handle is None or already_handled:
            raise
        # Provisioning or isolation failed after a container was created: destroy it, and keep both
        # the primary failure and the cleanup answer.
        confirmed, cleanup_error = await _destroy(provider, handle)
        message = f"{type(error).__name__}: {error}"
        if not confirmed:
            message = f"{message}; sandbox cleanup also failed: {cleanup_error}"
        error_type = (
            type(error) if isinstance(error, NeutralLifecycleError) else NeutralLifecycleError
        )
        raise error_type(
            message,
            cleanup_failure=cleanup_error,
            sandbox_id=str(handle.id),
            destroy_confirmed=confirmed,
        ) from error
    finally:
        source.bundle_path.unlink(missing_ok=True)

    return NeutralSandbox(
        provider_name=capabilities.provider,
        sandbox_id=handle.id,
        handle=handle,
        workspace_root=config.workspace_root,
        source_commit=source.source_commit,
        source_digest=source.bundle_digest,
        isolation=isolation.as_dict(),
        provision_commands=report.commands,
        provision_uploads=report.uploads,
        timings_ms=timings,
    )


async def _provision_and_verify(
    provider: SandboxProvider,
    provisioner: GitProjectProvisioner,
    handle: Any,
    source: Any,
) -> tuple[Any, Any, dict[str, float]]:
    """Provision, then run the isolation sweep; classify each failure at its own stage."""

    timings: dict[str, float] = {}
    started = time.perf_counter()
    try:
        report = await provisioner.provision(provider, handle, source)
    except SandboxProvisionError as error:
        raise NeutralProvisionError(str(error)) from error
    timings["provision_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    started = time.perf_counter()
    isolation = await enforce_evaluation_isolation(provider, handle)
    timings["isolation_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    if not isolation.passed:
        raise NeutralIsolationError(
            "evaluation isolation check failed: " + "; ".join(isolation.failures)
        )
    return report, isolation, timings


async def close_neutral_sandbox(
    provider: SandboxProvider, sandbox: NeutralSandbox
) -> tuple[bool, str | None]:
    return await _destroy(provider, sandbox.handle)


async def _probe_command(
    provider: SandboxProvider, handle: Any, command: str, *, timeout_seconds: int = 60
) -> str:
    result = await provider.execute(
        handle, SandboxCommand(command=command, cwd="/", timeout_seconds=timeout_seconds)
    )
    if not result.ok or result.exit_code != 0:
        raise NeutralLifecycleError(
            f"environment probe failed for {command!r}: "
            f"{result.stderr or result.error or result.exit_code}"
        )
    return (result.stdout or "").strip()


async def probe_container_environment(
    provider: SandboxProvider, sandbox: NeutralSandbox
) -> dict[str, Any]:
    """Linux/Python/Git facts from inside the container, for the parity table.

    One command per fact on purpose: the provider's stdout is not guaranteed to preserve the
    newlines a chained shell command produces, so parsing a combined line would silently mangle
    every value.
    """

    head = await _probe_command(
        provider,
        sandbox.handle,
        f"git -C {sandbox.workspace_root} rev-parse HEAD",
    )
    return {
        "uname": await _probe_command(provider, sandbox.handle, "uname -s"),
        "python": await _probe_command(provider, sandbox.handle, "python3 -V"),
        "git": await _probe_command(provider, sandbox.handle, "git --version"),
        "head_commit": head,
        "workspace_root": sandbox.workspace_root,
    }
