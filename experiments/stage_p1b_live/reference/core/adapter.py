"""Reference implementation of the unified path. HARNESS ONLY.

This is a complete replacement for the fixture's ``core/adapter.py`` with the two holes filled, and
``reference/core/registry.py`` is the same for ``PluginRegistry.dispatch``. The offline verifier
copies both over a throwaway fixture checkout to prove the task is solvable and that the hidden
oracle can tell a correct implementation from a wrong one. Neither file is ever provisioned into a
sandbox or copied into ``experiments/fixtures``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .contracts import NormalizedEvent, Severity
from .errors import NormalizationError, UnknownSourceError

#: A channel-native mapping -> unified event.
Normalizer = Callable[[Mapping[str, Any]], NormalizedEvent]

_NORMALIZERS: dict[str, Normalizer] = {}


def register_normalizer(source: str, normalizer: Normalizer) -> None:
    """Bind a channel name to its normalizer. Re-registration replaces the previous binding."""

    if not isinstance(source, str) or not source.strip():
        raise ValueError("a normalizer must be registered under a non-empty channel name")
    if not callable(normalizer):
        raise ValueError(f"the normalizer for {source!r} is not callable")
    _NORMALIZERS[source] = normalizer


def supported_sources() -> tuple[str, ...]:
    """Registered channel names, sorted."""

    return tuple(sorted(_NORMALIZERS))


def normalize_event(source: str, raw: Mapping[str, Any]) -> NormalizedEvent:
    """Turn one channel-native record into a :class:`NormalizedEvent`."""

    normalizer = _NORMALIZERS.get(source)
    if normalizer is None:
        raise UnknownSourceError(source, known=supported_sources())
    if not isinstance(raw, Mapping):
        raise NormalizationError(
            f"{source}: a native record must be a mapping, got {type(raw).__name__}",
            code="wrong_record_type",
        )
    return normalizer(raw)


def normalize_file_event(record: Mapping[str, Any]) -> NormalizedEvent:
    """``file_events`` native mapping -> unified event."""

    from plugins.file_events import (
        merge_attributes,
        parse_timestamp,
        severity_of,
        split_tags,
    )

    event_id = record.get("event_id")
    kind = record.get("kind")
    if not isinstance(event_id, str) or not isinstance(kind, str):
        raise NormalizationError("file_events: event_id and kind must be strings")
    return NormalizedEvent(
        event_id=event_id,
        source="file_events",
        kind=kind,
        occurred_at=parse_timestamp(str(record.get("ts"))),
        severity=Severity(severity_of(record.get("level"))),
        attributes=merge_attributes(record.get("attrs")),
        tags=split_tags(record.get("tags")),
    )


def normalize_message_event(record: Mapping[str, Any]) -> NormalizedEvent:
    """``message_events`` native mapping -> unified event."""

    from plugins.message_events import (
        normalize_labels,
        parse_timestamp,
        severity_of,
        stringify_payload,
    )

    native_id = record.get("id")
    if not isinstance(native_id, int) or isinstance(native_id, bool):
        raise NormalizationError("message_events: id must be an integer")
    kind = record.get("type")
    if not isinstance(kind, str):
        raise NormalizationError("message_events: type must be a string")
    return NormalizedEvent(
        event_id=f"msg-{native_id}",
        source="message_events",
        kind=kind.lower(),
        occurred_at=parse_timestamp(record.get("sent_at")),
        severity=Severity(severity_of(record.get("priority"))),
        attributes=stringify_payload(record.get("payload")),
        tags=normalize_labels(record.get("labels")),
    )


def _install_defaults() -> None:
    from plugins.file_events import SOURCE as FILE_SOURCE
    from plugins.message_events import SOURCE as MESSAGE_SOURCE

    register_normalizer(FILE_SOURCE, normalize_file_event)
    register_normalizer(MESSAGE_SOURCE, normalize_message_event)


_install_defaults()


__all__ = [
    "Normalizer",
    "normalize_event",
    "normalize_file_event",
    "normalize_message_event",
    "register_normalizer",
    "supported_sources",
]
