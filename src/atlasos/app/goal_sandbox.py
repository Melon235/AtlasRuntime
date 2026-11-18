"""Stage P: the sandbox half of the main-driven goal pipeline.

One goal run owns exactly one project sandbox. The Main and every delegated child share it through
the *same* ``AtlasToolRuntime``, which is what makes "children see the Main's workspace" true
without giving a child a second sandbox or a second tool surface.

What this module owns, and what it deliberately does not:

* it creates, binds, probes, exports and destroys the sandbox (lifecycle);
* it does **not** implement tools: ``SandboxWorkspaceTools`` copies the six AnyCode workspace tool
  schemas and swaps only their physical backend;
* it does **not** decide permissions: the M3 action gate, the M4 ledger and the R1-1 ownership
  policy see the same tool calls either way. A path outside the workspace is refused by the
  sandbox tools, a path outside a worker's grant is refused by the ownership policy.

Evidence is observation only. A failed probe is a failure to *demonstrate* isolation, and it is
reported as such instead of being silently downgraded.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anycode.identity.context import ExecutionContext
from anycode.sandbox import SandboxCommand
from anycode.security.redaction import safe_exception_message

from atlasos.config import AtlasConfig
from atlasos.sandbox import (
    GitProjectProvisioner,
    ProjectSandboxBinding,
    ProjectSandboxManager,
    SandboxCapabilityError,
    SandboxEnvironmentBlocked,
    SandboxIsolationError,
    SandboxProjectConfig,
    SandboxWorkspaceTools,
    SQLiteSandboxBindingStore,
    build_sandbox_provider,
    capability_digest,
    capability_summary,
    export_patch,
    export_workspace,
    is_real_sandbox_provider,
    require_capabilities,
)
from atlasos.sandbox.opensandbox_provider import OpenSandboxOptions
from atlasos.sandbox.validation import (
    probe_host_separation,
    probe_network_none,
    require_host_separation,
)
from atlasos.trajectory.recorder import TrajectoryRecorder

#: The fake provider is deterministic test infrastructure. It runs real subprocesses, but on the
#: Host, so it can never be presented as isolation evidence.
FAKE_PROVIDER = "fake"
FAKE_IMAGE = "fake@sha256:0123456789abcdef"


class GoalSandboxError(RuntimeError):
    """The goal pipeline cannot obtain or keep the sandbox it was configured with."""


@dataclass(frozen=True)
class GoalSandboxOptions:
    """Host-owned sandbox configuration for one goal run."""

    #: ``opensandbox`` or ``daytona``. ``fake`` is accepted only with ``require_real=False`` and an
    #: injected ``provider_factory``.
    provider: str = "opensandbox"
    #: A clean git repository. The sandbox clones it; the Host copy is never written to.
    source_repo: Path = Path()
    image: str | None = None
    snapshot: str | None = None
    workspace_root: str = "/workspace/project"
    max_bundle_bytes: int = 64 * 1024 * 1024
    retain_on_failure: bool = False
    #: Refuse the deterministic provider. The default keeps the honest answer: this is a real
    #: sandbox unless a caller explicitly opts out.
    require_real: bool = True
    #: Test seam. Only consulted when ``provider`` is ``fake``.
    provider_factory: Callable[[Path], Any] | None = None

    def __post_init__(self) -> None:
        if self.provider == FAKE_PROVIDER:
            if self.require_real:
                raise ValueError(
                    "provider='fake' cannot be combined with require_real=True: the fake provider "
                    "runs on the Host and is not isolation evidence"
                )
            if self.provider_factory is None:
                raise ValueError("provider='fake' requires an injected provider_factory")
        elif not self.require_real:
            raise ValueError(
                "require_real=False is only valid for the deterministic fake provider; a real "
                "provider must always be verified"
            )


@dataclass
class _SandboxEvidence:
    provider: str = ""
    provider_is_real: bool = False
    capabilities: dict[str, Any] = field(default_factory=dict)
    capabilities_digest: str = ""
    binding_id: str = ""
    sandbox_id: str = ""
    binding_state: str = ""
    workspace_root: str = ""
    network_probe: str = "NOT_RUN"
    host_separation: dict[str, Any] = field(default_factory=dict)
    provision_commands: int = 0
    provision_uploads: int = 0
    created: bool = False
    destroyed: bool = False
    retained: bool = False
    patch_digest: str | None = None
    patch_changed_files: int = 0
    export_digest: str | None = None
    export_files: int = 0
    export_size_bytes: int = 0
    check_runs: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class GoalSandbox:
    """Lifecycle wrapper: create → probe → (tools) → export → destroy."""

    def __init__(
        self,
        options: GoalSandboxOptions,
        *,
        config: AtlasConfig,
        run_id: str,
        run_dir: Path,
        recorder: TrajectoryRecorder | None = None,
    ) -> None:
        self.options = options
        self.config = config
        self.run_id = run_id
        self.run_dir = run_dir
        self.recorder = recorder
        self.evidence = _SandboxEvidence(
            provider=options.provider,
            workspace_root=options.workspace_root,
        )
        self._provider: Any | None = None
        self._manager: ProjectSandboxManager | None = None
        self._binding: ProjectSandboxBinding | None = None
        self._tools: SandboxWorkspaceTools | None = None
        self._task_id: str | None = None
        self._exported = False

    # -- properties ----------------------------------------------------------

    @property
    def workspace_root(self) -> str:
        """The workspace as the *tools* see it, not the Host directory."""

        if self._binding is not None:
            return self._binding.workspace_root
        return self.options.workspace_root

    @property
    def sandbox_tools(self) -> SandboxWorkspaceTools:
        if self._tools is None:
            raise GoalSandboxError("the goal sandbox has not been started")
        return self._tools

    @property
    def binding(self) -> ProjectSandboxBinding:
        if self._binding is None:
            raise GoalSandboxError("the goal sandbox has not been started")
        return self._binding

    @property
    def ready(self) -> bool:
        return self._binding is not None and self._manager is not None

    # -- lifecycle -----------------------------------------------------------

    def _provider_config(self) -> SandboxProjectConfig:
        sandbox = self.config.sandbox
        if self.options.provider == FAKE_PROVIDER:
            project = SandboxProjectConfig(
                provider_name=FAKE_PROVIDER,
                image=FAKE_IMAGE,
                workspace_root=self.options.workspace_root,
                max_bundle_bytes=self.options.max_bundle_bytes,
            )
        else:
            project = SandboxProjectConfig(
                provider_name=self.options.provider,
                image=self.options.image or sandbox.image,
                snapshot=self.options.snapshot or sandbox.snapshot,
                workspace_root=self.options.workspace_root,
                max_bundle_bytes=self.options.max_bundle_bytes,
            )
        project.validate_v1()
        return project

    def _build_provider(self) -> Any:
        sandbox = self.config.sandbox
        if self.options.provider == FAKE_PROVIDER:
            factory = self.options.provider_factory
            assert factory is not None  # __post_init__ guarantees it
            return factory(self.run_dir / "sandbox")
        return build_sandbox_provider(
            self.options.provider,
            opensandbox_options=OpenSandboxOptions(
                endpoint=sandbox.endpoint,
                protocol=sandbox.protocol,
                timeout_minutes=sandbox.timeout_minutes,
                ready_timeout_seconds=sandbox.ready_timeout_seconds,
                cpu=sandbox.cpu,
                memory=sandbox.memory,
                use_server_proxy=sandbox.use_server_proxy,
            ),
        )

    async def start(
        self,
        *,
        task_id: str,
        context: ExecutionContext,
        worker_id: str,
    ) -> ProjectSandboxBinding:
        """Create the binding and prove the two isolation properties before any tool runs."""

        self._task_id = task_id
        provider = self._build_provider()
        self._provider = provider
        capabilities = provider.capabilities()
        self.evidence.capabilities = capability_summary(capabilities)
        self.evidence.capabilities_digest = capability_digest(capabilities)
        self.evidence.provider_is_real = is_real_sandbox_provider(
            provider, expected_name=self.options.provider
        )
        try:
            require_capabilities(capabilities)
        except SandboxCapabilityError as error:
            raise GoalSandboxError(safe_exception_message(error)) from error
        health = await provider.health()
        if health.status == "unavailable":
            raise SandboxEnvironmentBlocked(
                f"sandbox provider {self.options.provider!r} is unavailable: {health.message}"
            )

        project = self._provider_config()
        # The provisioner needs a clean git repository; a dirty one is a configuration error, not a
        # silent snapshot of uncommitted work.
        provisioner = GitProjectProvisioner(
            max_bundle_bytes=project.max_bundle_bytes,
            workspace_root=project.workspace_root,
        )
        try:
            provisioner.inspect(self.options.source_repo)
        except Exception as error:  # noqa: BLE001 - classified as a configuration failure
            raise GoalSandboxError(
                f"source repository {self.options.source_repo} cannot be provisioned: "
                f"{safe_exception_message(error)}"
            ) from error

        manager = ProjectSandboxManager(
            provider,
            project,
            run_id=self.run_id,
            binding_store=SQLiteSandboxBindingStore(self.run_dir / "sandbox_bindings.db"),
            trajectory_recorder=self.recorder,
        )
        self._manager = manager
        binding = await manager.ensure_binding(
            task_id=task_id,
            source_repo=str(self.options.source_repo),
            context=context,
            worker_id=worker_id,
        )
        self._binding = binding
        self.evidence.created = manager.create_count > 0
        self.evidence.binding_id = binding.binding_id
        self.evidence.sandbox_id = binding.sandbox_handle.id
        self.evidence.binding_state = binding.state
        self.evidence.workspace_root = binding.workspace_root

        await self._probe_isolation()
        self._tools = SandboxWorkspaceTools(manager, task_id, provider)
        return binding

    async def _probe_isolation(self) -> None:
        provider = self._provider
        binding = self._binding
        assert provider is not None and binding is not None

        if not self.evidence.provider_is_real:
            # The fake provider executes on the Host by construction. Running the egress or
            # host-separation probes against it would observe the Host and produce a number that
            # says nothing about isolation, so the evidence states that instead.
            self.evidence.network_probe = "NOT_APPLICABLE_FAKE_PROVIDER"
            self.evidence.host_separation = {"status": "NOT_APPLICABLE_FAKE_PROVIDER"}
            return

        outcome = await probe_network_none(provider, binding.sandbox_handle)
        self.evidence.network_probe = outcome
        if outcome == "FAIL":
            raise SandboxIsolationError(
                "sandbox declared network='none' but the egress probe reached the network"
            )

        separation = await probe_host_separation(
            provider, binding.sandbox_handle, host_dir=self.run_dir
        )
        self.evidence.host_separation = {
            "host_only_visible": separation.host_only_visible,
            "sandbox_only_visible_on_host": separation.sandbox_only_visible_on_host,
            "sandbox_only_read_back": separation.sandbox_only_read_back,
        }
        require_host_separation(separation)

    # -- runs the harness controls, not the model ----------------------------

    async def run_check(self, command: str, *, timeout_seconds: int = 300) -> dict[str, Any]:
        """Run a fixed harness command in the sandbox and digest its output.

        This is the oracle's execution channel. It is not registered as a tool, so the Main cannot
        call it, cannot see its result before finishing, and cannot influence it.
        """

        if self._provider is None or self._binding is None or self._task_id is None:
            raise GoalSandboxError("the goal sandbox has not been started")
        started = time.perf_counter()
        result = await self._provider.execute(
            self._binding.sandbox_handle,
            SandboxCommand(
                command=command,
                cwd=self._binding.workspace_root,
                timeout_seconds=timeout_seconds,
            ),
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        self.evidence.check_runs += 1
        stdout = result.stdout or ""
        return {
            "command": command,
            "cwd": self._binding.workspace_root,
            "ok": bool(result.ok),
            "exit_code": int(result.exit_code)
            if getattr(result, "exit_code", None) is not None
            else None,
            "stdout_digest": "sha256:" + hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            "stdout_chars": len(stdout),
            "stderr_chars": len(result.stderr or ""),
            "elapsed_ms": elapsed_ms,
        }

    # -- export --------------------------------------------------------------

    async def export(self) -> dict[str, Any]:
        """Export the patch and the full workspace to the run directory."""

        if self._exported:
            raise GoalSandboxError("the goal sandbox workspace was already exported")
        if self._provider is None or self._binding is None:
            raise GoalSandboxError("the goal sandbox has not been started")
        binding = self._binding
        patch = await export_patch(
            self._provider,
            binding.sandbox_handle,
            base_commit=binding.source_commit,
            workspace_root=binding.workspace_root,
        )
        (self.run_dir / "workspace.patch").write_text(patch.patch_text, encoding="utf-8")
        export = await export_workspace(
            self._provider,
            binding.sandbox_handle,
            destination=self.run_dir / "workspace",
            workspace_root=binding.workspace_root,
        )
        self._exported = True
        self.evidence.patch_digest = patch.patch_digest
        self.evidence.patch_changed_files = patch.changed_file_count
        self.evidence.export_digest = export.archive_digest
        self.evidence.export_files = export.file_count
        self.evidence.export_size_bytes = export.size_bytes
        return {
            "patch_path": str(self.run_dir / "workspace.patch"),
            "patch_digest": patch.patch_digest,
            "patch_changed_files": patch.changed_file_count,
            "workspace_path": str(export.destination),
            "workspace_digest": export.archive_digest,
            "workspace_files": export.file_count,
            "workspace_size_bytes": export.size_bytes,
        }

    # -- teardown ------------------------------------------------------------

    async def close(self, *, failed: bool = False) -> bool:
        """Destroy the sandbox unless the caller asked to retain a failed one."""

        if self._manager is None or self._task_id is None:
            return False
        if self.options.retain_on_failure and failed:
            self.evidence.retained = True
            self.evidence.provision_commands = self._manager.provision_commands
            self.evidence.provision_uploads = self._manager.provision_uploads
            return False
        try:
            destroyed = await self._manager.close(self._task_id)
        except Exception as error:  # noqa: BLE001 - teardown failure is recorded, not raised
            self.evidence.error = safe_exception_message(error)
            raise
        finally:
            self.evidence.provision_commands = self._manager.provision_commands
            self.evidence.provision_uploads = self._manager.provision_uploads
        self.evidence.destroyed = bool(destroyed)
        return bool(destroyed)


__all__ = [
    "FAKE_PROVIDER",
    "GoalSandbox",
    "GoalSandboxError",
    "GoalSandboxOptions",
]
