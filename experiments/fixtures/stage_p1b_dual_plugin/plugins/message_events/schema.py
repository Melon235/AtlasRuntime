"""Native record of the message channel.

This channel is fed by a chat backend, so its identifiers are integers, its timestamps are epoch
milliseconds and its payload values are not necessarily strings. Nothing here converts anything:
``converters`` owns every value rule.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import MessageEventError

#: Fields a message must carry.
REQUIRED_FIELDS = ("id", "sent_at", "type")
#: Optional fields and their raw types.
OPTIONAL_FIELDS = ("priority", "labels", "payload")
#: ``type`` is an uppercase identifier: ``USER_JOIN``, ``BOT_ERROR``, ...
TYPE_PATTERN = r"^[A-Z][A-Z0-9_]{0,47}$"
#: Priority range the backend may send. ``None`` means "not stated".
PRIORITY_MIN = 1
PRIORITY_MAX = 5

#: Keys this plugin ignores, for forward compatibility with newer backends.
IGNORED_FIELDS = ("schema_version", "trace_id")


@dataclass(frozen=True, slots=True)
class MessageRecord:
    """One parsed message, in the channel's own terms."""

    id: int
    sent_at: int
    type: str
    priority: int | None = None
    labels: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)

    def as_mapping(self) -> dict[str, Any]:
        """The native wire form this plugin hands to the rest of the process."""

        return {
            "id": self.id,
            "sent_at": self.sent_at,
            "type": self.type,
            "priority": self.priority,
            "labels": list(self.labels),
            "payload": dict(self.payload),
        }


def validate_fields(raw: Mapping[str, Any], *, index: int | None = None) -> None:
    """Check presence and raw types. Values are never interpreted here."""

    for name in REQUIRED_FIELDS:
        if name not in raw:
            raise MessageEventError("missing_field", f"{name} is required", index=index)
    if not isinstance(raw["id"], int) or isinstance(raw["id"], bool):
        raise MessageEventError("bad_message_id", "id must be an integer", index=index)
    if raw["id"] <= 0:
        raise MessageEventError("bad_message_id", "id must be positive", index=index)
    if not isinstance(raw["sent_at"], int) or isinstance(raw["sent_at"], bool):
        raise MessageEventError(
            "wrong_type", "sent_at must be integer epoch milliseconds", index=index
        )
    if not isinstance(raw["type"], str):
        raise MessageEventError("wrong_type", "type must be a string", index=index)
    priority = raw.get("priority")
    if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool)):
        raise MessageEventError("wrong_type", "priority must be an integer", index=index)
    labels = raw.get("labels")
    if labels is not None and not isinstance(labels, (list, tuple)):
        raise MessageEventError("wrong_type", "labels must be a list", index=index)
    payload = raw.get("payload")
    if payload is not None and not isinstance(payload, Mapping):
        raise MessageEventError("wrong_type", "payload must be an object", index=index)
