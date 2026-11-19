"""Value rules of the message channel.

The backend speaks in priorities, epoch milliseconds and uppercase message types; every consumer of
this channel has always called the functions below to make sense of that. They are public API.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .errors import MessageEventError

#: Priority a message is treated as when the backend does not send one.
DEFAULT_PRIORITY = 3

#: Priority -> this channel's canonical severity name.
PRIORITY_TO_SEVERITY: dict[int, str] = {
    1: "debug",
    2: "info",
    3: "info",
    4: "warning",
    5: "error",
}


def coerce_priority(raw_priority: Any | None) -> int:
    """Return a priority in ``1..5``, defaulting to ``DEFAULT_PRIORITY`` when it is absent.

    A value outside the range is refused: clamping it would silently change how loudly the message
    is treated.
    """

    if raw_priority is None:
        return DEFAULT_PRIORITY
    if not isinstance(raw_priority, int) or isinstance(raw_priority, bool):
        raise MessageEventError("wrong_type", "priority must be an integer")
    if not PRIORITY_TO_SEVERITY.get(raw_priority):
        raise MessageEventError(
            "priority_out_of_range",
            f"priority {raw_priority} is outside {sorted(PRIORITY_TO_SEVERITY)}",
        )
    return raw_priority


def severity_of(raw_priority: Any | None) -> str:
    """Resolve a raw priority to one of ``debug``, ``info``, ``warning``, ``error``."""

    priority = coerce_priority(raw_priority)
    return PRIORITY_TO_SEVERITY[priority]


def parse_timestamp(value: Any) -> datetime:
    """Convert epoch milliseconds into an aware UTC datetime."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise MessageEventError("wrong_type", f"sent_at must be integer milliseconds: {value!r}")
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise MessageEventError("wrong_type", f"sent_at is out of range: {value!r}") from error


def normalize_labels(labels: Sequence[str] | None) -> tuple[str, ...]:
    """Return the labels as a sorted, lowercased, duplicate-free tuple of non-empty strings."""

    if not labels:
        return ()
    seen = {str(label).strip().lower() for label in labels}
    return tuple(sorted(label for label in seen if label))


def stringify_value(value: Any) -> str:
    """Render one payload value as a stable string.

    The backend sends booleans, numbers and nested objects; consumers of this channel expect
    strings, and the mapping below is the documented one (``true``/``false``, shortest float form,
    compact JSON for containers).
    """

    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def stringify_payload(payload: Mapping[str, Any] | None) -> dict[str, str]:
    """Render the payload as a sorted string map. ``None`` values are dropped, not rendered."""

    if not payload:
        return {}
    rendered: dict[str, str] = {}
    for key, value in payload.items():
        if value is None:
            continue
        rendered[str(key)] = stringify_value(value)
    return dict(sorted(rendered.items()))


__all__ = [
    "DEFAULT_PRIORITY",
    "PRIORITY_TO_SEVERITY",
    "coerce_priority",
    "normalize_labels",
    "parse_timestamp",
    "severity_of",
    "stringify_payload",
    "stringify_value",
]
