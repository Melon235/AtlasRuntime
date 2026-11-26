"""JSON message helpers (v1).

v1 uses the default encoder settings, so the same body can produce two different byte strings.
"""

from __future__ import annotations

import json
from typing import Any


def encode_document(document: dict[str, Any]) -> bytes:
    return json.dumps(document).encode("utf-8")


def canonical_body(body: dict[str, Any]) -> bytes:
    return json.dumps(body).encode("utf-8")


def loads(text: str) -> Any:
    """v1 lets the json module keep the last duplicate key."""

    return json.loads(text)
