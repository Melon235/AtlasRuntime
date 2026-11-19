"""Failure vocabulary of the file channel.

The ``code`` attribute is part of the plugin's public API: callers branch on it, so a record that
fails for a known reason must keep reporting the same code.
"""

from __future__ import annotations

#: Every code this plugin may report. Nothing generates a new one at runtime.
FILE_ERROR_CODES = (
    "bad_json",
    "bad_event_id",
    "bad_kind",
    "missing_field",
    "wrong_type",
    "naive_timestamp",
    "unknown_level",
)


class FileEventError(ValueError):
    """A file record that cannot be parsed, with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, line_number: int | None = None) -> None:
        if code not in FILE_ERROR_CODES:
            raise ValueError(f"unknown FileEventError code: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.line_number = line_number

    def __str__(self) -> str:
        if self.line_number is not None:
            return f"{self.code} (line {self.line_number}): {self.message}"
        return f"{self.code}: {self.message}"
