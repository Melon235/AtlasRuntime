"""Value rules of the file channel.

These four functions are the entire interpretation surface of the file channel, and they are public
API: the file reader CLI, the retention job and the channel's own tests all call them. A consumer
that re-implements any of them instead of calling it will drift from the channel's behaviour the
next time a producer adds a level spelling or changes the tag separator.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from .errors import FileEventError

#: Level a record is treated as when the line does not carry one.
DEFAULT_LEVEL = "info"

#: Raw level spelling -> the channel's canonical severity name.
#: ``warning``/``error`` are accepted as inputs even though no producer emits them today.
LEVEL_TO_SEVERITY: dict[str, str] = {
    "trace": "debug",
    "debug": "debug",
    "info": "info",
    "warn": "warning",
    "warning": "warning",
    "err": "error",
    "error": "error",
    "fatal": "error",
}


def severity_of(raw_level: str | None) -> str:
    """Resolve a raw level to one of ``debug``, ``info``, ``warning``, ``error``.

    An absent level is ``DEFAULT_LEVEL``; a level this channel does not know is a hard error, not a
    guess.
    """

    value = (raw_level or DEFAULT_LEVEL).strip().lower()
    try:
        return LEVEL_TO_SEVERITY[value]
    except KeyError:
        raise FileEventError(
            "unknown_level",
            f"level {raw_level!r} is not one of {sorted(LEVEL_TO_SEVERITY)}",
        ) from None


def parse_timestamp(value: str) -> datetime:
    """Parse the ISO-8601 timestamp of a line into an aware UTC datetime.

    This channel's producers always write an explicit offset. A timestamp without one is ambiguous
    and is refused rather than read as local time.
    """

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise FileEventError("wrong_type", f"ts is not ISO-8601: {value!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FileEventError("naive_timestamp", f"ts must carry an offset: {value!r}")
    return parsed.astimezone(UTC)


def split_tags(value: str | None) -> tuple[str, ...]:
    """Split the comma-separated tag string into a sorted, lowercased, duplicate-free tuple."""

    if not value:
        return ()
    seen = {item.strip().lower() for item in value.split(",")}
    return tuple(sorted(tag for tag in seen if tag))


def merge_attributes(attrs: Mapping[str, str] | None) -> dict[str, str]:
    """Copy the attribute map into a sorted plain dict (this channel never coerces values)."""

    if not attrs:
        return {}
    return {str(key): str(value) for key, value in sorted(attrs.items())}


__all__ = [
    "DEFAULT_LEVEL",
    "LEVEL_TO_SEVERITY",
    "merge_attributes",
    "parse_timestamp",
    "severity_of",
    "split_tags",
]
