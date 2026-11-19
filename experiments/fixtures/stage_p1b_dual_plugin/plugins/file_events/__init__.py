"""The file channel: a JSON Lines document of build and deployment events.

This package is vendored. It knows nothing about ``core`` and must keep working on its own.
"""

from __future__ import annotations

from .converters import (
    DEFAULT_LEVEL,
    LEVEL_TO_SEVERITY,
    merge_attributes,
    parse_timestamp,
    severity_of,
    split_tags,
)
from .errors import FILE_ERROR_CODES, FileEventError
from .parser import parse_document, parse_line, parse_path
from .schema import EVENT_ID_PATTERN, KIND_PATTERN, REQUIRED_FIELDS, FileEventRecord

#: The channel name this package is registered under.
SOURCE = "file_events"

__all__ = [
    "DEFAULT_LEVEL",
    "EVENT_ID_PATTERN",
    "FILE_ERROR_CODES",
    "FileEventError",
    "FileEventRecord",
    "KIND_PATTERN",
    "LEVEL_TO_SEVERITY",
    "REQUIRED_FIELDS",
    "SOURCE",
    "merge_attributes",
    "parse_document",
    "parse_line",
    "parse_path",
    "parse_timestamp",
    "severity_of",
    "split_tags",
]
