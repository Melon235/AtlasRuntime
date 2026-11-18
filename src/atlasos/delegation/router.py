"""Admission router for Main-driven delegation (Stage M).

The Main proposes; the runtime admits. This module builds the evidence for that decision and then
asks the **frozen** R0.1.0 router (:func:`atlasos.workers.routing.route_delegation`,
``POLICY_VERSION = "r0.1.0"``) whether the plan may run. It does not re-implement a gate, does not
score context savings, and does not plan a DAG.

What is new here is only the *evidence*, and every item is a fact the runtime itself will enforce:

``RISK_CLASS``
    ``read_only`` when no Host grant applies (the child then owns nothing, so every write is
    denied by R1-1), ``local_reversible_write`` when a Host grant covers exactly the declared
    paths.
``WRITE_PATHS``
    The declared paths, emitted **only** when a Host grant already covers them.
``DELIVERABLE`` / ``CONTEXT`` / ``GOAL_COVERAGE``
    The runtime owns the candidate set and delivers the assignment text, so these are trusted
    metadata rather than planner claims.

Consequence, stated plainly: the Main cannot widen its own authority by proposing paths, and a
task that needs a write outside the Host grant is refused before the router is consulted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from atlasos.delegation.models import (
    REASON_ADMITTED,
    REASON_DEPENDENCY_NOT_SUPPORTED,
    REASON_INVALID_PLAN,
    REASON_WRITE_SCOPE_NOT_GRANTED,
    ChildAdmission,
    DelegatedTaskSpec,
    DelegateTasksInput,
    DelegationDecision,
    DelegationRoute,
)
from atlasos.ownership.models import path_is_within
from atlasos.ownership.paths import OwnershipPathError, normalize_workspace_path
from atlasos.workers.routing import (
    CandidateTask,
    CapabilityCatalog,
    DelegationProposal,
    EvidenceItem,
    EvidenceKind,
    EvidenceStatus,
    RoutingPolicy,
    SourceKind,
    WorkerCapability,
    decision_to_dict,
    route_delegation,
)

#: Distinct from ``r0.1.0``: the evidence model and the concurrency bound differ, so the decision
#: must not be readable as a frozen R0.1.0 decision.
STAGE_M_POLICY_VERSION = "m.1.0"


@dataclass(frozen=True)
class ProfileFacts:
    """Read-only routing metadata derived from the trusted ``WorkerCatalog``."""

    profile_id: str
    base_tools: tuple[str, ...]
    read_only: bool
    can_delegate: bool = False


@dataclass(frozen=True)
class DelegationPlanContext:
    """Trusted facts the router is allowed to use. Never model text."""

    run_id: str
    parent_task_id: str
    max_children: int
    profiles: Mapping[str, ProfileFacts]
    #: ``profile_id -> paths the Host has granted to a child of that profile``.
    granted_write_paths: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    workspace_root: str = ""


class DelegationRouter:
    """Turn a Main proposal into an auditable admission decision."""

    def __init__(self, *, policy: RoutingPolicy | None = None) -> None:
        self.policy = policy or RoutingPolicy(version=STAGE_M_POLICY_VERSION)

    def policy_for(self, max_children: int) -> RoutingPolicy:
        return RoutingPolicy(
            version=self.policy.version,
            max_parallel_workers=max_children,
            allow_shared_mutation=self.policy.allow_shared_mutation,
            allow_unknown_risk=self.policy.allow_unknown_risk,
            require_goal_coverage=self.policy.require_goal_coverage,
            require_context_evidence=self.policy.require_context_evidence,
            min_tasks_for_delegation=self.policy.min_tasks_for_delegation,
            allow_single_read_only_delegation=self.policy.allow_single_read_only_delegation,
        )

    # -- decisions ------------------------------------------------------------

    def decide(
        self,
        request: DelegateTasksInput,
        *,
        context: DelegationPlanContext,
    ) -> DelegationDecision:
        policy = self.policy_for(context.max_children)
        refused = self._stage_m_refusal(request, context=context, policy=policy)
        if refused is not None:
            return refused

        proposal = self._proposal(request, context=context)
        capabilities = _catalog(context)
        frozen = route_delegation(proposal, capabilities, policy)
        approved = set(frozen.approved_task_ids)
        route = DelegationRoute(frozen.route.value)
        children = tuple(
            ChildAdmission(
                key=spec.key,
                profile_id=spec.profile_id,
                admitted=spec.key in approved,
                reason_codes=(
                    (REASON_ADMITTED,)
                    if spec.key in approved
                    else tuple(frozen.reason_codes) or (REASON_INVALID_PLAN,)
                ),
            )
            for spec in request.tasks
        )
        return DelegationDecision(
            route=route,
            reason_codes=tuple(frozen.reason_codes),
            blocked_gates=tuple(frozen.blocked_gates),
            children=children,
            policy_version=frozen.policy_version,
            policy_digest=frozen.policy_digest,
            max_wave_width=frozen.max_parallel_wave_width,
            frozen_decision=decision_to_dict(frozen),
        )

    # -- stage-M guards -------------------------------------------------------

    def _stage_m_refusal(
        self,
        request: DelegateTasksInput,
        *,
        context: DelegationPlanContext,
        policy: RoutingPolicy,
    ) -> DelegationDecision | None:
        """Refuse the two plan shapes the frozen vocabulary cannot express.

        Everything else — duplicates, cycles, unknown profiles, shared writes, insufficient
        context, wave width — is decided by R0.1.0 and is not duplicated here.
        """

        reasons: tuple[str, ...] = ()
        if any(spec.depends_on for spec in request.tasks):
            # A delegation call is one parallel wave by construction: children receive no sibling
            # results, so a declared dependency would be silently unmet.
            reasons = (REASON_DEPENDENCY_NOT_SUPPORTED,)
        else:
            for spec in request.tasks:
                problem = self._write_scope_problem(spec, context=context)
                if problem is not None:
                    reasons = (problem,)
                    break
        if not reasons:
            return None
        return DelegationDecision(
            route=DelegationRoute.DIRECT,
            reason_codes=reasons,
            blocked_gates=("GATE_M_PROPOSAL",),
            children=tuple(
                ChildAdmission(
                    key=spec.key,
                    profile_id=spec.profile_id,
                    admitted=False,
                    reason_codes=reasons,
                )
                for spec in request.tasks
            ),
            policy_version=policy.version,
            policy_digest=policy.digest(),
            max_wave_width=0,
            frozen_decision=None,
        )

    @staticmethod
    def _write_scope_problem(
        spec: DelegatedTaskSpec,
        *,
        context: DelegationPlanContext,
    ) -> str | None:
        if not spec.write_paths:
            return None
        facts = context.profiles.get(spec.profile_id)
        if facts is not None and facts.read_only:
            # A profile without a write tool cannot honour the request; refusing is clearer than
            # running a child that will fail at the first edit.
            return REASON_WRITE_SCOPE_NOT_GRANTED
        grant = context.granted_write_paths.get(spec.profile_id, ())
        if not grant:
            return REASON_WRITE_SCOPE_NOT_GRANTED
        for raw in spec.write_paths:
            try:
                target = normalize_workspace_path(raw, workspace_root=context.workspace_root)
            except OwnershipPathError:
                return REASON_INVALID_PLAN
            if not any(path_is_within(declared, target) for declared in grant):
                return REASON_WRITE_SCOPE_NOT_GRANTED
        return None

    # -- evidence -------------------------------------------------------------

    def _proposal(
        self,
        request: DelegateTasksInput,
        *,
        context: DelegationPlanContext,
    ) -> DelegationProposal:
        specs: Sequence[DelegatedTaskSpec] = request.tasks
        keys = tuple(spec.key for spec in specs)
        candidates = tuple(
            CandidateTask(
                task_id=spec.key,
                description=spec.objective,
                # Dependencies are refused above; the frozen router sees a flat wave.
                depends_on=(),
                proposed_profile_id=spec.profile_id,
            )
            for spec in specs
        )
        evidence: list[EvidenceItem] = [
            EvidenceItem(
                evidence_id="ev-goal-coverage",
                kind=EvidenceKind.GOAL_COVERAGE,
                subject_task_ids=keys,
                value=list(keys),
                source_kind=SourceKind.TRUSTED_METADATA,
                source_locator="delegation:runtime-candidate-set",
                status=EvidenceStatus.PROVEN,
            )
        ]
        for spec in specs:
            write_paths = tuple(
                normalize_workspace_path(raw, workspace_root=context.workspace_root)
                for raw in spec.write_paths
            )
            if write_paths:
                evidence.append(
                    EvidenceItem(
                        evidence_id=f"ev-write-{spec.key}",
                        kind=EvidenceKind.WRITE_PATHS,
                        subject_task_ids=(spec.key,),
                        value=list(write_paths),
                        source_kind=SourceKind.STATIC_ANALYSIS,
                        source_locator="delegation:host-grant",
                        status=EvidenceStatus.PROVEN,
                    )
                )
            evidence.append(
                EvidenceItem(
                    evidence_id=f"ev-risk-{spec.key}",
                    kind=EvidenceKind.RISK_CLASS,
                    subject_task_ids=(spec.key,),
                    value=("local_reversible_write" if write_paths else "read_only"),
                    # The runtime enforces the risk it reports: no grant means no writable path,
                    # and a grant means R1-1 checks every write against it.
                    source_kind=SourceKind.TRUSTED_METADATA,
                    source_locator="delegation:enforced-scope",
                    status=EvidenceStatus.PROVEN,
                )
            )
            evidence.append(
                EvidenceItem(
                    evidence_id=f"ev-deliverable-{spec.key}",
                    kind=EvidenceKind.DELIVERABLE,
                    subject_task_ids=(spec.key,),
                    value=[spec.key],
                    source_kind=SourceKind.TRUSTED_METADATA,
                    source_locator="delegation:runtime-candidate-set",
                    status=EvidenceStatus.PROVEN,
                )
            )
            evidence.append(
                EvidenceItem(
                    evidence_id=f"ev-context-{spec.key}",
                    kind=EvidenceKind.CONTEXT,
                    subject_task_ids=(spec.key,),
                    value="assignment",
                    source_kind=SourceKind.TRUSTED_METADATA,
                    source_locator="delegation:runtime-assignment",
                    # A child sees only its objective and the background the Main supplied. With no
                    # background, gate 5 refuses: the runtime will not invent the missing context
                    # by projecting the Main history into the child.
                    status=(
                        EvidenceStatus.PROVEN
                        if spec.background is not None and spec.background.strip()
                        else EvidenceStatus.UNKNOWN
                    ),
                )
            )
        return DelegationProposal(
            proposal_id=_proposal_id(request, context=context),
            overall_goal=request.reason or "; ".join(spec.objective for spec in specs),
            candidate_tasks=candidates,
            evidence=tuple(evidence),
        )


def _catalog(context: DelegationPlanContext) -> CapabilityCatalog:
    """Capabilities derived from the trusted catalog, without a ``WorkerCatalog`` instance."""

    capabilities = []
    for profile_id in sorted(context.profiles):
        facts = context.profiles[profile_id]
        capabilities.append(
            WorkerCapability(
                profile_id=facts.profile_id,
                supported_domains=("general",),
                preferred_work_types=(
                    ("read_only_analysis",) if facts.read_only else ("module_edit",)
                ),
                avoid_work_types=() if facts.read_only else ("read_only_analysis",),
                cost_tier="cheap" if facts.read_only else "standard",
                parallel_safe=not facts.can_delegate,
                max_parallel_instances=context.max_children,
            )
        )
    return CapabilityCatalog(tuple(capabilities))


def _proposal_id(request: DelegateTasksInput, *, context: DelegationPlanContext) -> str:
    payload = {
        "run_id": context.run_id,
        "parent_task_id": context.parent_task_id,
        "tasks": [
            {
                "key": spec.key,
                "objective": spec.objective,
                "background": spec.background,
                "profile_id": spec.profile_id,
                "write_paths": list(spec.write_paths),
            }
            for spec in request.tasks
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "m-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


__all__ = [
    "DelegationPlanContext",
    "DelegationRouter",
    "ProfileFacts",
    "STAGE_M_POLICY_VERSION",
]
