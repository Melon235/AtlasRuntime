"""Handler dispatch (v1): duplicate kinds overwrite and an unknown kind is a KeyError."""

from __future__ import annotations

from collections.abc import Callable

_HANDLERS: dict[str, Callable[[object], object]] = {}


def register(kind: str, handler: Callable[[object], object]) -> None:
    _HANDLERS[kind] = handler


def dispatch(decoded) -> object:
    payload = decoded.body if hasattr(decoded, "body") else decoded.payload
    return _HANDLERS[decoded.kind](payload)


def kinds() -> list[str]:
    return list(_HANDLERS)
