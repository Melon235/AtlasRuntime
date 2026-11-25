"""Render the worker-capability disclosure that travels with ``delegate_tasks``.

Two decisions are recorded here rather than in prose documents:

**The channel.** The disclosure is appended to the ``delegate_tasks`` tool description, not injected
into the Main system prompt. The system prompt is the frozen identity of every earlier stage
(P1/P2/P3 digests); the tool description is already a per-run parameter of
:class:`atlasos.delegation.tool.DelegateTasksTool`. Keeping the disclosure on the tool means an
ablation can change *what the Main knows about workers* without changing *the prompt under test*,
and ``none`` returns the shipped bytes unchanged.

**The ordering.** The shipped description is kept first, verbatim, and the disclosure is appended
after it. The stable prefix therefore survives a cache boundary that a prepended block would move.

``none`` is the default everywhere: no existing run, prompt digest or tool description changes
unless a caller asks for a level explicitly.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from enum import StrEnum

from atlasos.delegation.capability_card import WorkerCapabilityCard


class WorkerExposureLevel(StrEnum):
    """How much derived profile information accompanies ``delegate_tasks``."""

    #: The shipped description, byte for byte. No profile information at all.
    NONE = "none"
    #: One prose paragraph per offered profile: role, usable tools, missing tools, limits.
    NATIVE = "native"
    #: One structured card per offered profile, in the field order of the card schema.
    CARD = "card"


LEVELS: tuple[str, ...] = tuple(level.value for level in WorkerExposureLevel)

#: Appended to the description. It states the authority relationship once, for every level.
_AUTHORITY_NOTE = (
    "The profile information below is descriptive. The runtime decides admission, tool "
    "availability and write scope; a request that contradicts a profile is refused, not "
    "reinterpreted."
)


def render_exposure(
    level: str,
    cards: Sequence[WorkerCapabilityCard],
    *,
    base_description: str,
) -> str:
    """Return the exact description bytes for ``level``.

    ``none`` returns ``base_description`` unchanged — not a normalised copy of it. An ablation that
    does not ask for a disclosure cannot move the shipped bytes.
    """

    selected = WorkerExposureLevel(level)
    if selected is WorkerExposureLevel.NONE or not cards:
        return base_description
    if selected is WorkerExposureLevel.NATIVE:
        return f"{base_description}\n\n{_AUTHORITY_NOTE}\n\n{_native_block(cards)}"
    return f"{base_description}\n\n{_AUTHORITY_NOTE}\n\n{_card_block(cards)}"


def _native_block(cards: Sequence[WorkerCapabilityCard]) -> str:
    lines = ["WORKER PROFILES AVAILABLE TO THIS CALL"]
    for card in cards:
        lines.append("")
        lines.append(f"- {card.profile_id}: {card.role_purpose} (max {card.max_turns} turns)")
        lines.append(f"  Can use: {_join(card.allowed)}")
        lines.append(f"  Does not have: {_join(card.not_granted)}")
        for statement in card.conditional:
            lines.append(f"  Limit: {statement}")
        lines.append(f"  Delegation: {'may' if card.can_delegate else 'may not'} delegate further")
        lines.append(
            "  Returns: a bounded summary plus a ref_id; the full report is behind the ref_id"
        )
    return "\n".join(lines)


def _card_block(cards: Sequence[WorkerCapabilityCard]) -> str:
    lines = ["WORKER CAPABILITY CARDS"]
    for card in cards:
        lines.append("")
        lines.append(f"[{card.profile_id}] {card.role_purpose}")
        lines.append(f"role_id: {card.profile_id}")
        lines.append(f"max_turns: {card.max_turns}")
        _field(lines, "allowed_capabilities", card.allowed)
        _field(lines, "forbidden_capabilities", card.not_granted)
        _field(lines, "conditional_capabilities", card.conditional)
        _field(lines, "ownership_requirements", card.ownership)
        _field(lines, "task_input_contract", card.task_input)
        _field(lines, "deliverable_contract", card.deliverable)
        _field(lines, "verification_contract", card.verification)
        _field(lines, "handoff_semantics", card.handoff)
        _field(lines, "appropriate_use_cases", card.use_cases)
        lines.append(f"can_delegate: {'yes' if card.can_delegate else 'no'}")
    return "\n".join(lines)


def _field(lines: list[str], name: str, values: Sequence[str]) -> None:
    if not values:
        lines.append(f"{name}: none")
        return
    lines.append(f"{name}:")
    for value in values:
        lines.append(f"  - {value}")


def _join(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "none"


def exposure_evidence(
    level: str,
    description: str,
    cards: Sequence[WorkerCapabilityCard],
    *,
    base_description: str,
) -> dict[str, object]:
    """What the run records about this disclosure. Never used as an input to a decision."""

    return {
        "level": str(level),
        "levels": list(LEVELS),
        "chars": len(description),
        "digest": "sha256:" + hashlib.sha256(description.encode("utf-8")).hexdigest(),
        "base_chars": len(base_description),
        "base_digest": "sha256:" + hashlib.sha256(base_description.encode("utf-8")).hexdigest(),
        "appended_chars": len(description) - len(base_description),
        "profiles": [card.profile_id for card in cards],
        "cards": [
            {
                "role_id": card.profile_id,
                "digest": card.digest(),
                "read_only": card.read_only,
                "max_turns": card.max_turns,
            }
            for card in cards
        ],
        # Stated so a reader never has to infer it: the disclosure is not an authorization source.
        "authority": "disclosure_only",
    }


__all__ = [
    "LEVELS",
    "WorkerExposureLevel",
    "exposure_evidence",
    "render_exposure",
]
