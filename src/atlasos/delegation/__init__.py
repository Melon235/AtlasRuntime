"""Stage M: Main-driven delegation (Main → router → concurrent subworkers → handoff → Main).

The package is intentionally small and additive. It composes existing runtime components and adds
no second scheduler, no second context engine, and no second authorization path:

``router``
    Builds trusted evidence and asks the frozen R0.1.0 router for admission.
``service``
    Creates scoped workers through ``ScopedWorkerFactory``, runs them concurrently behind their own
    semaphore, and returns a bounded projection.
``ownership``
    Routes R1-1 write-scope enforcement to one policy per delegated worker.
``handoff``
    Parent-scoped reads over the existing run-scoped ``HandoffStore``.
``tool``
    The ``delegate_tasks`` core tool the Main calls from its own loop.
"""

from atlasos.delegation.config import DelegationConfig, load_delegation_config
from atlasos.delegation.handoff import (
    DELEGATE_READ_TOOL,
    DelegatedResultIndex,
    DelegatedResultReader,
)
from atlasos.delegation.models import (
    ChildOutcome,
    ChildStatus,
    DelegatedTaskSpec,
    DelegateTasksInput,
    DelegationBatch,
    DelegationCaller,
    DelegationDecision,
    DelegationEvidence,
    DelegationRoute,
    DelegationTotals,
)
from atlasos.delegation.ownership import (
    DelegatedOwnershipPolicy,
    build_delegation_ownership,
)
from atlasos.delegation.router import (
    STAGE_M_POLICY_VERSION,
    DelegationPlanContext,
    DelegationRouter,
    ProfileFacts,
)
from atlasos.delegation.service import DelegationLimits, DelegationService
from atlasos.delegation.tool import STAGE_M_DELEGATE_TOOL, DelegateTasksTool

__all__ = [
    "DELEGATE_READ_TOOL",
    "STAGE_M_DELEGATE_TOOL",
    "STAGE_M_POLICY_VERSION",
    "ChildOutcome",
    "ChildStatus",
    "DelegateTasksInput",
    "DelegateTasksTool",
    "DelegatedOwnershipPolicy",
    "DelegatedResultIndex",
    "DelegatedResultReader",
    "DelegatedTaskSpec",
    "DelegationBatch",
    "DelegationCaller",
    "DelegationConfig",
    "DelegationDecision",
    "DelegationEvidence",
    "DelegationLimits",
    "DelegationPlanContext",
    "DelegationRoute",
    "DelegationRouter",
    "DelegationService",
    "DelegationTotals",
    "ProfileFacts",
    "build_delegation_ownership",
    "load_delegation_config",
]
