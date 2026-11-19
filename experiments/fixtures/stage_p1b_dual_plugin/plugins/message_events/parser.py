"""Parsing for the message channel.

The document is either a bare JSON array of messages or an object with a ``messages`` key. Both
shapes are produced by the backend depending on which API version answered, so both are accepted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import MessageEventError
from .schema import TYPE_PATTERN, MessageRecord, validate_fields

_TYPE_RE = re.compile(TYPE_PATTERN)


def _messages_of(document: Any) -> Sequence[Any]:
    if isinstance(document, Mapping):
        if "messages" not in document:
            raise MessageEventError("missing_field", "document has no 'messages' key")
        messages = document["messages"]
    else:
        messages = document
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise MessageEventError("wrong_type", "messages must be a list")
    return messages


def parse_message(raw: Any, *, index: int | None = None) -> MessageRecord:
    """Parse one message object."""

    if not isinstance(raw, Mapping):
        raise MessageEventError("wrong_type", "a message must be a JSON object", index=index)
    validate_fields(raw, index=index)
    if not _TYPE_RE.match(raw["type"]):
        raise MessageEventError(
            "wrong_type", "type must be an uppercase identifier such as USER_JOIN", index=index
        )
    labels = raw.get("labels") or ()
    payload = raw.get("payload") or {}
    return MessageRecord(
        id=int(raw["id"]),
        sent_at=int(raw["sent_at"]),
        type=raw["type"],
        priority=raw.get("priority"),
        labels=tuple(str(label) for label in labels),
        payload=dict(payload),
    )


def parse_document(document: str | Mapping[str, Any] | Sequence[Any]) -> tuple[MessageRecord, ...]:
    """Parse a whole document (JSON text, an already-decoded object, or a list of messages)."""

    decoded: Any
    if isinstance(document, str):
        try:
            decoded = json.loads(document)
        except json.JSONDecodeError as error:
            raise MessageEventError("bad_json", str(error)) from error
    else:
        decoded = document
    return tuple(
        parse_message(item, index=index)
        for index, item in enumerate(_messages_of(decoded), start=1)
    )


def parse_path(path: Path) -> tuple[MessageRecord, ...]:
    """Read and parse a ``.json`` document of messages."""

    return parse_document(Path(path).read_text(encoding="utf-8"))


__all__ = ["parse_document", "parse_message", "parse_path"]
