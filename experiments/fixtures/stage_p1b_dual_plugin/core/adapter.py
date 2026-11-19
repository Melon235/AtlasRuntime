"""Channel-independent normalization.

This is the only module in the project that knows more than one channel exists. Each channel keeps
its own vocabulary in its own package; the job here is to translate a channel-native record into the
unified model from :mod:`core.contracts` without inventing rules the channel does not already
define.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .contracts import NormalizedEvent
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
    """Turn one channel-native record into a :class:`NormalizedEvent`.

    The record is a mapping as the plugin produced it (``Plugin.parse``); an unknown channel and a
    record that is not a mapping are shared-layer failures and are refused here. Everything else —
    defaults, aliases, timestamp units, attribute rendering — belongs to the channel.
    """

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
    """``file_events`` native mapping -> unified event.

    Use the file channel's own converter functions; a rule re-derived here will disagree with the
    channel the first time a producer adds a level spelling.
    """

    raise NotImplementedError("file_events records are not normalized yet")


def normalize_message_event(record: Mapping[str, Any]) -> NormalizedEvent:
    """``message_events`` native mapping -> unified event.

    The message channel's native identifier is numeric and its payload values are not strings; both
    facts are handled by the channel's own converters, not here.
    """

    raise NotImplementedError("message_events records are not normalized yet")


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
