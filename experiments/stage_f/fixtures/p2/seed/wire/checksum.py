"""CRC-32 helpers."""

from __future__ import annotations

import zlib

from wire.errors import ChecksumError


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def verify(data: bytes, expected: int) -> None:
    actual = crc32(data)
    if actual != expected:
        raise ChecksumError(f"crc mismatch: declared {expected:#010x}, computed {actual:#010x}")
