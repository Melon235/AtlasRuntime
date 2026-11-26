"""Dotted payload paths (v2)."""

from __future__ import annotations

from ingest.errors import SchemaError


def resolve(payload: dict, path: str) -> tuple[bool, object]:
    """Resolve ``a.b.c`` inside ``payload``.

    A missing key is simply missing. A path that walks *through* a non-object is a ``SchemaError``:
    that is a shape error in the payload, not an optional field.
    """

    if not path:
        raise SchemaError("empty payload path")
    current: object = payload
    for part in path.split("."):
        if not part:
            raise SchemaError(f"empty path segment in {path!r}")
        if not isinstance(current, dict):
            raise SchemaError(f"{path!r} walks through a non-object")
        if part not in current:
            return False, None
        current = current[part]
    return True, current
