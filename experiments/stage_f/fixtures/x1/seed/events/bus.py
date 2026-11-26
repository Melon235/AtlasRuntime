"""In-process event bus (frozen)."""

from __future__ import annotations

from collections.abc import Callable


class EventBus:
    """Synchronous publish/subscribe."""

    def __init__(self) -> None:
        self._callbacks: list[Callable[[dict], object]] = []

    @property
    def subscriber_count(self) -> int:
        return len(self._callbacks)

    def subscribe(self, callback: Callable[[dict], object]) -> Callable[[dict], object]:
        if not callable(callback):
            raise ValueError("callback must be callable")
        self._callbacks.append(callback)
        return callback

    def publish(self, event: dict) -> list[object]:
        if not isinstance(event, dict):
            raise ValueError("event must be a dict")
        return [callback(event) for callback in self._callbacks]
