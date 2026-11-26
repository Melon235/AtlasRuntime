"""Codec registry (v1).

v1 knows the JSON codec only and a duplicate registration silently replaces the entry.
"""

from __future__ import annotations

from wire import json_message


class JsonCodec:
    name = "json"

    def decode(self, data: bytes):
        return json_message.decode(data)

    def decoder(self, limit: int | None = None):
        return json_message.Decoder() if limit is None else json_message.Decoder(limit)


_CODECS: dict[str, object] = {}


def register(codec: object) -> None:
    _CODECS[codec.name] = codec


def get(name: str):
    return _CODECS[name]


def available() -> list[str]:
    return list(_CODECS)


def decode(name: str, data: bytes):
    return get(name).decode(data)


register(JsonCodec())
