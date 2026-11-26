"""Codec registry (v2): two protocols, duplicates rejected, typed lookup failures."""

from __future__ import annotations

from typing import Protocol

from wire import frame, json_message
from wire.errors import ProtocolError


class Codec(Protocol):
    name: str

    def decode(self, data: bytes):
        ...

    def decoder(self, limit: int | None = None):
        ...


class JsonCodec:
    name = "json"

    def decode(self, data: bytes) -> json_message.Message:
        return json_message.decode(data)

    def decoder(self, limit: int | None = None) -> json_message.Decoder:
        return json_message.Decoder() if limit is None else json_message.Decoder(limit)


class BinaryCodec:
    name = "binary"

    def decode(self, data: bytes) -> frame.Frame:
        return frame.decode(data)

    def decoder(self, limit: int | None = None) -> frame.Decoder:
        return frame.Decoder() if limit is None else frame.Decoder(limit)


_CODECS: dict[str, Codec] = {}


def register(codec: Codec) -> None:
    name = getattr(codec, "name", None)
    if not isinstance(name, str) or not name:
        raise ProtocolError("codec name must be a non-empty string")
    if name in _CODECS:
        raise ProtocolError(f"codec {name!r} is already registered")
    _CODECS[name] = codec


def get(name: str) -> Codec:
    if name not in _CODECS:
        raise ProtocolError(f"unknown codec {name!r}")
    return _CODECS[name]


def available() -> list[str]:
    return sorted(_CODECS)


def decode(name: str, data: bytes):
    return get(name).decode(data)


def decoder(name: str, limit: int | None = None):
    return get(name).decoder(limit)


register(JsonCodec())
register(BinaryCodec())
