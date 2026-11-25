"""Stage P: acceptance of a goal run, independent of what the Main says about it.

The oracle is two independent observations:

* **static** — the R1-2A :class:`VerificationEngine` checks the *exported* snapshot for declared
  properties (file present, function present, parses, ...). It reads the artifact, never the answer.
* **dynamic** — a fixed command the harness runs inside the still-live sandbox (``pytest`` in the
  stage-P pipeline). The Main has no tool for it and never sees its result.

A run passes only when both pass. "The command could not run" is ``INCONCLUSIVE``, never ``PASS``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from atlasos.verification.engine import VerificationEngine
from atlasos.verification.models import Category, CheckSpec, VerificationSpec

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

#: Where correctness comes from.
#:
#: ``IN_RUN_ORACLE`` is the stage-P shape: the harness runs a fixed command inside the still-live
#: sandbox and the run passes only when the static and dynamic observations both pass.
#:
#: ``EXTERNAL_SANDBOX_ORACLE`` is the stage-B+ shape: the run itself is scored afterwards, by a Host
#: oracle that runs in its own sandbox on the *exported* workspace. There is no in-run dynamic
#: command, so a preflight that demands one refuses every cell of that design. The mode is declared
#: here, by the Host-owned contract, and never inferred at runtime.
ORACLE_MODE_IN_RUN = "IN_RUN_ORACLE"
ORACLE_MODE_EXTERNAL = "EXTERNAL_SANDBOX_ORACLE"
ORACLE_MODES: tuple[str, ...] = (ORACLE_MODE_IN_RUN, ORACLE_MODE_EXTERNAL)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExternalOracleContract:
    """The Host's declaration of the oracle that decides correctness *after* the run.

    It is a declaration, not a proof: it says which oracle, on which artifact, over which paths, in
    which sandbox. ``contract_digest`` is recomputed from ``body`` so a contract that names one
    oracle and carries another cannot verify. Nothing here runs the oracle.
    """

    oracle_id: str
    task_id: str
    artifact_root: str
    body: Mapping[str, Any] = field(default_factory=dict)
    hidden_paths: tuple[str, ...] = ()
    public_paths: tuple[str, ...] = ()
    sandbox_required: bool = True
    independent_sandbox: bool = True
    #: The digest the Host recorded when it authored the contract. Kept separate from ``body`` so a
    #: body that was edited after signing no longer matches the identity that was signed.
    declared_digest: str = ""

    @property
    def contract_digest(self) -> str:
        return _digest(dict(self.body))

    def problems(self) -> list[str]:
        """What makes this declaration unusable as an admission basis."""

        problems: list[str] = []
        if not self.oracle_id.strip():
            problems.append("the external oracle has no identity")
        if not self.task_id.strip():
            problems.append("the external oracle is not bound to a task")
        if not self.artifact_root.strip():
            problems.append("the external oracle does not name the artifact it scores")
        if not self.body:
            problems.append("the external oracle carries no contract body")
        if not self.declared_digest:
            problems.append("the external oracle declares no contract digest")
        elif self.declared_digest != self.contract_digest:
            problems.append(
                f"the external oracle digest is {self.declared_digest}, its body hashes to "
                f"{self.contract_digest}"
            )
        if not self.hidden_paths:
            problems.append("the external oracle names no hidden paths")
        if not self.sandbox_required:
            problems.append("the external oracle does not require a sandbox")
        if not self.independent_sandbox:
            problems.append("the external oracle is not declared independent of the agent sandbox")
        overlap = sorted(set(self.hidden_paths) & set(self.public_paths))
        if overlap:
            problems.append(f"the hidden paths overlap the public suite: {overlap[:3]}")
        return problems


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
    #: Which observation decides correctness. Defaults to the stage-P in-run oracle so every
    #: existing caller keeps exactly the semantics it had before this field existed.
    oracle_mode: str = ORACLE_MODE_IN_RUN
    #: Present only for ``EXTERNAL_SANDBOX_ORACLE``; the Host declaration the preflight verifies.
    external_oracle: ExternalOracleContract | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> GoalAcceptance:
        """Load a Host-authored contract from JSON (``spec`` + ``dynamic_command``).

        The digest is recomputed from the content: a payload cannot declare one digest and carry
        another set of checks.
        """

        raw = dict(payload["spec"])
        spec = VerificationSpec(
            spec_id=str(raw["spec_id"]),
            version=str(raw["version"]),
            deliverable_type=str(raw["deliverable_type"]),
            required_artifacts=tuple(raw.get("required_artifacts") or ()),
            checks=tuple(
                CheckSpec(
                    check_id=str(item["check_id"]),
                    category=cast(Category, str(item["category"])),
                    kind=str(item["kind"]),
                    artifact=item.get("artifact"),
                    params=dict(item.get("params") or {}),
                    authoritative=bool(item.get("authoritative", True)),
                )
                for item in raw.get("checks") or ()
            ),
            notes=str(raw.get("notes") or ""),
        ).with_declared_digest()
        command = payload.get("dynamic_command")
        external_raw = payload.get("external_oracle")
        external = None
        if isinstance(external_raw, Mapping):
            external = ExternalOracleContract(
                oracle_id=str(external_raw.get("oracle_id") or ""),
                task_id=str(external_raw.get("task_id") or ""),
                artifact_root=str(external_raw.get("artifact_root") or ""),
                body=dict(external_raw.get("body") or {}),
                hidden_paths=tuple(str(item) for item in external_raw.get("hidden_paths") or ()),
                public_paths=tuple(str(item) for item in external_raw.get("public_paths") or ()),
                sandbox_required=bool(external_raw.get("sandbox_required", True)),
                independent_sandbox=bool(external_raw.get("independent_sandbox", True)),
                declared_digest=str(external_raw.get("declared_digest") or ""),
            )
        return cls(
            spec=spec,
            dynamic_command=str(command) if command else None,
            dynamic_timeout_seconds=int(payload.get("dynamic_timeout_seconds", 300)),
            require_dynamic=bool(payload.get("require_dynamic", True)),
            oracle_mode=str(payload.get("oracle_mode") or ORACLE_MODE_IN_RUN),
            external_oracle=external,
        )


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

    external = acceptance.external_oracle
    return {
        "verdict": verdict,
        "spec_id": acceptance.spec.spec_id,
        "spec_digest": acceptance.spec.declared_digest or acceptance.spec.digest,
        "static_status": static_status,
        "static": static,
        "dynamic": dynamic,
        "snapshot": str(snapshot),
        "oracle_mode": acceptance.oracle_mode,
        # A static PASS is not a correctness verdict when the oracle is external. The field is here
        # so no reader of this record can mistake the two, whatever they do with ``verdict``.
        "final_correctness": (
            "EXTERNAL_ORACLE_PENDING"
            if acceptance.oracle_mode == ORACLE_MODE_EXTERNAL
            else verdict
        ),
        "external_oracle": (
            None
            if external is None
            else {
                "oracle_id": external.oracle_id,
                "task_id": external.task_id,
                "contract_digest": external.contract_digest,
                "artifact_root": external.artifact_root,
            }
        ),
    }


__all__ = [
    "ExternalOracleContract",
    "GoalAcceptance",
    "ORACLE_MODES",
    "ORACLE_MODE_EXTERNAL",
    "ORACLE_MODE_IN_RUN",
    "SandboxCheckRunner",
    "VERDICT_FAIL",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_PASS",
    "verify_goal",
]
