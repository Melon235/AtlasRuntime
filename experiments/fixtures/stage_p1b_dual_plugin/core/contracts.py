"""The unified event contract.

Everything downstream of normalization sees only :class:`NormalizedEvent`. Its rules are part of the
project's public surface, so they are stated here once and nowhere else:

``event_id``
    The event's identifier, globally unique across channels. A channel whose native identifier is
    already a string keeps it; a channel whose native identifier is a number is prefixed with the
    channel's short name, e.g. message ``1041`` becomes ``msg-1041``.
``source``
    The registered channel name (``file_events``, ``message_events``).
``kind``
    The event type in lowercase, dot-separated or underscored as the channel wrote it
    (``build.started``, ``user_join``).
``occurred_at``
    A timezone-aware timestamp, stored in UTC. A naive timestamp is never accepted: it cannot be
    ordered against the other channel's events.
``severity``
    Exactly one of :class:`Severity`. Channels translate their own vocabulary into these four words.
``attributes``
    Flat ``str -> str`` data. Values are always strings; a channel that carries numbers or booleans
    renders them through its own converter before they reach this model.
``tags``
    A tuple of non-empty lowercase strings, sorted and free of duplicates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .errors import NormalizationError


class Severity(StrEnum):
    """The only severity words the unified model knows."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


SEVERITY_VOCABULARY: tuple[str, ...] = tuple(item.value for item in Severity)


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    """One event, in the shape every consumer of ``eventpipe`` reads."""

    event_id: str
    source: str
    kind: str
    occurred_at: datetime
    severity: Severity
    attributes: Mapping[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise NormalizationError("event_id must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise NormalizationError(f"{self.source}: kind must be a non-empty string")

        occurred = self.occurred_at
        if not isinstance(occurred, datetime):
            raise NormalizationError(f"{self.source}: occurred_at must be a datetime")
        if occurred.tzinfo is None or occurred.utcoffset() is None:
            raise NormalizationError(
                f"{self.source}: occurred_at must be timezone-aware",
                code="naive_timestamp",
            )
        object.__setattr__(self, "occurred_at", occurred.astimezone(UTC))

        try:
            severity = Severity(self.severity)
        except ValueError:
            raise NormalizationError(
                f"{self.source}: severity {self.severity!r} is not one of "
                f"{list(SEVERITY_VOCABULARY)}",
                code="unknown_severity",
            ) from None
        object.__setattr__(self, "severity", severity)

        attributes = self.attributes or {}
        if not isinstance(attributes, Mapping):
            raise NormalizationError(f"{self.source}: attributes must be a mapping")
        for key, value in attributes.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise NormalizationError(
                    f"{self.source}: attributes must map strings to strings; "
                    f"got {key!r}: {type(value).__name__}",
                    code="non_string_attribute",
                )
        object.__setattr__(self, "attributes", dict(sorted(attributes.items())))

        tags = tuple(self.tags or ())
        for tag in tags:
            if not isinstance(tag, str) or not tag.strip():
                raise NormalizationError(f"{self.source}: tags must be non-empty strings")
        object.__setattr__(self, "tags", tags)

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready projection, used by the CLI and by the evidence exporters."""

        return {
            "event_id": self.event_id,
            "source": self.source,
            "kind": self.kind,
            "occurred_at": self.occurred_at.isoformat(),
            "severity": self.severity.value,
            "attributes": dict(self.attributes),
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class Plugin:
    """One registered channel: its name and the function that decodes a payload into native records.

    ``parse`` returns the channel's records already rendered as plain mappings (each plugin record
    exposes ``as_mapping``). It never normalizes: interpretation belongs to the shared layer.
    """

    source: str
    description: str
    parse: Callable[[Any], tuple[Mapping[str, Any], ...]]


__all__ = [
    "SEVERITY_VOCABULARY",
    "NormalizedEvent",
    "Plugin",
    "Severity",
]
