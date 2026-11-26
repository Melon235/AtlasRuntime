"""JSON message helpers (v2): canonical body bytes and duplicate-key rejection."""

from __future__ import annotations

import json
from typing import Any

from wire.errors import ProtocolError


def canonical_body(body: dict[str, Any]) -> bytes:
    """The bytes the v2 CRC is computed over."""

    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def encode_document(document: dict[str, Any]) -> bytes:
    """Compact, stable document encoding (key order is preserved as given)."""

    text = json.dumps(document, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def loads(text: str) -> Any:
    """Parse JSON, rejecting any duplicated object key at any depth."""

    try:
        return json.loads(text, object_pairs_hook=_object)
    except json.JSONDecodeError as error:
        raise ProtocolError(f"invalid json: {error}") from error
