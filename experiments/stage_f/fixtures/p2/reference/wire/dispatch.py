"""Handler dispatch (v2): duplicate kinds rejected, unknown kind typed, sorted listing."""

from __future__ import annotations

from collections.abc import Callable

from wire.errors import ProtocolError, UnknownType
from wire.model import Frame, Message

Handler = Callable[[object], object]

_HANDLERS: dict[str, Handler] = {}


def register(kind: str, handler: Handler) -> None:
    if not isinstance(kind, str) or not kind:
        raise ProtocolError("handler kind must be a non-empty string")
    if not callable(handler):
        raise ProtocolError("handler must be callable")
    if kind in _HANDLERS:
        raise ProtocolError(f"handler for {kind!r} is already registered")
    _HANDLERS[kind] = handler


def _payload(decoded: Message | Frame) -> object:
    if isinstance(decoded, Message):
        return decoded.body
    if isinstance(decoded, Frame):
        return decoded.payload
    raise ProtocolError(f"{type(decoded).__name__} is not a decoded message or frame")


def dispatch(decoded: Message | Frame) -> object:
    kind = getattr(decoded, "kind", None)
    if not isinstance(kind, str) or not kind:
        raise ProtocolError("decoded value has no kind")
    if kind not in _HANDLERS:
        raise UnknownType(f"no handler for kind {kind!r}")
    return _HANDLERS[kind](_payload(decoded))


def kinds() -> list[str]:
    return sorted(_HANDLERS)
