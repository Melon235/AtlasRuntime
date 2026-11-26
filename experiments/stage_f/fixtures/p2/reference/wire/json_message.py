"""JSON message protocol (v2).

One versioned JSON document per line, an optional v1 form, a body CRC and a streaming decoder that
accepts any split of the input.
"""

from __future__ import annotations

from typing import Any

from wire.checksum import crc32, verify
from wire.errors import ChecksumError, ProtocolError, Truncated, VersionError
from wire.json_fields import canonical_body, encode_document, loads
from wire.model import Message

VERSION = 2


def _kind(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError("message type must be a non-empty string")
    return value


def _body(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("message body must be an object")
    return value


def encode(kind: str, body: dict[str, Any], *, identifier: str) -> bytes:
    """Encode one v2 message, newline terminated."""

    if not isinstance(identifier, str) or not identifier:
        raise ProtocolError("message id must be a non-empty string")
    document = {
        "v": VERSION,
        "type": _kind(kind),
        "id": identifier,
        "body": _body(body),
        "crc": crc32(canonical_body(body)),
    }
    return encode_document(document) + b"\n"


def _payload_text(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError(f"message is not utf-8: {error}") from error
    stripped = text.strip("\r\n")
    if not stripped:
        raise ProtocolError("empty message")
    if "\n" in stripped or "\r" in stripped:
        raise ProtocolError("expected exactly one message")
    return stripped


def decode(data: bytes) -> Message:
    """Decode exactly one message."""

    text = _payload_text(data)
    document = loads(text)
    if not isinstance(document, dict):
        raise ProtocolError("message root must be an object")

    version = document.get("v")
    if isinstance(version, bool) or not isinstance(version, int) or version not in (1, VERSION):
        raise VersionError(f"unsupported message version {version!r}")

    kind = _kind(document.get("type"))
    body = _body(document.get("body"))
    if version == 1:
        identifier = document.get("id", "")
        if not isinstance(identifier, str):
            raise ProtocolError("message id must be a string")
        return Message(version=1, kind=kind, identifier=identifier, body=body, checksum=None)

    identifier = document.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ProtocolError("v2 message id must be a non-empty string")
    declared = document.get("crc")
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise ChecksumError("v2 message requires an integer crc")
    if not 0 <= declared <= 0xFFFFFFFF:
        raise ChecksumError("crc is outside the uint32 range")
    verify(canonical_body(body), declared)
    return Message(
        version=VERSION,
        kind=kind,
        identifier=identifier,
        body=body,
        checksum=declared,
    )


class Decoder:
    """Streaming decoder: feed any split, receive every completed message."""

    def __init__(self, limit: int = 65536) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self.limit = limit
        self._buffer = b""

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[Message]:
        self._buffer += bytes(chunk)
        messages: list[Message] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > self.limit:
                    raise ProtocolError("message exceeds the line limit")
                break
            line = self._buffer[:newline]
            self._buffer = self._buffer[newline + 1 :]
            if len(line) > self.limit:
                raise ProtocolError("message exceeds the line limit")
            line = line.rstrip(b"\r")
            if not line.strip():
                continue
            messages.append(decode(line))
        return messages

    def finish(self) -> None:
        if self._buffer.strip():
            self._buffer = b""
            raise Truncated("stream ended inside a message")
        self._buffer = b""
