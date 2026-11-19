"""The plugin registry and the single dispatch entry point.

Registration is explicit and Host-side: nothing is discovered by walking a directory, so a payload
can never introduce a channel. Dispatch is the only place where a payload becomes normalized events,
and it is the only place that decides their order.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import NormalizedEvent, Plugin
from .errors import PluginRegistrationError, UnknownSourceError


class PluginRegistry:
    """Channel name -> plugin, plus dispatch."""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    # -- registration --------------------------------------------------------

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
        """Registered channel names, sorted."""

        return tuple(sorted(self._plugins))

    def plugin(self, source: str) -> Plugin:
        try:
            return self._plugins[source]
        except KeyError:
            raise UnknownSourceError(source, known=self.sources()) from None

    # -- decoding ------------------------------------------------------------

    def parse(self, source: str, payload: Any) -> tuple[Mapping[str, Any], ...]:
        """Decode a payload into channel-native records. No normalization happens here."""

        return self.plugin(source).parse(payload)

    def dispatch(self, source: str, payload: Any) -> tuple[NormalizedEvent, ...]:
        """Decode and normalize one payload.

        The returned sequence is ordered by ``(occurred_at, event_id)`` across every event of the
        payload, so two consumers reading the same payload see the same order even when the channel
        itself emits another one.
        """

        # The channel is known and its records decode; only the normalization step is missing.
        self.parse(source, payload)
        raise NotImplementedError(
            "PluginRegistry.dispatch does not normalize yet: it must return unified events in "
            "event order"
        )


def build_default_registry() -> PluginRegistry:
    """The registry every entry point builds: both vendored channels, nothing else."""

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
