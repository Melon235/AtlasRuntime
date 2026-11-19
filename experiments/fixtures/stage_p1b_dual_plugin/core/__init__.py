"""The shared layer of ``eventpipe``: the unified contract, the adapter and the registry."""

from __future__ import annotations

from .adapter import (
    normalize_event,
    normalize_file_event,
    normalize_message_event,
    register_normalizer,
    supported_sources,
)
from .contracts import SEVERITY_VOCABULARY, NormalizedEvent, Plugin, Severity
from .errors import (
    AdapterError,
    NormalizationError,
    PluginRegistrationError,
    UnknownSourceError,
)
from .registry import PluginRegistry, build_default_registry

__all__ = [
    "SEVERITY_VOCABULARY",
    "AdapterError",
    "NormalizationError",
    "NormalizedEvent",
    "Plugin",
    "PluginRegistrationError",
    "PluginRegistry",
    "Severity",
    "UnknownSourceError",
    "build_default_registry",
    "normalize_event",
    "normalize_file_event",
    "normalize_message_event",
    "register_normalizer",
    "supported_sources",
]
