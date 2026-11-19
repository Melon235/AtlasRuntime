"""Failure vocabulary of the message channel.

Note that this channel reports ``priority_out_of_range`` where the file channel would report
``unknown_level``: the two packages were written by different teams and their codes are not
interchangeable.
"""

from __future__ import annotations

#: Every code this plugin may report.
MESSAGE_ERROR_CODES = (
    "bad_json",
    "bad_message_id",
    "wrong_type",
    "missing_field",
    "priority_out_of_range",
    "naive_timestamp",
)


class MessageEventError(ValueError):
    """A message record that cannot be parsed, with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, index: int | None = None) -> None:
        if code not in MESSAGE_ERROR_CODES:
            raise ValueError(f"unknown MessageEventError code: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.index = index

    def __str__(self) -> str:
        if self.index is not None:
            return f"{self.code} (message {self.index}): {self.message}"
        return f"{self.code}: {self.message}"
