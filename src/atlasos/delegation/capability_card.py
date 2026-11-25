"""Derived worker capability facts, for disclosure only.

Why this module exists
----------------------
Before Stage B+ the only description of a worker the Main could read was hand-written prose: a
constant ``delegate_tasks`` description plus, in the S2 prompt, a hand-maintained paragraph about
``research_worker``. Both are profile-independent constants, so neither can track a profile whose
tools or Host grant changed. This module derives the same facts from the objects that actually
authorise execution — :class:`atlasos.workers.models.WorkerProfile`, the run's registered tool
names, and the Host-owned write grant — so a disclosure cannot drift from the grant it describes.

What this module is *not*
-------------------------
It is not an authorization source. Nothing here is consulted by the router, the action gate, the
ownership policy or the worker factory. A card that disagrees with the runtime is a bug in the card;
the runtime decision is unchanged either way (see :data:`CARD_AUTHORITY`).

Every field is a mechanical projection of an input:

``allowed``          the profile's ``base_tools`` that the run's registry can actually execute.
``not_granted``      run-surface tools the profile does not carry (never "impossible", only "not
                     in this profile").
``conditional``      statements whose truth depends on the Host grant or on the shell classifier.
``max_turns``        ``min(profile.max_turns, child_max_turns)`` — the number the child agent is
                     actually built with, not the number a config file asked for.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from atlasos.tools.catalog import DEFAULT_CORE_TOOL_NAMES
from atlasos.workers.models import WorkerProfile

#: Read by an auditor, never by the runtime: a card confers no permission.
CARD_AUTHORITY = "disclosure_only"

CARD_SCHEMA_VERSION = "worker-capability-card/1.0"

#: Tools that make a profile write-capable. Mirrors ``delegation.service._WRITE_TOOLS`` and the
#: R1-1 classifier vocabulary; a profile is read-only when it has none of them.
WRITE_TOOLS: tuple[str, ...] = ("file_write", "file_edit")

#: A shell tool whose commands are textually classified by the ownership policy.
SHELL_TOOLS: tuple[str, ...] = ("bash",)

#: Tools only the delegation authority may hold.
AUTHORITY_TOOLS: tuple[str, ...] = ("delegate_tasks", "delegate.read")

#: Tools that exist on the Main's surface and are never part of any worker profile.
HOST_ONLY_TOOLS: tuple[str, ...] = ("run_public_tests",)

#: The reference vocabulary for "this worker profile does not have it": the six workspace tools, the
#: two control tools, the public suite and the delegation surface. A profile can never carry the
#: last three (the profile validator refuses delegation tools and the child tool filter removes
#: anything outside ``base_tools``), so naming them states a structural fact, not a per-run one.
REFERENCE_SURFACE: frozenset[str] = frozenset(
    set(DEFAULT_CORE_TOOL_NAMES) | set(HOST_ONLY_TOOLS) | set(AUTHORITY_TOOLS)
)


@dataclass(frozen=True)
class WorkerCapabilityCard:
    """One profile's derived capability facts, in the shape the Main can read."""

    profile_id: str
    role_purpose: str
    allowed: tuple[str, ...]
    not_granted: tuple[str, ...]
    conditional: tuple[str, ...]
    ownership: tuple[str, ...]
    task_input: tuple[str, ...]
    deliverable: tuple[str, ...]
    verification: tuple[str, ...]
    handoff: tuple[str, ...]
    use_cases: tuple[str, ...]
    max_turns: int
    can_delegate: bool

    @property
    def read_only(self) -> bool:
        return not (set(self.allowed) & set(WRITE_TOOLS))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": CARD_SCHEMA_VERSION,
            "authority": CARD_AUTHORITY,
            "role_id": self.profile_id,
            "role_purpose": self.role_purpose,
            "allowed_capabilities": list(self.allowed),
            "forbidden_capabilities": list(self.not_granted),
            "conditional_capabilities": list(self.conditional),
            "ownership_requirements": list(self.ownership),
            "task_input_contract": list(self.task_input),
            "deliverable_contract": list(self.deliverable),
            "verification_contract": list(self.verification),
            "handoff_semantics": list(self.handoff),
            "appropriate_use_cases": list(self.use_cases),
            "max_turns": self.max_turns,
            "can_delegate": self.can_delegate,
        }

    def digest(self) -> str:
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_card(
    profile: WorkerProfile,
    *,
    registered_tools: Iterable[str],
    surface_tools: Iterable[str],
    granted_write_paths: tuple[str, ...] = (),
    child_max_turns: int,
    result_preview_chars: int,
) -> WorkerCapabilityCard:
    """Project one trusted profile into the disclosure the Main may read.

    ``registered_tools`` is what the run can execute; ``surface_tools`` is what the Main itself was
    offered, which is the only meaningful reference set for "this worker does not have it".
    """

    registered = frozenset(registered_tools)
    surface = frozenset(surface_tools)
    allowed = tuple(name for name in profile.base_tools if name in registered)
    unavailable = tuple(name for name in profile.base_tools if name not in registered)
    not_granted = tuple(
        sorted(
            name
            for name in surface
            if name not in allowed
            and name not in unavailable
            and name != profile.profile_id
        )
    )
    effective_turns = min(profile.max_turns, child_max_turns)
    write_capable = bool(set(allowed) & set(WRITE_TOOLS))

    conditional: list[str] = []
    if write_capable:
        if granted_write_paths:
            conditional.append(
                "writes are accepted only inside the Host-granted paths "
                + ", ".join(granted_write_paths)
                + "; a task that declares any other path is refused before a worker starts"
            )
        else:
            conditional.append(
                "this run grants no write paths to this profile, so every write it attempts is "
                "denied by the ownership policy: treat it as read-only"
            )
    else:
        conditional.append(
            "read-only profile: it carries no write tool, so a task that declares write_paths is "
            "refused before a worker starts"
        )
    if set(allowed) & set(SHELL_TOOLS):
        conditional.append(
            "bash is available, but the ownership policy refuses any command whose writes it "
            "cannot bound, including every interpreter invocation (python, pytest, node, a "
            "script); read-only commands and commands writing only inside the granted paths are "
            "accepted"
        )
    else:
        conditional.append(
            "no shell tool: it cannot run commands, tests or interpreter probes of any kind"
        )
    if unavailable:
        conditional.append(
            "the profile names tools this run does not register, which are withheld: "
            + ", ".join(unavailable)
        )

    return WorkerCapabilityCard(
        profile_id=profile.profile_id,
        role_purpose=profile.role,
        allowed=allowed,
        not_granted=not_granted,
        conditional=tuple(conditional),
        ownership=(
            "every task declares the paths it intends to write in write_paths; the runtime refuses "
            "a declared path the Host has not granted, and records the refusal as a reason code",
            "a write outside the declared set is denied by the ownership policy at execution time, "
            "not by the prompt",
        ),
        task_input=(
            "objective: the bounded outcome this worker must produce",
            "background: everything the worker needs that is not in the objective. The worker sees "
            "no part of this conversation, so an empty background is refused at admission",
            "profile_id: this role id",
            "write_paths: only when the task must write, and only inside the granted paths",
        ),
        deliverable=(
            "a final report of what it actually did, which the runtime bounds to "
            f"{result_preview_chars} characters before it reaches this conversation",
            "the complete report stays behind a ref_id readable with delegate.read",
        ),
        verification=(
            "it may run the public suite only if run_public_tests appears in its tool list; when "
            "it does not, treat any claim about test results as unverified",
        ),
        handoff=(
            "one bounded summary plus a ref_id per worker; workers never see each other's results "
            "and this agent remains the only reader",
        ),
        use_cases=_use_cases(write_capable=write_capable, has_grant=bool(granted_write_paths)),
        max_turns=effective_turns,
        can_delegate=profile.can_delegate,
    )


def _use_cases(*, write_capable: bool, has_grant: bool) -> tuple[str, ...]:
    """Use cases implied by the tool set and the grant, never by a benchmark task."""

    common = (
        "independent investigation whose raw detail should not stay in this conversation",
        "locating symbols, interfaces and edge cases across many files",
        "compatibility or interface audits over an existing codebase",
    )
    if write_capable and has_grant:
        return (
            "implementing a bounded change inside the granted paths",
            "editing one module while this agent works on another",
            *common,
        )
    return common


__all__ = [
    "AUTHORITY_TOOLS",
    "CARD_AUTHORITY",
    "CARD_SCHEMA_VERSION",
    "HOST_ONLY_TOOLS",
    "REFERENCE_SURFACE",
    "SHELL_TOOLS",
    "WRITE_TOOLS",
    "WorkerCapabilityCard",
    "build_card",
]
