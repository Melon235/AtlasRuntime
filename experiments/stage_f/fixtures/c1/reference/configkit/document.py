"""TOML document parsing (v2): arbitrary table depth, typed rejection."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping

from configkit.errors import ParseError


def flatten(data: Mapping, prefix: str = "") -> dict:
    """Flatten a mapping into dotted paths; arrays of tables are rejected."""

    flat: dict = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise ParseError(prefix or "<root>", "keys must be non-empty strings")
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(flatten(value, prefix=f"{path}."))
        elif isinstance(value, list) and any(isinstance(item, Mapping) for item in value):
            raise ParseError(path, "arrays of tables are not supported")
        else:
            flat[path] = value
    return flat


def parse(text: str) -> dict:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ParseError("<document>", str(error)) from error
    return flatten(data)
