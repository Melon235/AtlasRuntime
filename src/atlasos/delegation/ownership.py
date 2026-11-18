"""Write-scope enforcement for Main-driven delegation.

The R1-1 ownership policy (:class:`atlasos.ownership.policy.WorkspaceOwnershipPolicy`) is bound to
**one immutable manifest built from Host declarations**. Delegated workers are created at runtime,
so their identity is not known when the composition is built.

This module does not re-implement R1-1. It is an ``ExternalPolicyAdapter`` that keeps one
``WorkspaceOwnershipPolicy`` per declared worker (each over its own single-worker manifest, built by
``atlasos.ownership.manifest.build_manifest`` from a Host declaration) and routes the trusted
``worker_id`` of the request to it. An unknown identity is denied, never allowed by default, and the
per-worker policies keep their full semantics: exact file checks, textual shell classification, and
refusal of anything they cannot prove.

The declared path list is therefore *Host-owned*: a model cannot add a grant by asking for one, and
:func:`atlasos.workers.models.WorkerProfile` cannot widen it. A child with no declaration owns
nothing and is read-only by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from anycode.contracts.models import PolicyDecision
from anycode.identity.policy import PolicyEnforcer, PolicyRequest

from atlasos.ownership.manifest import build_manifest
from atlasos.ownership.models import (
    OWNERSHIP_COVERAGE_PARTIAL,
    OWNERSHIP_MODE_ENFORCE,
    OWNERSHIP_POLICY_VERSION,
    OWNERSHIP_UNKNOWN_WORKER,
    PROVENANCE_HOST,
    OwnedPathDeclaration,
)
from atlasos.ownership.policy import WorkspaceOwnershipPolicy

_DECLARATION_LIMIT = 256


class DelegationOwnershipError(ValueError):
    """A declaration set the Host must fix; it is never silently weakened."""


class DelegatedOwnershipPolicy:
    """``ExternalPolicyAdapter`` that routes ownership checks to one policy per trusted worker."""

    def __init__(self, *, run_id: str, workspace_root: str) -> None:
        if not run_id.strip():
            raise DelegationOwnershipError("delegated ownership requires a trusted run id")
        if not workspace_root.strip():
            raise DelegationOwnershipError("delegated ownership requires a workspace root")
        self.run_id = run_id
        self.workspace_root = workspace_root
        self.mode = OWNERSHIP_MODE_ENFORCE
        self._policies: dict[str, WorkspaceOwnershipPolicy] = {}
        self._declarations: dict[str, dict[str, object]] = {}
        self._unknown_workers: list[str] = []
        self._denied_unknown = 0

    # -- declarations ---------------------------------------------------------

    def declare(
        self,
        *,
        worker_id: str,
        task_id: str,
        paths: tuple[str, ...] = (),
        excluded: tuple[str, ...] = (),
    ) -> None:
        """Grant ``worker_id`` exactly ``paths``. Both values are Host-supplied."""

        worker = worker_id.strip()
        task = task_id.strip()
        if not worker or not task:
            raise DelegationOwnershipError("a declaration requires a worker id and a task id")
        if worker in self._policies:
            raise DelegationOwnershipError(f"worker {worker!r} is already declared in this run")
        if len(self._policies) >= _DECLARATION_LIMIT:
            raise DelegationOwnershipError(
                f"at most {_DECLARATION_LIMIT} delegated workers may be declared in one run"
            )
        manifest = build_manifest(
            run_id=self.run_id,
            workspace_root=self.workspace_root,
            declarations=[
                OwnedPathDeclaration(
                    worker_id=worker,
                    task_id=task,
                    paths=tuple(paths),
                    excluded=tuple(excluded),
                    provenance=PROVENANCE_HOST,
                )
            ],
        )
        self._policies[worker] = WorkspaceOwnershipPolicy(manifest, mode=OWNERSHIP_MODE_ENFORCE)
        self._declarations[worker] = {
            "task_id": task,
            "paths": list(manifest.declared_paths(worker)),
            "excluded": list(manifest.excluded_paths(worker)),
            "manifest_digest": manifest.digest,
            "provenance": PROVENANCE_HOST,
        }

    def declared(self, worker_id: str) -> dict[str, object] | None:
        return self._declarations.get(worker_id)

    def workers(self) -> tuple[str, ...]:
        return tuple(sorted(self._declarations))

    # -- ExternalPolicyAdapter ------------------------------------------------

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        worker_id = _attribute(request, "worker_id")
        if worker_id is None:
            self._denied_unknown += 1
            return self._deny(request, OWNERSHIP_UNKNOWN_WORKER, "<identity>")
        policy = self._policies.get(worker_id)
        if policy is None:
            self._denied_unknown += 1
            if worker_id not in self._unknown_workers and len(self._unknown_workers) < 64:
                self._unknown_workers.append(worker_id)
            return self._deny(request, OWNERSHIP_UNKNOWN_WORKER, worker_id)
        return await policy.decide(request)

    # -- evidence -------------------------------------------------------------

    def evidence_snapshot(self) -> dict[str, Any]:
        checked = allowed = denied = 0
        denials: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        for worker_id in self.workers():
            evidence = self._policies[worker_id].evidence
            checked += evidence.checked_calls
            allowed += evidence.allowed_calls
            denied += evidence.denied_calls
            denials.extend(evidence.denials)
            findings.extend(evidence.audit_findings)
        return {
            "mode": self.mode,
            "coverage": OWNERSHIP_COVERAGE_PARTIAL,
            "policy_version": OWNERSHIP_POLICY_VERSION,
            "run_id": self.run_id,
            "workspace_root": self.workspace_root,
            "workers": {
                worker: dict(declaration)
                for worker, declaration in sorted(self._declarations.items())
            },
            "checked_calls": checked,
            "allowed_calls": allowed,
            "denied_calls": denied,
            "unknown_worker_denials": self._denied_unknown,
            "unknown_workers": list(self._unknown_workers),
            "denials": denials,
            "audit_findings": findings,
        }

    # -- helpers --------------------------------------------------------------

    def _deny(self, request: PolicyRequest, reason: str, target: str) -> PolicyDecision:
        del target
        return PolicyDecision(
            id=f"delegation-ownership-{request.correlation_id}",
            run_id=request.run_id,
            task_id=request.task_id,
            outcome="deny",
            policy_version=OWNERSHIP_POLICY_VERSION,
            reason_codes=(reason,),
            correlation_id=request.correlation_id,
            causation_id=request.causation_id,
            generation=request.generation,
            attempt=request.attempt,
        )


def build_delegation_ownership(
    *,
    run_id: str,
    workspace_root: str,
) -> tuple[PolicyEnforcer, DelegatedOwnershipPolicy]:
    """Return the AnyCode enforcer plus the Atlas adapter. Always enforcing."""

    policy = DelegatedOwnershipPolicy(run_id=run_id, workspace_root=workspace_root)
    # ``fail_closed`` keeps provider-side failures denials; the adapter already denies every
    # identity it cannot verify, so the wrapper only has to preserve denies.
    return PolicyEnforcer(policy, fail_closed=True), policy


def _attribute(request: PolicyRequest, key: str) -> str | None:
    attributes: Mapping[str, Any] = request.context.attributes
    value = attributes.get(key)
    if value is None:
        nested = attributes.get("atlas_worker")
        if isinstance(nested, Mapping):
            value = nested.get(key)
    return str(value) if value is not None else None


__all__ = [
    "DelegatedOwnershipPolicy",
    "DelegationOwnershipError",
    "build_delegation_ownership",
]
