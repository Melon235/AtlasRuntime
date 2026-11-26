"""Decode-then-dispatch service (v2)."""

from __future__ import annotations

from wire import codecs, dispatch


def handle(codec_name: str, data: bytes) -> object:
    return dispatch.dispatch(codecs.decode(codec_name, data))


class StreamingService:
    def __init__(self, codec_name: str, limit: int | None = None) -> None:
        self._name = codec_name
        self._decoder = codecs.decoder(codec_name, limit)

    def feed(self, chunk: bytes) -> list[object]:
        return [dispatch.dispatch(decoded) for decoded in self._decoder.feed(chunk)]

    def finish(self) -> None:
        self._decoder.finish()
