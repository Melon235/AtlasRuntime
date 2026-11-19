"""Reference implementation of ``PluginRegistry.dispatch``. HARNESS ONLY.

See ``reference/core/adapter.py``: this file is copied over a throwaway fixture checkout during the
offline verification and is never provisioned into a sandbox.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .adapter import normalize_event
from .contracts import NormalizedEvent, Plugin
from .errors import PluginRegistrationError, UnknownSourceError


class PluginRegistry:
    """Channel name -> plugin, plus dispatch."""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin) -> None:
        if not isinstance(plugin, Plugin):
            raise PluginRegistrationError(f"register() takes a Plugin, got {type(plugin).__name__}")
        if not plugin.source.strip():
            raise PluginRegistrationError("a plugin must declare a non-empty channel name")
        if not callable(plugin.parse):
            raise PluginRegistrationError(f"channel {plugin.source!r} has no usable parser")
        if plugin.source in self._plugins:
            raise PluginRegistrationError(f"channel {plugin.source!r} is already registered")
        self._plugins[plugin.source] = plugin

    def sources(self) -> tuple[str, ...]:
        return tuple(sorted(self._plugins))

    def plugin(self, source: str) -> Plugin:
        try:
            return self._plugins[source]
        except KeyError:
            raise UnknownSourceError(source, known=self.sources()) from None

    def parse(self, source: str, payload: Any) -> tuple[Mapping[str, Any], ...]:
        return self.plugin(source).parse(payload)

    def dispatch(self, source: str, payload: Any) -> tuple[NormalizedEvent, ...]:
        records = self.parse(source, payload)
        events = [normalize_event(source, record) for record in records]
        return tuple(sorted(events, key=lambda event: (event.occurred_at, event.event_id)))


def build_default_registry() -> PluginRegistry:
    from plugins.file_events import SOURCE as FILE_SOURCE
    from plugins.file_events import parse_document as parse_file_document
    from plugins.message_events import SOURCE as MESSAGE_SOURCE
    from plugins.message_events import parse_document as parse_message_document

    registry = PluginRegistry()
    registry.register(
        Plugin(
            source=FILE_SOURCE,
            description="JSON Lines document of build and deployment events",
            parse=lambda payload: tuple(
                record.as_mapping() for record in parse_file_document(payload)
            ),
        )
    )
    registry.register(
        Plugin(
            source=MESSAGE_SOURCE,
            description="chat backend message log",
            parse=lambda payload: tuple(
                record.as_mapping() for record in parse_message_document(payload)
            ),
        )
    )
    return registry


__all__ = ["PluginRegistry", "build_default_registry"]
