"""The shared error vocabulary of the normalization layer.

Channel plugins raise their own errors; the shared layer never rewrites them into its own type,
because a caller that branches on ``FileEventError.code`` must keep seeing that code. These classes
exist for the failures that belong to the shared layer itself: an unregistered channel, a record the
unified model cannot represent, a plugin that cannot be registered.
"""

from __future__ import annotations

from typing import Any


class AdapterError(Exception):
    """A failure of the shared layer, carrying a stable machine-readable code."""

    code = "adapter_error"

    def __init__(self, message: str, *, code: str | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class UnknownSourceError(AdapterError):
    """No plugin is registered under the requested channel name."""

    code = "unknown_source"

    def __init__(self, source: str, *, known: tuple[str, ...] = ()) -> None:
        super().__init__(
            f"no plugin is registered for channel {source!r}; known channels: {list(known)}",
            code="unknown_source",
            detail={"source": source, "known": list(known)},
        )
        self.source = source
        self.known = known


class NormalizationError(AdapterError):
    """A channel-native record that the unified model refuses to represent."""

    def __init__(self, message: str, *, code: str = "normalization_failed", detail: Any = None):
        super().__init__(message, code=code, detail=detail)


class PluginRegistrationError(AdapterError):
    """A plugin cannot be registered: duplicate channel name, or no usable parser."""

    code = "plugin_registration_failed"


__all__ = [
    "AdapterError",
    "NormalizationError",
    "PluginRegistrationError",
    "UnknownSourceError",
]
