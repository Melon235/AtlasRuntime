"""Native record of the file channel, plus the structural rules it enforces.

The dataclass keeps the channel's own vocabulary: ``level`` is the raw level string as received
(``None`` when the line does not carry one), ``tags`` is the raw comma-separated string, and
``attrs`` is the flat string map. Interpreting those values is the job of ``converters``; this
module only decides whether a line is structurally acceptable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import FileEventError

#: ``ev-`` followed by eight lowercase hex characters.
EVENT_ID_PATTERN = re.compile(r"^ev-[0-9a-f]{8}$")
#: Lowercase dotted identifier, at most 32 characters.
KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")

#: Fields a line must carry. Everything else is optional.
REQUIRED_FIELDS = ("event_id", "ts", "kind")
#: Optional fields and their raw types.
OPTIONAL_FIELDS = ("level", "tags", "attrs")
#: Raw level spellings a line may use. ``None`` means "not stated".
RAW_LEVELS = ("trace", "debug", "info", "warn", "err", "fatal")

#: Keys this plugin ignores. Forward compatibility: a producer may add a field before the reader
#: knows about it, and dropping it is better than refusing the whole line.
IGNORED_FIELDS = ("schema_version", "producer")


@dataclass(frozen=True, slots=True)
class FileEventRecord:
    """One parsed line, in the channel's own terms."""

    event_id: str
    ts: str
    kind: str
    level: str | None = None
    tags: str | None = None
    attrs: Mapping[str, str] = field(default_factory=dict)

    def as_mapping(self) -> dict[str, Any]:
        """The native wire form this plugin hands to the rest of the process."""

        return {
            "event_id": self.event_id,
            "ts": self.ts,
            "kind": self.kind,
            "level": self.level,
            "tags": self.tags,
            "attrs": dict(self.attrs),
        }


def validate_fields(raw: Mapping[str, Any], *, line_number: int | None = None) -> None:
    """Check presence and raw types. Values are never interpreted here."""

    for name in REQUIRED_FIELDS:
        if name not in raw:
            raise FileEventError("missing_field", f"{name} is required", line_number=line_number)
        if not isinstance(raw[name], str):
            raise FileEventError("wrong_type", f"{name} must be a string", line_number=line_number)
    level = raw.get("level")
    if level is not None and not isinstance(level, str):
        raise FileEventError("wrong_type", "level must be a string", line_number=line_number)
    tags = raw.get("tags")
    if tags is not None and not isinstance(tags, str):
        raise FileEventError("wrong_type", "tags must be a string", line_number=line_number)
    attrs = raw.get("attrs")
    if attrs is not None:
        if not isinstance(attrs, Mapping):
            raise FileEventError("wrong_type", "attrs must be an object", line_number=line_number)
        for key, value in attrs.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise FileEventError(
                    "wrong_type",
                    "attrs must map strings to strings; this channel does not coerce values",
                    line_number=line_number,
                )
