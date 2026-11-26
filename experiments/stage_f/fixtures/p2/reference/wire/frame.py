"""Binary frame protocol (v2 writer, v1/v2 reader).

v2 frames carry a CRC32; v1 frames are read for compatibility and carry none. Both forms share the
magic/version/kind-length header so a stream can be decoded incrementally.
"""

from __future__ import annotations

import re

from wire.checksum import crc32
from wire.errors import ChecksumError, ProtocolError, Truncated, VersionError
from wire.model import Frame

MAGIC_V1 = b"ATL1"
MAGIC_V2 = b"ATL2"
VERSION_V1 = 1
VERSION_V2 = 2
MAX_PAYLOAD = 1 << 20
MAX_KIND = 32

_KIND = re.compile(r"[A-Za-z0-9_-]{1,32}")


def _encode_kind(kind: str) -> bytes:
    if not isinstance(kind, str) or _KIND.fullmatch(kind) is None:
        raise ProtocolError(f"invalid frame kind {kind!r}")
    encoded = kind.encode("ascii")
    if len(encoded) > MAX_KIND:
        raise ProtocolError("frame kind is too long")
    return encoded


def encode(kind: str, payload: bytes) -> bytes:
    """Encode one complete v2 frame."""

    kind_bytes = _encode_kind(kind)
    if not isinstance(payload, bytes):
        raise ProtocolError("frame payload must be bytes")
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError("frame payload is too large")
    return b"".join(
        [
            MAGIC_V2,
            bytes([VERSION_V2, len(kind_bytes)]),
            kind_bytes,
            len(payload).to_bytes(4, "big"),
            crc32(payload).to_bytes(4, "big"),
            payload,
        ]
    )


def _version_of(magic: bytes) -> int:
    if magic == MAGIC_V1:
        return VERSION_V1
    if magic == MAGIC_V2:
        return VERSION_V2
    raise ProtocolError("frame magic does not match")


def _split(data: bytes) -> tuple[int, str, bytes, int]:
    """Parse one frame and return ``(version, kind, payload, checksum)``."""

    if len(data) < 6:
        raise Truncated("frame header is incomplete")
    version = _version_of(data[:4])
    if data[4] != version:
        raise VersionError(f"magic and version disagree: {data[4]}")
    kind_len = data[5]
    if kind_len == 0 or kind_len > MAX_KIND:
        raise ProtocolError("invalid frame kind length")
    crc_len = 4 if version == VERSION_V2 else 0
    header_len = 6 + kind_len + 4 + crc_len
    if len(data) < header_len:
        raise Truncated("frame header is incomplete")
    kind_bytes = data[6 : 6 + kind_len]
    try:
        kind = kind_bytes.decode("ascii")
    except UnicodeDecodeError as error:
        raise ProtocolError(f"frame kind is not ascii: {error}") from error
    if _KIND.fullmatch(kind) is None:
        raise ProtocolError(f"invalid frame kind {kind!r}")
    length_at = 6 + kind_len
    payload_len = int.from_bytes(data[length_at : length_at + 4], "big")
    if payload_len > MAX_PAYLOAD:
        raise ProtocolError("declared payload is too large")
    declared = 0
    if version == VERSION_V2:
        declared = int.from_bytes(data[length_at + 4 : length_at + 8], "big")
    total = header_len + payload_len
    if len(data) < total:
        raise Truncated("frame payload is incomplete")
    if len(data) > total:
        raise ProtocolError("trailing bytes after the frame")
    payload = data[header_len:total]
    if version == VERSION_V2:
        actual = crc32(payload)
        if actual != declared:
            raise ChecksumError(
                f"crc mismatch: declared {declared:#010x}, computed {actual:#010x}"
            )
    return version, kind, payload, declared


def decode(data: bytes) -> Frame:
    """Decode exactly one frame (v1 or v2)."""

    version, kind, payload, declared = _split(bytes(data))
    return Frame(version=version, kind=kind, payload=payload, checksum=declared)


class Decoder:
    """Streaming decoder for a sequence of v1 and/or v2 frames."""

    def __init__(self, limit: int = MAX_PAYLOAD) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self.limit = limit
        self._buffer = b""

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buffer += bytes(chunk)
        frames: list[Frame] = []
        while self._buffer:
            head = self._buffer[:4]
            prefixes = (MAGIC_V1.startswith(head), MAGIC_V2.startswith(head))
            if not any(prefixes):
                raise ProtocolError("frame magic does not match")
            if len(self._buffer) < 6:
                break
            magic = self._buffer[:4]
            version = _version_of(magic)
            if self._buffer[4] != version:
                raise VersionError(f"magic and version disagree: {self._buffer[4]}")
            kind_len = self._buffer[5]
            if kind_len == 0 or kind_len > MAX_KIND:
                raise ProtocolError("invalid frame kind length")
            header_len = 6 + kind_len + 4 + (4 if version == VERSION_V2 else 0)
            if len(self._buffer) < header_len:
                break
            length_at = 6 + kind_len
            payload_len = int.from_bytes(self._buffer[length_at : length_at + 4], "big")
            if payload_len > self.limit:
                raise ProtocolError("declared payload exceeds the decoder limit")
            total = header_len + payload_len
            if len(self._buffer) < total:
                break
            frames.append(decode(self._buffer[:total]))
            self._buffer = self._buffer[total:]
        return frames

    def finish(self) -> None:
        if self._buffer:
            self._buffer = b""
            raise Truncated("stream ended inside a frame")
