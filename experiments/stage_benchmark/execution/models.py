"""Identity and result types for the Stage B per-cell execution layer.

These types are deliberately separate from the frozen protocol v1.1: a cell is an *instance* of a
plan entry, and an attempt is a one-shot claim on that instance. Nothing here changes how a run is
analysed; the analyser stays ``reconstruct.py`` ``stage-b-reconstruct/1.1``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The thirteen outcomes the execution layer must be able to name. ``none`` is a completed run;
#: the rest are terminal, and none of them authorises a second provider call.
FAILURE_KINDS = (
    "none",
    "sandbox_creation",
    "provider_exception",
    "budget_exhaustion",
    "main_max_turns",
    "router_rejected",
    "child_failure",
    "handoff_missing",
    "public_tests_failed",
    "oracle_failed",
    "collector_failure",
    "aggregation_failure",
    "sandbox_cleanup",
    "goal_unknown",
)


class ExecutionBlocked(RuntimeError):
    """Preflight, identity verification or authorization refused the run."""


class AttemptAlreadyStarted(RuntimeError):
    """An attempt marker already exists for this cell. Never delete it to retry."""


class EvidenceAlreadyExists(RuntimeError):
    """An evidence bundle already exists for this cell. Never overwrite it."""


class CellFailed(RuntimeError):
    """A run ended in one of the named failure kinds. Carries the evidence that was kept."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class CellIdentity:
    """The full frozen identity of one matrix cell: task + arm + repetition + cell_id."""

    task_id: str
    arm_id: str
    repetition: int
    cell_id: str
    round: int = 0
    order: int = 0
    #: The matrix cell is one of the 36. A calibration run is deliberately outside it.
    in_matrix: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "arm_id": self.arm_id,
            "repetition": self.repetition,
            "cell_id": self.cell_id,
            "round": self.round,
            "order": self.order,
            "in_matrix": self.in_matrix,
        }


@dataclass(frozen=True)
class Attempt:
    """A durable one-shot claim, written with O_CREAT | O_EXCL before any provider call."""

    cell: CellIdentity
    state: str
    claimed_at: str
    preflight_digest: str
    authorization_digest: str
    runner_source_digest: str
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cell": self.cell.to_dict(),
            "state": self.state,
            "claimed_at": self.claimed_at,
            "preflight_digest": self.preflight_digest,
            "authorization_digest": self.authorization_digest,
            "runner_source_digest": self.runner_source_digest,
            "retries_allowed": False,
        }
        if self.path is not None:
            payload["path"] = str(self.path)
        return payload


@dataclass(frozen=True)
class CellRequest:
    """Everything one cell execution needs, including explicit offline seams.

    ``provider_factory`` and ``sandbox_factory`` are only set by the offline harness. The live CLI
    leaves both ``None``; ``run_goal`` then performs its own live preflight and never falls back to
    a fake provider. ``run_fn`` is the narrowest test seam: it defaults to ``run_goal`` and may be
    replaced by an offline simulation that returns a synthetic session.
    """

    cell: CellIdentity
    runs_root: Path
    evidence_root: Path
    live: bool = False
    authorization: Mapping[str, Any] | None = None
    authorization_digest: str = "UNAUTHORIZED"
    provider_factory: Callable[[], Any] | None = None
    sandbox_factory: Callable[[Path], Any] | None = None
    run_fn: Callable[[Any], Awaitable[dict[str, Any]]] | None = None
    protocol_document: Mapping[str, Any] | None = None
    #: Offline-only seam for a synthetic Host Oracle result. The live CLI never sets this.
    host_oracle: Mapping[str, Any] | None = None
    #: The evidence root dedicated to calibration. Live calls must supply it explicitly.
    calibration: bool = False


@dataclass(frozen=True)
class CellResult:
    """The terminal outcome of one cell. Failed runs still carry their evidence paths."""

    cell: CellIdentity
    ok: bool
    status: str
    failure_kind: str = "none"
    detail: str = ""
    evidence_dir: Path | None = None
    analysis_path: Path | None = None
    gaps: tuple[str, ...] = field(default_factory=tuple)
    digests: Mapping[str, str] = field(default_factory=dict)
    execution_integrity: str = "UNKNOWN"
    goal_correctness: str = "UNKNOWN"
    delegation_diagnostics: str = "NOT_APPLICABLE"
    oracle_agreement: str = "UNKNOWN"
    host_oracle_source: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell": self.cell.to_dict(),
            "ok": self.ok,
            "status": self.status,
            "failure_kind": self.failure_kind,
            "detail": self.detail,
            "evidence_dir": str(self.evidence_dir) if self.evidence_dir else None,
            "analysis_path": str(self.analysis_path) if self.analysis_path else None,
            "evidence_gaps": list(self.gaps),
            "digests": dict(self.digests),
            "execution_integrity": self.execution_integrity,
            "goal_correctness": self.goal_correctness,
            "delegation_diagnostics": self.delegation_diagnostics,
            "oracle_agreement": self.oracle_agreement,
            "host_oracle_source": self.host_oracle_source,
        }
