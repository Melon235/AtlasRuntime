"""Strict value conversion (frozen).

Every connector funnels its resolved cells through :func:`convert`, so the conversion rules are one
implementation and not two.
"""

from __future__ import annotations

import datetime as _datetime
import math
from typing import Any

from ingest.errors import ConversionError

KINDS = ("text", "int", "float", "bool", "date")


def _context(where: str) -> str:
    return f"{where}: " if where else ""


def convert(kind: str, value: Any, *, where: str = "") -> Any:
    """Convert ``value`` to ``kind`` or raise :class:`ConversionError`."""

    if kind not in KINDS:
        raise ConversionError(f"{_context(where)}unknown field kind {kind!r}")
    if value is None:
        raise ConversionError(f"{_context(where)}missing value for {kind} field")

    if kind == "text":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (str, int, float)):
            return str(value)
        raise ConversionError(f"{_context(where)}{type(value).__name__} is not text")

    if kind == "int":
        if isinstance(value, bool):
            raise ConversionError(f"{_context(where)}bool is not an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text and text.lstrip("-").isdigit():
                return int(text)
        raise ConversionError(f"{_context(where)}{value!r} is not an integer")

    if kind == "float":
        if isinstance(value, bool):
            raise ConversionError(f"{_context(where)}bool is not a number")
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError as error:
                raise ConversionError(f"{_context(where)}{value!r} is not a number") from error
        else:
            raise ConversionError(f"{_context(where)}{type(value).__name__} is not a number")
        if not math.isfinite(number):
            raise ConversionError(f"{_context(where)}{value!r} is not finite")
        return number

    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise ConversionError(f"{_context(where)}{value!r} is not a boolean")

    text = value if isinstance(value, str) else str(value)
    try:
        parsed = _datetime.date.fromisoformat(text.strip())
    except ValueError as error:
        raise ConversionError(f"{_context(where)}{value!r} is not an ISO date") from error
    return parsed.isoformat()


__all__ = ["KINDS", "convert"]
