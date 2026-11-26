"""Protocol auto-detection (v1): looks at the first byte and never re-checks."""

from __future__ import annotations

from wire import frame, json_message


class Selector:
    def __init__(self, limit: int | None = None) -> None:
        self.limit = limit
        self._decoder = None

    @property
    def protocol(self) -> str | None:
        if self._decoder is None:
            return None
        return "json" if isinstance(self._decoder, json_message.Decoder) else "binary"

    def feed(self, chunk: bytes) -> list[object]:
        if self._decoder is None:
            if chunk[:1] == b"{":
                self._decoder = json_message.Decoder(
                    65536 if self.limit is None else self.limit
                )
            else:
                self._decoder = frame.Decoder(frame.MAX_PAYLOAD if self.limit is None else self.limit)
        return self._decoder.feed(chunk)

    def finish(self) -> None:
        if self._decoder is not None:
            self._decoder.finish()
