"""Stage P: acceptance of a goal run, independent of what the Main says about it.

The oracle is two independent observations:

* **static** — the R1-2A :class:`VerificationEngine` checks the *exported* snapshot for declared
  properties (file present, function present, parses, ...). It reads the artifact, never the answer.
* **dynamic** — a fixed command the harness runs inside the still-live sandbox (``pytest`` in the
  stage-P pipeline). The Main has no tool for it and never sees its result.

A run passes only when both pass. "The command could not run" is ``INCONCLUSIVE``, never ``PASS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from atlasos.verification.engine import VerificationEngine
from atlasos.verification.models import VerificationSpec

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"


class SandboxCheckRunner(Protocol):
    """The only sandbox capability the oracle needs: run one fixed command.

    Narrow on purpose. The verifier cannot create, export or destroy a sandbox, so a bug here can
    never change the run's lifecycle or the exported artifact.
    """

    @property
    def ready(self) -> bool: ...

    async def run_check(self, command: str, *, timeout_seconds: int = ...) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GoalAcceptance:
    """The Host-owned acceptance contract for one goal run."""

    spec: VerificationSpec
    #: A fixed command executed in the sandbox by the harness, not by the model.
    dynamic_command: str | None = None
    dynamic_timeout_seconds: int = 300
    #: When ``True`` a missing dynamic result makes the run ``INCONCLUSIVE`` instead of ``PASS``.
    require_dynamic: bool = True


async def verify_goal(
    acceptance: GoalAcceptance,
    *,
    snapshot: Path,
    sandbox: SandboxCheckRunner | None,
    sandbox_failed: bool = False,
) -> dict[str, Any]:
    """Run the static and dynamic checks and return one verdict."""

    engine = VerificationEngine()
    result = await engine.verify_async(acceptance.spec, snapshot)
    static = result.as_dict()
    static_status = result.overall

    dynamic: dict[str, Any] | None = None
    if acceptance.dynamic_command:
        if sandbox is None or sandbox_failed or not sandbox.ready:
            dynamic = {
                "status": VERDICT_INCONCLUSIVE,
                "reason": "the sandbox is not available for the dynamic check",
                "command": acceptance.dynamic_command,
            }
        else:
            outcome = await sandbox.run_check(
                acceptance.dynamic_command,
                timeout_seconds=acceptance.dynamic_timeout_seconds,
            )
            passed = bool(outcome["ok"]) and outcome["exit_code"] == 0
            dynamic = {
                "status": VERDICT_PASS if passed else VERDICT_FAIL,
                **outcome,
            }

    dynamic_status = dynamic["status"] if dynamic is not None else None
    if static_status == "FAIL":
        verdict = VERDICT_FAIL
    elif static_status != "PASS":
        verdict = VERDICT_INCONCLUSIVE
    elif dynamic_status == VERDICT_FAIL:
        verdict = VERDICT_FAIL
    elif dynamic_status == VERDICT_PASS:
        verdict = VERDICT_PASS
    elif acceptance.require_dynamic:
        verdict = VERDICT_INCONCLUSIVE
    else:
        verdict = VERDICT_PASS

    return {
        "verdict": verdict,
        "spec_id": acceptance.spec.spec_id,
        "spec_digest": acceptance.spec.declared_digest or acceptance.spec.digest,
        "static_status": static_status,
        "static": static,
        "dynamic": dynamic,
        "snapshot": str(snapshot),
    }


__all__ = [
    "GoalAcceptance",
    "SandboxCheckRunner",
    "VERDICT_FAIL",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_PASS",
    "verify_goal",
]
