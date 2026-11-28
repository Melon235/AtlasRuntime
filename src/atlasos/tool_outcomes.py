"""Trusted metadata for workspace errors with a known execution phase."""

from typing import Literal

from anycode.types import ToolResult


class KnownToolError(ToolResult):
    """The tool can prove either no invocation or a completed read-only check."""

    phase: Literal["REJECTED_BEFORE_EXECUTION", "OBSERVED_NO_MUTATION"]
    is_error: Literal[True] = True
    retry_safe: Literal[True] = True
