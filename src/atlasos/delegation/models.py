"""Stage M Main-driven delegation models.

Two kinds of value live here and they are never mixed:

* :class:`DelegatedTaskSpec` / :class:`DelegateTasksInput` are **untrusted model output**. They
  describe what the Main *wants*; they cannot grant a profile, a tool, a turn limit or a writable
  path.
* :class:`ChildOutcome`, :class:`DelegationDecision` and :class:`DelegationEvidence` are **runtime
  observations**. They say what the runtime decided and what actually happened.

No model in this module authorizes anything. Authorization is the frozen router
(``atlasos.workers.routing``) plus the M3 action gate plus the R1-1 ownership policy.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Frozen router vocabulary reused verbatim, so a decision can be compared with R0.1.0 output.
REASON_NESTED_DELEGATION = "NESTED_DELEGATION"
REASON_ROUND_LIMIT_EXCEEDED = "ROUND_LIMIT_EXCEEDED"
REASON_WRITE_SCOPE_NOT_GRANTED = "WRITE_SCOPE_NOT_GRANTED"
REASON_DEPENDENCY_NOT_SUPPORTED = "DEPENDENCY_NOT_SUPPORTED"
REASON_DELEGATION_DISABLED = "DELEGATION_DISABLED"
REASON_RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
REASON_SHARED_WRITE_RISK = "SHARED_WRITE_RISK"
REASON_INVALID_PLAN = "INVALID_PLAN"
REASON_ADMITTED = "DELEGATION_ADMITTED"

#: Hard ceiling on one ``delegate_tasks`` result. The Main receives a bounded projection, never a
#: child transcript, so this number is part of the context-isolation contract.
MAX_TOOL_RESULT_CHARS = 8_000

_TASK_KEY = r"^[a-z0-9][a-z0-9_-]{0,47}$"


class DelegatedTaskSpec(BaseModel):
    """One candidate subtask proposed by the Main. Untrusted; nothing here is a grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=48, pattern=_TASK_KEY)
    objective: str = Field(min_length=1, max_length=4_000)
    #: What the child needs that is not in the objective. The child never sees the Main history, so
    #: an empty background is admission-relevant, not cosmetic.
    background: str | None = Field(default=None, max_length=8_000)
    profile_id: str = Field(default="research_worker", min_length=1, max_length=64)
    #: Paths the Main believes the subtask must write. The router accepts these only when a Host
    #: grant already covers them; otherwise the plan is refused rather than trusted.
    write_paths: tuple[str, ...] = ()
    #: Subtasks of this same call this task depends on. Stage M does not schedule a nested DAG.
    depends_on: tuple[str, ...] = ()

    @field_validator("objective")
    @classmethod
    def _objective_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("objective must contain text")
        return value


class DelegateTasksInput(BaseModel):
    """Wire schema of the ``delegate_tasks`` tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tasks: tuple[DelegatedTaskSpec, ...] = Field(min_length=1, max_length=4)
    reason: str = Field(default="", max_length=500)


class DelegationRoute(StrEnum):
    """Same three-valued outcome as the frozen R0.1.0 ``RouteKind``."""

    DIRECT = "direct"
    DELEGATE_ONE = "delegate_one"
    DELEGATE_PARALLEL = "delegate_parallel"


class ChildStatus(StrEnum):
    """Terminal state of one child. Every value is reachable and observable."""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    DENIED = "denied"


class ChildAdmission(BaseModel):
    """Per-candidate admission outcome, so "why was this key not run?" is answerable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    profile_id: str
    admitted: bool
    reason_codes: tuple[str, ...] = ()


class DelegationDecision(BaseModel):
    """The router's answer for one ``delegate_tasks`` call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    route: DelegationRoute
    reason_codes: tuple[str, ...] = ()
    blocked_gates: tuple[str, ...] = ()
    children: tuple[ChildAdmission, ...] = ()
    policy_version: str
    policy_digest: str
    max_wave_width: int = 0
    #: ``decision_to_dict`` of the frozen router when it was consulted; ``None`` when a stage-M
    #: guard refused the plan before the router could speak.
    frozen_decision: dict[str, object] | None = None

    @property
    def admitted(self) -> bool:
        return self.route is not DelegationRoute.DIRECT

    def admitted_specs(self) -> tuple[str, ...]:
        return tuple(child.key for child in self.children if child.admitted)


class ChildOutcome(BaseModel):
    """What one child actually did. Usage comes from that child's own runner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    worker_id: str
    profile_id: str
    status: ChildStatus
    success: bool
    stop_reason: str | None = None
    turns: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)

    #: Monotonic offsets from the start of the batch. Overlap is decided from these numbers only.
    started_offset_ms: float = Field(default=0.0, ge=0)
    finished_offset_ms: float = Field(default=0.0, ge=0)

    #: Cache-miss input tokens, the historical meaning of ``prompt_tokens`` in this repository.
    prompt_tokens: int = Field(default=0, ge=0)
    #: Full prompt size (miss + cache reads + cache creation); the value comparable with a
    #: provider ``prompt_tokens`` field.
    total_prompt_tokens: int = Field(default=0, ge=0)
    cache_hit_tokens: int = Field(default=0, ge=0)
    cache_miss_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)

    output_chars: int = Field(default=0, ge=0)
    summary: str = ""
    summary_chars: int = Field(default=0, ge=0)
    ref_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def duration_ms(self) -> float:
        return max(0.0, self.finished_offset_ms - self.started_offset_ms)


class DelegationTotals(BaseModel):
    """Aggregate accounting for one batch or one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    children_started: int = Field(default=0, ge=0)
    children_completed: int = Field(default=0, ge=0)
    children_failed: int = Field(default=0, ge=0)
    children_timed_out: int = Field(default=0, ge=0)
    children_cancelled: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    total_prompt_tokens: int = Field(default=0, ge=0)
    cache_hit_tokens: int = Field(default=0, ge=0)
    cache_miss_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)


class DelegationBatch(BaseModel):
    """One ``delegate_tasks`` call: the decision, the children, and the isolation facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    round: int = Field(ge=1)
    route: DelegationRoute
    decision: DelegationDecision
    children: tuple[ChildOutcome, ...] = ()
    totals: DelegationTotals = DelegationTotals()
    batch_span_ms: float = Field(default=0.0, ge=0)
    max_observed_concurrency: int = Field(default=0, ge=0)
    result_chars: int = Field(default=0, ge=0)
    result_truncated: bool = False


class DelegationEvidence(BaseModel):
    """Run-level delegation evidence. Observation only; never an execution precondition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    enabled: bool = True
    limits: dict[str, object] = Field(default_factory=dict)
    batches: tuple[DelegationBatch, ...] = ()
    totals: DelegationTotals = DelegationTotals()
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def as_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class DelegationCaller(BaseModel):
    """The trusted caller identity as read from ``ExecutionContext`` attributes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str | None = None
    depth: int = 0
    is_worker: bool = False
    task_id: str | None = None
    root: str | None = None
    worker_id: str | None = None

    @property
    def is_main(self) -> bool:
        return self.role == "main" and self.depth == 0 and not self.is_worker


Mode = Literal["refuse", "execute"]
