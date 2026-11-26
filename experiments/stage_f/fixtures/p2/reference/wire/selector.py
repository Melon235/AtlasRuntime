"""Protocol auto-detection (v2): whitespace-tolerant, prefix-safe, sticky."""

from __future__ import annotations

from wire import frame, json_message
from wire.errors import ProtocolError, Truncated

_SPACE = b" \t\r\n"
_MAGICS = (frame.MAGIC_V1, frame.MAGIC_V2)


class Selector:
    """Detect the protocol from the first non-whitespace bytes, then stay on it."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = limit
        self._decoder = None
        self._buffer = b""

    @property
    def protocol(self) -> str | None:
        if self._decoder is None:
            return None
        return "json" if isinstance(self._decoder, json_message.Decoder) else "binary"

    @property
    def pending(self) -> int:
        if self._decoder is not None:
            return self._decoder.pending
        return len(self._buffer)

    def _detect(self) -> bytes | None:
        stripped = self._buffer.lstrip(_SPACE)
        if not stripped:
            return None
        if stripped[:1] == b"{":
            self._decoder = json_message.Decoder(65536 if self.limit is None else self.limit)
            data = self._buffer
        elif stripped[:1] == b"A":
            head = stripped[:4]
            if not any(magic.startswith(head) for magic in _MAGICS):
                raise ProtocolError("stream is neither json nor a known frame")
            if len(head) < 4:
                return None
            self._decoder = frame.Decoder(frame.MAX_PAYLOAD if self.limit is None else self.limit)
            data = stripped
        else:
            raise ProtocolError("stream is neither json nor a known frame")
        self._buffer = b""
        return data

    def feed(self, chunk: bytes) -> list[object]:
        if self._decoder is not None:
            return list(self._decoder.feed(chunk))
        self._buffer += bytes(chunk)
        data = self._detect()
        if data is None:
            return []
        return list(self._decoder.feed(data))

    def finish(self) -> None:
        if self._decoder is None:
            if self._buffer.strip():
                self._buffer = b""
                raise Truncated("stream ended before the protocol was detected")
            self._buffer = b""
            return
        self._decoder.finish()
