"""Binary frame protocol (v1).

v1 frames are ``kind:length(4 bytes)payload`` with no magic, no version and no checksum.
"""

from __future__ import annotations

import struct

from wire.model import Frame

MAX_PAYLOAD = 1 << 20

_LENGTH = struct.Struct(">I")


def encode(kind: str, payload: bytes) -> bytes:
    return kind.encode("ascii") + b":" + _LENGTH.pack(len(payload)) + payload


def decode(data: bytes) -> Frame:
    kind, _, rest = data.partition(b":")
    declared = _LENGTH.unpack(rest[:4])[0]
    return Frame(version=2, kind=kind.decode("ascii"), payload=rest[4 : 4 + declared], checksum=0)


class Decoder:
    """Buffers bytes until the declared payload is complete."""

    def __init__(self, limit: int = MAX_PAYLOAD) -> None:
        self.limit = limit
        self._buffer = b""

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buffer += chunk
        frames: list[Frame] = []
        while b":" in self._buffer:
            _, _, rest = self._buffer.partition(b":")
            if len(rest) < 4:
                break
            declared = _LENGTH.unpack(rest[:4])[0]
            if len(rest) < 4 + declared:
                break
            frames.append(decode(self._buffer))
            self._buffer = self._buffer[4 + declared :]
        return frames

    def finish(self) -> None:
        self._buffer = b""
