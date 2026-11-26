"""Decode-validate-dispatch gateway (v2): one entry point over codecs, selector and dispatch."""

from __future__ import annotations

from wire import codecs, dispatch, validation
from wire.model import Message
from wire.selector import Selector


class Gateway:
    def __init__(self, codec_name: str | None = None, limit: int | None = None) -> None:
        self._codec_name = codec_name
        self._selector = Selector(limit) if codec_name is None else None
        self._decoder = codecs.decoder(codec_name, limit) if codec_name is not None else None

    @property
    def protocol(self) -> str | None:
        if self._selector is not None:
            return self._selector.protocol
        return self._codec_name

    def _handle(self, decoded) -> object:
        if isinstance(decoded, Message):
            validation.validate(decoded.kind, decoded.body)
        return dispatch.dispatch(decoded)

    def feed(self, chunk: bytes) -> list[object]:
        if self._selector is not None:
            return [self._handle(item) for item in self._selector.feed(chunk)]
        if self._decoder is None:
            return []
        return [self._handle(item) for item in self._decoder.feed(chunk)]

    def finish(self) -> None:
        if self._selector is not None:
            self._selector.finish()
        elif self._decoder is not None:
            self._decoder.finish()


__all__ = ["Gateway"]
