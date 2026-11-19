"""The message channel: a chat backend's message log.

This package is vendored. It knows nothing about ``core`` and must keep working on its own.
"""

from __future__ import annotations

from .converters import (
    DEFAULT_PRIORITY,
    PRIORITY_TO_SEVERITY,
    coerce_priority,
    normalize_labels,
    parse_timestamp,
    severity_of,
    stringify_payload,
    stringify_value,
)
from .errors import MESSAGE_ERROR_CODES, MessageEventError
from .parser import parse_document, parse_message, parse_path
from .schema import PRIORITY_MAX, PRIORITY_MIN, REQUIRED_FIELDS, TYPE_PATTERN, MessageRecord

#: The channel name this package is registered under.
SOURCE = "message_events"

__all__ = [
    "DEFAULT_PRIORITY",
    "MESSAGE_ERROR_CODES",
    "MessageEventError",
    "MessageRecord",
    "PRIORITY_MAX",
    "PRIORITY_MIN",
    "PRIORITY_TO_SEVERITY",
    "REQUIRED_FIELDS",
    "SOURCE",
    "TYPE_PATTERN",
    "coerce_priority",
    "normalize_labels",
    "parse_document",
    "parse_message",
    "parse_path",
    "parse_timestamp",
    "severity_of",
    "stringify_payload",
    "stringify_value",
]
