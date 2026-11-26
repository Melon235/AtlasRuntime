"""JSON message protocol (v1).

v1 writes one unversioned object per line, reads the whole input at once and ignores checksums.
"""

from __future__ import annotations

from typing import Any

from wire.json_fields import encode_document, loads
from wire.model import Message


def encode(kind: str, body: dict[str, Any], *, identifier: str) -> bytes:
    return encode_document({"type": kind, "body": body}) + b"\n"


def decode(data: bytes) -> Message:
    document = loads(data.decode("utf-8"))
    return Message(
        version=1,
        kind=document["type"],
        identifier="",
        body=document["body"],
        checksum=None,
    )


class Decoder:
    """Line splitter with no version or CRC validation."""

    def __init__(self, limit: int = 65536) -> None:
        self.limit = limit
        self._buffer = b""

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[Message]:
        self._buffer += chunk
        messages: list[Message] = []
        while b"\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition(b"\n")
            if line.strip():
                messages.append(decode(line))
        return messages

    def finish(self) -> None:
        self._buffer = b""
